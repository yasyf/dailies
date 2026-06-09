from __future__ import annotations

import json

import pytest

from dailies.agent import AgentResult
from dailies.interview import InterviewRunner, draft_triggers, extract_json
from dailies.models import CronExpr, CronTrigger, Interview, InterviewTurn, Trigger, WorkflowDraft
from tests.fakes import ScriptedProvider

pytestmark = pytest.mark.unit


async def test_next_turn_parses_question() -> None:
    payload = json.dumps({"finished": False, "question": "When should it run?"})
    runner = InterviewRunner(ScriptedProvider([AgentResult(payload, ok=True)]))
    turn = await runner.next_turn(Interview(scenario="email me a digest"))
    assert turn == InterviewTurn(finished=False, question="When should it run?")


async def test_next_turn_parses_finished() -> None:
    payload = json.dumps({"finished": True, "question": None})
    runner = InterviewRunner(ScriptedProvider([AgentResult(payload, ok=True)]))
    assert await runner.next_turn(Interview(scenario="x")) == InterviewTurn(finished=True, question=None)


async def test_synthesize_parses_proposal() -> None:
    proposal = {
        "task": {
            "name": "Digest",
            "description": "Daily digest",
            "user_input": "email me a digest",
            "prompt": "summarize the day",
        },
        "workflows": [
            {
                "name": "send",
                "prompt": "send the digest",
                "rules": ["be brief"],
                "ddl": "CREATE TABLE sent (day TEXT)",
                "cron_expression": "0 9 * * *",
            }
        ],
    }
    runner = InterviewRunner(ScriptedProvider([AgentResult(json.dumps(proposal), ok=True)]))
    result = await runner.synthesize(Interview(scenario="email me a digest"))
    assert result.task.name == "Digest"
    assert result.task.user_input == "email me a digest"
    assert [w.name for w in result.workflows] == ["send"]
    assert result.workflows[0].rules == ["be brief"]
    assert result.workflows[0].cron_expression == "0 9 * * *"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param('{"finished": true, "question": null}', {"finished": True, "question": None}, id="bare"),
        pytest.param(
            '```json\n{"finished": true, "question": null}\n```', {"finished": True, "question": None}, id="fenced"
        ),
        pytest.param(
            'Sure! Here it is: {"finished": false, "question": "When?"} Hope that helps.',
            {"finished": False, "question": "When?"},
            id="prose",
        ),
    ],
)
def test_extract_json_tolerates_wrapping(text: str, expected: dict[str, object]) -> None:
    assert json.loads(extract_json(text)) == expected


@pytest.mark.parametrize(
    ("cron", "expected"),
    [
        pytest.param("0 9 * * *", [CronTrigger(cron_expression=CronExpr("0 9 * * *"))], id="cron"),
        pytest.param(None, [], id="none"),
    ],
)
def test_draft_triggers(cron: str | None, expected: list[Trigger]) -> None:
    draft = WorkflowDraft(name="w", prompt="p", rules=[], ddl="CREATE TABLE t (x TEXT)", cron_expression=cron)
    assert draft_triggers(draft) == expected
