from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from dailies.models import WorkflowId

__all__ = ["RunContext"]


@dataclass(frozen=True, slots=True)
class RunContext:
    workflow_id: WorkflowId
    workflow_doc_id: UUID
    run_id: UUID
