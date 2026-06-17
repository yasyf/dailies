from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from uuid import UUID, uuid4

import anyio
import click
import httpx

from dailies.activation import ActivationError, TaskNotFound, activate_task, latest_workflows
from dailies.agent import ClaudeAgentSDKProvider
from dailies.bluebubbles import imessage_client
from dailies.browser import COOKIE_BROWSERS, import_cookies
from dailies.connections import (
    INTEGRATIONS,
    Integration,
    NangoCredential,
    NangoIntegration,
    NotConnected,
    WizardCredential,
    WizardIntegration,
    credential_store,
    integration_ready,
    unready_fix,
)
from dailies.db import lifespan
from dailies.discovery import DISCOVERY_MAX_TURNS, discover_profile
from dailies.documents import Task
from dailies.engine import Engine, TriggerFired
from dailies.gmail import NANGO_API, NangoGmailClient, checked, gmail_client
from dailies.interface import TextualPresenter, run_tui
from dailies.interview import InterviewError, InterviewRunner
from dailies.models import Firing, ManualTrigger, SpendPolicy, TaskId, Timezone, WorkflowId
from dailies.profile import (
    Profile,
    ProfileNotFound,
    ProfileScalar,
    Sourced,
    UserSource,
    describe,
    load_profile,
    save_profile,
)
from dailies.refresh import REFRESH_TASK_ID, seed_refresh_task
from dailies.storage import state_storage
from dailies.tools import TOOLSETS
from dailies.web import web_client

AUTH_POLL_INTERVAL = 3.0
AUTH_TIMEOUT = 300.0

PROFILE_SCALARS = get_args(ProfileScalar.__value__)
PARTNER_SCALARS = ("email", "phone", "imessage_handle")
PROFILE_FIELDS = (*PROFILE_SCALARS, "timezone", *(f"partner.{sub}" for sub in PARTNER_SCALARS))


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
            return (await NangoGmailClient().profile()).email
        case unknown:
            raise KeyError(unknown)


