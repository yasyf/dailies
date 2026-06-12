from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError

from dailies.agent import AgentResult
from dailies.documents import Run, Workflow
from dailies.engine import LOOKBACK, Engine, TriggerFired, system_prompt, workflow_cursor
from dailies.models import (
    Action,
    CronExpr,
    CronTrigger,
    EventTrigger,
    Firing,
    ManualTrigger,
    PromptStr,
    SchemaStr,
    StatusUpdate,
    TaskId,
    TaskStatus,
    Trigger,
    WorkflowDefinition,
    WorkflowId,
    WorkflowTrigger,
)
from dailies.tools.base import ToolSpec
from tests.fakes import FakeProvider

pytestmark = pytest.mark.integration

FIRED_AT = datetime(2026, 6, 11, 16, 0, tzinfo=UTC)
SIBLING_WORKFLOW = WorkflowId(uuid4())
SIBLING_RUN = str(uuid4())


def make_workflow(
    *,
    workflow_id: WorkflowId | None = None,
    version: int = 1,
    status: TaskStatus = "active",
    triggers: list[Trigger] | None = None,
    created_at: datetime | None = None,
) -> Workflow:
    workflow = Workflow(
        task_id=TaskId(uuid4()),
        workflow_id=workflow_id or WorkflowId(uuid4()),
        version=version,
        name="wf",
        definition=WorkflowDefinition(summary="s", prompt=PromptStr("p")),
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
    await engine.dispatch(TriggerFired(workflow.workflow_id, [Firing(trigger=ManualTrigger())]))
    runs = await Run.find(Run.workflow_id == workflow.workflow_id).to_list()
    assert [run.fired_by for run in runs] == [[Firing(trigger=ManualTrigger())]]


async def test_dispatch_persists_event_firings(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    engine = Engine(provider=FakeProvider(AgentResult("ok", ok=True)))
    firings = [
        Firing(trigger=EventTrigger(source="gmail", event="query", key="from:a@b.com"), occurrence_ids=["m1", "m2"]),
        Firing(trigger=EventTrigger(source="gmail", event="thread", key="t1"), occurrence_ids=["m3"]),
    ]
    run = await engine.dispatch(TriggerFired(workflow.workflow_id, firings))
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert reloaded.fired_by == firings


async def test_tick_funnels_one_run_per_due_trigger(
    mongo: AsyncMongoClient[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    due = CronTrigger(cron_expression=CronExpr("*/1 * * * *"))
    workflow = make_workflow(triggers=[due], created_at=datetime.now(UTC) - timedelta(minutes=5))
    await workflow.insert()
    seen: list[TriggerFired] = []

    async def fake_dispatch(self: Engine, fired: TriggerFired) -> None:
        seen.append(fired)

    monkeypatch.setattr(Engine, "dispatch", fake_dispatch)
    await Engine().tick(now=datetime.now(UTC))
    assert seen == [TriggerFired(workflow.workflow_id, [Firing(trigger=due)])]
    assert await Run.find_all().count() == 0


async def test_tick_emits_one_run_per_due_trigger_multi(
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
    await Engine().tick(now=datetime.now(UTC))
    assert [fired.firings for fired in seen] == [[Firing(trigger=trigger)] for trigger in triggers]


async def test_tick_skips_inactive_workflows(
    mongo: AsyncMongoClient[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    due = CronTrigger(cron_expression=CronExpr("*/1 * * * *"))
    workflow = make_workflow(status="draft", triggers=[due], created_at=datetime.now(UTC) - timedelta(minutes=5))
    await workflow.insert()
    seen: list[TriggerFired] = []

    async def fake_dispatch(self: Engine, fired: TriggerFired) -> None:
        seen.append(fired)

    monkeypatch.setattr(Engine, "dispatch", fake_dispatch)
    await Engine().tick(now=datetime.now(UTC))
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
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=workflow.task_id,
        fired_by=[Firing(trigger=ManualTrigger())],
    )
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
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=workflow.task_id,
        fired_by=[Firing(trigger=ManualTrigger())],
    )
    await run.insert()
    provider = FakeProvider(AgentResult("done", ok=True))
    engine = Engine(provider=provider, chrome=False)
    await engine.invoke_agent(run)
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert reloaded.status == "succeeded"
    assert [block.text for update in reloaded.status_updates for block in update.blocks] == ["done"]
    request = provider.requests[0]
    assert request.system == system_prompt(chrome=False)
    assert request.prompt == (
        f"This run was fired by:\n- a manual run requested by the user\n\n{workflow.definition.prompt}"
    )
    assert request.chrome is False
    assert "browse" in {spec.name for spec in request.tools}
    assert len(request.tools) == sum(len(ts.get_tools()) for ts in engine.build_toolsets(run))


@pytest.mark.parametrize(
    ("firings", "bullets"),
    [
        pytest.param([Firing(trigger=ManualTrigger())], ["- a manual run requested by the user"], id="manual"),
        pytest.param(
            [Firing(trigger=CronTrigger(cron_expression=CronExpr("0 9 * * *"), timezone="America/Los_Angeles"))],
            ["- the cron schedule `0 9 * * *` (America/Los_Angeles), fired at 2026-06-11T09:00-07:00"],
            id="cron",
        ),
        pytest.param(
            [Firing(trigger=CronTrigger(cron_expression=CronExpr("0 6 * * *"), timezone="America/Los_Angeles"))],
            ["- the cron schedule `0 6 * * *` (America/Los_Angeles), fired at 2026-06-11T09:00-07:00"],
            id="cron-catch-up-renders-firing-time-not-slot",
        ),
        pytest.param(
            [
                Firing(
                    trigger=EventTrigger(source="gmail", event="query", key="from:a@b.com"),
                    occurrence_ids=["m1", "m2"],
                ),
                Firing(trigger=EventTrigger(source="gmail", event="thread", key="t1"), occurrence_ids=["m3"]),
            ],
            [
                "- new gmail query activity for `from:a@b.com` (message ids: m1, m2)",
                "- new gmail thread activity for `t1` (message ids: m3)",
            ],
            id="event",
        ),
        pytest.param(
            [Firing(trigger=WorkflowTrigger(workflow_id=SIBLING_WORKFLOW), occurrence_ids=[SIBLING_RUN])],
            [f"- completion of sibling workflow `{SIBLING_WORKFLOW}` (run ids: {SIBLING_RUN})"],
            id="workflow",
        ),
    ],
)
async def test_invoke_agent_prepends_firing_context(
    mongo: AsyncMongoClient[dict[str, Any]], firings: list[Firing], bullets: list[str]
) -> None:
    workflow = make_workflow()
    await workflow.insert()
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=workflow.task_id,
        fired_by=firings,
    )
    run.created_at = FIRED_AT
    await run.insert()
    provider = FakeProvider(AgentResult("done", ok=True))
    await Engine(provider=provider, chrome=False).invoke_agent(run)
    expected = "\n".join(["This run was fired by:", *bullets]) + f"\n\n{workflow.definition.prompt}"
    assert provider.requests[0].prompt == expected


async def test_invoke_agent_chrome_drops_browse(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=workflow.task_id,
        fired_by=[Firing(trigger=ManualTrigger())],
    )
    await run.insert()
    provider = FakeProvider(AgentResult("done", ok=True))
    await Engine(provider=provider, chrome=True).invoke_agent(run)
    request = provider.requests[0]
    assert request.chrome is True
    assert request.system == system_prompt(chrome=True)
    assert "browse" not in {spec.name for spec in request.tools}


async def test_invoke_agent_marks_failure(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=workflow.task_id,
        fired_by=[Firing(trigger=ManualTrigger())],
    )
    await run.insert()
    engine = Engine(provider=FakeProvider(AgentResult("oops", ok=False)))
    await engine.invoke_agent(run)
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert reloaded.status == "failed"


async def test_set_status_terminal_sets_finished_at(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=workflow.task_id,
        fired_by=[Firing(trigger=ManualTrigger())],
    )
    await run.insert()
    engine = Engine()
    await engine.set_status(run, "running")
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert reloaded.finished_at is None
    before = datetime.now(UTC).replace(microsecond=0)
    await engine.set_status(run, "succeeded")
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert reloaded.finished_at is not None
    assert before <= reloaded.finished_at <= datetime.now(UTC)


async def test_record_status_appends_once(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=workflow.task_id,
        fired_by=[Firing(trigger=ManualTrigger())],
    )
    await run.insert()
    await Engine().record_status(run, StatusUpdate(title="hi"))
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert [u.title for u in reloaded.status_updates] == ["hi"]


async def test_record_action_appends_once(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=workflow.task_id,
        fired_by=[Firing(trigger=ManualTrigger())],
    )
    await run.insert()
    await Engine().record_action(run, Action(kind="email", target="a@b.com"))
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert [a.kind for a in reloaded.actions] == ["email"]


async def test_action_tools_round_trip_through_engine(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=workflow.task_id,
        fired_by=[Firing(trigger=ManualTrigger())],
    )
    await run.insert()
    sets = Engine(chrome=False).build_toolsets(run)

    def spec(name: str) -> ToolSpec:
        return next(t.to_spec() for ts in sets for t in ts.get_tools() if t.name == name)

    first = await spec("record_action").invoke({"kind": "demo", "target": "alpha"})
    second = await spec("record_action").invoke({"kind": "demo", "target": "beta", "payload": {"n": 1}})
    assert [action.id for action in await spec("list_actions").invoke({})] == [first, second]
    reloaded = await Run.get(run.uid)
    assert reloaded is not None
    assert [(action.id, action.kind, action.target, action.payload) for action in reloaded.actions] == [
        (first, "demo", "alpha", {}),
        (second, "demo", "beta", {"n": 1}),
    ]
