"""Onboarding interview: drive an agent conversation, then synthesize and persist a task."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel

from dailies.agent import AgentProvider, AgentRequest
from dailies.documents import Task, Workflow
from dailies.models import (
    CronExpr,
    CronTrigger,
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

TURN_SYSTEM = (
    "You are running a short onboarding interview to design a recurring automated task for the user. "
    "Given the scenario and any prior questions and answers, ask exactly one focused follow-up question "
    "that resolves the biggest remaining ambiguity (trigger timing, data sources, outputs, edge cases). "
    "Keep it answerable in a single line. Once you know enough to design a concrete Task plus one or more "
    "Workflows, set finished=true and leave question null."
)

SYNTHESIS_SYSTEM = (
    "You are designing a recurring automated task from a completed onboarding interview. Produce one "
    "TaskProposal. The task has a human name, a one-sentence description, user_input set to the user's "
    "verbatim opening scenario, and prompt — a standing instruction the agent follows on every run. Provide "
    "one or more workflows; each has a name, a per-run prompt, a list of plain-language rules, a one-line "
    "ddl describing the state it tracks (a short SQL-like schema), and cron_expression as a standard 5-field "
    "cron string, or null for an event- or manually-triggered workflow."
)


class InterviewError(Exception):
    """The agent failed to produce a usable interview response."""


def extract_json(text: str) -> str:
    return text[text.index("{") : text.rindex("}") + 1]


async def generate[T: BaseModel](provider: AgentProvider, *, system: str, user: str, output: type[T]) -> T:
    result = await provider.run(
        AgentRequest(
            system=f"{system}\n\nReturn ONLY a JSON object matching this schema, no prose, no code fences:\n"
            f"{json.dumps(output.model_json_schema())}",
            prompt=user,
        )
    )
    if not result.ok:
        raise InterviewError(result.text)
    return output.model_validate_json(extract_json(result.text))


def render_interview(interview: Interview) -> str:
    return "\n\n".join(
        [f"Scenario: {interview.scenario}", *(f"Q: {e.question}\nA: {e.answer}" for e in interview.exchanges)]
    )


def draft_triggers(draft: WorkflowDraft) -> list[Trigger]:
    match draft.cron_expression:
        case None:
            return []
        case expr:
            return [CronTrigger(cron_expression=CronExpr(expr))]


@dataclass(frozen=True, slots=True)
class InterviewRunner:
    """Drives the onboarding interview and synthesizes a proposal through an agent provider."""

    provider: AgentProvider

    async def next_turn(self, interview: Interview) -> InterviewTurn:
        return await generate(self.provider, system=TURN_SYSTEM, user=render_interview(interview), output=InterviewTurn)

    async def synthesize(self, interview: Interview) -> TaskProposal:
        return await generate(
            self.provider, system=SYNTHESIS_SYSTEM, user=render_interview(interview), output=TaskProposal
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
