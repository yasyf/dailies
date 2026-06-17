"""Integration registry and readiness: Nango-connected services and wizard-credentialed ones.

Stored credentials live unencrypted at rest in Mongo, the same posture as the prior
env token (and the onepassword secrets-transit caveat) — encryption is future work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import Field
from pymongo import ASCENDING, IndexModel

from dailies.documents import TimestampedDocument
from dailies.models import FrozenModel


class NangoIntegration(FrozenModel):
    """A registry entry mapping an integration name to its Nango unique key."""

    kind: Literal["nango"] = "nango"
    name: str
    provider_config_key: str


class CredentialField(FrozenModel):
    """One credential value the `dly auth` wizard prompts for and stores."""

    key: str
    prompt: str
    secret: bool = False


class WizardIntegration(FrozenModel):
    """A registry entry for an integration configured through the `dly auth` wizard."""

    kind: Literal["wizard"] = "wizard"
    name: str
    fields: tuple[CredentialField, ...]


type Integration = Annotated[NangoIntegration | WizardIntegration, Field(discriminator="kind")]

INTEGRATIONS: dict[str, Integration] = {
    "gmail": NangoIntegration(name="gmail", provider_config_key="google-mail"),
    "onepassword": WizardIntegration(
        name="onepassword",
        fields=(
            CredentialField(key="OP_SERVICE_ACCOUNT_TOKEN", prompt="1Password service account token", secret=True),
        ),
    ),
    "bluebubbles": WizardIntegration(
        name="bluebubbles",
        fields=(
            CredentialField(key="BLUEBUBBLES_URL", prompt="BlueBubbles server URL"),
            CredentialField(key="BLUEBUBBLES_PASSWORD", prompt="BlueBubbles server password", secret=True),
        ),
    ),
}


class NotConnected(LookupError):
    """No stored connection for the integration; `dly auth <integration>` creates one."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is not connected — run `dly auth {name}` first")


class NangoCredential(FrozenModel):
    kind: Literal["nango"] = "nango"
    connection_id: str
    provider_config_key: str


class WizardCredential(FrozenModel):
    kind: Literal["wizard"] = "wizard"
    values: dict[str, str]


type Credential = Annotated[NangoCredential | WizardCredential, Field(discriminator="kind")]


class IntegrationCredentials(TimestampedDocument):
    """One integration's stored credential, keyed by registry name."""

    name: str
    credential: Credential

    class Settings:
        name = "credentials"
        indexes = [IndexModel([("name", ASCENDING)], unique=True)]


class CredentialStore(Protocol):
    """Persists one credential per integration name."""

    async def load(self, name: str) -> Credential: ...

    async def save(self, name: str, credential: Credential) -> None: ...


@dataclass(frozen=True, slots=True)
class MongoCredentialStore:
    async def load(self, name: str) -> Credential:
        document = await IntegrationCredentials.find_one(IntegrationCredentials.name == name)
        if document is None:
            raise NotConnected(name)
        return document.credential

    async def save(self, name: str, credential: Credential) -> None:
        match await IntegrationCredentials.find_one(IntegrationCredentials.name == name):
            case None:
                await IntegrationCredentials(name=name, credential=credential).insert()
            case document:
                document.credential = credential
                await document.replace()


def credential_store() -> CredentialStore:
    return MongoCredentialStore()


async def integration_ready(integration: Integration) -> bool:
    """Whether the integration's credential is stored and complete."""
    match integration:
        case NangoIntegration(name=name):
            try:
                await credential_store().load(name)
            except NotConnected:
                return False
            return True
        case WizardIntegration(name=name, fields=fields):
            try:
                credential = await credential_store().load(name)
            except NotConnected:
                return False
            return isinstance(credential, WizardCredential) and all(field.key in credential.values for field in fields)


async def unready_fix(integration: Integration) -> str:
    """The one fix string for an unready integration, shared by `dly auth status` and activation."""
    return f"run `dly auth {integration.name}`"
