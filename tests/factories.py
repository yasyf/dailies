"""Hand-rolled builders for real domain objects in tests — the import-side companion to tests.fakes.

The Document builders (``make_task``, ``make_workflow``, ``make_run``) construct real
beanie aggregates and so require an initialized beanie (the ``mongo`` fixture); they are
integration-only. The value builders (``make_context``, ``email``, ``engine``, ``future``,
``sourced``) touch no database and are safe in unit tests. ``seed_profile`` performs I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from dailies.agent import AgentResult
from dailies.documents import Run, Task, Workflow
from dailies.engine import Engine
from dailies.gmail import EmailMessage
from dailies.models import (
    Action,
    Firing,
    ManualTrigger,
    PromptStr,
    RunStatus,
    SchemaStr,
    SpendPolicy,
    StatusUpdate,
    TaskDefinition,
    TaskId,
    TaskStatus,
    Timezone,
    Trigger,
    WorkflowDefinition,
    WorkflowId,
    utcnow,
)
from dailies.profile import Profile, Sourced, UserSource, save_profile
from dailies.runtime import RunContext
from dailies.storage import state_storage
from tests.fakes import FakeGmail, FakeProvider


def make_task(
    *,
    name: str = "task",
    shared_ddl: SchemaStr | None = None,
    gaps: list[str] | None = None,
    spend_policy: SpendPolicy | None = None,
) -> Task:
    return Task(
        name=name,
        definition=TaskDefinition(user_input="i", description="d", prompt=PromptStr("p")),
        shared_ddl=shared_ddl,
        gaps=gaps or [],
        spend_policy=spend_policy,
    )


def make_workflow(
    *,
    task_id: TaskId | None = None,
    workflow_id: WorkflowId | None = None,
    version: int = 1,
    name: str = "wf",
    status: TaskStatus = "active",
    triggers: list[Trigger] | None = None,
    requires: list[str] | None = None,
    created_at: datetime | None = None,
) -> Workflow:
    workflow = Workflow(
        task_id=task_id or TaskId(uuid4()),
        workflow_id=workflow_id or WorkflowId(uuid4()),
        version=version,
        name=name,
        definition=WorkflowDefinition(summary="s", prompt=PromptStr("p")),
        ddl=SchemaStr("CREATE TABLE t (id TEXT)"),
        status=status,
        triggers=triggers or [],
        requires=requires or [],
    )
    if created_at is not None:
        workflow.created_at = created_at
    return workflow


def make_run(
    workflow: Workflow,
    *,
    fired_by: list[Firing] | None = None,
    status: RunStatus = "pending",
    finished_at: datetime | None = None,
    actions: list[Action] | None = None,
    status_updates: list[StatusUpdate] | None = None,
    created_at: datetime | None = None,
) -> Run:
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=workflow.task_id,
        fired_by=fired_by or [Firing(trigger=ManualTrigger())],
        status=status,
        finished_at=finished_at,
        actions=actions or [],
        status_updates=status_updates or [],
    )
    if created_at is not None:
        run.created_at = created_at
    return run


def make_context(task_id: TaskId | None = None) -> RunContext:
    return RunContext(
        workflow_id=WorkflowId(uuid4()),
        workflow_doc_id=uuid4(),
        task_id=task_id or TaskId(uuid4()),
        run_id=uuid4(),
    )


def email(
    message_id: str,
    *,
    thread_id: str = "t1",
    sender: str = "alice@example.com",
    to: str = "me@example.com",
    subject: str = "subj",
    body: str = "hello",
    date: datetime | None = None,
) -> EmailMessage:
    return EmailMessage(
        id=message_id,
        thread_id=thread_id,
        sender=sender,
        to=to,
        subject=subject,
        body=body,
        date=date or utcnow(),
    )


def engine(gmail: FakeGmail | None = None, *, ok: bool = True) -> Engine:
    return Engine(
        provider=FakeProvider(AgentResult("done", ok=ok)), storage=state_storage(), gmail=gmail or FakeGmail()
    )


def future(seconds: int) -> datetime:
    return (utcnow() + timedelta(seconds=seconds)).replace(microsecond=0)


def sourced(value: str) -> Sourced[str]:
    return Sourced[str](value=value, source=UserSource())


async def seed_profile(
    *, name: str = "Yasyf", email: str = "yasyf@example.com", timezone: Timezone | None = None
) -> None:
    await save_profile(
        Profile(
            name=sourced(name),
            email=sourced(email),
            **({"timezone": Sourced[Timezone](value=timezone, source=UserSource())} if timezone else {}),
        )
    )
