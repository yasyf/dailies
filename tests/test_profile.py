from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dailies.models import LOCAL_TZ, Timezone
from dailies.profile import (
    AccountSource,
    Contact,
    EmailSource,
    Profile,
    Source,
    Sourced,
    UserSource,
    WebSource,
    describe,
)

pytestmark = pytest.mark.unit


def sourced(value: str) -> Sourced[str]:
    return Sourced[str](value=value, source=UserSource())


def profile() -> Profile:
    return Profile(name=sourced("Yasyf"), email=sourced("yasyf@example.com"))


def test_profile_defaults() -> None:
    built = profile()
    assert built.timezone == Sourced[Timezone](
        value=LOCAL_TZ, source=AccountSource(detail="your machine's local timezone")
    )
    assert built.partner == Contact(name="Rebecca")
    assert (built.loyalty_programs, built.merchants, built.facts) == ([], [], [])
    assert (built.phone, built.home_address, built.birthday, built.employer, built.role) == (None,) * 5


def test_sourced_defaults_high_confidence() -> None:
    assert sourced("x").confidence == "high"


def test_profile_rejects_unknown_timezone() -> None:
    with pytest.raises(ValidationError, match="unknown IANA timezone"):
        Profile.model_validate(
            {
                "name": {"value": "Yasyf", "source": {"kind": "user"}},
                "email": {"value": "yasyf@example.com", "source": {"kind": "user"}},
                "timezone": {"value": "Mars/Olympus_Mons", "source": {"kind": "account", "detail": "guess"}},
            }
        )


def test_sourced_rejects_unknown_source_kind() -> None:
    with pytest.raises(ValidationError, match="kind"):
        Sourced[str].model_validate({"value": "x", "source": {"kind": "carrier-pigeon"}})


def test_sourced_rejects_unknown_confidence() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        Sourced[str].model_validate({"value": "x", "source": {"kind": "user"}, "confidence": "certain"})


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            EmailSource(
                message_id="m1",
                sender="receipts@doordash.com",
                subject="Your DoorDash receipt",
                date=datetime(2026, 5, 12, 18, 30, tzinfo=UTC),
            ),
            "found in email from receipts@doordash.com, May 12, 2026 ('Your DoorDash receipt')",
            id="email",
        ),
        pytest.param(
            WebSource(url="https://example.com/about", title="About"),
            "found at https://example.com/about",
            id="web",
        ),
        pytest.param(AccountSource(detail="your gmail account"), "from your gmail account", id="account"),
        pytest.param(UserSource(), "entered by you", id="user"),
    ],
)
def test_describe_renders_provenance(source: Source, expected: str) -> None:
    assert describe(source) == expected
