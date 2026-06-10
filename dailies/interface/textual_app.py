from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from beanie.operators import In
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Label, ListItem, ListView, Static
from textual.worker import Worker, WorkerState

from dailies.documents import Run, Task, TaskState, Workflow, WorkflowState
from dailies.interface.presenter import BlastRadius, Presenter
from dailies.interface.rendering import (
    WorkflowCard,
    block_widget,
    excerpt,
    render_trigger,
    run_status_text,
    state_table,
    state_widgets,
    status_badge,
    task_header,
    workflow_flow,
)
from dailies.interface.screens import InterviewScreen
from dailies.interview import InterviewRunner
from dailies.models import TaskId, WorkflowId


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

    async def get_state(self, workflow_id: WorkflowId) -> WorkflowState | None:
        return await WorkflowState.find(WorkflowState.workflow_id == workflow_id).first_or_none()

    async def get_task_state(self, task_id: TaskId) -> TaskState | None:
        return await TaskState.find(TaskState.task_id == task_id).first_or_none()

    async def blast_radius(self, task_id: TaskId) -> BlastRadius:
        return BlastRadius(
            workflows=await Workflow.find(Workflow.task_id == task_id).count(),
            runs=await Run.find(Run.task_id == task_id).count(),
        )

    async def delete_task(self, task_id: TaskId) -> None:
        workflow_ids = [w.workflow_id for w in await Workflow.find(Workflow.task_id == task_id).to_list()]
        await Run.find(Run.task_id == task_id).delete()
        await WorkflowState.find(In(WorkflowState.workflow_id, workflow_ids)).delete()
        await TaskState.find(TaskState.task_id == task_id).delete()
        await Workflow.find(Workflow.task_id == task_id).delete()
        await Task.find(Task.id == task_id).delete()


