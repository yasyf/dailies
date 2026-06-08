from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar
from uuid import UUID

from pydantic import JsonValue
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from dailies.documents import Run, Task, Workflow, WorkflowState
from dailies.interface.presenter import Presenter
from dailies.models import Block, ImageBlock, TextBlock, WorkflowId


async def mount_block(container: VerticalScroll, block: Block) -> None:
    match block:
        case TextBlock(text=text):
            await container.mount(Static(text))
        case ImageBlock(url=url):
            await container.mount(Static(f"[image] {url}"))


class TextualPresenter:
    async def list_tasks(self) -> list[Task]:
        return await Task.find_all().to_list()

    async def list_workflows(self, task_id: UUID) -> list[Workflow]:
        return await Workflow.find(Workflow.task_id == task_id).to_list()

    async def list_runs(self, workflow_id: WorkflowId) -> list[Run]:
        return await Run.find(Run.workflow_id == workflow_id).sort("-created_at").to_list()

    async def get_run(self, run_id: UUID) -> Run:
        run = await Run.get(run_id)
        if run is None:
            raise LookupError(run_id)
        return run

    async def get_state(self, workflow_id: WorkflowId) -> Mapping[str, JsonValue]:
        state = await WorkflowState.find(WorkflowState.workflow_id == workflow_id).first_or_none()
        return state.data if state else {}


class TaskListScreen(Screen[None]):
    def __init__(self, presenter: Presenter) -> None:
        super().__init__()
        self.presenter = presenter
        self.tasks: list[Task] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(id="list")
        yield Footer()

    async def on_mount(self) -> None:
        self.tasks = list(await self.presenter.list_tasks())
        view = self.query_one("#list", ListView)
        for task in self.tasks:
            await view.append(ListItem(Label(task.name)))
        if self.tasks:
            view.index = 0
        view.focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if (index := event.list_view.index) is not None:
            self.app.push_screen(WorkflowListScreen(self.presenter, self.tasks[index].uid))


class WorkflowListScreen(Screen[None]):
    def __init__(self, presenter: Presenter, task_id: UUID) -> None:
        super().__init__()
        self.presenter = presenter
        self.task_id = task_id
        self.workflows: list[Workflow] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(id="list")
        yield Footer()

    async def on_mount(self) -> None:
        self.workflows = list(await self.presenter.list_workflows(self.task_id))
        view = self.query_one("#list", ListView)
        for workflow in self.workflows:
            await view.append(ListItem(Label(f"{workflow.name} v{workflow.version}")))
        if self.workflows:
            view.index = 0
        view.focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if (index := event.list_view.index) is not None:
            self.app.push_screen(RunListScreen(self.presenter, self.workflows[index].workflow_id))


class RunListScreen(Screen[None]):
    def __init__(self, presenter: Presenter, workflow_id: WorkflowId) -> None:
        super().__init__()
        self.presenter = presenter
        self.workflow_id = workflow_id
        self.runs: list[Run] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(id="list")
        yield Footer()

    async def on_mount(self) -> None:
        self.runs = list(await self.presenter.list_runs(self.workflow_id))
        view = self.query_one("#list", ListView)
        for run in self.runs:
            await view.append(ListItem(Label(f"{run.status} {run.created_at:%Y-%m-%d %H:%M}")))
        if self.runs:
            view.index = 0
        view.focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if (index := event.list_view.index) is not None:
            self.app.push_screen(RunDetailScreen(self.presenter, self.runs[index].uid))


class RunDetailScreen(Screen[None]):
    def __init__(self, presenter: Presenter, run_id: UUID) -> None:
        super().__init__()
        self.presenter = presenter
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield VerticalScroll(Label("Status"), id="status")
            yield VerticalScroll(Label("Actions"), id="actions")
            yield VerticalScroll(Label("State"), id="state")
        yield Footer()

    async def on_mount(self) -> None:
        run = await self.presenter.get_run(self.run_id)
        status = self.query_one("#status", VerticalScroll)
        for update in run.status_updates:
            await status.mount(Label(update.title))
            for block in update.blocks:
                await mount_block(status, block)
        actions = self.query_one("#actions", VerticalScroll)
        for action in run.actions:
            await actions.mount(Static(f"{action.kind} -> {action.target}"))
        state = self.query_one("#state", VerticalScroll)
        for key, value in (await self.presenter.get_state(run.workflow_id)).items():
            await state.mount(Static(f"{key}: {value}"))


class DailiesApp(App[None]):
    """Textual drill-down UI over dailies tasks, workflows, runs, and stored state."""

    BINDINGS: ClassVar = [("q", "quit", "Quit")]

    def __init__(self, *, presenter: Presenter) -> None:
        super().__init__()
        self.presenter = presenter

    def on_mount(self) -> None:
        self.push_screen(TaskListScreen(self.presenter))


def run_tui(presenter: Presenter) -> None:
    """Launch the Textual UI against the given presenter (blocking; Textual owns the loop)."""
    DailiesApp(presenter=presenter).run()
