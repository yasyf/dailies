from __future__ import annotations

from uuid import uuid4

import pytest
from textual.widgets import Input

from dailies.interface import screens
from dailies.interface.screens import ReviewScreen
from dailies.interface.textual_app import DailiesApp
from dailies.interview import InterviewRunner
from dailies.models import TaskId, TaskProposal, TaskStatus
from tests.fakes import FakePresenter, FakeTask, ToolScriptedProvider

pytestmark = pytest.mark.tui

TURN_FINISHED = {"value": {"finished": True, "question": None}}
PROPOSAL = {
    "value": {
        "task": {"name": "Digest", "description": "d", "user_input": "email me a digest", "prompt": "p"},
        "workflows": [
            {
                "name": "send",
                "prompt": "p",
                "rules": [],
                "ddl": "CREATE TABLE t (x TEXT)",
                "triggers": [{"kind": "manual"}],
            }
        ],
    }
}


async def test_interview_to_review_and_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[TaskStatus] = []

    async def fake_persist(proposal: TaskProposal, *, status: TaskStatus) -> FakeTask:
        calls.append(status)
        return FakeTask(name="Digest", uid=TaskId(uuid4()))

    monkeypatch.setattr(screens, "persist_proposal", fake_persist)

    provider = ToolScriptedProvider([TURN_FINISHED, PROPOSAL])
    app = DailiesApp(presenter=FakePresenter(), interviewer=InterviewRunner(provider), start_interview=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#answer", Input).value = "email me a digest"
        await pilot.press("enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)

        await pilot.press("a")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls == ["active"]
