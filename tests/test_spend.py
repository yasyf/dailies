from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dailies.documents import Task
from dailies.models import PromptStr, SpendPolicy, TaskDefinition, TaskId, Timezone
from dailies.spend import SpendDenied, authorize, week_start

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 12, 15, 30, tzinfo=UTC)


def make_task(policy: SpendPolicy | None) -> Task:
    return Task.model_construct(
        name="errands",
        definition=TaskDefinition(user_input="i", description="d", prompt=PromptStr("p")),
        spend_policy=policy,
    )


@pytest.mark.parametrize(
    ("now", "timezone", "expected"),
    [
        pytest.param(
            datetime(2026, 6, 12, 15, 30, tzinfo=UTC),
            "UTC",
            datetime(2026, 6, 8, tzinfo=UTC),
            id="utc-midweek",
        ),
        pytest.param(
            datetime(2026, 6, 8, tzinfo=UTC),
            "UTC",
            datetime(2026, 6, 8, tzinfo=UTC),
            id="utc-exactly-monday-midnight",
        ),
        pytest.param(
            datetime(2026, 6, 8, 3, 59, tzinfo=UTC),
            "America/New_York",
            datetime(2026, 6, 1, 4, 0, tzinfo=UTC),
            id="new-york-still-sunday-locally",
        ),
        pytest.param(
            datetime(2026, 6, 7, 13, 0, tzinfo=UTC),
            "Pacific/Auckland",
            datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
            id="auckland-already-monday-locally",
        ),
        pytest.param(
            datetime(2026, 3, 9, 3, 0, tzinfo=UTC),
            "America/New_York",
            datetime(2026, 3, 2, 5, 0, tzinfo=UTC),
            id="new-york-week-spans-spring-forward",
        ),
        pytest.param(
            datetime(2026, 10, 25, 12, 0, tzinfo=UTC),
            "Europe/London",
            datetime(2026, 10, 18, 23, 0, tzinfo=UTC),
            id="london-week-spans-fall-back",
        ),
    ],
)
def test_week_start_returns_most_recent_local_monday_midnight_in_utc(
    now: datetime, timezone: Timezone, expected: datetime
) -> None:
    assert week_start(now, timezone=timezone) == expected


async def test_authorize_without_policy_denies_naming_activation_flags() -> None:
    with pytest.raises(SpendDenied) as excinfo:
        await authorize(make_task(None), amount_cents=500, now=NOW, timezone="UTC")
    assert str(excinfo.value) == (
        "task has no spend policy — activate with --per-order-cap/--weekly-cap to allow spending"
    )


async def test_authorize_over_per_order_cap_denies_naming_the_cap() -> None:
    task = make_task(SpendPolicy(per_order_cents=1000, weekly_cents=5000))
    with pytest.raises(SpendDenied) as excinfo:
        await authorize(task, amount_cents=1001, now=NOW, timezone="UTC")
    assert str(excinfo.value) == "1001¢ exceeds the per-order cap of 1000¢"


async def test_authorize_over_weekly_cap_denies_naming_spent_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    task = make_task(SpendPolicy(per_order_cents=1000, weekly_cents=2500))
    calls: list[tuple[TaskId, datetime]] = []

    async def spent(task_id: TaskId, *, since: datetime) -> int:
        calls.append((task_id, since))
        return 2000

    monkeypatch.setattr("dailies.spend.weekly_spent_cents", spent)
    with pytest.raises(SpendDenied) as excinfo:
        await authorize(task, amount_cents=600, now=NOW, timezone="America/New_York")
    assert str(excinfo.value) == "2000¢ already spent this week — 600¢ more breaks the weekly cap of 2500¢"
    assert calls == [(task.uid, datetime(2026, 6, 8, 4, 0, tzinfo=UTC))]


async def test_authorize_at_both_caps_returns_new_weekly_total(monkeypatch: pytest.MonkeyPatch) -> None:
    task = make_task(SpendPolicy(per_order_cents=500, weekly_cents=2500))

    async def spent(task_id: TaskId, *, since: datetime) -> int:
        return 2000

    monkeypatch.setattr("dailies.spend.weekly_spent_cents", spent)
    assert await authorize(task, amount_cents=500, now=NOW, timezone="UTC") == 2500
