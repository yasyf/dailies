from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dailies.models import LOCAL_TZ, Timezone
from dailies.profile import (
    AccountSource,
    Confidence,
    Contact,
    EmailSource,
    Fact,
    Profile,
    Source,
    Sourced,
    UserSource,
    WebSource,
    describe,
    merge_fact,
    merge_field,
)

pytestmark = pytest.mark.unit


def sourced(value: str) -> Sourced[str]:
    return Sourced[str](value=value, source=UserSource())


def discovered(value: str, *, confidence: Confidence = "high") -> Sourced[str]:
    return Sourced[str](value=value, source=AccountSource(detail="discovery"), confidence=confidence)


def afact(label: str, value: str, *, confidence: Confidence = "high") -> Fact:
    return Fact(label=label, value=value, source=AccountSource(detail="discovery"), confidence=confidence)


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


@pytest.mark.parametrize(
    ("existing", "incoming", "expected", "changed"),
    [
        pytest.param(
            Sourced[str](value="OldCo", source=UserSource(), confidence="low"),
            discovered("NewCo", confidence="high"),
            "OldCo",
            False,
            id="keeps_user_source",
        ),
        pytest.param(
            discovered("OldCo", confidence="low"),
            discovered("NewCo", confidence="high"),
            "NewCo",
            True,
            id="replaces_lower_confidence_non_user",
        ),
        pytest.param(
            discovered("OldCo", confidence="high"),
            discovered("NewCo", confidence="high"),
            "NewCo",
            True,
            id="incoming_wins_equal_confidence_tie",
        ),
        pytest.param(
            discovered("OldCo", confidence="high"),
            discovered("NewCo", confidence="low"),
            "OldCo",
            False,
            id="no_op_when_existing_higher_confidence",
        ),
        pytest.param(
            None,
            discovered("NewCo", confidence="high"),
            "NewCo",
            True,
            id="sets_when_absent",
        ),
    ],
)
def test_merge_field(existing: Sourced[str] | None, incoming: Sourced[str], expected: str, changed: bool) -> None:
    base = Profile(name=sourced("Yasyf"), email=sourced("yasyf@example.com"), employer=existing)
    merged = merge_field(base, "employer", incoming)
    assert merged.employer is not None
    assert merged.employer.value == expected
    assert (merged is not base) == changed
    assert base.employer == existing


@pytest.mark.parametrize(
    ("base_facts", "incoming", "expected_facts", "changed"),
    [
        pytest.param(
            (afact("a", "1"), afact("b", "2")),
            afact("c", "3"),
            (afact("a", "1"), afact("b", "2"), afact("c", "3")),
            True,
            id="appends_new_label",
        ),
        pytest.param(
            (afact("a", "1"), afact("b", "old", confidence="low"), afact("c", "3")),
            afact("b", "new", confidence="high"),
            (afact("a", "1"), afact("b", "new", confidence="high"), afact("c", "3")),
            True,
            id="replaces_in_place_same_label_lower_confidence",
        ),
        pytest.param(
            (
                afact("a", "1"),
                Fact(label="b", value="old", source=UserSource(), confidence="low"),
                afact("c", "3"),
            ),
            afact("b", "new", confidence="high"),
            (
                afact("a", "1"),
                Fact(label="b", value="old", source=UserSource(), confidence="low"),
                afact("c", "3"),
            ),
            False,
            id="no_op_on_user_source_fact",
        ),
        pytest.param(
            (afact("a", "1"), afact("b", "old", confidence="high"), afact("c", "3")),
            afact("b", "new", confidence="low"),
            (afact("a", "1"), afact("b", "old", confidence="high"), afact("c", "3")),
            False,
            id="no_op_when_existing_higher_confidence",
        ),
    ],
)
def test_merge_fact(
    base_facts: tuple[Fact, ...], incoming: Fact, expected_facts: tuple[Fact, ...], changed: bool
) -> None:
    base = Profile(name=sourced("Yasyf"), email=sourced("yasyf@example.com"), facts=list(base_facts))
    merged = merge_fact(base, incoming)
    assert merged.facts == list(expected_facts)
    assert [fact.label for fact in merged.facts] == [fact.label for fact in expected_facts]
    assert (merged is not base) == changed
    assert base.facts == list(base_facts)
