"""User profile: provenance-carrying personal data and its singleton persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import Field

from dailies.documents import TimestampedDocument
from dailies.gmail import EmailMessage, truncate
from dailies.models import LOCAL_TZ, FrozenModel, Timezone

if TYPE_CHECKING:
    from dailies.agent import AgentProvider
    from dailies.gmail import GmailClient
    from dailies.web import WebClient

PROFILE_ID = UUID("00000000-0000-4000-8000-000000000001")

DISCOVERY_SYSTEM = (
    "You are mining the user's Gmail inbox, then the web, to build their personal profile. Be maximally "
    "aggressive about recall — hunt for every field — but never fabricate: the user reviews every value "
    "afterward, so a wrong guess costs more than a blank. Every value carries its provenance: the email's "
    "message_id, sender, subject, and date, or the web page's URL. Set confidence to high when the value is "
    "stated in a primary document (a receipt, an itinerary, a signature block), medium when inferred (an "
    "employer from a mail domain), and low when guessed. When sources disagree, prefer the most recent "
    "occurrence. Leave a field absent when no evidence supports it — except name: always submit your best "
    "inference, at low confidence if need be. "
    "search_emails truncates bodies and returns at most 20 matches, so ALWAYS call get_message for the full "
    "body before extracting an address, a member number, or a signature — those details live in the footers "
    "truncation cuts off. Keep queries narrow and recent with operators like newer_than:1y. "
    "Hunt field by field. Name, role, employer, and phone hide in in:sent mail — From display names and "
    'signature blocks. Home address: from:doordash.com OR from:ubereats.com "delivered to", from:amazon.com '
    '"shipped to", and utility or ISP statements; cross-check two independent sources before claiming high '
    "confidence. Phone: 2FA and verification mail, and contact numbers on orders. Airline loyalty: "
    "from:united.com OR from:delta.com OR from:aa.com OR from:alaskaair.com plus itinerary subjects — member "
    'numbers live in full bodies. Hotel loyalty: from:marriott.com (Bonvoy OR "member number") and the '
    "Hilton, Hyatt, and IHG analogues. Merchants: recurring receipt senders, with cadence read off the "
    'result dates. Birthday: subject:"happy birthday" date clustering and birthday-reward promos. Partner: '
    "Rebecca — the most frequent personal correspondent (to:rebecca, in:sent rebecca); pull her email, "
    "phone, and iMessage handle from signatures. Timezone: infer it from the home address, else keep the "
    "default. Everything else worth knowing — office, socials, gym, vet, car, household — goes into facts. "
    'Finish with web enrichment seeded by what you found: search_web "<name> <employer>", look for '
    "LinkedIn, GitHub, and a personal site, and fetch_url to confirm before recording anything. Mail "
    "outranks the web for contact details."
)

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


# Imported after Profile as a cycle workaround: dailies.tools.__init__ imports tools.profile,
# which imports Profile back from this module, so the package import must run once Profile exists.
from dailies.tools.base import StructuredSink, ToolSet, tool  # noqa: E402
from dailies.tools.inputs import WebToolSet  # noqa: E402


@dataclass(frozen=True, slots=True)
class MiningToolSet(ToolSet):
    """Read-only Gmail facade for profile discovery: search and read mail, never subscribe."""

    gmail: GmailClient

    @tool
    async def search_emails(self, query: str) -> list[EmailMessage]:
        """Search the mailbox with Gmail query syntax; returns at most 20 matches, bodies truncated."""
        return [truncate(message) for message in await self.gmail.search(query)]

    @tool
    async def get_message(self, message_id: str) -> EmailMessage:
        """Return a single email message by id with its full body."""
        return await self.gmail.message(message_id)

    @tool
    async def get_thread(self, thread_id: str) -> list[EmailMessage]:
        """Return all messages in an email thread, bodies truncated; fetch a full body with get_message."""
        return [truncate(message) for message in await self.gmail.thread(thread_id)]


def discovery_prompt(account_email: str) -> str:
    """Render the discovery run's opening prompt, seeding the facts the account already proves."""
    return (
        f"Build the profile of the owner of {account_email}. Record that address as the email field with an "
        f"account source at high confidence. Their machine's timezone is {LOCAL_TZ} and today is "
        f"{datetime.now():%A, %B %d, %Y}. Their partner is named Rebecca."
    )


async def discover_profile(provider: AgentProvider, *, gmail: GmailClient, web: WebClient) -> Profile:
    """Mine the inbox, then the web, into a Profile through one agent run.

    Raises NotConnected when gmail has no stored connection and InterviewError
    when the agent finishes without submitting a profile.
    """
    from dailies.interview import collect

    account = await gmail.profile()
    return await collect(
        provider,
        StructuredSink(Profile),
        system=DISCOVERY_SYSTEM,
        prompt=discovery_prompt(account.email),
        toolsets=(MiningToolSet(gmail), WebToolSet(web)),
    )


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
