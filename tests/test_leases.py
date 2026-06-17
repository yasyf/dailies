from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

import anyio
import pytest
from beanie.operators import Set
from pymongo import AsyncMongoClient

import dailies.engine as engine_mod
from dailies.agent import AgentRequest, AgentResult
from dailies.documents import Run, Subscription, Workflow, WorkflowLease
from dailies.engine import Engine, TriggerFired, claim_lease, extend_lease, release_lease
from dailies.models import (
    CronExpr,
    CronTrigger,
    EventTrigger,
    WorkflowId,
    new_uuid,
    utcnow,
)
from dailies.storage import state_storage
from tests.factories import email, engine, future, make_workflow
from tests.fakes import BlockingProvider, FakeGmail, SlowProvider

pytestmark = pytest.mark.integration

QUERY_TRIGGER = EventTrigger(source="gmail", event="query", key="alice@")


async def subscription_for(workflow: Workflow) -> Subscription:
    (subscription,) = await Subscription.find(Subscription.workflow_id == workflow.workflow_id).to_list()
    return subscription


async def expire_lease(workflow_id: WorkflowId) -> None:
    await WorkflowLease.find_one(WorkflowLease.workflow_id == workflow_id).update(
        Set({WorkflowLease.expires_at: utcnow() - timedelta(seconds=1)})
    )


@dataclass(frozen=True, slots=True)
class TokenStealingProvider:
    """Simulates a takeover landing mid-run: the holder's token is gone by the time it finishes."""

    workflow_id: WorkflowId

    async def run(self, request: AgentRequest) -> AgentResult:
        await WorkflowLease.find_one(WorkflowLease.workflow_id == self.workflow_id).update(
            Set({WorkflowLease.token: new_uuid()})
        )
        return AgentResult("done", ok=True)


@dataclass(frozen=True, slots=True)
class UnsubscribingProvider:
    """Simulates the agent unsubscribing mid-run from the watch whose occurrence fired it."""

    workflow_id: WorkflowId

    async def run(self, request: AgentRequest) -> AgentResult:
        await Subscription.find(Subscription.workflow_id == self.workflow_id).delete()
        return AgentResult("done", ok=True)


