from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from dailies.models import TaskId, WorkflowId


@dataclass(frozen=True, slots=True)
class RunContext:
    workflow_id: WorkflowId
    workflow_doc_id: UUID
    task_id: TaskId
    run_id: UUID
