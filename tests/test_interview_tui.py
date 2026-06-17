from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

import pytest
from pydantic import JsonValue
from rich.table import Table
from textual.widgets import Input, LoadingIndicator, Static, TabbedContent

from dailies.agent import AgentRequest, AgentResult
from dailies.interface import screens
from dailies.interface.screens import InterviewScreen, ReviewScreen
from dailies.interface.textual_app import DailiesApp, TaskListScreen
from dailies.interview import InterviewRunner
from dailies.models import TaskId, TaskProposal, TaskStatus
from tests.fakes import FakePresenter, FakeTask, ToolScriptedProvider

pytestmark = pytest.mark.tui


@dataclass(frozen=True, slots=True)
class BlockingTurnProvider:
    """Tool-driving provider that parks inside ``run`` until released, so a worker stays mid-flight."""

    started: asyncio.Event
    release: asyncio.Event
    calls: list[dict[str, JsonValue]]

    async def run(self, request: AgentRequest) -> AgentResult:
        self.started.set()
        await self.release.wait()
        await next(spec for spec in request.tools if spec.name == "submit").invoke(self.calls.pop(0))
        return AgentResult(text="", ok=True)


def transcript_text(screen: InterviewScreen) -> str:
    return "\n".join(str(widget.render()) for widget in screen.query_one("#transcript").query(Static))

TURN_FINISHED = {"value": {"finished": True, "question": None}}
PROPOSAL = {
    "value": {
        "task": {"name": "Digest", "description": "d", "user_input": "email me a digest", "prompt": "p"},
        "workflows": [
            {
                "name": "send",
                "summary": "Sends the digest",
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
        state_box = app.screen.query_one(".flow-box.state")
        state_text = "\n".join(str(widget.render()) for widget in state_box.query(Static))
        assert "🗄 t" in state_text
        assert "x TEXT" in state_text
        assert not state_box.query(TabbedContent)
        assert not any(isinstance(widget.content, Table) for widget in state_box.query(Static))

        await pilot.press("a")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls == ["active"]
        assert isinstance(app.screen, TaskListScreen)


async def test_interview_turn_shows_thinking_indicator() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[dict[str, JsonValue]] = [{"value": {"finished": False, "question": "When should it run?"}}]
    provider = BlockingTurnProvider(started, release, calls)
    app = DailiesApp(presenter=FakePresenter(), interviewer=InterviewRunner(provider), start_interview=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, InterviewScreen)
        screen.query_one("#answer", Input).value = "email me a digest"
        await pilot.press("enter")
        await asyncio.wait_for(started.wait(), timeout=5)
        await pilot.pause()
        indicator = screen.query_one(LoadingIndicator)
        assert indicator.region.height > 0
        assert screen.query_one("#answer", Input).disabled

        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not screen.query(LoadingIndicator)
        assert not screen.query_one("#answer", Input).disabled
        assert "When should it run?" in transcript_text(screen)
