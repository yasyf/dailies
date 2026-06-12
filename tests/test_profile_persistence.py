from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pymongo import AsyncMongoClient

from dailies.profile import (
    PROFILE_ID,
    Contact,
    EmailSource,
    Fact,
    LoyaltyProgram,
    Merchant,
    Profile,
    ProfileNotFound,
    Sourced,
    UserProfile,
    UserSource,
    WebSource,
    load_profile,
    save_profile,
)
from dailies.tools.base import ToolError, ToolSpec
from dailies.tools.profile import ProfileToolSet

pytestmark = pytest.mark.integration


def sourced(value: str) -> Sourced[str]:
    return Sourced[str](value=value, source=UserSource())


def profile(name: str = "Yasyf") -> Profile:
    return Profile(name=sourced(name), email=sourced("yasyf@example.com"))


def get_profile_spec() -> ToolSpec:
    return next(t.to_spec() for t in ProfileToolSet().get_tools() if t.name == "get_profile")


async def test_save_twice_keeps_one_document_at_fixed_id(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    await save_profile(profile("Yasyf"))
    await save_profile(profile("Yasyf Mohamedali"))
    assert await UserProfile.count() == 1
    document = await UserProfile.get(PROFILE_ID)
    assert document is not None
    assert document.profile.name == sourced("Yasyf Mohamedali")


async def test_sourced_values_round_trip(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    saved = Profile(
        name=Sourced[str](
            value="Yasyf",
            source=EmailSource(
                message_id="m1",
                sender="me@example.com",
                subject="signature",
                date=datetime(2026, 5, 12, 18, 30, tzinfo=UTC),
            ),
            confidence="medium",
        ),
        email=sourced("yasyf@example.com"),
        partner=Contact(name="Rebecca", email=sourced("rebecca@example.com")),
        loyalty_programs=[LoyaltyProgram(kind="airline", program="MileagePlus", member_number=sourced("UA12345"))],
        merchants=[Merchant(name="DoorDash", category="food delivery", cadence="weekly", source=UserSource())],
        facts=[Fact(label="gym", value="Equinox", source=WebSource(url="https://example.com"), confidence="low")],
    )
    await save_profile(saved)
    assert await load_profile() == saved


async def test_load_profile_on_empty_db_names_profile_init(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    with pytest.raises(ProfileNotFound, match="run `dly profile init` first"):
        await load_profile()


async def test_get_profile_returns_saved_profile(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    await save_profile(profile())
    assert await get_profile_spec().invoke({}) == profile()


async def test_get_profile_without_profile_raises_tool_error(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    with pytest.raises(ToolError) as excinfo:
        await get_profile_spec().invoke({})
    assert excinfo.value.error_type == "profile_missing"
    assert "dly profile init" in excinfo.value.detail
    assert excinfo.value.fix == "tell the user to run `dly profile init`"
