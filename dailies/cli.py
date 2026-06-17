from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, get_args
from uuid import UUID, uuid4

import anyio
import click
import httpx
from rich.console import Group
from rich.text import Text

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
from dailies.interface.console import Glyphs, KvRow, confirm, console, kv_table, panel, status, step, success
from dailies.interface.profile_view import MiningDashboard, profile_panel
from dailies.interview import InterviewError, InterviewRunner
from dailies.models import Firing, ManualTrigger, SpendPolicy, TaskId, Timezone, WorkflowId
from dailies.profile import (
    Profile,
    ProfileNotFound,
    ProfileScalar,
    Sourced,
    UserSource,
    load_profile,
    save_profile,
)
from dailies.refresh import REFRESH_TASK_ID, seed_refresh_task
from dailies.storage import state_storage
from dailies.tools import TOOLSETS
from dailies.web import web_client

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rich.panel import Panel

    from dailies.models import TaskStatus

AUTH_POLL_INTERVAL = 3.0
AUTH_TIMEOUT = 300.0

PROFILE_SCALARS = get_args(ProfileScalar.__value__)
PARTNER_SCALARS = ("email", "phone", "imessage_handle")
PROFILE_FIELDS = (*PROFILE_SCALARS, "timezone", *(f"partner.{sub}" for sub in PARTNER_SCALARS))
TASK_STATUS_STYLES: Mapping[TaskStatus, str] = {"draft": "muted", "active": "success", "inactive": "warning"}
TASK_STATUS_GLYPHS: Mapping[TaskStatus, str] = {
    "draft": Glyphs.PENDING,
    "active": Glyphs.SUCCESS,
    "inactive": Glyphs.WARN,
}


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
    con = console()

    async def go() -> None:
        with status(con, "Connecting to MongoDB and building indexes…"):
            async with lifespan():
                pass
        con.print(success("Database initialised."))

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
    con = console()
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
        con.print(
            step(
                f"Complete the connection in your browser: {(link := session['data']['connect_link'])}",
                glyph=Glyphs.WEB,
            )
        )
        click.launch(link)
        with status(con, f"Waiting for the {integration.name} connection…"):
            credential = await await_connection(client, end_user_id=end_user_id, integration=integration)
    await credential_store().save(integration.name, credential)
    con.print(success(f"Authenticated {await account_email(integration)}"))


async def auth_wizard(integration: WizardIntegration) -> None:
    con = console()
    con.print(step(integration.hint))
    values = {field.key: click.prompt(field.prompt, hide_input=field.secret) for field in integration.fields}
    await credential_store().save(integration.name, WizardCredential(values=values))
    if integration.name == "bluebubbles" and not await imessage_client().ping():
        raise click.ClickException(
            "could not reach the bluebubbles server — check the URL and password and run `dly auth bluebubbles` again"
        )
    con.print(success(f"{integration.name}: configured"))


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


async def status_row(name: str, integration: Integration) -> KvRow:
    users = ", ".join(sorted(ts.__name__ for ts in TOOLSETS if name in ts.integrations))
    note = f"used by {users}" if users else None
    if not await integration_ready(integration):
        return KvRow(
            label=name,
            value=f"not ready — {await unready_fix(integration)}",
            note=note,
            value_style="warning",
            glyph=Glyphs.ERROR,
        )
    match integration:
        case NangoIntegration() as nango:
            value = f"connected as {await account_email(nango)}"
        case WizardIntegration():
            value = "configured"
    return KvRow(label=name, value=value, note=note, value_style="success", glyph=Glyphs.SUCCESS)


@auth.command("status")
def auth_status() -> None:
    """Show each integration's readiness, account, and dependent toolsets."""
    con = console()

    async def go() -> None:
        async with lifespan():
            with status(con, "Checking integrations…"):
                rows = [await status_row(name, integration) for name, integration in INTEGRATIONS.items()]
        con.print(kv_table(rows))

    anyio.run(go)


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
    con = console()
    con.print(step("Mining your inbox and the web — this can take a few minutes...", glyph=Glyphs.SEARCH))
    with MiningDashboard(con) as dash:
        try:
            discovered = await discover_profile(
                ClaudeAgentSDKProvider(max_turns=DISCOVERY_MAX_TURNS),
                gmail=gmail_client(),
                web=web_client(),
                listener=dash.on_event,
            )
        except NotConnected as exc:
            raise click.ClickException(str(exc)) from exc
        except InterviewError as exc:
            raise click.ClickException(f"profile discovery failed: {exc} — re-run `dly profile init`") from exc
    merged = merge_profiles(discovered, existing) if existing is not None else discovered
    con.print(profile_panel(merged))
    confirm("Save this profile?", abort=True)
    await save_profile(merged)
    con.print(success("Profile saved."))
    await seed_refresh_task()
    await activate_task(REFRESH_TASK_ID, ack_gaps=True, spend_policy=None)
    con.print(success("Weekly profile refresh scheduled.", glyph=Glyphs.SCHEDULE))


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
            console().print(profile_panel(saved))

    anyio.run(go)


