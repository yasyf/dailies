from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal
from uuid import UUID

import httpx
from pydantic import JsonValue

from dailies.bluebubbles import MessageSendFailed
from dailies.models import Action, FrozenModel
from dailies.runtime import RunContext
from dailies.tools.base import ToolError, ToolSet, tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from dailies.bluebubbles import IMessageClient
    from dailies.gmail import GmailClient

type ActionRecorder = Callable[[Action], Awaitable[None]]
type ActionReader = Callable[[], Awaitable[list[Action]]]


class Notification(FrozenModel):
    channel: Literal["imessage", "email"]
    to: str
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
    imessage: IMessageClient
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

    @tool
    async def notify(self, notification: Notification) -> UUID:
        """Send a one-way notification over iMessage or email and return the emitted action id; pick the channel by urgency — imessage interrupts, email waits; if iMessage delivery fails, retry the same notification with channel='email'."""  # noqa: E501
        match notification.channel:
            case "imessage":
                try:
                    await self.imessage.send(to=notification.to, text=f"{notification.title}\n{notification.body}")
                except (MessageSendFailed, httpx.HTTPError) as exc:
                    raise ToolError("notify_failed", str(exc), fix="retry with channel='email'") from exc
            case "email":
                await self.gmail.send(to=notification.to, subject=notification.title, body=notification.body)
        action = Action(
            kind="notification",
            target=notification.to,
            payload={"channel": notification.channel, "title": notification.title, "body": notification.body},
        )
        await self.record(action)
        return action.id

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
