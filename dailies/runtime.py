from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from dailies.models import WorkflowId


@dataclass(frozen=True, slots=True)
class RunContext:
    workflow_id: WorkflowId
    workflow_doc_id: UUID
    run_id: UUID
