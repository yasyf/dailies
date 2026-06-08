from __future__ import annotations

import click
from loguru import logger


@click.group()
@click.version_option(package_name="dly")
def main() -> None:
    """Daily automation and scheduled task runner."""


@main.command()
@click.option(
    "--date",
    "date",
    default=None,
    metavar="YYYY-MM-DD",
    help="Date to run tasks for; defaults to today.",
)
def run(date: str | None) -> None:
    """Run the daily tasks scheduled for a date."""
    target = date or "today"
    logger.debug("running daily tasks for {}", target)
    click.echo(f"No tasks configured for {target}.")
