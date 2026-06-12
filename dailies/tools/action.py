from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from pydantic import JsonValue

from dailies.models import Action, FrozenModel
from dailies.runtime import RunContext
from dailies.tools.base import ToolSet, tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from dailies.gmail import GmailClient

type ActionRecorder = Callable[[Action], Awaitable[None]]
type ActionReader = Callable[[], Awaitable[list[Action]]]


class Notification(FrozenModel):
    channel: str
    title: str
    body: str


class SentReceipt(FrozenModel):
    action_id: UUID
    message_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class ActionToolSet(ToolSet):
    integrations: ClassVar[tuple[str, ...]] = ("gmail",)

    context: RunContext
    gmail: GmailClient
    record: ActionRecorder
    recorded: ActionReader

    @tool
    async def send_email(self, to: str, subject: str, body: str) -> SentReceipt:
        """Send an email and return a receipt with the action, message, and thread ids.

        When an action needs the user's approval (it is irreversible, spends
        unauthorized money, or the rules say confirm first), gate it across
        runs — never wait for the reply inside a run: send the request, call
        subscribe_to_thread with the receipt's thread_id, record the pending
        decision in a state table (what awaits approval and why), and end the
        run. The user's reply fires a new run, which reads the pending row,
        interprets the reply, and proceeds or abandons.
        """
        sent = await self.gmail.send(to=to, subject=subject, body=body)
        action = Action(
            kind="email",
            target=to,
            payload={"subject": subject, "message_id": sent.message_id, "thread_id": sent.thread_id},
        )
        await self.record(action)
        return SentReceipt(action_id=action.id, message_id=sent.message_id, thread_id=sent.thread_id)

    @tool(draft=True)
    async def notify(self, notification: Notification) -> UUID:
        """Send a notification and return the emitted action id."""
        raise NotImplementedError

    @tool
    async def record_action(self, kind: str, target: str, payload: dict[str, JsonValue] | None = None) -> UUID:
        """Record an action this run performed and return the emitted action id.

        A log entry only — it performs no side effect. Use it after completing
        work that has no dedicated action tool so the run's history shows what
        happened: kind is a short category, target names what was acted on, and
        payload carries any details worth keeping.
        """
        action = Action(kind=kind, target=target, payload=payload or {})
        await self.record(action)
        return action.id

    @tool
    async def list_actions(self) -> list[Action]:
        """Return the actions already recorded in this run.

        Check it before repeating an action (e.g. re-sending an email) to keep
        a run idempotent.
        """
        return await self.recorded()
