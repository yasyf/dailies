"""Profile discovery: one agent run that mines the inbox, then the web, into a Profile."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from dailies.agent import AgentRequest
from dailies.gmail import EmailMessage, truncate
from dailies.interview import InterviewError
from dailies.models import LOCAL_TZ
from dailies.profile import AccountSource, Profile, Sourced
from dailies.tools.base import ToolSet, tool
from dailies.tools.inputs import WebToolSet
from dailies.tools.profile import DraftProfile, log_draft_event

if TYPE_CHECKING:
    from dailies.agent import AgentProvider
    from dailies.gmail import GmailClient
    from dailies.tools.profile import DraftListener
    from dailies.web import WebClient

DISCOVERY_MAX_TURNS = 80

DISCOVERY_SYSTEM = (
    "You are mining the user's Gmail inbox, then the web, to build their personal profile. Be maximally "
    "aggressive about recall — hunt for every field — but never fabricate: the user reviews every value "
    "afterward, so a wrong guess costs more than a blank. Every value carries its provenance: the email's "
    "message_id, sender, subject, and date, or the web page's URL. Set confidence to high when the value is "
    "stated in a primary document (a receipt, an itinerary, a signature block), medium when inferred (an "
    "employer from a mail domain), and low when guessed. When sources disagree, prefer the most recent "
    "occurrence. Leave a field absent when no evidence supports it — except name: always record your best "
    "inference via update_profile_field, at low confidence if need be. Never use the user source kind — it is "
    "reserved for values the user types at review; defaults you keep (like the machine timezone) carry an "
    "account source. "
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

DISCOVERY_RECORDING = (
    "Record every value the instant you confirm it, then keep mining — there is no final submit and nothing to "
    "assemble at the end. Use update_profile_field for each scalar (name, phone, imessage_handle, home_address, "
    "birthday, employer, role), record_fact for any other durable fact, record_loyalty_program for each airline "
    "or hotel membership, and record_merchant for each recurring merchant. The email field is already filled "
    "from the connected account — never record it again. Stop once your leads are exhausted."
)


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


def seed_profile(email: str) -> Profile:
    """Seed a Profile from the connected account: email at high confidence, name as a low-confidence local-part."""
    source = AccountSource(detail="the connected gmail account")
    return Profile(
        email=Sourced[str](value=email, source=source),
        name=Sourced[str](value=email.split("@", 1)[0], source=source, confidence="low"),
    )


async def discover_profile(
    provider: AgentProvider, *, gmail: GmailClient, web: WebClient, listener: DraftListener = log_draft_event
) -> Profile:
    """Mine the inbox, then the web, into a Profile through one agent run.

    Seeds the profile from the connected account (email at high confidence, name as a
    low-confidence local-part placeholder), then records each discovered value onto a
    draft as the agent finds it, notifying ``listener`` on every effective merge, and
    returns the accumulated draft.

    Raises NotConnected when gmail has no stored connection and InterviewError when the
    run fails before anything is recorded.
    """
    account = await gmail.profile()
    seed = seed_profile(account.email)
    draft = DraftProfile(seed, listener)
    result = await provider.run(
        AgentRequest(
            system=f"{DISCOVERY_SYSTEM}\n\n{DISCOVERY_RECORDING}",
            prompt=discovery_prompt(account.email),
            tools=tuple(
                t.to_spec() for ts in (MiningToolSet(gmail), WebToolSet(web), draft) for t in ts.get_tools()
            ),
        )
    )
    if not result.ok and draft.draft is seed:
        raise InterviewError(result.text)
    return draft.draft
