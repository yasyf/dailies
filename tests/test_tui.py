from __future__ import annotations

import pytest
from textual.widgets import Static

from dailies.interface.textual_app import DailiesApp, TaskDetailScreen
from dailies.interview import InterviewRunner
from tests.fakes import FakePresenter, ScriptedProvider

pytestmark = pytest.mark.tui


async def test_task_detail_shows_full_layout() -> None:
    app = DailiesApp(presenter=FakePresenter(), interviewer=InterviewRunner(ScriptedProvider([])))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # task -> detail
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TaskDetailScreen)
        text = "\n".join(str(widget.render()) for widget in screen.query_one("#detail").query(Static))
        assert "Send a daily digest" in text
        assert "send the digest" in text
        assert "CREATE TABLE sent (day TEXT)" in text
        assert "cron 0 9 * * *" in text


async def test_drilldown_renders_three_panes() -> None:
    app = DailiesApp(presenter=FakePresenter(), interviewer=InterviewRunner(ScriptedProvider([])))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # task -> detail
        await pilot.pause()
        await pilot.press("w")  # detail -> workflow list
        await pilot.pause()
        await pilot.press("enter")  # workflow -> run list
        await pilot.pause()
        await pilot.press("enter")  # run -> run detail
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#status")
        assert screen.query_one("#actions")
        assert screen.query_one("#state")