@profile.command("edit")
@click.argument("field")
@click.argument("value")
def profile_edit(field: str, value: str) -> None:
    """Set one profile FIELD to VALUE, recorded as entered by you.

    FIELD is a scalar profile field or a dotted partner subfield such as
    partner.email; lists are re-mined via `dly profile init --force`.
    """

    con = console()

    async def go() -> None:
        async with lifespan():
            try:
                saved = await load_profile()
            except ProfileNotFound as exc:
                raise click.ClickException(str(exc)) from exc
            await save_profile(edit_field(saved, field, value))
            con.print(success(f"{field} = {value}"))

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

    con = console()

    async def go() -> None:
        with status(con, f"Importing cookies into workflow {workflow_id}…"):
            count = await import_cookies(
                state_storage(),
                WorkflowId(workflow_id),
                domains=domains,
                source_browser=from_browser,
                from_file=from_file,
            )
        con.print(success(f"Imported {count} cookies into workflow {workflow_id}"))

    anyio.run(go)


@main.command()
@click.argument("workflow_id", type=click.UUID)
def run(workflow_id: UUID) -> None:
    """Fire a single manual run of the workflow with the given id."""
    con = console()

    async def go() -> None:
        with status(con, f"Dispatching a manual run of workflow {workflow_id}…"):
            async with lifespan():
                await Engine().dispatch(TriggerFired(WorkflowId(workflow_id), [Firing(trigger=ManualTrigger())]))
        con.print(success(f"Dispatched a manual run of workflow {workflow_id}."))

    anyio.run(go)


@main.command()
def tick() -> None:
    """Sweep cron-due workflows, then poll event subscriptions for news.

    Safe to overlap: each workflow is processed under a short MongoDB lease, so a
    concurrent tick — on this host or another sharing the database — skips
    in-flight workflows and processes the rest. Drive it from cron or launchd on
    a ~1-minute cadence.
    """

    con = console()

    async def go() -> None:
        with status(con, "Sweeping cron-due workflows and polling subscriptions…"):
            async with lifespan():
                runs = await Engine().tick(now=datetime.now(UTC))
        con.print(success(f"Swept {len(runs)} run{'s' if len(runs) != 1 else ''}."))

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


def task_line(task: Task, live: int) -> Text:
    return Text.assemble(
        (f"{TASK_STATUS_GLYPHS[task.status]} ", TASK_STATUS_STYLES[task.status]),
        (f"{task.uid}  {task.name} — ", "foreground"),
        (task.status, TASK_STATUS_STYLES[task.status]),
        (f" ({live} workflow{'s' if live != 1 else ''})", "muted"),
    )


@main.command()
def tasks() -> None:
    """List every task with its status, workflow count, and open gaps."""
    con = console()

    async def go() -> None:
        async with lifespan():
            async for task in Task.find_all():
                con.print(task_line(task, len(await latest_workflows(task.uid))))
                for gap in task.gaps:
                    con.print(Text(f"  {Glyphs.WARN} {gap}", style="warning"))

    anyio.run(go)


def dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"


def activation_panel(name: str, err: ActivationError) -> Panel:
    return panel(
        Group(
            *(
                renderable
                for number, problem in enumerate(err.problems, start=1)
                for renderable in (
                    Text.assemble((f"{number}. ", "error"), (problem.detail, "foreground")),
                    Text(f"   fix: {problem.fix}", style="muted"),
                )
            )
        ),
        title=f'Cannot activate "{name}"',
        glyph=Glyphs.ERROR,
        style="error",
    )


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
    con = console()

    async def go() -> None:
        async with lifespan():
            tid = TaskId(task_id)
            if (named := await Task.get(tid)) is None:
                raise click.ClickException(str(TaskNotFound(tid)))
            try:
                activated = await activate_task(tid, ack_gaps=ack_gaps, spend_policy=policy)
            except ActivationError as err:
                con.print(activation_panel(named.name, err))
                raise click.ClickException(str(err)) from err
            capped = (
                f"; spend capped at {dollars(policy.per_order_cents)}/order, {dollars(policy.weekly_cents)}/week."
                if policy is not None
                else ""
            )
            live = len(await latest_workflows(tid))
            con.print(success(f'Activated "{activated.name}": {live} workflow{"s" if live != 1 else ""} live{capped}'))
            con.print(step("Next: `dly tick` runs due workflows (or wait for the scheduler)."))

    anyio.run(go)
