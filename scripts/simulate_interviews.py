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
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import anyio
from pydantic import ValidationError

from dailies.agent import AgentProvider, AgentRequest, AgentResult, ClaudeAgentSDKProvider
from dailies.db import lifespan
from dailies.documents import Task
from dailies.interface.rendering import render_trigger
from dailies.interview import (
    InterviewError,
    InterviewRunner,
    persist_proposal,
    render_interview,
)
from dailies.models import (
    LOCAL_TZ,
    CronTrigger,
    Exchange,
    Interview,
    InterviewTurn,
    TaskProposal,
    WorkflowDraft,
    WorkflowTriggerDraft,
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

SUBSTRATE_LEAK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (r"\bspreadsheet\b", r"\btab\b", r"\bchrome group\b", r"\bsub-?agents?\b")
)
INVENTED_TOOL_PATTERNS = tuple(
    re.compile(rf"\b{name}\b", re.IGNORECASE)
    for name in (
        "flighty",
        "1password",
        "onepassword",
        "8sleep",
        r"eight\s*sleep",
        "bluebubbles",
        "11labs",
        "elevenlabs",
    )
)
PENALTY_STATE = re.compile(r"penalt", re.IGNORECASE)
PENALTY_WORKFLOW_DECL = re.compile(
    r"\bpenalt\w*\s+INTEGER\b|create\s+table\s+(?:if\s+not\s+exists\s+)?\w*penalt\w*(?<!_log)(?<!_logs)\s*\(",
    re.IGNORECASE,
)

type Check = tuple[str, Callable[[TaskProposal], bool]]

GLOBAL_CHECKS: tuple[Check, ...] = (
    (
        "no substrate leak in any prompt, summary, or rule",
        lambda p: not any(pattern.search(text) for pattern in SUBSTRATE_LEAK_PATTERNS for text in texts(p)),
    ),
    (
        "no invented outside tool in any prompt, summary, or rule",
        lambda p: not any(pattern.search(text) for pattern in INVENTED_TOOL_PATTERNS for text in texts(p)),
    ),
)

EVENT_TRIGGER_CHECK: Check = (
    "at least one event trigger",
    lambda p: any(trigger.kind == "event" for draft in p.workflows for trigger in draft.triggers),
)

LOCAL_CRON_CHECK: Check = (
    f"every cron trigger in the user's local timezone ({LOCAL_TZ})",
    lambda p: bool(crons := cron_triggers(p)) and all(trigger.timezone == LOCAL_TZ for trigger in crons),
)

GAPS_CHECK: Check = ("gaps flagged for uncatalogued capabilities", lambda p: bool(p.gaps))

PENALTY_COUNTER_CHECK: Check = (
    "penalty state lives in shared_ddl, with no penalty state declared per-workflow",
    lambda p: bool(PENALTY_STATE.search(p.task.shared_ddl or ""))
    and not any(PENALTY_WORKFLOW_DECL.search(draft.ddl) for draft in p.workflows),
)

SCENARIO2_CHECKS: tuple[Check, ...] = (
    ("exactly three workflows", lambda p: len(p.workflows) == 3),
    ("two cron collectors without workflow triggers", lambda p: len(collectors(p)) == 2),
    ("one decider with only workflow triggers", lambda p: len(deciders(p)) == 1),
    (
        "decider fans in over both collectors by name",
        lambda p: len(d := deciders(p)) == 1 and upstream_names(d[0]) == {w.name for w in collectors(p)},
    ),
    ("shared_ddl declared", lambda p: bool(p.task.shared_ddl)),
    ("decider reads shared state", lambda p: len(d := deciders(p)) == 1 and "shared." in d[0].prompt),
)

EXPECTATIONS: tuple[tuple[Check, ...], ...] = tuple(
    (*GLOBAL_CHECKS, *extra)
    for extra in (
        (),
        (*SCENARIO2_CHECKS, LOCAL_CRON_CHECK),
        (EVENT_TRIGGER_CHECK, GAPS_CHECK),
        (EVENT_TRIGGER_CHECK, GAPS_CHECK),
        (LOCAL_CRON_CHECK, PENALTY_COUNTER_CHECK, GAPS_CHECK),
    )
)


@dataclass(frozen=True, slots=True)
class ThrottledProvider:
    inner: AgentProvider
    sem: asyncio.Semaphore

    async def run(self, request: AgentRequest) -> AgentResult:
        async with self.sem:
            return await self.inner.run(request)


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool


