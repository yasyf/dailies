from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import anyio
import click

from dailies.agent import ClaudeAgentSDKProvider
from dailies.db import lifespan
from dailies.engine import Engine, TriggerFired
from dailies.interface import TextualPresenter, run_tui
from dailies.interview import InterviewRunner
from dailies.models import ManualTrigger, WorkflowId


@click.group()
@click.version_option(package_name="dly")
def main() -> None:
    """Daily automation and scheduled task runner."""


@main.group()
def db() -> None:
    """Database management."""


@db.command("init")
def db_init() -> None:
    """Connect to MongoDB and initialise beanie indexes."""

    async def go() -> None:
        async with lifespan():
            click.echo("Database initialised.")

    anyio.run(go)


@main.command()
@click.argument("workflow_id", type=click.UUID)
def run(workflow_id: UUID) -> None:
    """Fire a single manual run of the workflow with the given id."""

    async def go() -> None:
        async with lifespan():
            await Engine().dispatch(TriggerFired(WorkflowId(workflow_id), ManualTrigger()))

    anyio.run(go)


@main.command()
def tick() -> None:
    """Sweep cron-due workflows and fire a run for each due trigger."""

    async def go() -> None:
        async with lifespan():
            await Engine().fire_due(now=datetime.now(UTC))

    anyio.run(go)


def build_interviewer() -> InterviewRunner:
    return InterviewRunner(ClaudeAgentSDKProvider())


@main.command()
def tui() -> None:
    """Launch the Textual UI."""

    async def go() -> None:
        async with lifespan():
            await run_tui(TextualPresenter(), build_interviewer())

    anyio.run(go)


@main.command()
def interview() -> None:
    """Launch straight into the onboarding interview."""

    async def go() -> None:
        async with lifespan():
            await run_tui(TextualPresenter(), build_interviewer(), start_interview=True)

    anyio.run(go)
