from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from dailies.models import (
    LOCAL_TZ,
    Action,
    CronExpr,
    CronTrigger,
    EventTrigger,
    Firing,
    ManualTrigger,
    StatusUpdate,
    StopCondition,
    Trigger,
)

pytestmark = pytest.mark.unit

trigger_adapter: TypeAdapter[Trigger] = TypeAdapter(Trigger)
stop_adapter: TypeAdapter[StopCondition] = TypeAdapter(StopCondition)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"kind": "cron", "cron_expression": "*/5 * * * *", "timezone": "UTC"}, CronTrigger, id="cron"),
        pytest.param(
            {"kind": "cron", "cron_expression": "0 9 * * *", "timezone": "America/New_York"}, CronTrigger, id="cron-tz"
        ),
        pytest.param({"kind": "event", "source": "gmail", "event": "query", "key": "abc"}, EventTrigger, id="event"),
        pytest.param({"kind": "manual"}, ManualTrigger, id="manual"),
    ],
)
def test_trigger_discriminated_roundtrip(payload: dict[str, str], expected: type) -> None:
    parsed = trigger_adapter.validate_python(payload)
    assert isinstance(parsed, expected)
    assert trigger_adapter.dump_python(parsed) == payload


def test_firing_defaults_to_no_occurrences() -> None:
    assert Firing(trigger=ManualTrigger()).occurrence_ids == []


def test_firing_roundtrips_trigger_union() -> None:
    firing = Firing(trigger=EventTrigger(source="gmail", event="thread", key="t1"), occurrence_ids=["m1"])
    assert Firing.model_validate(firing.model_dump()) == firing


def test_cron_trigger_timezone_defaults_local() -> None:
    assert CronTrigger(cron_expression=CronExpr("0 9 * * *")).timezone == LOCAL_TZ


def test_unknown_trigger_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        trigger_adapter.validate_python({"kind": "nope"})


def test_frozen_model_rejects_extra() -> None:
    with pytest.raises(ValidationError):
        CronTrigger(cron_expression="* * * * *", bogus=1)  # type: ignore[call-arg]


def test_frozen_model_is_immutable() -> None:
    trigger = ManualTrigger()
    with pytest.raises(ValidationError):
        trigger.kind = "cron"  # type: ignore[misc]


def test_stored_model_ignores_extra() -> None:
    update = StatusUpdate.model_validate({"title": "t", "surprise": 1})
    assert update.title == "t"
    assert not hasattr(update, "surprise")


def test_default_factory_ids_and_timestamps() -> None:
    a = StatusUpdate(title="a")
    b = StatusUpdate(title="b")
    assert a.id != b.id
    assert a.created_at.tzinfo is not None


def test_action_accepts_json_payload() -> None:
    action = Action(kind="notify", target="slack", payload={"n": 3, "items": ["a", "b"], "ok": True})
    assert action.payload["items"] == ["a", "b"]


def test_stop_condition_datetime_survives_bson_path() -> None:
    dt = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
    assert isinstance(stop_adapter.validate_python(dt), datetime)


def test_stop_condition_datetime_survives_json_boundary() -> None:
    dt = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
    restored = stop_adapter.validate_json(stop_adapter.dump_json(dt))
    assert isinstance(restored, datetime)
    assert restored == dt


def test_stop_condition_prompt_stays_str() -> None:
    assert isinstance(stop_adapter.validate_python("when complete"), str)
