from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pydantic import JsonValue

from dailies.models import FrozenModel
from dailies.runtime import RunContext
from dailies.tools.base import ToolSet, tool


class Notification(FrozenModel):
    channel: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class ActionToolSet(ToolSet):
    context: RunContext

    @tool
    async def send_email(self, to: str, subject: str, body: str) -> UUID:
        """Send an email and return the emitted action id."""
        raise NotImplementedError

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
