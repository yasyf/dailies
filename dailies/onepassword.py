"""1Password vault client shelling out to the `op` CLI.

Secrets transit caveat: Login values fetched here enter the agent's context
window and the run transcript. Fetch a login only after a tool reports needing
credentials (a login wall, an expired session) — never preemptively — and
never echo its values into status updates or state tables. The future path for
keeping secrets out of the model entirely is browser-use-style
``sensitive_data`` placeholder hardening.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

import anyio

from dailies.models import FrozenModel


class VaultLookupFailed(RuntimeError):
    """`op item get` exited non-zero for the requested item."""

    def __init__(self, item: str, stderr: str) -> None:
        super().__init__(f"1Password lookup for {item!r} failed: {stderr.strip()}")
        self.item = item
        self.stderr = stderr


class Login(FrozenModel):
    username: str
    password: str
    otp: str | None = None


class VaultClient(Protocol):
    """Async vault surface shared by agent tools; 1Password-backed in production."""

    async def get_login(self, item: str) -> Login: ...


def parse_login(data: dict[str, Any]) -> Login:
    fields = {field["id"]: field for field in data["fields"]}
    return Login(
        username=fields["username"]["value"],
        password=fields["password"]["value"],
        otp=next((totp for field in data["fields"] if field["type"] == "OTP" and (totp := field.get("totp"))), None),
    )


@dataclass(frozen=True, slots=True)
class OnePasswordClient:
    """VaultClient shelling out to the 1Password CLI with a service-account token.

    Construction performs no I/O and reads no environment;
    ``OP_SERVICE_ACCOUNT_TOKEN`` is resolved per call so unconfigured machines
    only fail when a login is actually fetched.
    """

    async def get_login(self, item: str) -> Login:
        result = await anyio.run_process(
            ["op", "item", "get", item, "--format", "json", "--reveal"],
            check=False,
            env=os.environ | {"OP_SERVICE_ACCOUNT_TOKEN": os.environ["OP_SERVICE_ACCOUNT_TOKEN"]},
        )
        if result.returncode != 0:
            raise VaultLookupFailed(item, result.stderr.decode())
        return parse_login(json.loads(result.stdout))


def vault_client() -> VaultClient:
    return OnePasswordClient()
