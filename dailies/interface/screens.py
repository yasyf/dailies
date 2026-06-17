from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, ClassVar

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, LoadingIndicator, Static
from textual.worker import Worker, WorkerState

from dailies.interface.rendering import WorkflowCard, ddl_block, workflow_flow
from dailies.interview import InterviewError, InterviewRunner, persist_proposal
from dailies.models import Exchange, Interview, TaskProposal, TaskStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from textual.widget import Widget


@asynccontextmanager
async def loading(parent: Widget, *, before: Widget | None = None, classes: str = "loader") -> AsyncIterator[None]:
    indicator = LoadingIndicator(classes=classes)
    await parent.mount(indicator, before=before)
    try:
        yield
    finally:
        await indicator.remove()


class InterviewScreen(Screen[None]):
    """Conversational onboarding: one question per turn until the agent can propose a task."""

    def __init__(self, runner: InterviewRunner) -> None:
        super().__init__()
        self.runner = runner
        self.interview: Interview | None = None
        self.pending_question: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="transcript")
        yield Input(id="answer", placeholder="Describe what you want automated…")
        yield Footer()

    async def on_mount(self) -> None:
        await self.echo("Interviewer: What would you like to automate?", classes="msg-agent")
        self.query_one("#answer", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if not (answer := event.value.strip()):
            return
        await self.echo(f"You: {answer}", classes="msg-user")
        event.input.value = ""
        event.input.disabled = True
        interview = self.next_interview(answer)
        self.interview = interview
        self.turn(interview)

    def next_interview(self, answer: str) -> Interview:
        match (self.interview, self.pending_question):
            case (Interview() as interview, str() as question):
                return Interview(
                    scenario=interview.scenario,
                    exchanges=[*interview.exchanges, Exchange(question=question, answer=answer)],
                )
            case _:
                return Interview(scenario=answer)

    @work(exclusive=True, exit_on_error=False, group="interview")
    async def turn(self, interview: Interview) -> None:
        await self.start_thinking()
        result = await self.runner.next_turn(interview)
        if result.finished:
            self.app.push_screen(ReviewScreen(await self.runner.synthesize(interview)))
        elif result.question is not None:
            self.pending_question = result.question
            await self.echo(f"Interviewer: {result.question}", classes="msg-agent")
            self.reenable_input()
        else:
            raise InterviewError("interview turn was not finished but asked no question")

    async def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group == "interview" and event.state is WorkerState.ERROR:
            await self.echo(f"error: {event.worker.error}", classes="msg-error")
            self.reenable_input()

    async def echo(self, text: str, *, classes: str) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(Static(text, classes=classes, markup=False))
        transcript.scroll_end(animate=False)

    async def start_thinking(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(LoadingIndicator(classes="working"))
        transcript.scroll_end(animate=False)

    def reenable_input(self) -> None:
        self.query_one(LoadingIndicator).remove()
        answer = self.query_one("#answer", Input)
        answer.disabled = False
        answer.focus()


class ReviewScreen(Screen[None]):
    """Review the synthesized proposal, then persist it as active (Approve) or draft (Save)."""

    BINDINGS: ClassVar = [("a", "approve", "Approve"), ("s", "save", "Save draft"), ("d", "discard", "Discard")]

    def __init__(self, proposal: TaskProposal) -> None:
        super().__init__()
        self.proposal = proposal

    def compose(self) -> ComposeResult:
        task = self.proposal.task
        yield Header()
        with VerticalScroll(id="review"):
            yield Label(task.name, classes="task-name", markup=False)
            yield Static(task.description, markup=False)
            yield Static(task.prompt, classes="muted", markup=False)
            if self.proposal.gaps:
                yield Static("\n".join(f"gap: {gap}" for gap in self.proposal.gaps), classes="muted", markup=False)
            if task.shared_ddl:
                yield ddl_block(task.shared_ddl)
            for wf in self.proposal.workflows:
                yield workflow_flow(WorkflowCard.from_draft(wf))
        with Horizontal(id="buttons"):
            yield Button("Approve", id="approve", variant="success")
            yield Button("Save draft", id="save")
            yield Button("Discard", id="discard", variant="error")
        yield Footer()

    def action_approve(self) -> None:
        self.persist(status="active")

    def action_save(self) -> None:
        self.persist(status="draft")

    def action_discard(self) -> None:
        self.close_review()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "approve":
                self.action_approve()
            case "save":
                self.action_save()
            case "discard":
                self.action_discard()

    def close_review(self) -> None:
        self.app.pop_screen()
        self.app.pop_screen()

    @work(exclusive=True, exit_on_error=False, group="persist")
    async def persist(self, *, status: TaskStatus) -> None:
        async with loading(self.query_one("#review", VerticalScroll), classes="working"):
            task = await persist_proposal(self.proposal, status=status)
        self.notify(f"Saved “{task.name}” as {status}.", markup=False)
        self.close_review()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group == "persist" and event.state is WorkerState.ERROR:
            self.notify(f"Save failed: {event.worker.error}", severity="error", markup=False)
