from __future__ import annotations

from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Static
from textual.worker import Worker, WorkerState

from dailies.interview import InterviewError, InterviewRunner, persist_proposal
from dailies.models import Exchange, Interview, TaskProposal, TaskStatus


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
        await self.echo("Interviewer: What would you like to automate?")
        self.query_one("#answer", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if not (answer := event.value.strip()):
            return
        await self.echo(f"You: {answer}")
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
        result = await self.runner.next_turn(interview)
        if result.finished:
            self.app.push_screen(ReviewScreen(await self.runner.synthesize(interview)))
        elif result.question is not None:
            self.pending_question = result.question
            await self.echo(f"Interviewer: {result.question}")
            self.reenable_input()
        else:
            raise InterviewError("interview turn was not finished but asked no question")

    async def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group == "interview" and event.state is WorkerState.ERROR:
            await self.echo(f"[error] {event.worker.error}")
            self.reenable_input()

    async def echo(self, text: str) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(Static(text))
        transcript.scroll_end(animate=False)

    def reenable_input(self) -> None:
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
            yield Static(f"Task: {task.name}")
            yield Static(task.description)
            yield Static(f"Prompt: {task.prompt}")
            for wf in self.proposal.workflows:
                yield Static(f"\nWorkflow: {wf.name}")
                yield Static(f"  prompt: {wf.prompt}")
                yield Static(f"  rules: {', '.join(wf.rules) or '—'}")
                yield Static(f"  ddl: {wf.ddl}")
                yield Static(f"  cron: {wf.cron_expression or '—'}")
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
        self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "approve":
                self.action_approve()
            case "save":
                self.action_save()
            case "discard":
                self.action_discard()

    @work(exclusive=True, exit_on_error=False, group="persist")
    async def persist(self, *, status: TaskStatus) -> None:
        task = await persist_proposal(self.proposal, status=status)
        self.notify(f"Saved “{task.name}” as {status}.")
        self.app.pop_screen()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group == "persist" and event.state is WorkerState.ERROR:
            self.notify(f"Save failed: {event.worker.error}", severity="error")
