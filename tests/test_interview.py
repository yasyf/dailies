from __future__ import annotations

import pytest

from dailies.interview import SYNTHESIS_SYSTEM, InterviewRunner, draft_triggers
from dailies.models import (
    CronExpr,
    CronTrigger,
    DraftTrigger,
    EventTrigger,
    Interview,
    InterviewTurn,
    ManualTrigger,
    WorkflowDraft,
    WorkflowId,
    WorkflowTrigger,
    WorkflowTriggerDraft,
    new_uuid,
)
from dailies.tools import render_catalog
from tests.fakes import ToolScriptedProvider

pytestmark = pytest.mark.unit


async def test_next_turn_parses_question() -> None:
    provider = ToolScriptedProvider([{"value": {"finished": False, "question": "When should it run?"}}])
    turn = await InterviewRunner(provider).next_turn(Interview(scenario="email me a digest"))
    assert turn == InterviewTurn(finished=False, question="When should it run?")


async def test_next_turn_parses_finished() -> None:
    provider = ToolScriptedProvider([{"value": {"finished": True, "question": None}}])
    turn = await InterviewRunner(provider).next_turn(Interview(scenario="x"))
    assert turn == InterviewTurn(finished=True, question=None)


PROPOSAL = {
    "task": {
        "name": "Digest",
        "description": "Daily digest",
        "user_input": "email me a digest",
        "prompt": "summarize the day",
        "shared_ddl": "CREATE TABLE totals (sent INTEGER)",
    },
    "workflows": [
        {
            "name": "send",
            "summary": "Sends the digest each morning",
            "prompt": "send the digest",
            "rules": ["be brief"],
            "ddl": "CREATE TABLE sent (day TEXT)",
            "triggers": [{"kind": "cron", "cron_expression": "0 9 * * *", "timezone": "America/New_York"}],
        }
    ],
}


async def test_synthesize_parses_proposal() -> None:
    provider = ToolScriptedProvider([{"value": PROPOSAL}])
    result = await InterviewRunner(provider).synthesize(Interview(scenario="email me a digest"))
    assert result.task.name == "Digest"
    assert result.task.user_input == "email me a digest"
    assert result.task.shared_ddl == "CREATE TABLE totals (sent INTEGER)"
    assert [w.name for w in result.workflows] == ["send"]
    assert result.workflows[0].summary == "Sends the digest each morning"
    assert result.workflows[0].rules == ["be brief"]
    assert result.workflows[0].triggers == [
        CronTrigger(cron_expression=CronExpr("0 9 * * *"), timezone="America/New_York")
    ]


async def test_synthesize_parses_gaps() -> None:
    provider = ToolScriptedProvider([{"value": PROPOSAL | {"gaps": ["push notifications to a phone"]}}])
    result = await InterviewRunner(provider).synthesize(Interview(scenario="email me a digest"))
    assert result.gaps == ["push notifications to a phone"]


def test_synthesis_system_embeds_catalog() -> None:
    assert render_catalog() in SYNTHESIS_SYSTEM


async def test_synthesize_returns_structured_data_via_a_single_submit_tool() -> None:
    provider = ToolScriptedProvider([{"value": PROPOSAL}])
    await InterviewRunner(provider).synthesize(Interview(scenario="email me a digest"))
    request = provider.requests[0]
    assert [spec.name for spec in request.tools] == ["submit"]
    schema = request.tools[0].input_schema
    assert set(schema["properties"]) == {"value"}
    assert "$defs" in schema  # the TaskProposal model rides as structured tool schema...
    assert '"properties"' not in request.system  # ...not as a free-hand JSON dump in the prompt


@pytest.mark.parametrize(
    "trigger",
    [
        pytest.param(CronTrigger(cron_expression=CronExpr("0 9 * * *")), id="cron-utc"),
        pytest.param(CronTrigger(cron_expression=CronExpr("0 9 * * *"), timezone="America/New_York"), id="cron-tz"),
        pytest.param(EventTrigger(source="gmail", event="query", key="abc"), id="event"),
        pytest.param(ManualTrigger(), id="manual"),
    ],
)
def test_draft_triggers(trigger: DraftTrigger) -> None:
    draft = WorkflowDraft(
        name="w", summary="s", prompt="p", rules=[], ddl="CREATE TABLE t (x TEXT)", triggers=[trigger]
    )
    assert draft_triggers(draft, ids={}) == [trigger]


def test_draft_triggers_resolves_workflow_name_to_id() -> None:
    tracker_id = WorkflowId(new_uuid())
    draft = WorkflowDraft(
        name="decider",
        summary="s",
        prompt="p",
        rules=[],
        ddl="CREATE TABLE t (x TEXT)",
        triggers=[WorkflowTriggerDraft(workflow="tracker")],
    )
    assert draft_triggers(draft, ids={"tracker": tracker_id}) == [WorkflowTrigger(workflow_id=tracker_id)]


FAN_IN_PROPOSAL = {
    "task": {"name": "Scout", "description": "d", "user_input": "u", "prompt": "p", "shared_ddl": None},
    "workflows": [
        {
            "name": "tracker",
            "summary": "s",
            "prompt": "p",
            "rules": [],
            "ddl": "CREATE TABLE t (x TEXT)",
            "triggers": [{"kind": "cron", "cron_expression": "0 7 * * *", "timezone": "UTC"}],
        },
        {
            "name": "decider",
            "summary": "s",
            "prompt": "p",
            "rules": [],
            "ddl": "CREATE TABLE d (x TEXT)",
            "triggers": [{"kind": "workflow", "workflow": "tracker"}],
        },
    ],
}


async def test_synthesize_parses_workflow_trigger() -> None:
    provider = ToolScriptedProvider([{"value": FAN_IN_PROPOSAL}])
    result = await InterviewRunner(provider).synthesize(Interview(scenario="u"))
    assert result.workflows[1].triggers == [WorkflowTriggerDraft(workflow="tracker")]