async def test_claim_then_contend(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow_id = WorkflowId(uuid4())
    assert await claim_lease(workflow_id) is not None
    assert await claim_lease(workflow_id) is None


async def test_expired_lease_takeover(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow_id = WorkflowId(uuid4())
    stale = WorkflowLease(workflow_id=workflow_id, token=new_uuid(), expires_at=utcnow() - timedelta(seconds=1))
    await stale.insert()
    fresh = await claim_lease(workflow_id)
    assert fresh is not None
    assert fresh.token != stale.token
    assert fresh.expires_at > utcnow()
    assert await WorkflowLease.find_all().count() == 1


async def test_extend_with_stale_token_returns_none(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow_id = WorkflowId(uuid4())
    lease = await claim_lease(workflow_id)
    assert lease is not None
    await WorkflowLease.find_one(WorkflowLease.workflow_id == workflow_id).update(
        Set({WorkflowLease.token: new_uuid()})
    )
    assert await extend_lease(lease) is None


async def test_release_is_fenced(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow_id = WorkflowId(uuid4())
    stale = await claim_lease(workflow_id)
    assert stale is not None
    successor = new_uuid()
    await WorkflowLease.find_one(WorkflowLease.workflow_id == workflow_id).update(Set({WorkflowLease.token: successor}))
    await release_lease(stale)
    (remaining,) = await WorkflowLease.find_all().to_list()
    assert remaining.token == successor


async def test_tick_skips_leased_workflow_and_processes_others(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    leased = make_workflow(triggers=[QUERY_TRIGGER])
    open_workflow = make_workflow(triggers=[QUERY_TRIGGER])
    await leased.insert()
    await open_workflow.insert()
    assert await engine(gmail).tick(now=utcnow()) == []
    gmail.add(email("m-1", date=future(60)))
    await WorkflowLease(
        workflow_id=leased.workflow_id, token=new_uuid(), expires_at=utcnow() + timedelta(minutes=5)
    ).insert()
    (run,) = await engine(gmail).tick(now=utcnow())
    assert run.workflow_id == open_workflow.workflow_id
    assert await Run.find(Run.workflow_id == leased.workflow_id).count() == 0
    await expire_lease(leased.workflow_id)
    (recovered,) = await engine(gmail).tick(now=utcnow())
    assert recovered.workflow_id == leased.workflow_id
    assert recovered.fired_by[0].occurrence_ids == ["m-1"]


async def test_stale_holder_does_not_advance_watermark(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    workflow = make_workflow(triggers=[QUERY_TRIGGER])
    await workflow.insert()
    await engine(gmail).tick(now=utcnow())
    seed = (await subscription_for(workflow)).watermark
    gmail.add(email("m-new", date=future(60)))
    stealing = Engine(provider=TokenStealingProvider(workflow.workflow_id), storage=state_storage(), gmail=gmail)
    (stale_run,) = await stealing.tick(now=utcnow())
    assert stale_run.status == "succeeded"
    assert (await subscription_for(workflow)).watermark == seed
    await expire_lease(workflow.workflow_id)
    (refire,) = await engine(gmail).tick(now=utcnow())
    assert refire.fired_by[0].occurrence_ids == ["m-new"]


async def test_heartbeat_blocks_takeover_past_ttl(
    mongo: AsyncMongoClient[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_mod, "LEASE_TTL", timedelta(seconds=0.5))
    monkeypatch.setattr(engine_mod, "LEASE_HEARTBEAT", timedelta(seconds=0.1))
    gmail = FakeGmail()
    workflow = make_workflow(triggers=[QUERY_TRIGGER])
    await workflow.insert()
    await engine(gmail).tick(now=utcnow())
    gmail.add(message := email("m-slow", date=future(60)))
    slow = Engine(provider=SlowProvider(1.5), storage=state_storage(), gmail=gmail)
    runs: list[Run] = []

    async def tick_slow() -> None:
        runs.extend(await slow.tick(now=utcnow()))

    async with anyio.create_task_group() as tg:
        tg.start_soon(tick_slow)
        await anyio.sleep(0.8)
        assert await claim_lease(workflow.workflow_id) is None
    (run,) = runs
    assert run.status == "succeeded"
    assert (await subscription_for(workflow)).watermark == message.date
    assert await WorkflowLease.find_all().count() == 0


async def test_concurrent_ticks_fire_cron_once(
    mongo: AsyncMongoClient[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    due = CronTrigger(cron_expression=CronExpr("*/1 * * * *"))
    workflow = make_workflow(triggers=[due], created_at=utcnow() - timedelta(minutes=5))
    await workflow.insert()
    first, second = engine(), engine()
    started, release = anyio.Event(), anyio.Event()
    real_dispatch = Engine.dispatch

    async def gated(self: Engine, fired: TriggerFired) -> Run:
        if self is first:
            started.set()
            await release.wait()
        return await real_dispatch(self, fired)

    monkeypatch.setattr(Engine, "dispatch", gated)
    runs: list[Run] = []

    async def tick_first() -> None:
        runs.extend(await first.tick(now=utcnow()))

    async with anyio.create_task_group() as tg:
        tg.start_soon(tick_first)
        await started.wait()
        assert await second.tick(now=utcnow()) == []
        release.set()
    assert len(runs) == 1
    assert await Run.find_all().count() == 1


async def test_concurrent_ticks_deliver_news_once(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    workflow = make_workflow(triggers=[QUERY_TRIGGER])
    await workflow.insert()
    await engine(gmail).tick(now=utcnow())
    gmail.add(email("m-1", date=future(60)))
    started, release = anyio.Event(), anyio.Event()
    blocked = Engine(provider=BlockingProvider(started, release), storage=state_storage(), gmail=gmail)
    runs: list[Run] = []

    async def tick_blocked() -> None:
        runs.extend(await blocked.tick(now=utcnow()))

    async with anyio.create_task_group() as tg:
        tg.start_soon(tick_blocked)
        await started.wait()
        assert await engine(gmail).tick(now=utcnow()) == []
        release.set()
    (run,) = runs
    assert run.fired_by[0].occurrence_ids == ["m-1"]
    assert await Run.find_all().count() == 1


async def test_unsubscribed_mid_run_watermark_noop(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    workflow = make_workflow(triggers=[QUERY_TRIGGER])
    await workflow.insert()
    await engine(gmail).tick(now=utcnow())
    gmail.add(email("m-1", date=future(60)))
    unsubscribing = Engine(provider=UnsubscribingProvider(workflow.workflow_id), storage=state_storage(), gmail=gmail)
    (run,) = await unsubscribing.tick(now=utcnow())
    assert run.status == "succeeded"
    assert await Subscription.find(Subscription.workflow_id == workflow.workflow_id).count() == 0
