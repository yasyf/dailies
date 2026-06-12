from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from dailies.onepassword import Login
from dailies.runtime import RunContext
from dailies.tools.base import ToolSet, tool

if TYPE_CHECKING:
    from dailies.onepassword import VaultClient


@dataclass(frozen=True, slots=True)
class VaultToolSet(ToolSet):
    integrations: ClassVar[tuple[str, ...]] = ("onepassword",)

    context: RunContext
    vault: VaultClient

    @tool
    async def get_login(self, item: str) -> Login:
        """Fetch a login (username, password, otp) from 1Password by item name.

        Use it to recover a logged-out session: when browse or scrape reports
        a login wall or an expired session, fetch that site's credentials,
        then repeat the browse with the credentials included in the goal.
        Fetch a login only after a tool reports needing it, never
        preemptively.
        """
        return await self.vault.get_login(item)
