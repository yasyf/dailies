from __future__ import annotations

import pytest

from dailies import connections
from dailies.connections import (
    INTEGRATIONS,
    EnvIntegration,
    NangoCredential,
    NangoIntegration,
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
    assert ONEPASSWORD == EnvIntegration(
        name="onepassword",
        env_vars=("OP_SERVICE_ACCOUNT_TOKEN",),
        hint="create a 1Password service account with read access to your vaults and copy its token",
    )
    assert BLUEBUBBLES == EnvIntegration(
        name="bluebubbles",
        env_vars=("BLUEBUBBLES_URL", "BLUEBUBBLES_PASSWORD"),
        hint="pair a BlueBubbles server on a Mac (e.g. reachable over Tailscale) and copy its URL and password",
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


async def test_env_ready_when_var_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_token")
    assert await integration_ready(ONEPASSWORD) is True


async def test_env_unready_when_var_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    assert await integration_ready(ONEPASSWORD) is False


async def test_env_requires_every_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLUEBUBBLES_URL", "http://mac.tailnet:1234")
    monkeypatch.delenv("BLUEBUBBLES_PASSWORD", raising=False)
    assert await integration_ready(BLUEBUBBLES) is False
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "hunter2")
    assert await integration_ready(BLUEBUBBLES) is True


async def test_unready_fix_strings() -> None:
    assert await unready_fix(GMAIL) == "run `dly auth gmail`"
    assert await unready_fix(ONEPASSWORD) == "set OP_SERVICE_ACCOUNT_TOKEN (see `dly auth onepassword`)"
    assert await unready_fix(BLUEBUBBLES) == "set BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD (see `dly auth bluebubbles`)"
