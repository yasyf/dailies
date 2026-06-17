"""In-memory presenter stand-ins for the Docker-free TUI test.

Beanie 2.x requires ``init_beanie`` before a Document can even be constructed, so the
TUI fake uses lightweight rows (carrying real StatusUpdate/Action value objects) to
keep the pilot test independent of MongoDB.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import anyio
from pydantic import JsonValue

from dailies.agent import AgentRequest, AgentResult
from dailies.bluebubbles import MessageSendFailed, SentMessage
from dailies.connections import Credential, NotConnected
from dailies.gmail import SEARCH_LIMIT, GmailProfile, MessageMeta, SentEmail, ThreadNotFound
from dailies.gmail import EmailMessage as GmailMessage
from dailies.interface.presenter import BlastRadius
from dailies.models import (
    Action,
    CronExpr,
    CronTrigger,
    EventTrigger,
    Firing,
    PromptStr,
    RunStatus,
    SchemaStr,
    StatusUpdate,
    TaskDefinition,
    TaskId,
    TaskStatus,
    TextBlock,
    Trigger,
    WorkflowDefinition,
    WorkflowId,
    utcnow,
)
from dailies.onepassword import Login, VaultLookupFailed
from dailies.state import StateDump
from dailies.web import SEARCH_RESULTS, SearchResult


@dataclass(frozen=True, slots=True)
class FakeProvider:
    result: AgentResult
    requests: list[AgentRequest] = field(default_factory=list)

    async def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return self.result


@dataclass(frozen=True, slots=True)
class ScriptedProvider:
    results: list[AgentResult]
    requests: list[AgentRequest] = field(default_factory=list)

    async def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return self.results.pop(0)


@dataclass(frozen=True, slots=True)
class ToolScriptedProvider:
    """Fake provider that drives the request's submit tool with scripted args, like a real tool-calling provider."""

    calls: list[dict[str, JsonValue]]
    requests: list[AgentRequest] = field(default_factory=list)

    async def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        await next(spec for spec in request.tools if spec.name == "submit").invoke(self.calls.pop(0))
        return AgentResult(text="", ok=True)


@dataclass(frozen=True, slots=True)
class ToolDrivingProvider:
    """Fake provider that walks a script of named tool calls against the request's tools, capturing each result."""

    script: list[tuple[str, dict[str, JsonValue]]]
    outputs: list[object] = field(default_factory=list)
    requests: list[AgentRequest] = field(default_factory=list)

    async def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        specs = {spec.name: spec for spec in request.tools}
        self.outputs.extend([await specs[name].invoke(args) for name, args in self.script])
        return AgentResult(text="", ok=True)


@dataclass(frozen=True, slots=True)
class SlowProvider:
    delay: float

    async def run(self, request: AgentRequest) -> AgentResult:
        await anyio.sleep(self.delay)
        return AgentResult("done", ok=True)


@dataclass(frozen=True, slots=True)
class BlockingProvider:
    started: anyio.Event
    release: anyio.Event

    async def run(self, request: AgentRequest) -> AgentResult:
        self.started.set()
        await self.release.wait()
        return AgentResult("done", ok=True)


@dataclass(frozen=True, slots=True)
class InjectingProvider:
    """Provider that lands a new matching message while the run executes."""

    gmail: FakeGmail
    message: GmailMessage

    async def run(self, request: AgentRequest) -> AgentResult:
        self.gmail.add(self.message)
        return AgentResult("done", ok=True)


@dataclass(frozen=True, slots=True)
class FakeTask:
    name: str
    uid: TaskId
    definition: TaskDefinition = field(
        default_factory=lambda: TaskDefinition(user_input="i", description="d", prompt=PromptStr("p"))
    )
    shared_ddl: SchemaStr | None = None
    status: TaskStatus = "active"


@dataclass(frozen=True, slots=True)
class FakeWorkflow:
    name: str
    version: int
    workflow_id: WorkflowId
    definition: WorkflowDefinition
    ddl: SchemaStr
    status: TaskStatus
    triggers: list[Trigger]


