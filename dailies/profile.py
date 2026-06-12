"""User profile: provenance-carrying personal data and its singleton persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from dailies.documents import TimestampedDocument
from dailies.models import LOCAL_TZ, FrozenModel, Timezone

PROFILE_ID = UUID("00000000-0000-4000-8000-000000000001")

type Confidence = Literal["high", "medium", "low"]


class EmailSource(FrozenModel):
    kind: Literal["email"] = "email"
    message_id: str
    sender: str
    subject: str
    date: datetime


class WebSource(FrozenModel):
    kind: Literal["web"] = "web"
    url: str
    title: str | None = None


class AccountSource(FrozenModel):
    kind: Literal["account"] = "account"
    detail: str


class UserSource(FrozenModel):
    kind: Literal["user"] = "user"


type Source = Annotated[EmailSource | WebSource | AccountSource | UserSource, Field(discriminator="kind")]


class Sourced[T](FrozenModel):
    """A value paired with where it was found and how confident the finding is."""

    value: T
    source: Source
    confidence: Confidence = "high"


class Contact(FrozenModel):
    name: str
    email: Sourced[str] | None = None
    phone: Sourced[str] | None = None
    imessage_handle: Sourced[str] | None = None


class LoyaltyProgram(FrozenModel):
    kind: Literal["airline", "hotel"]
    program: str
    member_number: Sourced[str]
    status_tier: str | None = None


class Merchant(FrozenModel):
    name: str
    category: str
    cadence: str | None = None
    source: Source


class Fact(FrozenModel):
    label: str
    value: str
    source: Source
    confidence: Confidence = "high"


class Profile(FrozenModel):
    """Everything dailies knows about the user, every value carrying its provenance."""

    name: Sourced[str]
    email: Sourced[str]
    timezone: Sourced[Timezone] = Field(
        default_factory=lambda: Sourced[Timezone](
            value=LOCAL_TZ, source=AccountSource(detail="your machine's local timezone")
        )
    )
    phone: Sourced[str] | None = None
    imessage_handle: Sourced[str] | None = None
    home_address: Sourced[str] | None = None
    birthday: Sourced[str] | None = None
    employer: Sourced[str] | None = None
    role: Sourced[str] | None = None
    partner: Contact = Field(default_factory=lambda: Contact(name="Rebecca"))
    loyalty_programs: list[LoyaltyProgram] = Field(default_factory=list)
    merchants: list[Merchant] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)


class ProfileNotFound(LookupError):
    """No saved profile; `dly profile init` creates one."""

    def __init__(self) -> None:
        super().__init__("no profile saved — run `dly profile init` first")


class UserProfile(TimestampedDocument):
    """The singleton document holding the user's profile."""

    id: UUID = PROFILE_ID
    profile: Profile

    class Settings:
        name = "profile"


def describe(source: Source) -> str:
    """Render the one-line provenance description shown beside a profile value."""
    match source:
        case EmailSource(sender=sender, subject=subject, date=date):
            return f"found in email from {sender}, {date:%b} {date.day}, {date.year} ({subject!r})"
        case WebSource(url=url):
            return f"found at {url}"
        case AccountSource(detail=detail):
            return f"from {detail}"
        case UserSource():
            return "entered by you"


async def load_profile() -> Profile:
    """Load the saved profile — the one read codepath; raises ProfileNotFound when none is saved."""
    document = await UserProfile.get(PROFILE_ID)
    if document is None:
        raise ProfileNotFound
    return document.profile


async def save_profile(profile: Profile) -> None:
    """Persist the profile onto the singleton document — the one write codepath."""
    match await UserProfile.get(PROFILE_ID):
        case None:
            await UserProfile(profile=profile).insert()
        case document:
            document.profile = profile
            await document.replace()
