from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError

from dailies.agent import AgentResult
from dailies.documents import Run, Workflow
from dailies.engine import LOOKBACK, SYSTEM, Engine, TriggerFired, workflow_cursor
from dailies.models import (
    Action,
    CronExpr,
    CronTrigger,
    EventTrigger,
    ManualTrigger,
    PromptStr,
    SchemaStr,
    StatusUpdate,
    TaskStatus,
    Trigger,
    WorkflowDefinition,
    WorkflowId,
)
from tests.fakes import FakeProvider

pytestmark = pytest.mark.integration


def make_workflow(
    *,
    workflow_id: WorkflowId | None = None,
    version: int = 1,
    status: TaskStatus = "active",
    triggers: list[Trigger] | None = None,
    created_at: datetime | None = None,
) -> Workflow:
    workflow = Workflow(
        task_id=uuid4(),
        workflow_id=workflow_id or WorkflowId(uuid4()),
        version=version,
        name="wf",
        definition=WorkflowDefinition(prompt=PromptStr("p")),
        ddl=SchemaStr("CREATE TABLE t (id TEXT)"),
        status=status,
        triggers=triggers or [],
    )
    if created_at is not None:
        workflow.created_at = created_at
    return workflow


async def test_dispatch_persists_exactly_one_run(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    engine = Engine(provider=FakeProvider(AgentResult("ok", ok=True)))
    await engine.dispatch(TriggerFired(workflow.workflow_id, ManualTrigger()))
    assert await Run.find(Run.workflow_id == workflow.workflow_id).count() == 1


async def test_dispatch_event_funnels_through_dispatch(
    mongo: AsyncMongoClient[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[TriggerFired] = []

    async def fake_dispatch(self: Engine, fired: TriggerFired) -> None:
        seen.append(fired)

    monkeypatch.setattr(Engine, "dispatch", fake_dispatch)
    await Engine().dispatch_event(workflow_id=WorkflowId(uuid4()), event_type="email", event_key="k")
    assert len(seen) == 1
    assert isinstance(seen[0].trigger, EventTrigger)
    assert await Run.find_all().count() == 0


async def test_fire_due_funnels_one_run_per_due_trigger(
    mongo: AsyncMongoClient[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    due = CronTrigger(cron_expression=CronExpr("*/1 * * * *"))
    workflow = make_workflow(triggers=[due], created_at=datetime.now(UTC) - timedelta(minutes=5))
    await workflow.insert()
    seen: list[TriggerFired] = []

    async def fake_dispatch(self: Engine, fired: TriggerFired) -> None:
        seen.append(fired)

    monkeypatch.setattr(Engine, "dispatch", fake_dispatch)
    await Engine().fire_due(now=datetime.now(UTC))
    assert len(seen) == 1
    assert isinstance(seen[0].trigger, CronTrigger)
    assert await Run.find_all().count() == 0


async def test_fire_due_emits_one_run_per_due_trigger_multi(
    mongo: AsyncMongoClient[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    triggers = [
        CronTrigger(cron_expression=CronExpr("*/1 * * * *")),
        CronTrigger(cron_expression=CronExpr("*/2 * * * *")),
    ]
    workflow = make_workflow(triggers=triggers, created_at=datetime.now(UTC) - timedelta(minutes=10))
    await workflow.insert()
    seen: list[TriggerFired] = []

    async def fake_dispatch(self: Engine, fired: TriggerFired) -> None:
        seen.append(fired)

    monkeypatch.setattr(Engine, "dispatch", fake_dispatch)
    await Engine().fire_due(now=datetime.now(UTC))
    assert len(seen) == 2


async def test_fire_due_skips_inactive_workflows(
    mongo: AsyncMongoClient[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    due = CronTrigger(cron_expression=CronExpr("*/1 * * * *"))
    workflow = make_workflow(status="draft", triggers=[due], created_at=datetime.now(UTC) - timedelta(minutes=5))
    await workflow.insert()
    seen: list[TriggerFired] = []

    async def fake_dispatch(self: Engine, fired: TriggerFired) -> None:
        seen.append(fired)

    monkeypatch.setattr(Engine, "dispatch", fake_dispatch)
    await Engine().fire_due(now=datetime.now(UTC))
    assert seen == []


async def test_workflow_cursor_clamps_stale_created_at(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow(created_at=datetime.now(UTC) - timedelta(days=30))
    await workflow.insert()
    now = datetime.now(UTC)
    since = await workflow_cursor(workflow, now=now)
    assert abs((since - (now - LOOKBACK)).total_seconds()) < 1


async def test_workflow_cursor_prefers_latest_run(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow(created_at=datetime.now(UTC) - timedelta(hours=5))
    await workflow.insert()
    run = Run(workflow_doc_id=workflow.uid, workflow_id=workflow.workflow_id, trigger=ManualTrigger())
    await run.insert()
    now = datetime.now(UTC)
    since = await workflow_cursor(workflow, now=now)
    assert since > now - timedelta(minutes=1)


async def test_workflow_version_unique_constraint(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow_id = WorkflowId(uuid4())
    await make_workflow(workflow_id=workflow_id, version=1).insert()
    with pytest.raises(DuplicateKeyError):
        await make_workflow(workflow_id=workflow_id, version=1).insert()


async def test_invoke_agent_delegates(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    run = Run(workflow_doc_id=workflow.uid, workflow_id=workflow.workflow_id, trigger=ManualTrigger())
    await run.insert()
    provider = FakeProvider(AgentResult("done", ok=True))
    engine = Engine(provider=provider)
    await engine.invoke_agent(run)
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert reloaded.status == "succeeded"
    assert [block.text for update in reloaded.status_updates for block in update.blocks] == ["done"]
    request = provider.requests[0]
    assert request.system == SYSTEM
    assert request.prompt == workflow.definition.prompt
    assert len(request.tools) == sum(len(ts.get_tools()) for ts in engine.build_toolsets(run))


async def test_invoke_agent_marks_failure(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    run = Run(workflow_doc_id=workflow.uid, workflow_id=workflow.workflow_id, trigger=ManualTrigger())
    await run.insert()
    engine = Engine(provider=FakeProvider(AgentResult("oops", ok=False)))
    await engine.invoke_agent(run)
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert reloaded.status == "failed"


async def test_record_status_appends_once(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    run = Run(workflow_doc_id=workflow.uid, workflow_id=workflow.workflow_id, trigger=ManualTrigger())
    await run.insert()
    await Engine().record_status(run, StatusUpdate(title="hi"))
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert [u.title for u in reloaded.status_updates] == ["hi"]


async def test_record_action_appends_once(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    run = Run(workflow_doc_id=workflow.uid, workflow_id=workflow.workflow_id, trigger=ManualTrigger())
    await run.insert()
    await Engine().record_action(run, Action(kind="email", target="a@b.com"))
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert [a.kind for a in reloaded.actions] == ["email"]
