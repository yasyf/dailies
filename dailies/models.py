from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, NewType
from uuid import UUID

import uuid6
from pydantic import BaseModel, ConfigDict, Field, JsonValue

PromptStr = NewType("PromptStr", str)
SchemaStr = NewType("SchemaStr", str)
CronExpr = NewType("CronExpr", str)
WorkflowId = NewType("WorkflowId", UUID)
TaskId = NewType("TaskId", UUID)

TaskStatus = Literal["draft", "active", "inactive"]
RunStatus = Literal["pending", "running", "succeeded", "failed", "stopped"]

# datetime-left so smart-mode strict-matches an ISO string to datetime and never
# degrades it to str across a JSON boundary; native datetimes survive the BSON path.
type StopCondition = datetime | PromptStr


def new_uuid() -> UUID:
    return uuid6.uuid7()


def utcnow() -> datetime:
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    """Immutable value object that rejects unknown fields.

    Use for input-shaped value objects (triggers, definitions) where an
    unexpected key is a bug worth surfacing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class StoredModel(BaseModel):
    """Immutable value object that tolerates unknown fields.

    Use for value objects embedded in beanie Documents and round-tripped
    through BSON, so a stale or driver-injected key never fails a read.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")


class CronTrigger(FrozenModel):
    kind: Literal["cron"] = "cron"
    cron_expression: CronExpr
    timezone: str = "UTC"


class EventTrigger(FrozenModel):
    kind: Literal["event"] = "event"
    event_type: str
    event_key: str


class ManualTrigger(FrozenModel):
    kind: Literal["manual"] = "manual"


type Trigger = Annotated[CronTrigger | EventTrigger | ManualTrigger, Field(discriminator="kind")]


class TextBlock(StoredModel):
    kind: Literal["text"] = "text"
    text: str


class ImageBlock(StoredModel):
    kind: Literal["image"] = "image"
    url: str


type Block = Annotated[TextBlock | ImageBlock, Field(discriminator="kind")]


class StatusUpdate(StoredModel):
    id: UUID = Field(default_factory=new_uuid)
    title: str
    blocks: list[Block] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class Action(StoredModel):
    id: UUID = Field(default_factory=new_uuid)
    kind: str
    target: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class TaskDefinition(FrozenModel):
    user_input: str
    description: str
    prompt: PromptStr


class WorkflowDefinition(FrozenModel):
    prompt: PromptStr
    rules: list[str] = Field(default_factory=list)


class Exchange(FrozenModel):
    question: str
    answer: str


class Interview(FrozenModel):
    scenario: str
    exchanges: list[Exchange] = Field(default_factory=list)


class InterviewTurn(FrozenModel):
    finished: bool
    question: str | None


class TaskDraft(FrozenModel):
    name: str
    description: str
    user_input: str
    prompt: str
    shared_ddl: str | None = None


class WorkflowDraft(FrozenModel):
    name: str
    prompt: str
    rules: list[str] = Field(default_factory=list)
    ddl: str
    triggers: Annotated[list[Trigger], Field(min_length=1)]


class TaskProposal(FrozenModel):
    task: TaskDraft
    workflows: list[WorkflowDraft]
