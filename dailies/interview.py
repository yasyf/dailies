"""Onboarding interview: drive an agent conversation, then synthesize and persist a task."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from dailies.agent import AgentProvider, AgentRequest
from dailies.documents import Task, Workflow
from dailies.models import (
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
    new_uuid,
)
from dailies.tools.base import StructuredSink

TURN_SYSTEM = (
    "You are running a short onboarding interview to design a recurring automated task for the user. "
    "Given the scenario and any prior questions and answers, ask exactly one focused follow-up question "
    "that resolves the biggest remaining ambiguity (trigger timing and timezone, scheduled vs event-driven "
    "vs manual, data sources, outputs, edge cases). Keep it answerable in a single line. "
    "Map the scenario onto dailies' own primitives, not the user's incidental tools: anything to track or "
    "log is state you design as a SQL-like schema; cadence is a trigger; tool use (a browser, an email, a "
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
    "shared_ddl — a SQL-like schema for state shared across its workflows, or null when nothing is shared. "
    "Provide one or more workflows; each has a name, a per-run prompt, a list of plain-language rules, a "
    "ddl for the state private to that workflow, and one or more triggers. "
    "Map implementation details onto these primitives rather than baking the user's incidental tool choices "
    "into prompts; the workflow IS the agent and its tools come from the runtime, so never prescribe the "
    "execution substrate: "
    "track / log / a spreadsheet / a tab -> a ddl table (one tab ~ one table), never restated as prose in a "
    "prompt; every day / at 9am / when X arrives -> a trigger, not prose; spawn a subagent / use a browser / "
    "send an email -> a goal stated in the workflow prompt, never a named subagent, Chrome group, or file; "
    "standing behavior and constraints -> the task prompt and the workflow rules. "
    "Each trigger is one of: cron (a standard 5-field cron string plus an IANA timezone such as "
    "America/Los_Angeles — set the timezone whenever the scenario implies a local time, and never bake an "
    "offset into the cron fields); event (react to something arriving — an inbound email or reply, a "
    "webhook, a price change — instead of polling on a clock); or manual (run on demand). "
    "State that several workflows read or write (a counter, a running total) belongs in the task's "
    "shared_ddl, declared exactly once; state used by only one workflow belongs in that workflow's ddl."
)


class InterviewError(Exception):
    """The agent failed to produce a usable interview response."""


async def collect[T: BaseModel](provider: AgentProvider, sink: StructuredSink[T], *, system: str, prompt: str) -> T:
    result = await provider.run(
        AgentRequest(
            system=f"{system}\n\nCall the submit tool exactly once with the structured result; do not reply in prose.",
            prompt=prompt,
            tools=tuple(t.to_spec() for t in sink.get_tools()),
        )
    )
    if sink.result is None:
        raise InterviewError(result.text)
    return sink.result


def render_interview(interview: Interview) -> str:
    return "\n\n".join(
        [f"Scenario: {interview.scenario}", *(f"Q: {e.question}\nA: {e.answer}" for e in interview.exchanges)]
    )


def draft_triggers(draft: WorkflowDraft) -> list[Trigger]:
    return draft.triggers


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
    """Persist a reviewed proposal as a Task and its Workflows; Approve and Save differ only by ``status``."""
    task = await Task(
        name=proposal.task.name,
        definition=TaskDefinition(
            user_input=proposal.task.user_input,
            description=proposal.task.description,
            prompt=PromptStr(proposal.task.prompt),
        ),
        shared_ddl=SchemaStr(proposal.task.shared_ddl) if proposal.task.shared_ddl else None,
        status=status,
    ).insert()
    for draft in proposal.workflows:
        await Workflow(
            task_id=task.uid,
            workflow_id=WorkflowId(new_uuid()),
            version=1,
            name=draft.name,
            definition=WorkflowDefinition(prompt=PromptStr(draft.prompt), rules=draft.rules),
            ddl=SchemaStr(draft.ddl),
            status=status,
            triggers=draft_triggers(draft),
        ).insert()
    return task
