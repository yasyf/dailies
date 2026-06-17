from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from loguru import logger

from dailies.profile import (
    Confidence,
    DiscoveredSource,
    Fact,
    LoyaltyProgram,
    Merchant,
    Profile,
    ProfileNotFound,
    ProfileScalar,
    Sourced,
    load_profile,
    merge_fact,
    merge_field,
    merge_loyalty,
    merge_merchant,
    save_profile,
)
from dailies.tools.base import ToolError, ToolSet, tool


@dataclass(frozen=True, slots=True)
class FieldRecorded:
    field: ProfileScalar
    value: Sourced[str]
    profile: Profile


@dataclass(frozen=True, slots=True)
class FactRecorded:
    fact: Fact
    profile: Profile


@dataclass(frozen=True, slots=True)
class LoyaltyRecorded:
    program: LoyaltyProgram
    profile: Profile


@dataclass(frozen=True, slots=True)
class MerchantRecorded:
    merchant: Merchant
    profile: Profile


type DraftEvent = FieldRecorded | FactRecorded | LoyaltyRecorded | MerchantRecorded
type DraftListener = Callable[[DraftEvent], None]


def log_draft_event(event: DraftEvent) -> None:
    """Default draft listener: log one line per recorded value as the profile draft grows."""
    match event:
        case FieldRecorded(field=field, value=value):
            logger.info("draft field recorded: field={} value={}", field, value.value)
        case FactRecorded(fact=fact):
            logger.info("draft fact recorded: label={} value={}", fact.label, fact.value)
        case LoyaltyRecorded(program=program):
            logger.info("draft loyalty recorded: kind={} program={}", program.kind, program.program)
        case MerchantRecorded(merchant=merchant):
            logger.info("draft merchant recorded: name={} category={}", merchant.name, merchant.category)


class ProfileToolSet(ToolSet):
    @tool
    async def get_profile(self) -> Profile:
        """Return the user's profile: name, email, phone, iMessage handle, home address, timezone, birthday, employer and role, partner contact (Rebecca: email/phone), airline and hotel loyalty programs with member numbers, frequent merchants, and extra facts — each value with its source."""  # noqa: E501
        try:
            return await load_profile()
        except ProfileNotFound as exc:
            raise ToolError("profile_missing", str(exc), fix="tell the user to run `dly profile init`") from exc

    async def write(self, mutate: Callable[[Profile], Profile]) -> Profile:
        try:
            profile = await load_profile()
        except ProfileNotFound as exc:
            raise ToolError("profile_missing", str(exc), fix="tell the user to run `dly profile init`") from exc
        if (updated := mutate(profile)) is profile:
            return profile
        await save_profile(updated)
        return updated

    @tool
    async def update_profile_field(
        self, field: ProfileScalar, value: str, source: DiscoveredSource, confidence: Confidence = "high"
    ) -> Profile:
        """Update a scalar profile field from a discovered source; never overwrites a value the user entered."""
        return await self.write(
            lambda p: merge_field(p, field, Sourced[str](value=value, source=source, confidence=confidence))
        )

    @tool
    async def record_fact(
        self, label: str, value: str, source: DiscoveredSource, confidence: Confidence = "high"
    ) -> Profile:
        """Record a durable fact about the user, upserting by label; never overwrites a value the user entered."""
        return await self.write(
            lambda p: merge_fact(p, Fact(label=label, value=value, source=source, confidence=confidence))
        )

    @tool
    async def record_loyalty_program(
        self,
        kind: Literal["airline", "hotel"],
        program: str,
        member_number: str,
        source: DiscoveredSource,
        confidence: Confidence = "high",
        status_tier: str | None = None,
    ) -> Profile:
        """Record an airline or hotel loyalty program and its member number, upserting by program; never overwrites a value the user entered."""  # noqa: E501
        return await self.write(
            lambda p: merge_loyalty(
                p,
                LoyaltyProgram(
                    kind=kind,
                    program=program,
                    member_number=Sourced[str](value=member_number, source=source, confidence=confidence),
                    status_tier=status_tier,
                ),
            )
        )

    @tool
    async def record_merchant(
        self, name: str, category: str, source: DiscoveredSource, cadence: str | None = None
    ) -> Profile:
        """Record a merchant the user transacts with regularly, upserting by name."""
        return await self.write(
            lambda p: merge_merchant(p, Merchant(name=name, category=category, source=source, cadence=cadence))
        )


@dataclass(slots=True)
class DraftProfile(ToolSet):
    """In-memory profile accumulator: mirrors ProfileToolSet's record tools but mutates a draft and fires a listener.

    Each effective merge advances ``draft`` and notifies ``listener`` with the event carrying a full snapshot of
    the accumulated profile, so a single event can render the whole evolving draft.
    """

    draft: Profile
    listener: DraftListener

    @tool
    async def update_profile_field(
        self, field: ProfileScalar, value: str, source: DiscoveredSource, confidence: Confidence = "high"
    ) -> Profile:
        """Update a scalar profile field from a discovered source; never overwrites a value the user entered."""
        sourced = Sourced[str](value=value, source=source, confidence=confidence)
        if (updated := merge_field(self.draft, field, sourced)) is not self.draft:
            self.draft = updated
            self.listener(FieldRecorded(field=field, value=sourced, profile=updated))
        return self.draft

    @tool
    async def record_fact(
        self, label: str, value: str, source: DiscoveredSource, confidence: Confidence = "high"
    ) -> Profile:
        """Record a durable fact about the user, upserting by label; never overwrites a value the user entered."""
        fact = Fact(label=label, value=value, source=source, confidence=confidence)
        if (updated := merge_fact(self.draft, fact)) is not self.draft:
            self.draft = updated
            self.listener(FactRecorded(fact=fact, profile=updated))
        return self.draft

    @tool
    async def record_loyalty_program(
        self,
        kind: Literal["airline", "hotel"],
        program: str,
        member_number: str,
        source: DiscoveredSource,
        confidence: Confidence = "high",
        status_tier: str | None = None,
    ) -> Profile:
        """Record an airline or hotel loyalty program and its member number, upserting by program; never overwrites a value the user entered."""  # noqa: E501
        loyalty = LoyaltyProgram(
            kind=kind,
            program=program,
            member_number=Sourced[str](value=member_number, source=source, confidence=confidence),
            status_tier=status_tier,
        )
        if (updated := merge_loyalty(self.draft, loyalty)) is not self.draft:
            self.draft = updated
            self.listener(LoyaltyRecorded(program=loyalty, profile=updated))
        return self.draft

    @tool
    async def record_merchant(
        self, name: str, category: str, source: DiscoveredSource, cadence: str | None = None
    ) -> Profile:
        """Record a merchant the user transacts with regularly, upserting by name."""
        merchant = Merchant(name=name, category=category, source=source, cadence=cadence)
        if (updated := merge_merchant(self.draft, merchant)) is not self.draft:
            self.draft = updated
            self.listener(MerchantRecorded(merchant=merchant, profile=updated))
        return self.draft
