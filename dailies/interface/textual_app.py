from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar
from uuid import UUID

from pydantic import JsonValue
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from dailies.documents import Run, Task, Workflow, WorkflowState
from dailies.interface.presenter import Presenter
from dailies.interface.screens import InterviewScreen
from dailies.interview import InterviewRunner
from dailies.models import (
    Block,
    CronTrigger,
    EventTrigger,
    ImageBlock,
    ManualTrigger,
    TaskId,
    TextBlock,
    Trigger,
    WorkflowId,
)


async def mount_block(container: VerticalScroll, block: Block) -> None:
    match block:
        case TextBlock(text=text):
            await container.mount(Static(text))
        case ImageBlock(url=url):
            await container.mount(Static(f"[image] {url}"))


def render_trigger(trigger: Trigger) -> str:
    match trigger:
        case CronTrigger(cron_expression=expr):
            return f"cron {expr}"
        case EventTrigger(event_type=event_type, event_key=event_key):
            return f"event {event_type}/{event_key}"
        case ManualTrigger():
            return "manual"


def workflow_detail_widgets(workflow: Workflow) -> list[Static]:
    return [
        Static(f"\nWorkflow: {workflow.name} v{workflow.version}  ({workflow.status})"),
        Static(f"  prompt: {workflow.definition.prompt}"),
        Static(f"  rules: {', '.join(workflow.definition.rules) or '—'}"),
        Static(f"  ddl: {workflow.ddl}"),
        Static(f"  triggers: {', '.join(render_trigger(t) for t in workflow.triggers) or '—'}"),
    ]


def task_detail_widgets(task: Task, workflows: Sequence[Workflow]) -> list[Static]:
    return [
        Static(f"Task: {task.name}  ({task.status})"),
        Static(task.definition.description),
        Static(f"Prompt: {task.definition.prompt}"),
        *(widget for workflow in workflows for widget in workflow_detail_widgets(workflow)),
    ]


class TextualPresenter:
    async def list_tasks(self) -> list[Task]:
        return await Task.find_all().to_list()

    async def get_task(self, task_id: TaskId) -> Task:
        task = await Task.get(task_id)
        if task is None:
            raise LookupError(task_id)
        return task

    async def list_workflows(self, task_id: TaskId) -> list[Workflow]:
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
        await self.refresh_tasks()

    async def on_screen_resume(self) -> None:
        await self.refresh_tasks()

    async def refresh_tasks(self) -> None:
        self.tasks = list(await self.presenter.list_tasks())
        view = self.query_one("#list", ListView)
        await view.clear()
        for task in self.tasks:
            await view.append(ListItem(Label(task.name)))
        if self.tasks:
            view.index = 0
        view.focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if (index := event.list_view.index) is not None:
            self.app.push_screen(TaskDetailScreen(self.presenter, self.tasks[index].uid))


class TaskDetailScreen(Screen[None]):
    """Read-only layout for one task: its prompt plus every workflow's prompt, rules, ddl, and triggers."""

    BINDINGS: ClassVar = [("w", "workflows", "Workflow runs")]

    def __init__(self, presenter: Presenter, task_id: TaskId) -> None:
        super().__init__()
        self.presenter = presenter
        self.task_id = task_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="detail")
        yield Footer()

    async def on_mount(self) -> None:
        task = await self.presenter.get_task(self.task_id)
        workflows = await self.presenter.list_workflows(self.task_id)
        await self.query_one("#detail", VerticalScroll).mount(*task_detail_widgets(task, workflows))

    def action_workflows(self) -> None:
        self.app.push_screen(WorkflowListScreen(self.presenter, self.task_id))


class WorkflowListScreen(Screen[None]):
    def __init__(self, presenter: Presenter, task_id: TaskId) -> None:
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

    BINDINGS: ClassVar = [("q", "quit", "Quit"), ("i", "interview", "Interview")]

    def __init__(self, *, presenter: Presenter, interviewer: InterviewRunner, start_interview: bool = False) -> None:
        super().__init__()
        self.presenter = presenter
        self.interviewer = interviewer
        self.start_interview = start_interview

    def on_mount(self) -> None:
        self.push_screen(TaskListScreen(self.presenter))
        if self.start_interview:
            self.push_screen(InterviewScreen(self.interviewer))

    def action_interview(self) -> None:
        self.push_screen(InterviewScreen(self.interviewer))


async def run_tui(presenter: Presenter, interviewer: InterviewRunner, *, start_interview: bool = False) -> None:
    """Launch the Textual UI against the given presenter (runs on the caller's event loop)."""
    await DailiesApp(presenter=presenter, interviewer=interviewer, start_interview=start_interview).run_async()
