from __future__ import annotations

from typing import Any

import pytest
from pymongo import AsyncMongoClient

from dailies.connections import (
    IntegrationCredentials,
    NangoCredential,
    NotConnected,
    WizardCredential,
    credential_store,
)

pytestmark = pytest.mark.integration


async def test_nango_credential_round_trips(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    store = credential_store()
    credential = NangoCredential(connection_id="conn-1", provider_config_key="google-mail")
    await store.save("gmail", credential)
    assert await store.load("gmail") == credential


async def test_wizard_credential_round_trips(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    store = credential_store()
    credential = WizardCredential(values={"OP_SERVICE_ACCOUNT_TOKEN": "ops_token"})
    await store.save("onepassword", credential)
    assert await store.load("onepassword") == credential


async def test_save_twice_keeps_one_document_per_name(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    store = credential_store()
    await store.save("onepassword", WizardCredential(values={"OP_SERVICE_ACCOUNT_TOKEN": "old"}))
    updated = WizardCredential(values={"OP_SERVICE_ACCOUNT_TOKEN": "new"})
    await store.save("onepassword", updated)
    assert await IntegrationCredentials.count() == 1
    assert await store.load("onepassword") == updated


async def test_load_absent_raises_not_connected(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    with pytest.raises(NotConnected, match="run `dly auth gmail` first"):
        await credential_store().load("gmail")