class DrillScreen(Screen[None]):
    """Drill-down screen that pops itself on escape."""

    BINDINGS: ClassVar = [("escape", "back", "Back")]

    def action_back(self) -> None:
        self.app.pop_screen()


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Modal confirmation showing the blast radius; runs the cascade itself so dismissal means done."""

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]

    def __init__(self, presenter: Presenter, task: Task, radius: BlastRadius) -> None:
        super().__init__()
        self.presenter = presenter
        self.target = task
        self.radius = radius
        self.deleting = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                f"Delete “{self.target.name}”?\n"
                f"This deletes {plural(self.radius.workflows, 'workflow')}, {plural(self.radius.runs, 'run')}, "
                f"and all stored state. Permanent.",
                markup=False,
            ),
            Horizontal(Button("Delete", id="delete", variant="error"), Button("Cancel", id="cancel")),
        )

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def action_cancel(self) -> None:
        if not self.deleting:
            self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "delete":
                self.delete()
            case "cancel":
                self.action_cancel()

    @work(exclusive=True, exit_on_error=False, group="delete")
    async def delete(self) -> None:
        self.deleting = True
        self.query_one("#delete", Button).disabled = True
        await self.presenter.delete_task(self.target.uid)
        self.dismiss(True)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group == "delete" and event.state is WorkerState.ERROR:
            self.notify(f"Delete failed: {event.worker.error}", severity="error", markup=False)
            self.dismiss(False)


class TaskListScreen(Screen[None]):
    BINDINGS: ClassVar = [("d", "delete", "Delete")]

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
        await view.extend(
            ListItem(
                Vertical(
                    Horizontal(Label(task.name, classes="item-title", markup=False), status_badge(task.status)),
                    Label(excerpt(task.definition.description), classes="item-meta", markup=False),
                )
            )
            for task in self.tasks
        )
        if self.tasks:
            view.index = 0
        view.focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if (index := event.list_view.index) is not None:
            self.app.push_screen(TaskDetailScreen(self.presenter, self.tasks[index].uid))

    def action_delete(self) -> None:
        if (index := self.query_one("#list", ListView).index) is not None:
            self.delete_task(self.tasks[index])

    @work(exclusive=True, exit_on_error=False, group="delete")
    async def delete_task(self, task: Task) -> None:
        if await confirm_delete(self.app, self.presenter, task):
            self.notify(f"Deleted “{task.name}”.", markup=False)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group == "delete" and event.state is WorkerState.ERROR:
            self.notify(f"Delete failed: {event.worker.error}", severity="error", markup=False)


class TaskDetailScreen(DrillScreen):
    """Read-only layout for one task: its header plus a flow diagram per workflow."""

    BINDINGS: ClassVar = [("w", "workflows", "Workflows"), ("s", "state", "State"), ("d", "delete", "Delete")]

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
        await self.query_one("#detail", VerticalScroll).mount(
            task_header(task),
            *(workflow_flow(WorkflowCard.from_workflow(workflow)) for workflow in workflows),
        )
        self.sub_title = task.name

    def action_workflows(self) -> None:
        self.app.push_screen(WorkflowListScreen(self.presenter, self.task_id))

    def action_state(self) -> None:
        self.app.push_screen(StateScreen(self.presenter, self.task_id))

    def action_delete(self) -> None:
        self.delete_task()

    @work(exclusive=True, exit_on_error=False, group="delete")
    async def delete_task(self) -> None:
        task = await self.presenter.get_task(self.task_id)
        if await confirm_delete(self.app, self.presenter, task):
            self.notify(f"Deleted “{task.name}”.", markup=False)
            self.app.pop_screen()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group == "delete" and event.state is WorkerState.ERROR:
            self.notify(f"Delete failed: {event.worker.error}", severity="error", markup=False)


class StateScreen(DrillScreen):
    """Stored state for one task: highlighted DDL plus live key/value rows per workflow."""

    def __init__(self, presenter: Presenter, task_id: TaskId) -> None:
        super().__init__()
        self.presenter = presenter
        self.task_id = task_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="task-state")
        yield Footer()

    async def on_mount(self) -> None:
        task = await self.presenter.get_task(self.task_id)
        pane = self.query_one("#task-state", VerticalScroll)
        if task.shared_ddl:
            await pane.mount(
                *state_widgets("Shared task state", task.shared_ddl, await self.presenter.get_task_state(self.task_id))
            )
        for workflow in await self.presenter.list_workflows(self.task_id):
            await pane.mount(
                *state_widgets(
                    f"{workflow.name} v{workflow.version}",
                    workflow.ddl,
                    await self.presenter.get_state(workflow.workflow_id),
                )
            )
        self.sub_title = task.name


class WorkflowListScreen(DrillScreen):
    BINDINGS: ClassVar = [("s", "state", "State")]

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
        self.sub_title = (await self.presenter.get_task(self.task_id)).name
        self.workflows = list(await self.presenter.list_workflows(self.task_id))
        view = self.query_one("#list", ListView)
        await view.extend(
            ListItem(
                Vertical(
                    Horizontal(
                        Label(f"{workflow.name} v{workflow.version}", classes="item-title", markup=False),
                        status_badge(workflow.status),
                    ),
                    Label(
                        ", ".join(render_trigger(t) for t in workflow.triggers) or "—",
                        classes="item-meta",
                        markup=False,
                    ),
                )
            )
            for workflow in self.workflows
        )
        if self.workflows:
            view.index = 0
        view.focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if (index := event.list_view.index) is not None:
            self.app.push_screen(RunListScreen(self.presenter, self.workflows[index]))

    def action_state(self) -> None:
        self.app.push_screen(StateScreen(self.presenter, self.task_id))


class RunListScreen(DrillScreen):
    def __init__(self, presenter: Presenter, workflow: Workflow) -> None:
        super().__init__()
        self.presenter = presenter
        self.workflow = workflow

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="runs", cursor_type="row")
        yield Footer()

    async def on_mount(self) -> None:
        self.sub_title = f"{self.workflow.name} v{self.workflow.version}"
        table = self.query_one("#runs", DataTable)
        table.add_columns("status", "trigger", "started")
        for run in await self.presenter.list_runs(self.workflow.workflow_id):
            table.add_row(
                run_status_text(run.status),
                render_trigger(run.trigger),
                f"{run.created_at:%Y-%m-%d %H:%M}",
                key=str(run.uid),
            )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        match event.row_key.value:
            case str(run_id):
                self.app.push_screen(RunDetailScreen(self.presenter, UUID(run_id)))


class RunDetailScreen(DrillScreen):
    def __init__(self, presenter: Presenter, run_id: UUID) -> None:
        super().__init__()
        self.presenter = presenter
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            for pane_id, title in (("status", "Status"), ("actions", "Actions"), ("state", "State")):
                pane = VerticalScroll(id=pane_id)
                pane.border_title = title
                yield pane
        yield Footer()

    async def on_mount(self) -> None:
        run = await self.presenter.get_run(self.run_id)
        self.sub_title = f"run {run.created_at:%Y-%m-%d %H:%M}"
        await self.query_one("#status", VerticalScroll).mount(
            *(
                widget
                for update in run.status_updates
                for widget in (
                    Label(update.title, classes="update-title", markup=False),
                    Label(f"{update.created_at:%H:%M:%S}", classes="update-time"),
                    *(block_widget(block) for block in update.blocks),
                )
            )
        )
        await self.query_one("#actions", VerticalScroll).mount(
            *(Static(f"{action.kind} -> {action.target}", markup=False) for action in run.actions)
        )
        state = await self.presenter.get_state(run.workflow_id)
        await self.query_one("#state", VerticalScroll).mount(Static(state_table(state.data if state else {})))


class DailiesApp(App[None]):
    """Textual drill-down UI over dailies tasks, workflows, runs, and stored state."""

    CSS_PATH: ClassVar = "dailies.tcss"
    TITLE = "dailies"
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


async def confirm_delete(app: App[None], presenter: Presenter, task: Task) -> bool:
    return await app.push_screen_wait(ConfirmDeleteScreen(presenter, task, await presenter.blast_radius(task.uid)))


async def run_tui(presenter: Presenter, interviewer: InterviewRunner, *, start_interview: bool = False) -> None:
    """Launch the Textual UI against the given presenter (runs on the caller's event loop)."""
    await DailiesApp(presenter=presenter, interviewer=interviewer, start_interview=start_interview).run_async()
