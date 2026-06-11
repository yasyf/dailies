from __future__ import annotations

import fcntl
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import click
import httpx

from dailies.agent import ClaudeAgentSDKProvider
from dailies.connections import INTEGRATIONS, Connection, Integration, NotConnected, connection_store
from dailies.db import lifespan
from dailies.engine import Engine, TriggerFired
from dailies.gmail import NANGO_API, NangoGmailClient, checked
from dailies.interface import TextualPresenter, run_tui
from dailies.interview import InterviewRunner
from dailies.models import Firing, ManualTrigger, WorkflowId
from dailies.tools import ToolSet

AUTH_POLL_INTERVAL = 3.0
AUTH_TIMEOUT = 300.0


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


async def account_email(integration: Integration) -> str:
    match integration.name:
        case "gmail":
            return (await NangoGmailClient(store=connection_store()).profile()).email
        case unknown:
            raise KeyError(unknown)


async def await_connection(client: httpx.AsyncClient, *, end_user_id: str, integration: Integration) -> Connection:
    try:
        with anyio.fail_after(AUTH_TIMEOUT):
            while True:
                listing = checked(await client.get("/connections", params={"tags[end_user_id]": end_user_id})).json()
                if found := next(
                    (
                        connection
                        for connection in listing["connections"]
                        if connection["provider_config_key"] == integration.provider_config_key
                        and not connection["errors"]
                    ),
                    None,
                ):
                    return Connection(
                        connection_id=found["connection_id"], provider_config_key=found["provider_config_key"]
                    )
                await anyio.sleep(AUTH_POLL_INTERVAL)
    except TimeoutError:
        raise click.ClickException(
            f"timed out waiting for the {integration.name} connection — run `dly auth {integration.name}` again"
        ) from None


async def connect_integration(integration: Integration) -> None:
    headers = {"Authorization": f"Bearer {os.environ['NANGO_SECRET_KEY']}"}
    end_user_id = f"dly-{uuid4().hex}"
    async with httpx.AsyncClient(base_url=NANGO_API, headers=headers) as client:
        session = checked(
            await client.post(
                "/connect/sessions",
                json={
                    "tags": {"end_user_id": end_user_id},
                    "allowed_integrations": [integration.provider_config_key],
                },
            )
        ).json()
        click.echo(f"Complete the connection in your browser: {(link := session['data']['connect_link'])}")
        click.launch(link)
        connection = await await_connection(client, end_user_id=end_user_id, integration=integration)
    await connection_store().store(integration.name, connection)
    click.echo(f"Authenticated {await account_email(integration)}")


@main.group()
def auth() -> None:
    """Connect external integrations via Nango."""


def auth_command(integration: Integration) -> click.Command:
    @click.command(integration.name, help=f"Connect {integration.name} through a Nango connect link.")
    def connect() -> None:
        anyio.run(connect_integration, integration)

    return connect


for integration in INTEGRATIONS.values():
    auth.add_command(auth_command(integration))


@auth.command()
def status() -> None:
    """Show each integration's connection, account, and dependent toolsets."""

    async def go() -> None:
        store = connection_store()
        for name, integration in INTEGRATIONS.items():
            users = ", ".join(sorted(ts.__name__ for ts in ToolSet.__subclasses__() if name in ts.integrations))
            try:
                await store.load(name)
            except NotConnected:
                click.echo(f"{name}: not connected (run `dly auth {name}`) — used by {users}")
                continue
            click.echo(f"{name}: connected as {await account_email(integration)} — used by {users}")

    anyio.run(go)


@main.command()
@click.argument("workflow_id", type=click.UUID)
def run(workflow_id: UUID) -> None:
    """Fire a single manual run of the workflow with the given id."""

    async def go() -> None:
        async with lifespan():
            await Engine().dispatch(TriggerFired(WorkflowId(workflow_id), [Firing(trigger=ManualTrigger())]))

    anyio.run(go)


@main.command()
def tick() -> None:
    """Sweep cron-due workflows, then poll event subscriptions for news.

    Ticks never overlap: a second observer of the same pending occurrences would
    dispatch duplicate runs, so the whole sweep holds an exclusive lock and a
    concurrent invocation exits loudly instead.
    """

    async def go() -> None:
        async with lifespan():
            engine = Engine()
            now = datetime.now(UTC)
            await engine.fire_due(now=now)
            await engine.poll_subscriptions(now=now)

    lock_path = Path(os.environ["DAILIES_STATE_DIR"]) / "tick.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise click.ClickException("another tick is already running") from None
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
