from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from dailies.documents import Run, Task, TaskState, Workflow, WorkflowState
from dailies.models import TaskId, WorkflowId


@dataclass(frozen=True, slots=True)
class BlastRadius:
    workflows: int
    runs: int


@runtime_checkable
class Presenter(Protocol):
    """Query and curation surface over tasks, workflows, runs, and stored state for a UI."""

    async def list_tasks(self) -> Sequence[Task]: ...

    async def get_task(self, task_id: TaskId) -> Task: ...

    async def list_workflows(self, task_id: TaskId) -> Sequence[Workflow]: ...

    async def list_runs(self, workflow_id: WorkflowId) -> Sequence[Run]: ...

    async def get_run(self, run_id: UUID) -> Run: ...

    async def get_state(self, workflow_id: WorkflowId) -> WorkflowState | None: ...

    async def get_task_state(self, task_id: TaskId) -> TaskState | None: ...

    async def blast_radius(self, task_id: TaskId) -> BlastRadius: ...

    async def delete_task(self, task_id: TaskId) -> None: ...
