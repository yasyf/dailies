from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import click
import httpx
import pytest
from click.testing import CliRunner

from dailies import cli, connections
from dailies.activation import ActivationError, Problem
from dailies.agent import ClaudeAgentSDKProvider
from dailies.cli import main
from dailies.connections import NangoCredential, NotConnected
from dailies.engine import Engine, TriggerFired
from dailies.gmail import GmailClient, NangoGmailClient
from dailies.interview import InterviewError
from dailies.models import Firing, ManualTrigger, SpendPolicy, TaskId, TaskStatus, WorkflowId
from dailies.profile import AccountSource, EmailSource, Profile, ProfileNotFound, Sourced, UserSource
from dailies.web import WebClient
from tests.fakes import FakeCredentialStore

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
    for command in ("run", "tick", "tui", "interview", "db", "auth", "browser", "profile", "tasks", "activate"):
        assert command in result.output


def test_db_help_lists_init() -> None:
    result = CliRunner().invoke(main, ["db", "--help"])
    assert result.exit_code == 0
    assert "init" in result.output


def test_auth_help_lists_integrations_and_status() -> None:
    result = CliRunner().invoke(main, ["auth", "--help"])
    assert result.exit_code == 0
    assert "gmail" in result.output
    assert "onepassword" in result.output
    assert "bluebubbles" in result.output
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


def test_auth_gmail_connects_persists_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANGO_SECRET_KEY", "secret")
    monkeypatch.setattr(cli, "AUTH_POLL_INTERVAL", 0.0)
    store = FakeCredentialStore()
    monkeypatch.setattr(cli, "credential_store", lambda: store)
    monkeypatch.setattr(cli, "NangoGmailClient", lambda: NangoGmailClient(credentials=store))
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
    assert store.credentials == {"gmail": NangoCredential(connection_id="conn-1", provider_config_key="google-mail")}


