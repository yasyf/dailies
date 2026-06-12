"""Integration registry and readiness: Nango-connected services and env-credentialed ones."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import Field

from dailies.models import FrozenModel, StoredModel
from dailies.storage import StateStorage, state_storage


class NangoIntegration(FrozenModel):
    """A registry entry mapping an integration name to its Nango unique key."""

    kind: Literal["nango"] = "nango"
    name: str
    provider_config_key: str


class EnvIntegration(FrozenModel):
    """A registry entry for an integration credentialed by environment variables."""

    kind: Literal["env"] = "env"
    name: str
    env_vars: tuple[str, ...]
    hint: str


type Integration = Annotated[NangoIntegration | EnvIntegration, Field(discriminator="kind")]

INTEGRATIONS: dict[str, Integration] = {
    "gmail": NangoIntegration(name="gmail", provider_config_key="google-mail"),
    "onepassword": EnvIntegration(
        name="onepassword",
        env_vars=("OP_SERVICE_ACCOUNT_TOKEN",),
        hint="create a 1Password service account with read access to your vaults and copy its token",
    ),
    "bluebubbles": EnvIntegration(
        name="bluebubbles",
        env_vars=("BLUEBUBBLES_URL", "BLUEBUBBLES_PASSWORD"),
        hint="pair a BlueBubbles server on a Mac (e.g. reachable over Tailscale) and copy its URL and password",
    ),
}


class NotConnected(LookupError):
    """No stored connection for the integration; `dly auth <integration>` creates one."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is not connected — run `dly auth {name}` first")


class Connection(StoredModel):
    connection_id: str
    provider_config_key: str


class ConnectionStore(Protocol):
    """Persists one Nango connection per integration name."""

    async def load(self, name: str) -> Connection: ...

    async def store(self, name: str, connection: Connection) -> None: ...


@dataclass(frozen=True, slots=True)
class StateConnectionStore:
    storage: StateStorage

    async def load(self, name: str) -> Connection:
        async with self.storage.lease(f"connections/{name}.json") as path:
            if not path.exists():
                raise NotConnected(name)
            return Connection.model_validate_json(path.read_bytes())

    async def store(self, name: str, connection: Connection) -> None:
        async with self.storage.lease(f"connections/{name}.json") as path:
            path.write_text(connection.model_dump_json())


def connection_store() -> ConnectionStore:
    return StateConnectionStore(storage=state_storage())


async def integration_ready(integration: Integration) -> bool:
    """Whether the integration is usable: a stored Nango connection, or every env var present."""
    match integration:
        case NangoIntegration(name=name):
            try:
                await connection_store().load(name)
            except NotConnected:
                return False
            return True
        case EnvIntegration(env_vars=env_vars):
            return all(var in os.environ for var in env_vars)


async def unready_fix(integration: Integration) -> str:
    """The one fix string for an unready integration, shared by `dly auth status` and activation."""
    match integration:
        case NangoIntegration(name=name):
            return f"run `dly auth {name}`"
        case EnvIntegration(name=name, env_vars=env_vars):
            return f"set {' and '.join(env_vars)} (see `dly auth {name}`)"
