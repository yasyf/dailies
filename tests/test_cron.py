from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dailies.engine import cron_due
from dailies.models import CronExpr

pytestmark = pytest.mark.unit

NINE_AM = CronExpr("0 9 * * *")
BASE = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("since", "now", "due"),
    [
        pytest.param(BASE - timedelta(minutes=1), BASE, True, id="slot-at-now"),
        pytest.param(BASE, BASE, False, id="slot-at-since-excluded"),
        pytest.param(BASE - timedelta(hours=1), BASE - timedelta(seconds=1), False, id="just-before"),
        pytest.param(BASE - timedelta(hours=1), BASE + timedelta(seconds=1), True, id="just-after"),
        pytest.param(BASE - timedelta(days=2), BASE, True, id="multi-slot-fires-once"),
    ],
)
def test_cron_due_boundaries(since: datetime, now: datetime, due: bool) -> None:
    assert cron_due(NINE_AM, now=now, since=since) is due


def test_cron_due_is_half_open_across_adjacent_sweeps() -> None:
    # A sweep at the slot, then a later sweep seeded from the first run's time, must not refire.
    first = cron_due(NINE_AM, now=BASE, since=BASE - timedelta(minutes=1))
    second = cron_due(NINE_AM, now=BASE + timedelta(minutes=5), since=BASE)
    assert first is True
    assert second is False