@dataclass(frozen=True, slots=True)
class FakeRun:
    status: RunStatus
    created_at: datetime
    uid: UUID
    workflow_id: WorkflowId
    fired_by: list[Firing]
    status_updates: list[StatusUpdate]
    actions: list[Action]


class FakePresenter:
    def __init__(self) -> None:
        self.workflow_id = WorkflowId(uuid4())
        self.task = FakeTask(
            name="Daily digest",
            uid=TaskId(uuid4()),
            definition=TaskDefinition(
                user_input="email me a digest", description="Send a daily digest", prompt=PromptStr("summarize the day")
            ),
            shared_ddl=SchemaStr("CREATE TABLE totals (sent INTEGER)"),
            status="active",
        )
        self.workflow = FakeWorkflow(
            name="digest-workflow",
            version=1,
            workflow_id=self.workflow_id,
            definition=WorkflowDefinition(
                summary="Sends the digest each morning", prompt=PromptStr("send the digest"), rules=["be brief"]
            ),
            ddl=SchemaStr("CREATE TABLE sent (day TEXT)"),
            status="active",
            triggers=[
                CronTrigger(cron_expression=CronExpr("0 9 * * *")),
                EventTrigger(
                    source="gmail",
                    event="query",
                    key="from:digest@example.com OR subject:(daily digest OR weekly roundup) newer_than:7d",
                ),
            ],
        )
        self.run = FakeRun(
            status="succeeded",
            created_at=utcnow(),
            uid=uuid4(),
            workflow_id=self.workflow_id,
            fired_by=[Firing(trigger=CronTrigger(cron_expression=CronExpr("0 9 * * *")))],
            status_updates=[StatusUpdate(title="started", blocks=[TextBlock(text="hello world")])],
            actions=[Action(kind="email", target="user@example.com")],
        )
        self.tasks: list[FakeTask] = [self.task]
        self.deleted: list[TaskId] = []
        self.state: StateDump = {"sent": [{"day": "2026-06-09"}, {"day": "2026-06-10"}], "queue": []}
        self.task_state: StateDump = {"totals": [{"sent": 7}]}

    async def list_tasks(self) -> Sequence[FakeTask]:
        return self.tasks

    async def get_task(self, task_id: TaskId) -> FakeTask:
        return self.task

    async def list_workflows(self, task_id: TaskId) -> Sequence[FakeWorkflow]:
        return [self.workflow]

    async def list_runs(self, workflow_id: WorkflowId) -> Sequence[FakeRun]:
        return [self.run]

    async def get_run(self, run_id: UUID) -> FakeRun:
        return self.run

    async def get_state(self, workflow_id: WorkflowId) -> StateDump:
        return self.state

    async def get_task_state(self, task_id: TaskId) -> StateDump:
        return self.task_state

    async def blast_radius(self, task_id: TaskId) -> BlastRadius:
        return BlastRadius(workflows=1, runs=1)

    async def delete_task(self, task_id: TaskId) -> None:
        self.deleted.append(task_id)
        self.tasks = [task for task in self.tasks if task.uid != task_id]


def email_meta(message: GmailMessage) -> MessageMeta:
    return MessageMeta(id=message.id, thread_id=message.thread_id, date=message.date)


