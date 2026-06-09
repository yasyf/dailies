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
from dailies.models import Action, StatusUpdate, TaskId, TextBlock, WorkflowId, utcnow


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


@dataclass(frozen=True, slots=True)
class FakeWorkflow:
    name: str
    version: int
    workflow_id: WorkflowId


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
        self.task = FakeTask(name="Daily digest", uid=TaskId(uuid4()))
        self.workflow = FakeWorkflow(name="digest-workflow", version=1, workflow_id=self.workflow_id)
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

    async def list_workflows(self, task_id: TaskId) -> Sequence[FakeWorkflow]:
        return [self.workflow]

    async def list_runs(self, workflow_id: WorkflowId) -> Sequence[FakeRun]:
        return [self.run]

    async def get_run(self, run_id: UUID) -> FakeRun:
        return self.run

    async def get_state(self, workflow_id: WorkflowId) -> Mapping[str, JsonValue]:
        return {"processed": 3, "last": "ok"}
