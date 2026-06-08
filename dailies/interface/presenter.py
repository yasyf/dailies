from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import JsonValue

from dailies.documents import Run, Task, Workflow
from dailies.models import WorkflowId


@dataclass(frozen=True, slots=True)
class Intent:
    task_id: UUID | None
    text: str


@runtime_checkable
class Presenter(Protocol):
    """Read-only view over tasks, workflows, runs, and stored state for a UI."""

    async def list_tasks(self) -> Sequence[Task]: ...

    async def list_workflows(self, task_id: UUID) -> Sequence[Workflow]: ...

    async def list_runs(self, workflow_id: WorkflowId) -> Sequence[Run]: ...

    async def get_run(self, run_id: UUID) -> Run: ...

    async def get_state(self, workflow_id: WorkflowId) -> Mapping[str, JsonValue]: ...
