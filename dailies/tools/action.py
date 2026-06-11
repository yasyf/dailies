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


class Notification(FrozenModel):
    channel: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class ActionToolSet(ToolSet):
    integrations: ClassVar[tuple[str, ...]] = ("gmail",)

    context: RunContext
    gmail: GmailClient
    record: ActionRecorder

    @tool
    async def send_email(self, to: str, subject: str, body: str) -> UUID:
        """Send an email and return the emitted action id."""
        sent = await self.gmail.send(to=to, subject=subject, body=body)
        action = Action(
            kind="email",
            target=to,
            payload={"subject": subject, "message_id": sent.message_id, "thread_id": sent.thread_id},
        )
        await self.record(action)
        return action.id

    @tool
    async def notify(self, notification: Notification) -> UUID:
        """Send a notification and return the emitted action id."""
        raise NotImplementedError

    @tool
    async def record_action(self, kind: str, payload: dict[str, JsonValue]) -> UUID:
        """Record an action of the given kind and return its id."""
        raise NotImplementedError

    @tool
    async def list_actions(self) -> list[UUID]:
        """Return the ids of actions emitted in the current run."""
        raise NotImplementedError
