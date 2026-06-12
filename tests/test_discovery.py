from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dailies.gmail import MAX_BODY, EmailMessage
from dailies.models import LOCAL_TZ
from dailies.profile import (
    DISCOVERY_SYSTEM,
    AccountSource,
    EmailSource,
    LoyaltyProgram,
    MiningToolSet,
    Profile,
    Sourced,
    discover_profile,
    discovery_prompt,
)
from dailies.tools import TOOLSETS
from tests.fakes import FakeGmail, FakeWeb, ToolDrivingProvider

pytestmark = pytest.mark.unit

ADDRESS = "123 Mission St, San Francisco, CA 94105"

SIGNATURE = EmailMessage(
    id="sig-1",
    thread_id="t-sig",
    sender="Yasyf Mohamedali <fake@example.com>",
    to="rebecca@example.com",
    subject="Re: dinner plans",
    body="See you at 7!\n\n--\nYasyf Mohamedali\nVP of Engineering, Initech\n+1 (415) 555-0100",
    date=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
)

DOORDASH = EmailMessage(
    id="dd-1",
    thread_id="t-dd",
    sender="DoorDash <no-reply@doordash.com>",
    to="fake@example.com",
    subject="Your DoorDash receipt",
    body="Thanks for your order!\n" + "menu item details " * 250 + f"\nDelivered to: {ADDRESS}\n",
    date=datetime(2026, 5, 12, 18, 30, tzinfo=UTC),
)

UNITED = EmailMessage(
    id="ua-1",
    thread_id="t-ua",
    sender="United Airlines <mileageplus@united.com>",
    to="fake@example.com",
    subject="Your MileagePlus statement",
    body="MileagePlus member number: UA12345\nCurrent balance: 88,000 miles",
    date=datetime(2026, 4, 2, 8, 0, tzinfo=UTC),
)


def email_source(message: EmailMessage) -> EmailSource:
    return EmailSource(message_id=message.id, sender=message.sender, subject=message.subject, date=message.date)


def sourced_json(value: str, message: EmailMessage) -> dict[str, object]:
    return {
        "value": value,
        "source": {
            "kind": "email",
            "message_id": message.id,
            "sender": message.sender,
            "subject": message.subject,
            "date": message.date.isoformat(),
        },
        "confidence": "high",
    }


SUBMISSION = {
    "value": {
        "name": sourced_json("Yasyf Mohamedali", SIGNATURE),
        "email": {"value": "fake@example.com", "source": {"kind": "account", "detail": "the connected gmail account"}},
        "phone": sourced_json("+1 (415) 555-0100", SIGNATURE),
        "employer": sourced_json("Initech", SIGNATURE),
        "role": sourced_json("VP of Engineering", SIGNATURE),
        "home_address": sourced_json(ADDRESS, DOORDASH),
        "loyalty_programs": [
            {"kind": "airline", "program": "MileagePlus", "member_number": sourced_json("UA12345", UNITED)}
        ],
    }
}

EXPECTED = Profile(
    name=Sourced[str](value="Yasyf Mohamedali", source=email_source(SIGNATURE)),
    email=Sourced[str](value="fake@example.com", source=AccountSource(detail="the connected gmail account")),
    phone=Sourced[str](value="+1 (415) 555-0100", source=email_source(SIGNATURE)),
    employer=Sourced[str](value="Initech", source=email_source(SIGNATURE)),
    role=Sourced[str](value="VP of Engineering", source=email_source(SIGNATURE)),
    home_address=Sourced[str](value=ADDRESS, source=email_source(DOORDASH)),
    loyalty_programs=[
        LoyaltyProgram(
            kind="airline",
            program="MileagePlus",
            member_number=Sourced[str](value="UA12345", source=email_source(UNITED)),
        )
    ],
)

SCRIPT: list[tuple[str, dict[str, object]]] = [
    ("search_emails", {"query": "fake@example.com"}),
    ("search_emails", {"query": "doordash"}),
    ("get_message", {"message_id": "dd-1"}),
    ("search_emails", {"query": "united"}),
    ("get_message", {"message_id": "ua-1"}),
    ("submit", SUBMISSION),
]


def seeded_gmail() -> FakeGmail:
    gmail = FakeGmail()
    for message in (SIGNATURE, DOORDASH, UNITED):
        gmail.add(message)
    return gmail


async def test_discovery_reads_full_bodies_past_truncation_and_returns_exact_profile() -> None:
    provider = ToolDrivingProvider(list(SCRIPT))
    result = await discover_profile(provider, gmail=seeded_gmail(), web=FakeWeb())
    (truncated_receipt,) = provider.outputs[1]
    assert truncated_receipt.truncated is True
    assert len(truncated_receipt.body) == MAX_BODY
    assert ADDRESS not in truncated_receipt.body
    full_receipt = provider.outputs[2]
    assert full_receipt.truncated is False
    assert ADDRESS in full_receipt.body
    assert result == EXPECTED


async def test_discovery_request_exposes_exactly_mining_web_and_submit_tools() -> None:
    provider = ToolDrivingProvider([("submit", SUBMISSION)])
    await discover_profile(provider, gmail=seeded_gmail(), web=FakeWeb())
    (request,) = provider.requests
    assert sorted(spec.name for spec in request.tools) == [
        "fetch_url",
        "get_message",
        "get_thread",
        "scrape",
        "search_emails",
        "search_web",
        "submit",
    ]
    assert "in:sent" in request.system
    assert '"delivered to"' in request.system
    assert DISCOVERY_SYSTEM in request.system


def test_discovery_prompt_injects_account_timezone_date_and_partner() -> None:
    prompt = discovery_prompt("fake@example.com")
    assert "fake@example.com" in prompt
    assert LOCAL_TZ in prompt
    assert f"{datetime.now():%Y}" in prompt
    assert "partner is named Rebecca" in prompt


def test_mining_toolset_stays_out_of_workflow_toolsets() -> None:
    assert MiningToolSet not in TOOLSETS
