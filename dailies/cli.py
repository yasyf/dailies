from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import click
import httpx

from dailies.agent import ClaudeAgentSDKProvider
from dailies.browser import COOKIE_BROWSERS, import_cookies
from dailies.connections import (
    INTEGRATIONS,
    Connection,
    EnvIntegration,
    Integration,
    NangoIntegration,
    connection_store,
    integration_ready,
    unready_fix,
)
from dailies.db import lifespan
from dailies.engine import Engine, TriggerFired
from dailies.gmail import NANGO_API, NangoGmailClient, checked
from dailies.interface import TextualPresenter, run_tui
from dailies.interview import InterviewRunner
from dailies.models import Firing, ManualTrigger, WorkflowId
from dailies.storage import state_storage
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


async def account_email(integration: NangoIntegration) -> str:
    match integration.name:
        case "gmail":
            return (await NangoGmailClient(store=connection_store()).profile()).email
        case unknown:
            raise KeyError(unknown)


async def await_connection(
    client: httpx.AsyncClient, *, end_user_id: str, integration: NangoIntegration
) -> Connection:
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


async def connect_integration(integration: NangoIntegration) -> None:
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


async def verify_env_integration(integration: EnvIntegration) -> None:
    if await integration_ready(integration):
        click.echo(f"{integration.name}: ready")
        return
    click.echo(f"To set up {integration.name}:")
    for step, line in enumerate((integration.hint, *(f"set {var}" for var in integration.env_vars)), start=1):
        click.echo(f"  {step}. {line}")
    raise click.ClickException(f"{integration.name} is not ready")


@main.group()
def auth() -> None:
    """Connect external integrations."""


def auth_command(integration: Integration) -> click.Command:
    match integration:
        case NangoIntegration() as nango:

            @click.command(nango.name, help=f"Connect {nango.name} through a Nango connect link.")
            def connect() -> None:
                anyio.run(connect_integration, nango)

        case EnvIntegration() as env:

            @click.command(env.name, help=f"Verify {env.name} credentials or print setup instructions.")
            def connect() -> None:
                anyio.run(verify_env_integration, env)

    return connect


for integration in INTEGRATIONS.values():
    auth.add_command(auth_command(integration))


@auth.command()
def status() -> None:
    """Show each integration's readiness, account, and dependent toolsets."""

    async def go() -> None:
        for name, integration in INTEGRATIONS.items():
            users = ", ".join(sorted(ts.__name__ for ts in ToolSet.__subclasses__() if name in ts.integrations))
            suffix = f" — used by {users}" if users else ""
            if not await integration_ready(integration):
                click.echo(f"{name}: not ready — {await unready_fix(integration)}{suffix}")
                continue
            match integration:
                case NangoIntegration() as nango:
                    click.echo(f"{name}: connected as {await account_email(nango)}{suffix}")
                case EnvIntegration():
                    click.echo(f"{name}: ready{suffix}")

    anyio.run(go)


@main.group()
def browser() -> None:
    """Manage the per-workflow browser profile used by the browse tool."""


@browser.command("import-cookies")
@click.argument("workflow_id", type=click.UUID)
@click.option(
    "--domain", "domains", multiple=True, required=True, help="Domain to import (repeatable; matches subdomains)."
)
@click.option(
    "--from-browser",
    type=click.Choice(COOKIE_BROWSERS),
    default="chrome",
    show_default=True,
    help="Local browser to read cookies from.",
)
@click.option(
    "--from-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Import from a storage_state JSON or Netscape cookies.txt instead of a local browser.",
)
def import_cookies_cmd(workflow_id: UUID, domains: tuple[str, ...], from_browser: str, from_file: Path | None) -> None:
    """Seed the workflow's browser profile with cookies for the given domains.

    Reads the local browser's cookie store (a macOS keychain prompt may appear) or, with
    --from-file, a previously exported file, and merges the matching cookies into
    browser/<workflow_id>.json in the state store.
    """

    async def go() -> None:
        count = await import_cookies(
            state_storage(), WorkflowId(workflow_id), domains=domains, source_browser=from_browser, from_file=from_file
        )
        click.echo(f"Imported {count} cookies into workflow {workflow_id}")

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

    Safe to overlap: each workflow is processed under a short MongoDB lease, so a
    concurrent tick — on this host or another sharing the database — skips
    in-flight workflows and processes the rest. Drive it from cron or launchd on
    a ~1-minute cadence.
    """

    async def go() -> None:
        async with lifespan():
            await Engine().tick(now=datetime.now(UTC))

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
