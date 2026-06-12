from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from dailies.models import (
    LOCAL_TZ,
    Action,
    CronExpr,
    CronTrigger,
    DraftTrigger,
    EventTrigger,
    Firing,
    ManualTrigger,
    StatusUpdate,
    StopCondition,
    TaskDraft,
    TaskProposal,
    Trigger,
    WorkflowDraft,
    WorkflowId,
    WorkflowTrigger,
    WorkflowTriggerDraft,
)

pytestmark = pytest.mark.unit

trigger_adapter: TypeAdapter[Trigger] = TypeAdapter(Trigger)
draft_trigger_adapter: TypeAdapter[DraftTrigger] = TypeAdapter(DraftTrigger)
stop_adapter: TypeAdapter[StopCondition] = TypeAdapter(StopCondition)

CRON = CronTrigger(cron_expression=CronExpr("0 7 * * *"))


def draft(name: str, *triggers: DraftTrigger) -> WorkflowDraft:
    return WorkflowDraft(name=name, summary="s", prompt="p", ddl="CREATE TABLE t (x TEXT)", triggers=list(triggers))


def proposal(*workflows: WorkflowDraft) -> TaskProposal:
    return TaskProposal(
        task=TaskDraft(name="T", description="d", user_input="u", prompt="p"), workflows=list(workflows)
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"kind": "cron", "cron_expression": "*/5 * * * *", "timezone": "UTC"}, CronTrigger, id="cron"),
        pytest.param(
            {"kind": "cron", "cron_expression": "0 9 * * *", "timezone": "America/New_York"}, CronTrigger, id="cron-tz"
        ),
        pytest.param({"kind": "event", "source": "gmail", "event": "query", "key": "abc"}, EventTrigger, id="event"),
        pytest.param({"kind": "manual"}, ManualTrigger, id="manual"),
        pytest.param(
            {"kind": "workflow", "workflow_id": UUID("12345678-1234-5678-1234-567812345678")},
            WorkflowTrigger,
            id="workflow",
        ),
    ],
)
def test_trigger_discriminated_roundtrip(payload: dict[str, object], expected: type) -> None:
    parsed = trigger_adapter.validate_python(payload)
    assert isinstance(parsed, expected)
    assert trigger_adapter.dump_python(parsed) == payload


def test_draft_trigger_union_parses_workflow_by_name() -> None:
    parsed = draft_trigger_adapter.validate_python({"kind": "workflow", "workflow": "tracker-a"})
    assert parsed == WorkflowTriggerDraft(workflow="tracker-a")


def test_proposal_accepts_fan_in() -> None:
    fan_in = proposal(
        draft("tracker-a", CRON),
        draft("tracker-b", CRON),
        draft("decider", WorkflowTriggerDraft(workflow="tracker-a"), WorkflowTriggerDraft(workflow="tracker-b")),
    )
    assert [w.name for w in fan_in.workflows] == ["tracker-a", "tracker-b", "decider"]


def test_proposal_rejects_self_referencing_trigger() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        proposal(draft("a", WorkflowTriggerDraft(workflow="a")))


def test_proposal_rejects_unknown_sibling() -> None:
    with pytest.raises(ValidationError, match="unknown sibling"):
        proposal(draft("a", WorkflowTriggerDraft(workflow="ghost")))


def test_proposal_rejects_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        proposal(draft("a", WorkflowTriggerDraft(workflow="b")), draft("b", WorkflowTriggerDraft(workflow="a")))


def test_proposal_rejects_duplicate_workflow_names() -> None:
    with pytest.raises(ValidationError, match="duplicate workflow names"):
        proposal(draft("a", CRON), draft("a", CRON))


def test_workflow_trigger_roundtrips_in_firing() -> None:
    firing = Firing(
        trigger=WorkflowTrigger(workflow_id=WorkflowId(UUID("12345678-1234-5678-1234-567812345678"))),
        occurrence_ids=["r1"],
    )
    assert Firing.model_validate(firing.model_dump()) == firing


def test_firing_defaults_to_no_occurrences() -> None:
    assert Firing(trigger=ManualTrigger()).occurrence_ids == []


def test_firing_roundtrips_trigger_union() -> None:
    firing = Firing(trigger=EventTrigger(source="gmail", event="thread", key="t1"), occurrence_ids=["m1"])
    assert Firing.model_validate(firing.model_dump()) == firing


def test_cron_trigger_timezone_defaults_local() -> None:
    assert CronTrigger(cron_expression=CronExpr("0 9 * * *")).timezone == LOCAL_TZ


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        pytest.param(
            {"cron_expression": CronExpr("0 9 * * *"), "timezone": "Mars/Olympus_Mons"},
            "unknown IANA timezone",
            id="junk-timezone",
        ),
        pytest.param({"cron_expression": "not a cron"}, "invalid cron expression", id="junk-cron"),
    ],
)
def test_cron_trigger_rejects_junk_on_construction(kwargs: dict[str, str], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        CronTrigger(**kwargs)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param(
            {"kind": "cron", "cron_expression": "0 9 * * *", "timezone": "Not/AZone"},
            "unknown IANA timezone",
            id="junk-timezone",
        ),
        pytest.param(
            {"kind": "cron", "cron_expression": "not a cron", "timezone": "UTC"},
            "invalid cron expression",
            id="junk-cron",
        ),
    ],
)
def test_trigger_union_rejects_junk_cron_payload(payload: dict[str, str], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        trigger_adapter.validate_python(payload)


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
