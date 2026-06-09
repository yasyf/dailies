"""Beanie Document aggregate roots — the only mutable layer (value objects in models.py are frozen)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from beanie import Document, Insert, Replace, SaveChanges, Update, before_event
from pydantic import Field, JsonValue
from pymongo import ASCENDING, DESCENDING, IndexModel

from dailies.models import (
    Action,
    RunStatus,
    SchemaStr,
    StatusUpdate,
    StopCondition,
    TaskDefinition,
    TaskId,
    TaskStatus,
    Trigger,
    WorkflowDefinition,
    WorkflowId,
    new_uuid,
    utcnow,
)


class TimestampedDocument(Document):
    id: UUID = Field(default_factory=new_uuid)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @before_event(Insert, Replace, SaveChanges, Update)
    def touch(self) -> None:
        self.updated_at = utcnow()

    @property
    def uid(self) -> UUID:
        return self.id


class Task(TimestampedDocument):
    name: str
    definition: TaskDefinition
    shared_ddl: SchemaStr | None = None
    status: TaskStatus = "draft"
    summary: str | None = None
    stop_conditions: list[StopCondition] = Field(default_factory=list)

    @property
    def uid(self) -> TaskId:
        return TaskId(self.id)

    class Settings:
        name = "tasks"
        indexes = [IndexModel([("status", ASCENDING)])]


class Workflow(TimestampedDocument):
    task_id: TaskId
    workflow_id: WorkflowId
    version: int
    name: str
    definition: WorkflowDefinition
    ddl: SchemaStr
    status: TaskStatus = "draft"
    triggers: list[Trigger] = Field(default_factory=list)
    stop_conditions: list[StopCondition] = Field(default_factory=list)

    class Settings:
        name = "workflows"
        indexes = [
            IndexModel([("workflow_id", ASCENDING), ("version", ASCENDING)], unique=True),
            IndexModel([("task_id", ASCENDING)]),
            IndexModel([("status", ASCENDING)]),
        ]


class Run(TimestampedDocument):
    workflow_doc_id: UUID
    workflow_id: WorkflowId
    task_id: TaskId
    trigger: Trigger
    status: RunStatus = "pending"
    status_updates: list[StatusUpdate] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)

    class Settings:
        name = "runs"
        indexes = [
            IndexModel([("workflow_doc_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("workflow_id", ASCENDING), ("created_at", DESCENDING)]),
        ]


class WorkflowState(TimestampedDocument):
    workflow_id: WorkflowId
    ddl: SchemaStr
    data: dict[str, JsonValue] = Field(default_factory=dict)

    class Settings:
        name = "workflow_state"
        indexes = [IndexModel([("workflow_id", ASCENDING)], unique=True)]


class TaskState(TimestampedDocument):
    task_id: TaskId
    ddl: SchemaStr
    data: dict[str, JsonValue] = Field(default_factory=dict)

    class Settings:
        name = "task_state"
        indexes = [IndexModel([("task_id", ASCENDING)], unique=True)]


def document_models() -> list[type[Document]]:
    return [Task, Workflow, Run, WorkflowState, TaskState]
