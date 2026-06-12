"""Beanie Document aggregate roots — the only mutable layer (value objects in models.py are frozen)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from beanie import Document, Insert, Replace, SaveChanges, Update, before_event
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from dailies.models import (
    Action,
    Firing,
    RunStatus,
    SchemaStr,
    SpendPolicy,
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
    spend_policy: SpendPolicy | None = None

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
    fired_by: list[Firing]
    status: RunStatus = "pending"
    finished_at: datetime | None = None
    status_updates: list[StatusUpdate] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)

    class Settings:
        name = "runs"
        indexes = [
            IndexModel([("workflow_doc_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("workflow_id", ASCENDING), ("created_at", DESCENDING)]),
        ]


class Subscription(TimestampedDocument):
    workflow_id: WorkflowId
    source: str
    event: str
    key: str
    watermark: datetime
    origin: Literal["trigger", "agent"]

    class Settings:
        name = "subscriptions"
        indexes = [
            IndexModel(
                [("workflow_id", ASCENDING), ("source", ASCENDING), ("event", ASCENDING), ("key", ASCENDING)],
                unique=True,
            )
        ]


class WorkflowLease(TimestampedDocument):
    workflow_id: WorkflowId
    token: UUID
    expires_at: datetime

    class Settings:
        name = "workflow_leases"
        indexes = [IndexModel([("workflow_id", ASCENDING)], unique=True)]


def document_models() -> list[type[Document]]:
    from dailies.profile import UserProfile

    return [Task, Workflow, Run, Subscription, WorkflowLease, UserProfile]
