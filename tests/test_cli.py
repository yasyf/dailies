from __future__ import annotations

from click.testing import CliRunner

from dailies.cli import main


def test_help_exits_cleanly() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert result.output.startswith("Usage: main")


def test_run_defaults_to_today() -> None:
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code == 0
    assert result.output == "No tasks configured for today.\n"


def test_run_accepts_date() -> None:
    result = CliRunner().invoke(main, ["run", "--date", "2026-06-08"])
    assert result.exit_code == 0
    assert result.output == "No tasks configured for 2026-06-08.\n"
