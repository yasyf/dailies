from __future__ import annotations

import asyncio

import pytest
from rich.syntax import Syntax
from rich.table import Table
from textual.widget import Widget
from textual.widgets import Button, DataTable, Label, ListView, Static

from dailies.interface.textual_app import (
    ConfirmDeleteScreen,
    DailiesApp,
    StateScreen,
    TaskDetailScreen,
    TaskListScreen,
)
from dailies.interview import InterviewRunner
from dailies.models import TaskId
from tests.fakes import FakePresenter, ScriptedProvider

pytestmark = pytest.mark.tui


def make_app(presenter: FakePresenter) -> DailiesApp:
    return DailiesApp(presenter=presenter, interviewer=InterviewRunner(ScriptedProvider([])))


def statics_text(container: Widget) -> str:
    return "\n".join(str(widget.render()) for widget in container.query(Static))


def syntax_code(widget: Static) -> str:
    assert isinstance(syntax := widget.content, Syntax)
    return syntax.code


def rich_table(widget: Static) -> Table:
    assert isinstance(table := widget.content, Table)
    return table


async def test_task_detail_shows_full_layout() -> None:
    app = make_app(FakePresenter())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # task -> detail
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TaskDetailScreen)
        detail = screen.query_one("#detail")
        assert str(detail.query_one(".task-name", Label).render()) == "Daily digest"
        assert "Send a daily digest" in statics_text(detail.query_one(".task-header"))
        assert "summarize the day" in statics_text(detail.query_one(".task-header"))
        workflow_text = statics_text(detail.query_one(".flow-box.workflow"))
        assert "Sends the digest each morning" in workflow_text
        assert "send the digest" in workflow_text
        assert "cron 0 9 * * *" in statics_text(detail.query_one(".flow-box.trigger"))
        assert [syntax_code(widget) for widget in detail.query(".ddl").results(Static)] == [
            "CREATE TABLE totals (sent INTEGER)",
        ]
        state = detail.query_one(".flow-box.state")
        state_text = statics_text(state)
        assert "🗄 sent" in state_text
        assert "day TEXT" in state_text
        tables = [widget.content for widget in state.query(Static) if isinstance(widget.content, Table)]
        assert [table.title for table in tables] == ["sent"]
        assert tables[0].row_count == 2
        assert tables[0].caption is None
        assert "queue (no rows)" in state_text
        assert detail.query_one(".flow-terminus", Static)


async def test_flow_boxes_span_and_triggers_hug() -> None:
    app = make_app(FakePresenter())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # task -> detail
        await pilot.pause()
        detail = app.screen.query_one("#detail")
        flow = detail.query_one(".flow")
        assert detail.query_one(".flow-box.workflow").region.width == flow.region.width
        assert detail.query_one(".flow-box.state").region.width == flow.region.width
        trigger = detail.query_one(".flow-box.trigger")
        assert 0 < trigger.region.width <= flow.region.width
        assert trigger.region.right <= flow.region.right
        cron_line, event_line = trigger.query(Static).results(Static)
        assert cron_line.region.height == 1
        assert event_line.region.right <= flow.region.right
        assert detail.query_one(".flow-box.workflow .summary", Static).region.height == 1


async def test_drilldown_renders_three_panes() -> None:
    app = make_app(FakePresenter())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # task -> detail
        await pilot.pause()
        await pilot.press("w")  # detail -> workflow list
        await pilot.pause()
        await pilot.press("enter")  # workflow -> run list
        await pilot.pause()
        assert app.screen.query_one("#runs", DataTable).row_count == 1
        await pilot.press("enter")  # run -> run detail
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#status")
        assert screen.query_one("#actions")
        assert screen.query_one("#state")


async def test_delete_confirm_cascades_and_refreshes() -> None:
    presenter = FakePresenter()
    app = make_app(presenter)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConfirmDeleteScreen)
        assert str(screen.query_one(Static).render()) == (
            "Delete “Daily digest”?\nThis deletes 1 workflow, 1 run, and all stored state. Permanent."
        )
        assert isinstance(app.focused, Button)
        assert app.focused.id == "cancel"
        await pilot.click("#delete")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert presenter.deleted == [presenter.task.uid]
        assert isinstance(app.screen, TaskListScreen)
        assert len(app.screen.query_one("#list", ListView).children) == 0


@pytest.mark.parametrize("dismissal", ["cancel-button", "escape-key"])
async def test_delete_cancel_keeps_task(dismissal: str) -> None:
    presenter = FakePresenter()
    app = make_app(presenter)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDeleteScreen)
        match dismissal:
            case "cancel-button":
                await pilot.click("#cancel")
            case "escape-key":
                await pilot.press("escape")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert presenter.deleted == []
        assert isinstance(app.screen, TaskListScreen)
        assert len(app.screen.query_one("#list", ListView).children) == 1


async def test_escape_during_cascade_neither_cancels_nor_dismisses() -> None:
    presenter = FakePresenter()
    release = asyncio.Event()
    delete = presenter.delete_task

    async def slow_delete(task_id: TaskId) -> None:
        await release.wait()
        await delete(task_id)

    presenter.delete_task = slow_delete
    app = make_app(presenter)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.click("#delete")
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDeleteScreen)
        assert presenter.deleted == []
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert presenter.deleted == [presenter.task.uid]
        assert isinstance(app.screen, TaskListScreen)


async def test_delete_from_detail_pops_to_list() -> None:
    presenter = FakePresenter()
    app = make_app(presenter)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # task -> detail
        await pilot.pause()
        assert isinstance(app.screen, TaskDetailScreen)
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDeleteScreen)
        await pilot.click("#delete")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, TaskListScreen)
        assert presenter.deleted == [presenter.task.uid]
        assert len(app.screen.query_one("#list", ListView).children) == 0


async def test_state_screen_shows_ddl_and_data() -> None:
    app = make_app(FakePresenter())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # task -> detail
        await pilot.pause()
        await pilot.press("s")  # detail -> state
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, StateScreen)
        statics = list(screen.query_one("#task-state").query(Static))
        assert str(statics[0].render()) == "Shared task state"
        assert syntax_code(statics[1]) == "CREATE TABLE totals (sent INTEGER)"
        assert rich_table(statics[2]).title == "totals"
        assert rich_table(statics[2]).row_count == 1
        assert str(statics[3].render()) == "digest-workflow v1"
        assert syntax_code(statics[4]) == "CREATE TABLE sent (day TEXT)"
        assert rich_table(statics[5]).title == "sent"
        assert rich_table(statics[5]).row_count == 2
