"""Per-integration credential store: one Nango connection per external service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dailies.models import FrozenModel, StoredModel
from dailies.storage import StateStorage, state_storage


class Integration(FrozenModel):
    """A registry entry mapping an integration name to its Nango unique key."""

    name: str
    provider_config_key: str


INTEGRATIONS: dict[str, Integration] = {"gmail": Integration(name="gmail", provider_config_key="google-mail")}


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
