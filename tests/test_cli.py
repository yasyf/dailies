from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import click
import httpx
import pytest
from click.testing import CliRunner

from dailies import cli
from dailies.cli import main
from dailies.connections import Connection
from dailies.engine import Engine, TriggerFired
from dailies.models import Firing, ManualTrigger, WorkflowId

pytestmark = pytest.mark.unit


@asynccontextmanager
async def fake_lifespan() -> AsyncIterator[None]:
    yield None


@pytest.fixture(autouse=True)
def stub_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "lifespan", fake_lifespan)


def test_help_lists_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "tick", "tui", "interview", "db", "auth", "browser"):
        assert command in result.output


def test_db_help_lists_init() -> None:
    result = CliRunner().invoke(main, ["db", "--help"])
    assert result.exit_code == 0
    assert "init" in result.output


def test_auth_help_lists_integrations_and_status() -> None:
    result = CliRunner().invoke(main, ["auth", "--help"])
    assert result.exit_code == 0
    assert "gmail" in result.output
    assert "status" in result.output


def test_auth_unknown_integration_fails_loudly() -> None:
    result = CliRunner().invoke(main, ["auth", "slack"])
    assert result.exit_code == 2
    assert "No such command 'slack'" in result.output


def test_browser_import_cookies_reports_count(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_import(
        storage: object,
        workflow_id: object,
        *,
        domains: tuple[str, ...],
        source_browser: str = "chrome",
        from_file: Path | None = None,
    ) -> int:
        captured.update(workflow_id=workflow_id, domains=domains, source_browser=source_browser, from_file=from_file)
        return 3

    monkeypatch.setattr(cli, "import_cookies", fake_import)
    workflow_id = uuid4()
    result = CliRunner().invoke(
        main, ["browser", "import-cookies", str(workflow_id), "--domain", "github.com", "--domain", "example.com"]
    )
    assert result.exit_code == 0
    assert f"Imported 3 cookies into workflow {workflow_id}" in result.output
    assert captured == {
        "workflow_id": workflow_id,
        "domains": ("github.com", "example.com"),
        "source_browser": "chrome",
        "from_file": None,
    }


def test_browser_import_cookies_requires_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "import_cookies", lambda *a, **k: 0)
    result = CliRunner().invoke(main, ["browser", "import-cookies", str(uuid4())])
    assert result.exit_code == 2
    assert "--domain" in result.output


def test_browser_import_cookies_bad_uuid() -> None:
    result = CliRunner().invoke(main, ["browser", "import-cookies", "not-a-uuid", "--domain", "github.com"])
    assert result.exit_code == 2


def mock_clients(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real(**kwargs | {"transport": httpx.MockTransport(handler)})
    )


def test_auth_gmail_connects_persists_and_verifies(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    monkeypatch.setenv("NANGO_SECRET_KEY", "secret")
    monkeypatch.setattr(cli, "AUTH_POLL_INTERVAL", 0.0)
    launched: list[str] = []
    monkeypatch.setattr(click, "launch", lambda url: launched.append(url) or 0)
    minted: list[str] = []
    polls = [
        [],
        [{"connection_id": "conn-1", "provider_config_key": "google-mail", "errors": []}],
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        match (request.method, request.url.path):
            case ("POST", "/connect/sessions"):
                body = json.loads(request.content)
                assert body["allowed_integrations"] == ["google-mail"]
                minted.append(body["tags"]["end_user_id"])
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "token": "tok",
                            "expires_at": "2026-06-11T00:00:00Z",
                            "connect_link": "https://connect.nango.dev/abc",
                        }
                    },
                )
            case ("GET", "/connections"):
                assert request.url.params["tags[end_user_id]"] == minted[0]
                return httpx.Response(200, json={"connections": polls.pop(0)})
            case ("GET", "/proxy/gmail/v1/users/me/profile"):
                assert request.headers["connection-id"] == "conn-1"
                assert request.headers["provider-config-key"] == "google-mail"
                return httpx.Response(200, json={"emailAddress": "yasyfm@gmail.com"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    mock_clients(monkeypatch, handle)
    result = CliRunner().invoke(main, ["auth", "gmail"])
    assert result.exit_code == 0
    assert launched == ["https://connect.nango.dev/abc"]
    assert "https://connect.nango.dev/abc" in result.output
    assert "Authenticated yasyfm@gmail.com" in result.output
    assert polls == []
    stored = Connection.model_validate_json((state_dir / "connections" / "gmail.json").read_bytes())
    assert stored == Connection(connection_id="conn-1", provider_config_key="google-mail")


def test_auth_status_unconnected() -> None:
    result = CliRunner().invoke(main, ["auth", "status"])
    assert result.exit_code == 0
    assert "gmail: not connected (run `dly auth gmail`)" in result.output
    assert "used by ActionToolSet, EmailToolSet" in result.output


def test_auth_status_connected(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    monkeypatch.setenv("NANGO_SECRET_KEY", "secret")
    (state_dir / "connections").mkdir(parents=True)
    (state_dir / "connections" / "gmail.json").write_text(
        Connection(connection_id="conn-1", provider_config_key="google-mail").model_dump_json()
    )

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/proxy/gmail/v1/users/me/profile"
        return httpx.Response(200, json={"emailAddress": "yasyfm@gmail.com"})

    mock_clients(monkeypatch, handle)
    result = CliRunner().invoke(main, ["auth", "status"])
    assert result.exit_code == 0
    assert "gmail: connected as yasyfm@gmail.com" in result.output
    assert "used by ActionToolSet, EmailToolSet" in result.output


def test_db_init_runs() -> None:
    result = CliRunner().invoke(main, ["db", "init"])
    assert result.exit_code == 0
    assert "Database initialised." in result.output


def test_tui_invokes_run_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def record(*args: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli, "run_tui", record)
    result = CliRunner().invoke(main, ["tui"])
    assert result.exit_code == 0
    assert calls == [{}]


def test_interview_invokes_run_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def record(*args: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli, "run_tui", record)
    result = CliRunner().invoke(main, ["interview"])
    assert result.exit_code == 0
    assert calls == [{"start_interview": True}]


def test_run_dispatches_one_manual_firing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[TriggerFired] = []

    async def record(self: Engine, fired: TriggerFired) -> None:
        seen.append(fired)

    monkeypatch.setattr(Engine, "dispatch", record)
    workflow_id = uuid4()
    result = CliRunner().invoke(main, ["run", str(workflow_id)])
    assert result.exit_code == 0
    assert seen == [TriggerFired(WorkflowId(workflow_id), [Firing(trigger=ManualTrigger())])]


def test_run_propagates_engine_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(self: Engine, fired: object) -> None:
        raise NotImplementedError("seam")

    monkeypatch.setattr(Engine, "dispatch", boom)
    result = CliRunner().invoke(main, ["run", str(uuid4())])
    assert result.exit_code == 1
    assert isinstance(result.exception, NotImplementedError)


def test_run_rejects_bad_uuid() -> None:
    result = CliRunner().invoke(main, ["run", "not-a-uuid"])
    assert result.exit_code == 2


def test_tick_invokes_engine_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks: list[datetime] = []

    async def fake_tick(self: Engine, *, now: datetime) -> list[object]:
        ticks.append(now)
        return []

    monkeypatch.setattr(Engine, "tick", fake_tick)
    result = CliRunner().invoke(main, ["tick"])
    assert result.exit_code == 0
    assert len(ticks) == 1
    assert ticks[0].tzinfo is not None