@dataclass(frozen=True, slots=True)
class FakeGmail:
    """In-memory GmailClient: mutate ``messages`` mid-test to simulate arriving mail.

    Queries match by substring against sender, subject, and body; threads group
    messages by ``thread_id`` and raise ``ThreadNotFound`` when no message carries it.
    """

    messages: dict[str, GmailMessage] = field(default_factory=dict)
    sent: list[GmailMessage] = field(default_factory=list)
    address: str = "fake@example.com"

    def add(self, message: GmailMessage) -> None:
        self.messages[message.id] = message

    def matching(self, query: str) -> list[GmailMessage]:
        return sorted(
            (m for m in self.messages.values() if query in m.sender or query in m.subject or query in m.body),
            key=lambda m: m.date,
        )

    def in_thread(self, thread_id: str) -> list[GmailMessage]:
        if not (found := sorted((m for m in self.messages.values() if m.thread_id == thread_id), key=lambda m: m.date)):
            raise ThreadNotFound(thread_id)
        return found

    async def search(self, query: str, *, limit: int = SEARCH_LIMIT) -> list[GmailMessage]:
        return self.matching(query)[:limit]

    async def message(self, message_id: str) -> GmailMessage:
        return self.messages[message_id]

    async def thread(self, thread_id: str) -> list[GmailMessage]:
        return self.in_thread(thread_id)

    async def thread_metas(self, thread_id: str) -> list[MessageMeta]:
        return [email_meta(m) for m in self.in_thread(thread_id)]

    async def query_metas(self, query: str, *, after: datetime) -> list[MessageMeta]:
        return [email_meta(m) for m in self.matching(query) if m.date > after]

    async def send(self, *, to: str, subject: str, body: str) -> SentEmail:
        message = GmailMessage(
            id=f"sent-{len(self.sent)}",
            thread_id=f"sent-thread-{len(self.sent)}",
            sender=self.address,
            to=to,
            subject=subject,
            body=body,
            date=utcnow(),
        )
        self.sent.append(message)
        return SentEmail(message_id=message.id, thread_id=message.thread_id)

    async def profile(self) -> GmailProfile:
        return GmailProfile(email=self.address)


@dataclass(frozen=True, slots=True)
class FakeIMessage:
    """In-memory IMessageClient: records sends as ``(to, text)``; ``fail`` simulates a BlueBubbles outage."""

    sent: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    async def send(self, *, to: str, text: str) -> SentMessage:
        if self.fail:
            raise MessageSendFailed(f"BlueBubbles returned 500 sending to {to}: outage")
        self.sent.append((to, text))
        return SentMessage(guid=f"fake-{len(self.sent)}")

    async def ping(self) -> bool:
        return not self.fail


@dataclass(frozen=True, slots=True)
class FakeWeb:
    """In-memory WebClient: serves ``pages`` by url, returns canned ``results``, records scrapes."""

    pages: dict[str, str] = field(default_factory=dict)
    results: list[SearchResult] = field(default_factory=list)
    scraped: list[tuple[str, str]] = field(default_factory=list)

    async def search(self, query: str, *, limit: int = SEARCH_RESULTS) -> list[SearchResult]:
        return self.results[:limit]

    async def fetch(self, url: str) -> str:
        return self.pages[url]

    async def scrape(self, url: str, instruction: str) -> str:
        self.scraped.append((url, instruction))
        return f"scraped {url}"


@dataclass(frozen=True, slots=True)
class FakeBrowser:
    tasks: list[str] = field(default_factory=list)
    profiles: list[Path] = field(default_factory=list)
    result: str = "done"

    async def browse(self, task: str, *, profile: Path) -> str:
        self.tasks.append(task)
        self.profiles.append(profile)
        profile.write_text(json.dumps({"cookies": [], "origins": []}))
        return self.result


@dataclass(frozen=True, slots=True)
class FakeVault:
    """In-memory VaultClient: serves ``logins`` by item name; unknown items raise VaultLookupFailed."""

    logins: dict[str, Login] = field(default_factory=dict)
    fetched: list[str] = field(default_factory=list)

    async def get_login(self, item: str) -> Login:
        self.fetched.append(item)
        if item not in self.logins:
            raise VaultLookupFailed(item, f'"{item}" isn\'t an item in any vault')
        return self.logins[item]


@dataclass(frozen=True, slots=True)
class FakeCredentialStore:
    credentials: dict[str, Credential] = field(default_factory=dict)

    async def load(self, name: str) -> Credential:
        if name not in self.credentials:
            raise NotConnected(name)
        return self.credentials[name]

    async def save(self, name: str, credential: Credential) -> None:
        self.credentials[name] = credential
