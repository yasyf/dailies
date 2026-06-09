"""In-memory presenter stand-ins for the Docker-free TUI test.

Beanie 2.x requires ``init_beanie`` before a Document can even be constructed, so the
TUI fake uses lightweight rows (carrying real StatusUpdate/Action value objects) to
keep the pilot test independent of MongoDB.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue

from dailies.agent import AgentRequest, AgentResult
from dailies.models import (
    Action,
    CronExpr,
    CronTrigger,
    PromptStr,
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
class FakeTask:
    name: str
    uid: TaskId
    definition: TaskDefinition = field(
        default_factory=lambda: TaskDefinition(user_input="i", description="d", prompt=PromptStr("p"))
    )
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
    status: str
    created_at: datetime
    uid: UUID
    workflow_id: WorkflowId
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
            status="active",
        )
        self.workflow = FakeWorkflow(
            name="digest-workflow",
            version=1,
            workflow_id=self.workflow_id,
            definition=WorkflowDefinition(prompt=PromptStr("send the digest"), rules=["be brief"]),
            ddl=SchemaStr("CREATE TABLE sent (day TEXT)"),
            status="active",
            triggers=[CronTrigger(cron_expression=CronExpr("0 9 * * *"))],
        )
        self.run = FakeRun(
            status="succeeded",
            created_at=utcnow(),
            uid=uuid4(),
            workflow_id=self.workflow_id,
            status_updates=[StatusUpdate(title="started", blocks=[TextBlock(text="hello world")])],
            actions=[Action(kind="email", target="user@example.com")],
        )

    async def list_tasks(self) -> Sequence[FakeTask]:
        return [self.task]

    async def get_task(self, task_id: TaskId) -> FakeTask:
        return self.task

    async def list_workflows(self, task_id: TaskId) -> Sequence[FakeWorkflow]:
        return [self.workflow]

    async def list_runs(self, workflow_id: WorkflowId) -> Sequence[FakeRun]:
        return [self.run]

    async def get_run(self, run_id: UUID) -> FakeRun:
        return self.run

    async def get_state(self, workflow_id: WorkflowId) -> Mapping[str, JsonValue]:
        return {"processed": 3, "last": "ok"}
