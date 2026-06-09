"""Headless live-test of the onboarding interview over the recorded scenarios.

Drives the real `InterviewRunner` against `ClaudeAgentSDKProvider`, auto-answering each
follow-up with a simulator persona grounded in the scenario, then synthesizes and persists
each proposal as a draft Task + Workflows. Emits a markdown layout dump (stdout + file)
because the TUI lists only names. Run with `.env` loaded:

    uv run python scripts/simulate_interviews.py
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

import anyio
from pydantic import ValidationError

from dailies.agent import AgentProvider, AgentRequest, AgentResult, ClaudeAgentSDKProvider
from dailies.db import lifespan
from dailies.documents import Task
from dailies.interview import (
    InterviewError,
    InterviewRunner,
    draft_triggers,
    persist_proposal,
    render_interview,
)
from dailies.models import (
    CronTrigger,
    EventTrigger,
    Exchange,
    Interview,
    InterviewTurn,
    ManualTrigger,
    TaskProposal,
    Trigger,
    WorkflowDraft,
)

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "scenarios.md"
DUMP = ROOT / "scratch" / "interview-layouts.md"
MAX_TURNS = 4
CONCURRENCY = 3

ANSWERER_SYSTEM = (
    "You are the user who wrote the scenario below, being interviewed to design a recurring "
    "automated task. Answer the interviewer's question in one short line using only what the "
    "scenario implies; if the scenario is silent, pick a sensible default and say so briefly. "
    "The scenario's prescriptive details (e.g. 'a spreadsheet') are incidental — the system "
    "tracks state in its own schema, so don't fixate on the literal tool."
)


@dataclass(frozen=True, slots=True)
class ThrottledProvider:
    inner: AgentProvider
    sem: asyncio.Semaphore

    async def run(self, request: AgentRequest) -> AgentResult:
        async with self.sem:
            return await self.inner.run(request)


@dataclass(frozen=True, slots=True)
class Success:
    scenario: str
    interview: Interview
    proposal: TaskProposal
    task: Task


@dataclass(frozen=True, slots=True)
class Failure:
    scenario: str
    error: str


type Outcome = Success | Failure


def load_scenarios() -> list[str]:
    return [block.strip() for block in re.split(r"(?m)^## Scenario \d+\b.*$", SCENARIOS.read_text())[1:]]


async def simulate_answer(provider: AgentProvider, interview: Interview, question: str) -> str:
    result = await provider.run(
        AgentRequest(
            system=ANSWERER_SYSTEM,
            prompt=f"{render_interview(interview)}\n\nInterviewer asks: {question}\n\nYour one-line answer:",
        )
    )
    if not result.ok:
        raise InterviewError(result.text)
    return result.text.strip()


async def simulate(runner: InterviewRunner, provider: AgentProvider, scenario: str) -> Outcome:
    try:
        interview = Interview(scenario=scenario)
        for _ in range(MAX_TURNS):
            match await runner.next_turn(interview):
                case InterviewTurn(finished=True) | InterviewTurn(question=None):
                    break
                case InterviewTurn(question=str() as question):
                    answer = await simulate_answer(provider, interview, question)
                    interview = Interview(
                        scenario=interview.scenario,
                        exchanges=[*interview.exchanges, Exchange(question=question, answer=answer)],
                    )
        proposal = await runner.synthesize(interview)
    except (InterviewError, ValidationError) as exc:
        return Failure(scenario=scenario, error=str(exc))
    return Success(scenario, interview, proposal, await persist_proposal(proposal, status="draft"))


def render_trigger(trigger: Trigger) -> str:
    match trigger:
        case CronTrigger(cron_expression=expr, timezone=tz):
            return f"cron `{expr}` ({tz})"
        case EventTrigger(event_type=event_type, event_key=event_key):
            return f"event `{event_type}/{event_key}`"
        case ManualTrigger():
            return "manual"


def render_workflow(draft: WorkflowDraft) -> str:
    triggers = ", ".join(render_trigger(t) for t in draft_triggers(draft))
    rules = "\n".join(f"    - {rule}" for rule in draft.rules) or "    - _(none)_"
    return "\n".join(
        [
            f"- **{draft.name}**",
            f"  - prompt: {draft.prompt}",
            "  - rules:",
            rules,
            f"  - ddl: `{draft.ddl}`",
            f"  - triggers: {triggers}",
        ]
    )


def render_success(n: int, outcome: Success) -> str:
    proposal = outcome.proposal
    qa = (
        "\n".join(f"- **Q:** {e.question}\n  **A:** {e.answer}" for e in outcome.interview.exchanges)
        or "_(none — agent synthesized directly)_"
    )
    return "\n".join(
        [
            f"## Scenario {n} — {proposal.task.name}",
            "",
            f"**Raw input:** {outcome.scenario}",
            "",
            f"### Interview ({len(outcome.interview.exchanges)} follow-up Q&A)",
            qa,
            "",
            "### Task",
            f"- **description:** {proposal.task.description}",
            f"- **prompt:** {proposal.task.prompt}",
            f"- **shared_ddl:** `{proposal.task.shared_ddl or '—'}`",
            f"- **persisted:** `{outcome.task.uid}` · status=`{outcome.task.status}`",
            "",
            f"### Workflows ({len(proposal.workflows)})",
            *(render_workflow(w) for w in proposal.workflows),
        ]
    )


def render_outcome(n: int, outcome: Outcome) -> str:
    match outcome:
        case Failure(scenario=scenario, error=error):
            return f"## Scenario {n} — FAILED\n\n**Raw input:** {scenario}\n\n**Error:** {error}"
        case Success() as success:
            return render_success(n, success)


def render_report(outcomes: list[Outcome]) -> str:
    header = f"# Interview layouts\n\n{len(outcomes)} scenarios simulated and persisted as draft tasks."
    return "\n\n".join([header, *(render_outcome(n, o) for n, o in enumerate(outcomes, 1))])


async def main() -> None:
    scenarios = load_scenarios()
    async with lifespan():
        provider = ThrottledProvider(ClaudeAgentSDKProvider(), asyncio.Semaphore(CONCURRENCY))
        runner = InterviewRunner(provider)
        outcomes = await asyncio.gather(*(simulate(runner, provider, s) for s in scenarios))
    report = render_report(list(outcomes))
    DUMP.parent.mkdir(parents=True, exist_ok=True)
    DUMP.write_text(report)
    print(report)


if __name__ == "__main__":
    anyio.run(main)
