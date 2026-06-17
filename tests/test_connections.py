from __future__ import annotations

import pytest

from dailies import connections
from dailies.connections import (
    INTEGRATIONS,
    CredentialField,
    NangoCredential,
    NangoIntegration,
    WizardCredential,
    WizardIntegration,
    integration_ready,
    unready_fix,
)
from tests.fakes import FakeCredentialStore

pytestmark = pytest.mark.unit

GMAIL = INTEGRATIONS["gmail"]
ONEPASSWORD = INTEGRATIONS["onepassword"]
BLUEBUBBLES = INTEGRATIONS["bluebubbles"]


def test_registry_entries() -> None:
    assert GMAIL == NangoIntegration(name="gmail", provider_config_key="google-mail")
    assert ONEPASSWORD == WizardIntegration(
        name="onepassword",
        fields=(
            CredentialField(key="OP_SERVICE_ACCOUNT_TOKEN", prompt="1Password service account token", secret=True),
        ),
    )
    assert BLUEBUBBLES == WizardIntegration(
        name="bluebubbles",
        fields=(
            CredentialField(key="BLUEBUBBLES_URL", prompt="BlueBubbles server URL"),
            CredentialField(key="BLUEBUBBLES_PASSWORD", prompt="BlueBubbles server password", secret=True),
        ),
    )


async def test_nango_unready_without_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connections, "credential_store", lambda: FakeCredentialStore())
    assert await integration_ready(GMAIL) is False


async def test_nango_ready_with_stored_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeCredentialStore(
        credentials={"gmail": NangoCredential(connection_id="conn-1", provider_config_key="google-mail")}
    )
    monkeypatch.setattr(connections, "credential_store", lambda: store)
    assert await integration_ready(GMAIL) is True


async def test_wizard_unready_without_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connections, "credential_store", lambda: FakeCredentialStore())
    assert await integration_ready(ONEPASSWORD) is False


async def test_wizard_ready_when_all_fields_present(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeCredentialStore(
        credentials={"onepassword": WizardCredential(values={"OP_SERVICE_ACCOUNT_TOKEN": "ops_token"})}
    )
    monkeypatch.setattr(connections, "credential_store", lambda: store)
    assert await integration_ready(ONEPASSWORD) is True


async def test_wizard_requires_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = FakeCredentialStore(
        credentials={"bluebubbles": WizardCredential(values={"BLUEBUBBLES_URL": "http://mac.tailnet:1234"})}
    )
    monkeypatch.setattr(connections, "credential_store", lambda: partial)
    assert await integration_ready(BLUEBUBBLES) is False
    complete = FakeCredentialStore(
        credentials={
            "bluebubbles": WizardCredential(
                values={"BLUEBUBBLES_URL": "http://mac.tailnet:1234", "BLUEBUBBLES_PASSWORD": "hunter2"}
            )
        }
    )
    monkeypatch.setattr(connections, "credential_store", lambda: complete)
    assert await integration_ready(BLUEBUBBLES) is True


async def test_unready_fix_strings() -> None:
    assert await unready_fix(GMAIL) == "run `dly auth gmail`"
    assert await unready_fix(ONEPASSWORD) == "run `dly auth onepassword`"
    assert await unready_fix(BLUEBUBBLES) == "run `dly auth bluebubbles`"