def test_auth_status_unready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("BLUEBUBBLES_URL", raising=False)
    monkeypatch.delenv("BLUEBUBBLES_PASSWORD", raising=False)
    monkeypatch.setattr(connections, "credential_store", lambda: FakeCredentialStore())
    result = CliRunner().invoke(main, ["auth", "status"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert "gmail: not ready — run `dly auth gmail` — used by ActionToolSet, EmailToolSet" in lines
    assert (
        "onepassword: not ready — set OP_SERVICE_ACCOUNT_TOKEN (see `dly auth onepassword`) — used by VaultToolSet"
    ) in lines
    assert "bluebubbles: not ready — set BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD (see `dly auth bluebubbles`)" in lines


def test_auth_status_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANGO_SECRET_KEY", "secret")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_token")
    monkeypatch.setenv("BLUEBUBBLES_URL", "http://mac.tailnet:1234")
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "hunter2")
    store = FakeCredentialStore(
        credentials={"gmail": NangoCredential(connection_id="conn-1", provider_config_key="google-mail")}
    )
    monkeypatch.setattr(connections, "credential_store", lambda: store)
    monkeypatch.setattr(cli, "NangoGmailClient", lambda: NangoGmailClient(credentials=store))

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/proxy/gmail/v1/users/me/profile"
        return httpx.Response(200, json={"emailAddress": "yasyfm@gmail.com"})

    mock_clients(monkeypatch, handle)
    result = CliRunner().invoke(main, ["auth", "status"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert "gmail: connected as yasyfm@gmail.com — used by ActionToolSet, EmailToolSet" in lines
    assert "onepassword: ready — used by VaultToolSet" in lines
    assert "bluebubbles: ready" in lines


def test_auth_onepassword_unset_prints_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    result = CliRunner().invoke(main, ["auth", "onepassword"])
    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert "To set up onepassword:" in lines
    assert "  1. create a 1Password service account with read access to your vaults and copy its token" in lines
    assert "  2. set OP_SERVICE_ACCOUNT_TOKEN" in lines
    assert "Error: onepassword is not ready" in result.stderr


def test_auth_onepassword_set_reports_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_token")
    result = CliRunner().invoke(main, ["auth", "onepassword"])
    assert result.exit_code == 0
    assert result.output == "onepassword: ready\n"


def test_auth_bluebubbles_unset_prints_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLUEBUBBLES_URL", raising=False)
    monkeypatch.delenv("BLUEBUBBLES_PASSWORD", raising=False)
    result = CliRunner().invoke(main, ["auth", "bluebubbles"])
    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert "To set up bluebubbles:" in lines
    hint = "pair a BlueBubbles server on a Mac (e.g. reachable over Tailscale) and copy its URL and password"
    assert f"  1. {hint}" in lines
    assert "  2. set BLUEBUBBLES_URL" in lines
    assert "  3. set BLUEBUBBLES_PASSWORD" in lines
    assert "Error: bluebubbles is not ready" in result.stderr


def test_auth_bluebubbles_set_reports_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLUEBUBBLES_URL", "http://mac.tailnet:1234")
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "hunter2")
    result = CliRunner().invoke(main, ["auth", "bluebubbles"])
    assert result.exit_code == 0
    assert result.output == "bluebubbles: ready\n"


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


def user_sourced(value: str) -> Sourced[str]:
    return Sourced[str](value=value, source=UserSource())


DISCOVERED = Profile(
    name=Sourced[str](
        value="Yasyf",
        source=EmailSource(
            message_id="m1", sender="Yasyf <y@example.com>", subject="Re: hi", date=datetime(2026, 5, 12, tzinfo=UTC)
        ),
    ),
    email=Sourced[str](value="y@example.com", source=AccountSource(detail="the connected gmail account")),
)


def patch_profile_io(
    monkeypatch: pytest.MonkeyPatch, *, existing: Profile | None, discovered: Profile | None = None
) -> tuple[list[Profile], list[ClaudeAgentSDKProvider]]:
    saved: list[Profile] = []
    providers: list[ClaudeAgentSDKProvider] = []

    async def fake_load() -> Profile:
        if existing is None:
            raise ProfileNotFound
        return existing

    async def fake_save(profile: Profile) -> None:
        saved.append(profile)

    async def fake_discover(provider: ClaudeAgentSDKProvider, *, gmail: GmailClient, web: WebClient) -> Profile:
        providers.append(provider)
        assert discovered is not None, "discover_profile must not run in this scenario"
        return discovered

    monkeypatch.setattr(cli, "load_profile", fake_load)
    monkeypatch.setattr(cli, "save_profile", fake_save)
    monkeypatch.setattr(cli, "discover_profile", fake_discover)
    return saved, providers


def patch_run_tui(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def record(*args: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli, "run_tui", record)
    return calls


def test_interview_with_profile_skips_mining(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_profile_io(monkeypatch, existing=DISCOVERED)
    calls = patch_run_tui(monkeypatch)
    result = CliRunner().invoke(main, ["interview"])
    assert result.exit_code == 0
    assert "No profile yet" not in result.output
    assert calls == [{"start_interview": True}]


def test_interview_offers_mining_and_proceeds_on_decline(monkeypatch: pytest.MonkeyPatch) -> None:
    saved, providers = patch_profile_io(monkeypatch, existing=None)
    calls = patch_run_tui(monkeypatch)
    result = CliRunner().invoke(main, ["interview"], input="n\n")
    assert result.exit_code == 0
    assert "No profile yet — mine your inbox to build one first?" in result.output
    assert (saved, providers) == ([], [])
    assert calls == [{"start_interview": True}]


def test_interview_mines_inline_when_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    saved, _ = patch_profile_io(monkeypatch, existing=None, discovered=DISCOVERED)
    calls = patch_run_tui(monkeypatch)
    result = CliRunner().invoke(main, ["interview"], input="y\ny\n")
    assert result.exit_code == 0
    assert "Mining your inbox and the web — this can take a few minutes..." in result.output
    assert saved == [DISCOVERED]
    assert calls == [{"start_interview": True}]


def test_profile_init_mines_reviews_and_saves(monkeypatch: pytest.MonkeyPatch) -> None:
    saved, providers = patch_profile_io(monkeypatch, existing=None, discovered=DISCOVERED)
    result = CliRunner().invoke(main, ["profile", "init"], input="y\n")
    assert result.exit_code == 0
    assert "Mining your inbox and the web — this can take a few minutes..." in result.output
    assert "name: Yasyf" in result.output
    assert "found in email from Yasyf <y@example.com>, May 12, 2026 ('Re: hi')" in result.output
    assert "email: y@example.com" in result.output
    assert "from the connected gmail account" in result.output
    assert "partner: Rebecca" in result.output
    assert "Save this profile?" in result.output
    assert "Profile saved." in result.output
    assert saved == [DISCOVERED]
    assert [type(provider) for provider in providers] == [ClaudeAgentSDKProvider]
    assert providers[0].max_turns == 80


def test_profile_init_declined_save_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    saved, _ = patch_profile_io(monkeypatch, existing=None, discovered=DISCOVERED)
    result = CliRunner().invoke(main, ["profile", "init"], input="n\n")
    assert result.exit_code == 1
    assert saved == []


def test_profile_init_refuses_existing_without_force(monkeypatch: pytest.MonkeyPatch) -> None:
    saved, providers = patch_profile_io(monkeypatch, existing=DISCOVERED)
    result = CliRunner().invoke(main, ["profile", "init"])
    assert result.exit_code == 1
    assert "a profile already exists — re-run with --force to re-mine it" in result.stderr
    assert (saved, providers) == ([], [])


def test_profile_init_force_keeps_existing_values_discovery_missed(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = DISCOVERED.model_copy(update={"phone": user_sourced("+1 415 555 0100")})
    rediscovered = DISCOVERED.model_copy(update={"home_address": user_sourced("123 Mission St")})
    saved, _ = patch_profile_io(monkeypatch, existing=existing, discovered=rediscovered)
    result = CliRunner().invoke(main, ["profile", "init", "--force"], input="y\n")
    assert result.exit_code == 0
    assert saved == [rediscovered.model_copy(update={"phone": user_sourced("+1 415 555 0100")})]


def test_profile_init_not_connected_names_auth_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_profile_io(monkeypatch, existing=None)

    async def boom(provider: ClaudeAgentSDKProvider, *, gmail: GmailClient, web: WebClient) -> Profile:
        raise NotConnected("gmail")

    monkeypatch.setattr(cli, "discover_profile", boom)
    result = CliRunner().invoke(main, ["profile", "init"])
    assert result.exit_code == 1
    assert "gmail is not connected — run `dly auth gmail` first" in result.stderr


def test_profile_init_interview_error_suggests_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_profile_io(monkeypatch, existing=None)

    async def boom(provider: ClaudeAgentSDKProvider, *, gmail: GmailClient, web: WebClient) -> Profile:
        raise InterviewError("agent never submitted")

    monkeypatch.setattr(cli, "discover_profile", boom)
    result = CliRunner().invoke(main, ["profile", "init"])
    assert result.exit_code == 1
    assert "profile discovery failed: agent never submitted — re-run `dly profile init`" in result.stderr


def test_profile_show_renders_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_profile_io(monkeypatch, existing=DISCOVERED)
    result = CliRunner().invoke(main, ["profile", "show"])
    assert result.exit_code == 0
    assert "name: Yasyf" in result.output
    assert "found in email from Yasyf <y@example.com>, May 12, 2026 ('Re: hi')" in result.output
    assert "timezone:" in result.output
    assert "partner: Rebecca" in result.output


def test_profile_show_without_profile_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_profile_io(monkeypatch, existing=None)
    result = CliRunner().invoke(main, ["profile", "show"])
    assert result.exit_code == 1
    assert "no profile saved — run `dly profile init` first" in result.stderr


def test_profile_edit_sets_user_source(monkeypatch: pytest.MonkeyPatch) -> None:
    saved, _ = patch_profile_io(monkeypatch, existing=DISCOVERED)
    result = CliRunner().invoke(main, ["profile", "edit", "phone", "+1 415 555 0100"])
    assert result.exit_code == 0
    assert saved == [DISCOVERED.model_copy(update={"phone": user_sourced("+1 415 555 0100")})]


def test_profile_edit_partner_subfield(monkeypatch: pytest.MonkeyPatch) -> None:
    saved, _ = patch_profile_io(monkeypatch, existing=DISCOVERED)
    result = CliRunner().invoke(main, ["profile", "edit", "partner.email", "rebecca@example.com"])
    assert result.exit_code == 0
    partner = DISCOVERED.partner.model_copy(update={"email": user_sourced("rebecca@example.com")})
    assert saved == [DISCOVERED.model_copy(update={"partner": partner})]


def test_profile_edit_unknown_field_lists_valid_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    saved, _ = patch_profile_io(monkeypatch, existing=DISCOVERED)
    result = CliRunner().invoke(main, ["profile", "edit", "shoe_size", "12"])
    assert result.exit_code == 1
    assert "unknown field 'shoe_size'" in result.stderr
    assert "partner.email" in result.stderr
    assert "home_address" in result.stderr
    assert saved == []


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


@dataclass(frozen=True)
class StubTask:
    uid: UUID
    name: str
    status: TaskStatus = "draft"
    gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class FakeTasks:
    documents: tuple[StubTask, ...]

    def find_all(self) -> AsyncIterator[StubTask]:
        async def iterate() -> AsyncIterator[StubTask]:
            for document in self.documents:
                yield document

        return iterate()

    async def get(self, task_id: UUID) -> StubTask | None:
        return next((document for document in self.documents if document.uid == task_id), None)


def patch_activation(
    monkeypatch: pytest.MonkeyPatch, documents: tuple[StubTask, ...], counts: dict[UUID, int]
) -> None:
    async def fake_latest(task_id: TaskId) -> list[object]:
        return [object()] * counts[task_id]

    monkeypatch.setattr(cli, "Task", FakeTasks(documents))
    monkeypatch.setattr(cli, "latest_workflows", fake_latest)


def patch_activate_task(
    monkeypatch: pytest.MonkeyPatch, *, result: StubTask | None = None, error: ActivationError | None = None
) -> dict[str, object]:
    captured: dict[str, object] = {}

    async def fake_activate(task_id: TaskId, *, ack_gaps: bool, spend_policy: SpendPolicy | None) -> StubTask:
        captured.update(task_id=task_id, ack_gaps=ack_gaps, spend_policy=spend_policy)
        if error is not None:
            raise error
        assert result is not None, "activate_task must not succeed in this scenario"
        return result

    monkeypatch.setattr(cli, "activate_task", fake_activate)
    return captured


def test_tasks_lists_ids_names_status_and_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = StubTask(uid=uuid4(), name="Digest", status="active")
    chaser = StubTask(uid=uuid4(), name="Mileage credit chaser", gaps=("push notifications to a phone",))
    patch_activation(monkeypatch, (digest, chaser), {digest.uid: 2, chaser.uid: 1})
    result = CliRunner().invoke(main, ["tasks"])
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        f"{digest.uid}  Digest — active (2 workflows)",
        f"{chaser.uid}  Mileage credit chaser — draft (1 workflow)",
        "  gap: push notifications to a phone",
    ]


def test_activate_success_reports_caps_and_next_step(monkeypatch: pytest.MonkeyPatch) -> None:
    chaser = StubTask(uid=uuid4(), name="Mileage credit chaser")
    patch_activation(monkeypatch, (chaser,), {chaser.uid: 2})
    captured = patch_activate_task(monkeypatch, result=chaser)
    result = CliRunner().invoke(
        main, ["activate", str(chaser.uid), "--per-order-cap", "2000", "--weekly-cap", "10000"]
    )
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        'Activated "Mileage credit chaser": 2 workflows live; spend capped at $20.00/order, $100.00/week.',
        "Next: `dly tick` runs due workflows (or wait for the scheduler).",
    ]
    assert captured == {
        "task_id": chaser.uid,
        "ack_gaps": False,
        "spend_policy": SpendPolicy(per_order_cents=2000, weekly_cents=10_000),
    }


def test_activate_without_caps_omits_spend_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    chaser = StubTask(uid=uuid4(), name="Mileage credit chaser")
    patch_activation(monkeypatch, (chaser,), {chaser.uid: 1})
    captured = patch_activate_task(monkeypatch, result=chaser)
    result = CliRunner().invoke(main, ["activate", str(chaser.uid), "--ack-gaps"])
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        'Activated "Mileage credit chaser": 1 workflow live',
        "Next: `dly tick` runs due workflows (or wait for the scheduler).",
    ]
    assert captured == {"task_id": chaser.uid, "ack_gaps": True, "spend_policy": None}


def test_activate_failure_prints_numbered_problems_and_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    chaser = StubTask(uid=uuid4(), name="Mileage credit chaser")
    patch_activation(monkeypatch, (chaser,), {})
    patch_activate_task(
        monkeypatch,
        error=ActivationError(
            [
                Problem(
                    detail="unacknowledged gap: push notifications to a phone",
                    fix="review it, then re-run with --ack-gaps",
                ),
                Problem(detail="profile is not seeded", fix="run `dly profile init`"),
                Problem(
                    detail="integration onepassword is not ready",
                    fix="set OP_SERVICE_ACCOUNT_TOKEN (see `dly auth onepassword`)",
                ),
            ]
        ),
    )
    result = CliRunner().invoke(main, ["activate", str(chaser.uid)])
    assert result.exit_code == 1
    assert result.output.splitlines() == [
        'Cannot activate "Mileage credit chaser":',
        "  1. unacknowledged gap: push notifications to a phone",
        "     fix: review it, then re-run with --ack-gaps",
        "  2. profile is not seeded",
        "     fix: run `dly profile init`",
        "  3. integration onepassword is not ready",
        "     fix: set OP_SERVICE_ACCOUNT_TOKEN (see `dly auth onepassword`)",
        "Error: 3 problems block activation",
    ]
    assert result.stderr == "Error: 3 problems block activation\n"


def test_activate_lone_per_order_cap_is_usage_error() -> None:
    result = CliRunner().invoke(main, ["activate", str(uuid4()), "--per-order-cap", "2000"])
    assert result.exit_code == 2
    assert "--per-order-cap and --weekly-cap must be given together" in result.stderr


def test_activate_unknown_task_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_activation(monkeypatch, (), {})
    unknown = uuid4()
    result = CliRunner().invoke(main, ["activate", str(unknown)])
    assert result.exit_code == 1
    assert f"Error: no task {unknown} — run `dly tasks` to list tasks" in result.stderr
