from __future__ import annotations

from collections.abc import Callable

from dailies.profile import (
    Confidence,
    DiscoveredSource,
    Fact,
    Profile,
    ProfileNotFound,
    ProfileScalar,
    Sourced,
    load_profile,
    merge_fact,
    merge_field,
    save_profile,
)
from dailies.tools.base import ToolError, ToolSet, tool


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
        await save_profile(updated := mutate(profile))
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
