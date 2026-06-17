from __future__ import annotations

import pytest

from dailies.discovery import discover_profile, seed_profile
from dailies.interview import InterviewError
from dailies.profile import AccountSource, Profile, Sourced
from dailies.tools.profile import (
    DraftEvent,
    DraftProfile,
    FieldRecorded,
    ProfileToolSet,
)
from tests.factories import sourced
from tests.fakes import FakeGmail, FakeWeb, ToolDrivingProvider

pytestmark = pytest.mark.unit

ACCOUNT = AccountSource(detail="the connected gmail account")


def patch_store(monkeypatch: pytest.MonkeyPatch, profile: Profile) -> dict[str, Profile]:
    holder = {"profile": profile}

    async def fake_load() -> Profile:
        return holder["profile"]

    async def fake_save(updated: Profile) -> None:
        holder["profile"] = updated

    monkeypatch.setattr("dailies.tools.profile.load_profile", fake_load)
    monkeypatch.setattr("dailies.tools.profile.save_profile", fake_save)
    return holder


def test_seed_profile_seeds_email_high_and_name_low() -> None:
    seed = seed_profile("fake@example.com")
    assert seed.email == Sourced[str](value="fake@example.com", source=ACCOUNT)
    assert seed.email.confidence == "high"
    assert seed.name == Sourced[str](value="fake", source=ACCOUNT, confidence="low")


async def test_draft_fires_one_event_per_effective_merge_then_ignores_lower_confidence() -> None:
    events: list[DraftEvent] = []
    draft = DraftProfile(seed_profile("fake@example.com"), events.append)
    seed = draft.draft
    source = AccountSource(detail="a receipt")

    await draft.update_profile_field("phone", "+1 415 555 0100", source, "high")
    assert len(events) == 1
    match events[0]:
        case FieldRecorded(field="phone", value=value, profile=snapshot):
            assert value.value == "+1 415 555 0100"
            assert snapshot.phone == value
            assert snapshot is draft.draft
        case other:
            raise AssertionError(other)
    assert draft.draft is not seed

    effective = draft.draft
    await draft.update_profile_field("phone", "+1 000 000 0000", source, "low")
    assert len(events) == 1
    assert draft.draft is effective
    assert draft.draft.phone is not None
    assert draft.draft.phone.value == "+1 415 555 0100"


async def test_draft_loyalty_upserts_structurally() -> None:
    events: list[DraftEvent] = []
    draft = DraftProfile(seed_profile("fake@example.com"), events.append)
    source = AccountSource(detail="MileagePlus statement")

    await draft.record_loyalty_program("airline", "MileagePlus", "UA12345", source)
    await draft.record_loyalty_program("airline", "MileagePlus", "UA12345", source, status_tier="1K")

    assert [p.program for p in draft.draft.loyalty_programs] == ["MileagePlus"]
    assert draft.draft.loyalty_programs[0].status_tier == "1K"
    assert [type(event).__name__ for event in events] == ["LoyaltyRecorded", "LoyaltyRecorded"]


async def test_draft_merchant_upserts_and_skips_identical() -> None:
    events: list[DraftEvent] = []
    draft = DraftProfile(seed_profile("fake@example.com"), events.append)
    source = AccountSource(detail="receipts")

    await draft.record_merchant("DoorDash", "food delivery", source, "weekly")
    await draft.record_merchant("DoorDash", "food delivery", source, "weekly")
    assert [m.name for m in draft.draft.merchants] == ["DoorDash"]
    assert len(events) == 1

    await draft.record_merchant("DoorDash", "groceries", source, "weekly")
    assert [m.name for m in draft.draft.merchants] == ["DoorDash"]
    assert draft.draft.merchants[0].category == "groceries"
    assert len(events) == 2


async def test_profile_toolset_loyalty_upserts(monkeypatch: pytest.MonkeyPatch) -> None:
    holder = patch_store(monkeypatch, Profile(name=sourced("Yasyf"), email=sourced("y@example.com")))
    toolset = ProfileToolSet()
    source = AccountSource(detail="MileagePlus statement")

    await toolset.record_loyalty_program("airline", "MileagePlus", "UA12345", source)
    await toolset.record_loyalty_program("airline", "MileagePlus", "UA12345", source, status_tier="1K")

    programs = holder["profile"].loyalty_programs
    assert [p.program for p in programs] == ["MileagePlus"]
    assert programs[0].status_tier == "1K"


async def test_profile_toolset_merchant_upserts(monkeypatch: pytest.MonkeyPatch) -> None:
    holder = patch_store(monkeypatch, Profile(name=sourced("Yasyf"), email=sourced("y@example.com")))
    toolset = ProfileToolSet()
    source = AccountSource(detail="receipts")

    await toolset.record_merchant("DoorDash", "food delivery", source, "weekly")
    await toolset.record_merchant("DoorDash", "groceries", source, "weekly")

    merchants = holder["profile"].merchants
    assert [m.name for m in merchants] == ["DoorDash"]
    assert merchants[0].category == "groceries"


async def test_discover_profile_returns_seed_on_empty_run() -> None:
    result = await discover_profile(ToolDrivingProvider([]), gmail=FakeGmail(), web=FakeWeb())
    assert result == seed_profile("fake@example.com")


async def test_discover_profile_raises_when_failed_and_nothing_recorded() -> None:
    with pytest.raises(InterviewError):
        await discover_profile(ToolDrivingProvider([], ok=False), gmail=FakeGmail(), web=FakeWeb())


async def test_discover_profile_returns_partial_when_failed_but_something_recorded() -> None:
    script: list[tuple[str, dict[str, object]]] = [
        (
            "update_profile_field",
            {
                "field": "phone",
                "value": "+1 415 555 0100",
                "source": {"kind": "account", "detail": "a receipt"},
                "confidence": "high",
            },
        )
    ]
    result = await discover_profile(ToolDrivingProvider(script, ok=False), gmail=FakeGmail(), web=FakeWeb())
    assert result.phone is not None
    assert result.phone.value == "+1 415 555 0100"
    assert result != seed_profile("fake@example.com")


def test_draft_tools_mirror_profile_toolset_specs() -> None:
    draft = DraftProfile(seed_profile("fake@example.com"), lambda event: None)
    runtime = {tool.name: tool.to_spec() for tool in ProfileToolSet().get_tools()}
    draft_specs = {tool.name: tool.to_spec() for tool in draft.get_tools()}
    assert set(draft_specs) == {"update_profile_field", "record_fact", "record_loyalty_program", "record_merchant"}
    assert set(draft_specs) < set(runtime)
    for name, spec in draft_specs.items():
        mirror = runtime[name]
        assert (spec.name, spec.description, spec.input_schema) == (
            mirror.name,
            mirror.description,
            mirror.input_schema,
        )