async def await_connection(
    client: httpx.AsyncClient, *, end_user_id: str, integration: NangoIntegration
) -> NangoCredential:
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
                    return NangoCredential(
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
        credential = await await_connection(client, end_user_id=end_user_id, integration=integration)
    await credential_store().save(integration.name, credential)
    click.echo(f"Authenticated {await account_email(integration)}")


async def auth_wizard(integration: WizardIntegration) -> None:
    click.echo(integration.hint)
    values = {field.key: click.prompt(field.prompt, hide_input=field.secret) for field in integration.fields}
    await credential_store().save(integration.name, WizardCredential(values=values))
    if integration.name == "bluebubbles" and not await imessage_client().ping():
        raise click.ClickException(
            "could not reach the bluebubbles server — check the URL and password and run `dly auth bluebubbles` again"
        )
    click.echo(f"{integration.name}: configured")


@main.group()
def auth() -> None:
    """Connect external integrations."""


def auth_command(integration: Integration) -> click.Command:
    match integration:
        case NangoIntegration() as nango:

            @click.command(nango.name, help=f"Connect {nango.name} through a Nango connect link.")
            def connect() -> None:
                async def go() -> None:
                    async with lifespan():
                        await connect_integration(nango)

                anyio.run(go)

        case WizardIntegration() as wizard:

            @click.command(wizard.name, help=f"Configure {wizard.name} credentials through a guided prompt.")
            def connect() -> None:
                async def go() -> None:
                    async with lifespan():
                        await auth_wizard(wizard)

                anyio.run(go)

    return connect


for integration in INTEGRATIONS.values():
    auth.add_command(auth_command(integration))


@auth.command()
def status() -> None:
    """Show each integration's readiness, account, and dependent toolsets."""

    async def go() -> None:
        async with lifespan():
            for name, integration in INTEGRATIONS.items():
                users = ", ".join(sorted(ts.__name__ for ts in TOOLSETS if name in ts.integrations))
                suffix = f" — used by {users}" if users else ""
                if not await integration_ready(integration):
                    click.echo(f"{name}: not ready — {await unready_fix(integration)}{suffix}")
                    continue
                match integration:
                    case NangoIntegration() as nango:
                        click.echo(f"{name}: connected as {await account_email(nango)}{suffix}")
                    case WizardIntegration():
                        click.echo(f"{name}: configured{suffix}")

    anyio.run(go)


def render_profile(found: Profile) -> str:
    def entry(label: str, item: Sourced[str] | None, *, indent: str = "") -> list[str]:
        return [] if item is None else [f"{indent}{label}: {item.value}", f"{indent}  {describe(item.source)}"]

    def section(title: str, lines: list[str]) -> list[str]:
        return [title, *lines] if lines else []

    return "\n".join(
        [
            *(
                line
                for name in ("name", "email", "timezone", "phone", "imessage_handle", "home_address")
                for line in entry(name.replace("_", " "), getattr(found, name))
            ),
            *(line for name in ("birthday", "employer", "role") for line in entry(name, getattr(found, name))),
            f"partner: {found.partner.name}",
            *(
                line
                for sub in PARTNER_SCALARS
                for line in entry(sub.replace("_", " "), getattr(found.partner, sub), indent="  ")
            ),
            *section(
                "loyalty programs:",
                [
                    line
                    for program in found.loyalty_programs
                    for line in (
                        f"  {program.program} ({program.kind}"
                        f"{f', {program.status_tier}' if program.status_tier else ''}): "
                        f"{program.member_number.value}",
                        f"    {describe(program.member_number.source)}",
                    )
                ],
            ),
            *section(
                "merchants:",
                [
                    line
                    for merchant in found.merchants
                    for line in (
                        f"  {merchant.name} ({merchant.category}{f', {merchant.cadence}' if merchant.cadence else ''})",
                        f"    {describe(merchant.source)}",
                    )
                ],
            ),
            *section(
                "facts:",
                [
                    line
                    for fact in found.facts
                    for line in (f"  {fact.label}: {fact.value}", f"    {describe(fact.source)}")
                ],
            ),
        ]
    )


async def saved_profile() -> Profile | None:
    try:
        return await load_profile()
    except ProfileNotFound:
        return None


def merge_profiles(discovered: Profile, existing: Profile) -> Profile:
    partner = discovered.partner.model_copy(
        update={
            sub: value
            for sub in PARTNER_SCALARS
            if getattr(discovered.partner, sub) is None and (value := getattr(existing.partner, sub))
        }
    )
    return discovered.model_copy(
        update={"partner": partner}
        | {
            name: value
            for name in ("phone", "imessage_handle", "home_address", "birthday", "employer", "role")
            if getattr(discovered, name) is None and (value := getattr(existing, name))
        }
        | {
            name: value
            for name in ("loyalty_programs", "merchants", "facts")
            if not getattr(discovered, name) and (value := getattr(existing, name))
        }
    )


async def init_profile(*, force: bool = False) -> None:
    existing = await saved_profile()
    if existing is not None and not force:
        raise click.ClickException("a profile already exists — re-run with --force to re-mine it")
    click.echo("Mining your inbox and the web — this can take a few minutes...")
    try:
        discovered = await discover_profile(
            ClaudeAgentSDKProvider(max_turns=DISCOVERY_MAX_TURNS), gmail=gmail_client(), web=web_client()
        )
    except NotConnected as exc:
        raise click.ClickException(str(exc)) from exc
    except InterviewError as exc:
        raise click.ClickException(f"profile discovery failed: {exc} — re-run `dly profile init`") from exc
    merged = merge_profiles(discovered, existing) if existing is not None else discovered
    click.echo(render_profile(merged))
    click.confirm("Save this profile?", abort=True)
    await save_profile(merged)
    click.echo("Profile saved.")
    await seed_refresh_task()
    await activate_task(REFRESH_TASK_ID, ack_gaps=True, spend_policy=None)
    click.echo("Weekly profile refresh scheduled.")


def edit_field(saved: Profile, field: str, value: str) -> Profile:
    match field.split("."):
        case ["timezone"]:
            return saved.model_copy(update={"timezone": Sourced[Timezone](value=value, source=UserSource())})
        case [name] if name in PROFILE_SCALARS:
            return saved.model_copy(update={name: Sourced[str](value=value, source=UserSource())})
        case ["partner", sub] if sub in PARTNER_SCALARS:
            partner = saved.partner.model_copy(update={sub: Sourced[str](value=value, source=UserSource())})
            return saved.model_copy(update={"partner": partner})
        case _:
            raise click.ClickException(f"unknown field {field!r} — valid fields: {', '.join(PROFILE_FIELDS)}")


@main.group()
def profile() -> None:
    """Manage the mined user profile that personalizes workflows."""


@profile.command("init")
@click.option("--force", is_flag=True, help="Re-mine even though a profile already exists.")
def profile_init(force: bool) -> None:
    """Mine the inbox and the web into a profile, review it, and save it."""

    async def go() -> None:
        async with lifespan():
            await init_profile(force=force)

    anyio.run(go)


@profile.command("show")
def profile_show() -> None:
    """Show the saved profile, every value with its provenance."""

    async def go() -> None:
        async with lifespan():
            try:
                saved = await load_profile()
            except ProfileNotFound as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo(render_profile(saved))

    anyio.run(go)


@profile.command("edit")
@click.argument("field")
@click.argument("value")
def profile_edit(field: str, value: str) -> None:
    """Set one profile FIELD to VALUE, recorded as entered by you.

    FIELD is a scalar profile field or a dotted partner subfield such as
    partner.email; lists are re-mined via `dly profile init --force`.
    """

    async def go() -> None:
        async with lifespan():
            try:
                saved = await load_profile()
            except ProfileNotFound as exc:
                raise click.ClickException(str(exc)) from exc
            await save_profile(edit_field(saved, field, value))
            click.echo(f"{field} = {value}")

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
            if await saved_profile() is None and click.confirm(
                "No profile yet — mine your inbox to build one first?", default=True
            ):
                await init_profile()
            await run_tui(TextualPresenter(), build_interviewer(), start_interview=True)

    anyio.run(go)


@main.command()
def tasks() -> None:
    """List every task with its status, workflow count, and open gaps."""

    async def go() -> None:
        async with lifespan():
            async for task in Task.find_all():
                live = len(await latest_workflows(task.uid))
                click.echo(f"{task.uid}  {task.name} — {task.status} ({live} workflow{'s' if live != 1 else ''})")
                for gap in task.gaps:
                    click.echo(f"  gap: {gap}")

    anyio.run(go)


def dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"


@main.command()
@click.argument("task_id", type=click.UUID)
@click.option("--ack-gaps", is_flag=True, help="Acknowledge the task's open gaps and activate anyway.")
@click.option("--per-order-cap", type=int, default=None, metavar="CENTS", help="Per-order spend cap in cents.")
@click.option("--weekly-cap", type=int, default=None, metavar="CENTS", help="Weekly spend cap in cents.")
def activate(task_id: UUID, ack_gaps: bool, per_order_cap: int | None, weekly_cap: int | None) -> None:
    """Activate TASK_ID once every prerequisite is met.

    A refusal lists every unmet prerequisite at once, each with the exact
    command that fixes it. The two spend caps must be given together and
    become the task's spend policy.
    """
    if (per_order_cap is None) != (weekly_cap is None):
        raise click.UsageError("--per-order-cap and --weekly-cap must be given together")
    policy = (
        SpendPolicy(per_order_cents=per_order_cap, weekly_cents=weekly_cap)
        if per_order_cap is not None and weekly_cap is not None
        else None
    )

    async def go() -> None:
        async with lifespan():
            tid = TaskId(task_id)
            if (named := await Task.get(tid)) is None:
                raise click.ClickException(str(TaskNotFound(tid)))
            try:
                activated = await activate_task(tid, ack_gaps=ack_gaps, spend_policy=policy)
            except ActivationError as err:
                click.echo(f'Cannot activate "{named.name}":')
                for number, problem in enumerate(err.problems, start=1):
                    click.echo(f"  {number}. {problem.detail}")
                    click.echo(f"     fix: {problem.fix}")
                raise click.ClickException(str(err)) from err
            capped = (
                f"; spend capped at {dollars(policy.per_order_cents)}/order, {dollars(policy.weekly_cents)}/week."
                if policy is not None
                else ""
            )
            live = len(await latest_workflows(tid))
            click.echo(f'Activated "{activated.name}": {live} workflow{"s" if live != 1 else ""} live{capped}')
            click.echo("Next: `dly tick` runs due workflows (or wait for the scheduler).")

    anyio.run(go)
