from __future__ import annotations

import json
from subprocess import CompletedProcess
from typing import Any

import anyio
import pytest

from dailies.connections import NotConnected, WizardCredential
from dailies.onepassword import Login, OnePasswordClient, VaultLookupFailed, parse_login, vault_client
from tests.fakes import FakeCredentialStore

pytestmark = pytest.mark.unit


def op_store(token: str = "ops_token") -> FakeCredentialStore:
    return FakeCredentialStore(
        credentials={"onepassword": WizardCredential(values={"OP_SERVICE_ACCOUNT_TOKEN": token})}
    )


OTP_FIELD = {
    "id": "totp-1",
    "type": "OTP",
    "label": "one-time password",
    "value": "otpauth://totp/GitHub:yasyf?secret=ABC123",
    "totp": "488912",
}


def op_item(*extra_fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "item-1",
        "title": "github.com",
        "category": "LOGIN",
        "fields": [
            {"id": "username", "type": "STRING", "purpose": "USERNAME", "label": "username", "value": "yasyf"},
            {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password", "value": "hunter2"},
            *extra_fields,
        ],
    }


def completed(returncode: int, *, stdout: bytes = b"", stderr: bytes = b"") -> CompletedProcess[bytes]:
    return CompletedProcess(["op"], returncode, stdout=stdout, stderr=stderr)


def test_parse_login_with_otp_field() -> None:
    assert parse_login(op_item(OTP_FIELD)) == Login(username="yasyf", password="hunter2", otp="488912")


def test_parse_login_without_otp_field() -> None:
    assert parse_login(op_item()) == Login(username="yasyf", password="hunter2", otp=None)


def test_parse_login_skips_otp_fields_without_a_totp_value() -> None:
    blank = {"id": "totp-0", "type": "OTP", "label": "one-time password", "value": "otpauth://totp/Stale"}
    assert parse_login(op_item(blank, OTP_FIELD)) == Login(username="yasyf", password="hunter2", otp="488912")


async def test_get_login_shells_op_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OnePasswordClient(credentials=op_store())
    captured: dict[str, Any] = {}

    async def fake_run_process(command: list[str], *, check: bool, env: dict[str, str]) -> CompletedProcess[bytes]:
        captured.update(command=command, check=check, token=env["OP_SERVICE_ACCOUNT_TOKEN"])
        return completed(0, stdout=json.dumps(op_item(OTP_FIELD)).encode())

    monkeypatch.setattr(anyio, "run_process", fake_run_process)
    assert await client.get_login("github.com") == Login(username="yasyf", password="hunter2", otp="488912")
    assert captured == {
        "command": ["op", "item", "get", "github.com", "--format", "json", "--reveal"],
        "check": False,
        "token": "ops_token",
    }


async def test_get_login_nonzero_exit_raises_vault_lookup_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_process(command: list[str], *, check: bool, env: dict[str, str]) -> CompletedProcess[bytes]:
        return completed(1, stderr=b'[ERROR] "github.com" isn\'t an item in any vault\n')

    monkeypatch.setattr(anyio, "run_process", fake_run_process)
    with pytest.raises(VaultLookupFailed, match=r"1Password lookup for 'github.com' failed") as excinfo:
        await OnePasswordClient(credentials=op_store()).get_login("github.com")
    assert excinfo.value.item == "github.com"
    assert excinfo.value.stderr == '[ERROR] "github.com" isn\'t an item in any vault\n'


async def test_missing_credentials_raise_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explode(command: list[str], *, check: bool, env: dict[str, str]) -> CompletedProcess[bytes]:
        raise AssertionError("run_process must not run without stored credentials")

    monkeypatch.setattr(anyio, "run_process", explode)
    client = OnePasswordClient(credentials=FakeCredentialStore())
    with pytest.raises(NotConnected, match="onepassword is not connected"):
        await client.get_login("github.com")


def test_factory_returns_onepassword_client() -> None:
    assert isinstance(vault_client(), OnePasswordClient)
