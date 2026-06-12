"""Onboarding interview: drive an agent conversation, then synthesize and persist a task."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from dailies.agent import AgentProvider, AgentRequest
from dailies.connections import INTEGRATIONS
from dailies.documents import Task, Workflow
from dailies.models import (
    LOCAL_TZ,
    Interview,
    InterviewTurn,
    PromptStr,
    SchemaStr,
    TaskDefinition,
    TaskProposal,
    TaskStatus,
    Trigger,
    WorkflowDefinition,
    WorkflowDraft,
    WorkflowId,
    WorkflowTrigger,
    WorkflowTriggerDraft,
    new_uuid,
)
from dailies.state import apply_ddl, task_db_key, validate_ddl, workflow_db_key
from dailies.storage import state_storage
from dailies.tools import render_catalog
from dailies.tools.base import StructuredSink, ToolSet

TURN_SYSTEM = (
    "You are running a short onboarding interview to design a recurring automated task for the user. "
    "Given the scenario and any prior questions and answers, ask exactly one focused follow-up question "
    "that resolves the biggest remaining ambiguity (trigger timing and timezone, scheduled vs event-driven "
    "vs manual, data sources, outputs, edge cases). Keep it answerable in a single line. "
    "Map the scenario onto dailies' own primitives, not the user's incidental tools: anything to track or "
    "log is state you design as a SQLite schema; cadence is a trigger; tool use (a browser, an email, a "
    "subagent) is a goal you state in a prompt. Do not ask the user to name or locate an incidental tool "
    "(which spreadsheet, which tab, which channel) — infer the underlying need and design the schema "
    "yourself, and ask only about intent the scenario leaves genuinely ambiguous. "
    "Once you know enough to design a concrete Task plus one or more Workflows, set finished=true and leave "
    "question null."
)

SYNTHESIS_SYSTEM = (
    "You are designing a recurring automated task from a completed onboarding interview. Produce one "
    "TaskProposal. The task has a human name, a one-sentence description, user_input set to the user's "
    "verbatim opening scenario, a prompt (a standing instruction the agent follows on every run), and "
    "shared_ddl — a SQLite schema for state shared across its workflows, or null when nothing is shared. "
    "Provide one or more workflows; each has a name, a one-sentence summary (at most ~15 words, shown in "
    "the UI as the workflow's gloss — describe the outcome, don't restate the prompt), a per-run prompt, a "
    "list of plain-language rules, a ddl for the state private to that workflow, and one or more triggers. "
    "Decompose by independent concern: when collection is per-entity and parallel (several products, "
    "accounts, or sources, each checked on its own), give each entity or source its own collection "
    "workflow; decision or notification logic that fans in over several workflows' outputs is its own "
    "downstream workflow, triggered by those workflows' completion and reading their outputs from "
    "shared.<table> tables — which means those outputs are declared in shared_ddl, not in any one "
    "workflow's ddl. Do not over-split: a fetch that immediately acts on its own result alone is one "
    "workflow; split only when collectors are genuinely independent or a decision consumes more than one "
    "workflow's output. "
    "Every ddl and shared_ddl is executed verbatim against a fresh SQLite database when the task is saved, "
    "so it must be valid SQLite DDL: semicolon-terminated CREATE TABLE (and optional CREATE INDEX) "
    "statements using SQLite types (TEXT, INTEGER, REAL). At run time the workflow addresses its own "
    "tables bare and shared tables as shared.<table>. "
    "Map implementation details onto these primitives rather than baking the user's incidental tool choices "
    "into prompts; the workflow IS the agent and its tools come from the runtime, so never prescribe the "
    "execution substrate: "
    "track / log / a spreadsheet / a tab -> a ddl table (one tab ~ one table), never restated as prose in a "
    "prompt; every day / at 9am / when X arrives -> a trigger, not prose; spawn a subagent / use a browser / "
    "send an email -> a goal stated in the workflow prompt, never a named subagent, Chrome group, or file; "
    "standing behavior and constraints -> the task prompt and the workflow rules. "
    "Each trigger is one of: cron (a standard 5-field cron string plus an IANA timezone such as "
    "America/Los_Angeles — set the timezone whenever the scenario implies a local time; when a local time is "
    f"implied but no specific zone is named, default to the user's local timezone, {LOCAL_TZ}; never bake an "
    "offset into the cron fields); event (react to something arriving instead of polling on a clock: "
    "source names the integration and event/key name what to watch — today source gmail with either "
    "event query and key a Gmail search expression such as from:boss@example.com, or event thread and "
    "key a Gmail thread id; prefer a query watch unless a concrete thread id is already known); "
    "workflow (run after a sibling workflow in this same task completes successfully: workflow names that "
    "sibling exactly as it is spelled in this proposal; completions landing close together batch into one "
    "firing, so a fan-in workflow listening to several siblings runs once when they finish "
    "near-simultaneously); or manual (run on demand). "
    "State that several workflows read or write (a counter, a running total) belongs in the task's "
    "shared_ddl, declared exactly once; state used by only one workflow belongs in that workflow's ddl. "
    "A collector's output that a downstream workflow consumes is shared by definition: declare it in "
    "shared_ddl and have the collector write shared.<table>. "
    "The workflow agent runs with exactly the tools listed below and no others — no MCP servers, no apps, "
    "no integrations beyond these. Write every workflow prompt in terms of these capabilities only, and "
    "never name an outside product or service as the execution mechanism. When the scenario needs a "
    "capability this catalog lacks, do not invent a tool or write a prompt that pretends the capability "
    "exists: name the missing capability in the proposal's gaps list and design the workflows around what "
    "the catalog can actually do. "
    "Each toolset in the catalog names the integrations it requires; set each workflow's requires to "
    "exactly the integration names required by the toolsets its prompt depends on, and leave it empty "
    "when none. "
    "When a workflow's action is irreversible, spends money the user has not authorized, or is one the "
    "user asked to confirm first, design it as an email approval gate that spans runs — a run never "
    "waits for a reply: the workflow sends the request with send_email, calls subscribe_to_thread with "
    "the receipt's thread_id, records the pending decision in a state table (what awaits approval and "
    "why), and ends the run; the user's reply fires a new run of the same workflow, which reads the "
    "pending row, interprets the reply, and proceeds or abandons. Spell this gate out in the prompt and "
    "rules of any workflow whose actions need approval."
    f"\n\nAvailable tools:\n\n{render_catalog()}"
)


class InterviewError(Exception):
    """The agent failed to produce a usable interview response."""


async def collect[T: BaseModel](
    provider: AgentProvider,
    sink: StructuredSink[T],
    *,
    system: str,
    prompt: str,
    toolsets: tuple[ToolSet, ...] = (),
) -> T:
    result = await provider.run(
        AgentRequest(
            system=f"{system}\n\nCall the submit tool exactly once with the structured result; do not reply in prose.",
            prompt=prompt,
            tools=tuple(t.to_spec() for ts in (*toolsets, sink) for t in ts.get_tools()),
        )
    )
    if sink.result is None:
        raise InterviewError(result.text)
    return sink.result


def render_interview(interview: Interview) -> str:
    return "\n\n".join(
        [f"Scenario: {interview.scenario}", *(f"Q: {e.question}\nA: {e.answer}" for e in interview.exchanges)]
    )


def draft_triggers(draft: WorkflowDraft, *, ids: Mapping[str, WorkflowId]) -> list[Trigger]:
    return [
        WorkflowTrigger(workflow_id=ids[trigger.workflow]) if isinstance(trigger, WorkflowTriggerDraft) else trigger
        for trigger in draft.triggers
    ]


@dataclass(frozen=True, slots=True)
class InterviewRunner:
    """Drives the onboarding interview and synthesizes a proposal through an agent provider."""

    provider: AgentProvider

    async def next_turn(self, interview: Interview) -> InterviewTurn:
        return await collect(
            self.provider, StructuredSink(InterviewTurn), system=TURN_SYSTEM, prompt=render_interview(interview)
        )

    async def synthesize(self, interview: Interview) -> TaskProposal:
        return await collect(
            self.provider, StructuredSink(TaskProposal), system=SYNTHESIS_SYSTEM, prompt=render_interview(interview)
        )


async def persist_proposal(proposal: TaskProposal, *, status: TaskStatus) -> Task:
    """Persist a reviewed proposal as a Task and its Workflows; Approve and Save differ only by ``status``.

    Validates every workflow's ``requires`` against the integration registry and
    every DDL, then materializes the per-task and per-workflow state databases,
    then inserts the documents — so a proposal with an unknown integration or
    bad DDL persists nothing.
    """
    if unknown := {name for draft in proposal.workflows for name in draft.requires} - INTEGRATIONS.keys():
        raise ValueError(f"unknown integrations in requires: {sorted(unknown)}")
    storage = state_storage()
    for ddl in filter(None, [proposal.task.shared_ddl, *(draft.ddl for draft in proposal.workflows)]):
        validate_ddl(SchemaStr(ddl))
    task = Task(
        name=proposal.task.name,
        definition=TaskDefinition(
            user_input=proposal.task.user_input,
            description=proposal.task.description,
            prompt=PromptStr(proposal.task.prompt),
        ),
        shared_ddl=SchemaStr(proposal.task.shared_ddl) if proposal.task.shared_ddl else None,
        gaps=proposal.gaps,
        status=status,
    )
    ids = {draft.name: WorkflowId(new_uuid()) for draft in proposal.workflows}
    workflows = [
        Workflow(
            task_id=task.uid,
            workflow_id=ids[draft.name],
            version=1,
            name=draft.name,
            definition=WorkflowDefinition(summary=draft.summary, prompt=PromptStr(draft.prompt), rules=draft.rules),
            ddl=SchemaStr(draft.ddl),
            requires=draft.requires,
            status=status,
            triggers=draft_triggers(draft, ids=ids),
        )
        for draft in proposal.workflows
    ]
    await apply_ddl(storage, task_db_key(task.uid), task.shared_ddl)
    for workflow in workflows:
        await apply_ddl(storage, workflow_db_key(workflow.workflow_id), workflow.ddl)
    await task.insert()
    for workflow in workflows:
        await workflow.insert()
    return task
