from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, NewType
from uuid import UUID
from zoneinfo import available_timezones

import uuid6
from croniter import croniter
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, model_validator
from tzlocal import get_localzone_name

LOCAL_TZ = get_localzone_name()
IANA_TIMEZONES = frozenset(available_timezones())

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


def valid_cron(value: str) -> str:
    if not croniter.is_valid(value):
        raise ValueError(f"invalid cron expression: {value}")
    return value


def valid_timezone(value: str) -> str:
    if value not in IANA_TIMEZONES:
        raise ValueError(f"unknown IANA timezone: {value}")
    return value


type Timezone = Annotated[str, AfterValidator(valid_timezone)]


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
    cron_expression: Annotated[CronExpr, AfterValidator(valid_cron)]
    timezone: Timezone = LOCAL_TZ


class EventTrigger(FrozenModel):
    kind: Literal["event"] = "event"
    source: str
    event: str
    key: str


class ManualTrigger(FrozenModel):
    kind: Literal["manual"] = "manual"


class WorkflowTrigger(FrozenModel):
    kind: Literal["workflow"] = "workflow"
    workflow_id: WorkflowId


class WorkflowTriggerDraft(FrozenModel):
    kind: Literal["workflow"] = "workflow"
    workflow: str


type Trigger = Annotated[CronTrigger | EventTrigger | ManualTrigger | WorkflowTrigger, Field(discriminator="kind")]
type DraftTrigger = Annotated[
    CronTrigger | EventTrigger | ManualTrigger | WorkflowTriggerDraft, Field(discriminator="kind")
]


class Firing(StoredModel):
    trigger: Trigger
    occurrence_ids: list[str] = Field(default_factory=list)


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


class SpendPolicy(FrozenModel):
    per_order_cents: int
    weekly_cents: int


class TaskDefinition(FrozenModel):
    user_input: str
    description: str
    prompt: PromptStr


class WorkflowDefinition(FrozenModel):
    summary: str
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
    summary: str
    prompt: str
    rules: list[str] = Field(default_factory=list)
    ddl: str
    triggers: Annotated[list[DraftTrigger], Field(min_length=1)]


class TaskProposal(FrozenModel):
    task: TaskDraft
    workflows: list[WorkflowDraft]
    gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_workflow_references(self) -> TaskProposal:
        names = [draft.name for draft in self.workflows]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate workflow names: {sorted(names)}")
        edges = {
            draft.name: {trigger.workflow for trigger in draft.triggers if isinstance(trigger, WorkflowTriggerDraft)}
            for draft in self.workflows
        }
        if unknown := set().union(*edges.values()) - set(names):
            raise ValueError(f"workflow trigger references unknown sibling: {sorted(unknown)}")
        while leaves := [name for name, upstreams in edges.items() if not upstreams & edges.keys()]:
            for name in leaves:
                del edges[name]
        if edges:
            raise ValueError(f"workflow triggers form a cycle: {sorted(edges)}")
        return self
