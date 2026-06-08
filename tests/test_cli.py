from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from click.testing import CliRunner

from dailies import cli
from dailies.cli import main
from dailies.engine import Engine

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
    for command in ("run", "tick", "tui", "db"):
        assert command in result.output


def test_db_help_lists_init() -> None:
    result = CliRunner().invoke(main, ["db", "--help"])
    assert result.exit_code == 0
    assert "init" in result.output


def test_db_init_runs() -> None:
    result = CliRunner().invoke(main, ["db", "init"])
    assert result.exit_code == 0
    assert "Database initialised." in result.output


def test_tui_invokes_run_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(cli, "run_tui", calls.append)
    result = CliRunner().invoke(main, ["tui"])
    assert result.exit_code == 0
    assert len(calls) == 1


def test_run_propagates_agent_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(self: Engine, fired: object) -> None:
        raise NotImplementedError("seam")

    monkeypatch.setattr(Engine, "dispatch", boom)
    result = CliRunner().invoke(main, ["run", str(uuid4())])
    assert result.exit_code == 1
    assert isinstance(result.exception, NotImplementedError)


def test_run_rejects_bad_uuid() -> None:
    result = CliRunner().invoke(main, ["run", "not-a-uuid"])
    assert result.exit_code == 2


def test_tick_reaches_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    async def fake_fire(self: Engine, *, now: object) -> list:
        seen.append(now)
        return []

    monkeypatch.setattr(Engine, "fire_due", fake_fire)
    result = CliRunner().invoke(main, ["tick"])
    assert result.exit_code == 0
    assert len(seen) == 1