@dataclass(frozen=True, slots=True)
class Success:
    scenario: str
    interview: Interview
    proposal: TaskProposal
    task: Task
    checks: tuple[CheckResult, ...]


@dataclass(frozen=True, slots=True)
class Failure:
    scenario: str
    error: str


type Outcome = Success | Failure


def trigger_kinds(draft: WorkflowDraft) -> set[str]:
    return {trigger.kind for trigger in draft.triggers}


def texts(proposal: TaskProposal) -> list[str]:
    return [
        proposal.task.prompt,
        *(text for draft in proposal.workflows for text in (draft.prompt, draft.summary, *draft.rules)),
    ]


def cron_triggers(proposal: TaskProposal) -> list[CronTrigger]:
    return [trigger for draft in proposal.workflows for trigger in draft.triggers if isinstance(trigger, CronTrigger)]


def collectors(proposal: TaskProposal) -> list[WorkflowDraft]:
    return [
        draft for draft in proposal.workflows if "cron" in (kinds := trigger_kinds(draft)) and "workflow" not in kinds
    ]


def deciders(proposal: TaskProposal) -> list[WorkflowDraft]:
    return [draft for draft in proposal.workflows if trigger_kinds(draft) == {"workflow"}]


def upstream_names(draft: WorkflowDraft) -> set[str]:
    return {trigger.workflow for trigger in draft.triggers if isinstance(trigger, WorkflowTriggerDraft)}


def run_checks(proposal: TaskProposal, checks: tuple[Check, ...]) -> tuple[CheckResult, ...]:
    return tuple(CheckResult(name, predicate(proposal)) for name, predicate in checks)


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


async def simulate(
    runner: InterviewRunner, provider: AgentProvider, scenario: str, checks: tuple[Check, ...]
) -> Outcome:
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
        task = await persist_proposal(proposal, status="draft")
    except (InterviewError, ValidationError, sqlite3.Error) as exc:
        return Failure(scenario=scenario, error=str(exc))
    return Success(scenario, interview, proposal, task, run_checks(proposal, checks))


def render_workflow(draft: WorkflowDraft) -> str:
    triggers = ", ".join(f"`{render_trigger(t)}`" for t in draft.triggers)
    rules = "\n".join(f"    - {rule}" for rule in draft.rules) or "    - _(none)_"
    return "\n".join(
        [
            f"- **{draft.name}**",
            f"  - summary: {draft.summary}",
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
            f"- **gaps:** {'; '.join(proposal.gaps) or '—'}",
            f"- **persisted:** `{outcome.task.uid}` · status=`{outcome.task.status}`",
            "",
            f"### Workflows ({len(proposal.workflows)})",
            *(render_workflow(w) for w in proposal.workflows),
            *render_checks(outcome.checks),
        ]
    )


def render_checks(checks: tuple[CheckResult, ...]) -> list[str]:
    if not checks:
        return []
    return ["", "### Checks", *(f"- {'✓' if result.passed else '✗'} {result.name}" for result in checks)]


def render_outcome(n: int, outcome: Outcome) -> str:
    match outcome:
        case Failure(scenario=scenario, error=error):
            return f"## Scenario {n} — FAILED\n\n**Raw input:** {scenario}\n\n**Error:** {error}"
        case Success() as success:
            return render_success(n, success)


def render_report(outcomes: list[Outcome]) -> str:
    header = f"# Interview layouts\n\n{len(outcomes)} scenarios simulated and persisted as draft tasks."
    return "\n\n".join([header, *(render_outcome(n, o) for n, o in enumerate(outcomes, 1))])


def outcome_ok(outcome: Outcome) -> bool:
    match outcome:
        case Failure():
            return False
        case Success(checks=checks):
            return all(result.passed for result in checks)


async def main() -> bool:
    scenarios = load_scenarios()
    async with lifespan():
        provider = ThrottledProvider(ClaudeAgentSDKProvider(), asyncio.Semaphore(CONCURRENCY))
        runner = InterviewRunner(provider)
        outcomes = await asyncio.gather(
            *(
                simulate(runner, provider, scenario, checks)
                for scenario, checks in zip(scenarios, EXPECTATIONS, strict=True)
            )
        )
    report = render_report(list(outcomes))
    DUMP.parent.mkdir(parents=True, exist_ok=True)
    DUMP.write_text(report)
    print(report)
    return all(outcome_ok(outcome) for outcome in outcomes)


if __name__ == "__main__":
    sys.exit(0 if anyio.run(main) else 1)
