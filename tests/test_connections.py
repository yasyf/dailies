from __future__ import annotations

import pytest

from dailies.connections import (
    INTEGRATIONS,
    Connection,
    EnvIntegration,
    NangoIntegration,
    connection_store,
    integration_ready,
    unready_fix,
)

pytestmark = pytest.mark.unit

GMAIL = INTEGRATIONS["gmail"]
ONEPASSWORD = INTEGRATIONS["onepassword"]


def test_registry_entries() -> None:
    assert GMAIL == NangoIntegration(name="gmail", provider_config_key="google-mail")
    assert ONEPASSWORD == EnvIntegration(
        name="onepassword",
        env_vars=("OP_SERVICE_ACCOUNT_TOKEN",),
        hint="create a 1Password service account with read access to your vaults and copy its token",
    )


async def test_nango_unready_without_connection() -> None:
    assert await integration_ready(GMAIL) is False


async def test_nango_ready_with_stored_connection() -> None:
    await connection_store().store("gmail", Connection(connection_id="conn-1", provider_config_key="google-mail"))
    assert await integration_ready(GMAIL) is True


async def test_env_ready_when_var_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_token")
    assert await integration_ready(ONEPASSWORD) is True


async def test_env_unready_when_var_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    assert await integration_ready(ONEPASSWORD) is False


async def test_env_requires_every_var(monkeypatch: pytest.MonkeyPatch) -> None:
    duo = EnvIntegration(name="duo", env_vars=("DUO_URL", "DUO_PASSWORD"), hint="get both from the duo app")
    monkeypatch.setenv("DUO_URL", "http://localhost")
    monkeypatch.delenv("DUO_PASSWORD", raising=False)
    assert await integration_ready(duo) is False
    monkeypatch.setenv("DUO_PASSWORD", "hunter2")
    assert await integration_ready(duo) is True


async def test_unready_fix_strings() -> None:
    assert await unready_fix(GMAIL) == "run `dly auth gmail`"
    assert await unready_fix(ONEPASSWORD) == "set OP_SERVICE_ACCOUNT_TOKEN (see `dly auth onepassword`)"
    duo = EnvIntegration(name="duo", env_vars=("DUO_URL", "DUO_PASSWORD"), hint="get both from the duo app")
    assert await unready_fix(duo) == "set DUO_URL and DUO_PASSWORD (see `dly auth duo`)"
