from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from dailies.documents import Task
from dailies.models import Action, utcnow
from dailies.profile import load_profile
from dailies.runtime import RunContext
from dailies.spend import SpendDenied, authorize
from dailies.tools.base import ToolError, ToolSet, tool

if TYPE_CHECKING:
    from dailies.tools.action import ActionRecorder


@dataclass(frozen=True, slots=True)
class SpendToolSet(ToolSet):
    context: RunContext
    record: ActionRecorder

    @tool
    async def authorize_spend(self, amount_cents: int, merchant: str, reason: str) -> UUID:
        """Authorize spending money against the task's spend policy; call it before any purchase.

        Approval records a spend action counting against the weekly cap;
        spend_denied means the policy (or its absence) blocks the amount — do
        not retry, ask the user via the email approval gate.
        """
        task = await Task.get(self.context.task_id)
        if task is None:
            raise LookupError(self.context.task_id)
        profile = await load_profile()
        try:
            await authorize(task, amount_cents=amount_cents, now=utcnow(), timezone=profile.timezone.value)
        except SpendDenied as exc:
            raise ToolError(
                "spend_denied",
                str(exc),
                fix=(
                    "ask the user via the email approval gate: send_email the request, "
                    "subscribe_to_thread(receipt.thread_id), record the pending decision in state, and end the run"
                ),
            ) from exc
        action = Action(kind="spend", target=merchant, payload={"amount_cents": amount_cents, "reason": reason})
        await self.record(action)
        return action.id
