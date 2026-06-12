from __future__ import annotations

from dailies.profile import Profile
from dailies.tools.base import ToolError, ToolSet, tool


class ProfileToolSet(ToolSet):
    @tool
    async def get_profile(self) -> Profile:
        """Return the user's profile: name, email, phone, iMessage handle, home address, timezone, birthday, employer and role, partner contact (Rebecca: email/phone), airline and hotel loyalty programs with member numbers, frequent merchants, and extra facts — each value with its source."""  # noqa: E501
        from dailies.profile import ProfileNotFound, load_profile

        try:
            return await load_profile()
        except ProfileNotFound as exc:
            raise ToolError("profile_missing", str(exc), fix="tell the user to run `dly profile init`") from exc
