from __future__ import annotations

import pytest

from dailies.interface.textual_app import DailiesApp
from dailies.interview import InterviewRunner
from tests.fakes import FakePresenter, ScriptedProvider

pytestmark = pytest.mark.tui


async def test_drilldown_renders_three_panes() -> None:
    app = DailiesApp(presenter=FakePresenter(), interviewer=InterviewRunner(ScriptedProvider([])))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # task -> workflow list
        await pilot.pause()
        await pilot.press("enter")  # workflow -> run list
        await pilot.pause()
        await pilot.press("enter")  # run -> run detail
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#status")
        assert screen.query_one("#actions")
        assert screen.query_one("#state")
