"""Per-task spend policy: the single authorization codepath every spending tool routes through."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dailies.documents import Run, Task
from dailies.models import SpendPolicy, TaskId, Timezone


class SpendDenied(Exception):
    """The spend policy (or its absence) refuses an amount.

    Not retryable: route the request through the email approval gate —
    send_email the request, subscribe_to_thread, record the pending decision
    in state, and end the run.
    """


def week_start(now: datetime, *, timezone: Timezone) -> datetime:
    """Return midnight of the most recent Monday in the given IANA zone, as a UTC datetime."""
    local = now.astimezone(zone := ZoneInfo(timezone))
    return datetime.combine(local.date() - timedelta(days=local.weekday()), time(), tzinfo=zone).astimezone(UTC)


async def weekly_spent_cents(task_id: TaskId, *, since: datetime) -> int:
    """Sum the cents already spent across the task's runs since the window start."""
    return sum(
        int(action.payload["amount_cents"])
        for run in await Run.find(Run.task_id == task_id).to_list()
        for action in run.actions
        if action.kind == "spend" and action.created_at >= since
    )


async def authorize(task: Task, *, amount_cents: int, now: datetime, timezone: Timezone) -> int:
    """Authorize an amount against the task's spend policy and return the new weekly total.

    Raises:
        SpendDenied: when the task has no policy, the amount exceeds the
            per-order cap, or it would push the week over the weekly cap.
    """
    match task.spend_policy:
        case None:
            raise SpendDenied(
                "task has no spend policy — activate with --per-order-cap/--weekly-cap to allow spending"
            )
        case SpendPolicy(per_order_cents=cap) if amount_cents > cap:
            raise SpendDenied(f"{amount_cents}¢ exceeds the per-order cap of {cap}¢")
        case SpendPolicy(weekly_cents=cap):
            spent = await weekly_spent_cents(task.uid, since=week_start(now, timezone=timezone))
            if spent + amount_cents > cap:
                raise SpendDenied(
                    f"{spent}¢ already spent this week — {amount_cents}¢ more breaks the weekly cap of {cap}¢"
                )
            return spent + amount_cents
