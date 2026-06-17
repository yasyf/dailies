from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from pymongo import AsyncMongoClient

from dailies.documents import Run, Subscription, Workflow
from dailies.gmail import EmailMessage
from dailies.models import (
    EventTrigger,
    Firing,
    RunStatus,
    WorkflowId,
    WorkflowTrigger,
    utcnow,
)
from tests.factories import engine, future, make_run, make_workflow
from tests.fakes import FakeGmail

pytestmark = pytest.mark.integration

QUERY_TRIGGER = EventTrigger(source="gmail", event="query", key="alice@")


def completion_trigger(upstream: Workflow) -> WorkflowTrigger:
    return WorkflowTrigger(workflow_id=upstream.workflow_id)


def minutes_ahead(minutes: int) -> datetime:
    return utcnow() + timedelta(minutes=minutes)


async def completed_run(upstream: Workflow, *, finished_at: datetime | None, status: RunStatus = "succeeded") -> Run:
    run = make_run(upstream, status=status, finished_at=finished_at)
    await run.insert()
    return run


async def reloaded(run: Run) -> Run:
    refreshed = await Run.get(run.uid)
    assert refreshed is not None
    return refreshed


async def subscription_for(workflow: Workflow) -> Subscription:
    (subscription,) = await Subscription.find(Subscription.workflow_id == workflow.workflow_id).to_list()
    return subscription


def completion_firings(run: Run) -> dict[WorkflowId, list[str]]:
    return {
        firing.trigger.workflow_id: firing.occurrence_ids
        for firing in run.fired_by
        if isinstance(firing.trigger, WorkflowTrigger)
    }


async def test_poll_materializes_workflow_subscription(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(task_id=upstream.task_id, name="decider", triggers=[completion_trigger(upstream)])
    await upstream.insert()
    await downstream.insert()
    before = utcnow().replace(microsecond=0)
    assert await engine().tick(now=utcnow()) == []
    stored = await subscription_for(downstream)
    assert (stored.source, stored.event, stored.key, stored.origin) == (
        "workflow",
        "completed",
        str(upstream.workflow_id),
        "trigger",
    )
    assert before <= stored.watermark <= utcnow()
    assert await Run.find_all().count() == 0


async def test_new_version_dropping_workflow_trigger_prunes(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(task_id=upstream.task_id, name="decider", triggers=[completion_trigger(upstream)])
    await upstream.insert()
    await downstream.insert()
    await engine().tick(now=utcnow())
    assert await Subscription.find_all().count() == 1
    await make_workflow(
        task_id=downstream.task_id, workflow_id=downstream.workflow_id, version=2, name="decider"
    ).insert()
    await engine().tick(now=utcnow())
    assert await Subscription.find_all().count() == 0


async def test_upstream_completion_fires_one_run(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(task_id=upstream.task_id, name="decider", triggers=[completion_trigger(upstream)])
    await upstream.insert()
    await downstream.insert()
    eng = engine()
    await eng.tick(now=utcnow())
    finished = future(5)
    completion = await completed_run(upstream, finished_at=finished)
    (run,) = await eng.tick(now=minutes_ahead(10))
    refreshed = await reloaded(run)
    assert refreshed.workflow_id == downstream.workflow_id
    assert refreshed.status == "succeeded"
    assert refreshed.fired_by == [Firing(trigger=completion_trigger(upstream), occurrence_ids=[str(completion.uid)])]
    assert (await subscription_for(downstream)).watermark == finished
    assert await eng.tick(now=minutes_ahead(10)) == []
    assert await Run.find_all().count() == 2


async def test_two_upstream_completions_batch_into_one_run_with_two_firings(
    mongo: AsyncMongoClient[dict[str, Any]],
) -> None:
    tracker_a = make_workflow(name="tracker-a")
    tracker_b = make_workflow(task_id=tracker_a.task_id, name="tracker-b")
    downstream = make_workflow(
        task_id=tracker_a.task_id,
        name="decider",
        triggers=[completion_trigger(tracker_a), completion_trigger(tracker_b)],
    )
    for workflow in (tracker_a, tracker_b, downstream):
        await workflow.insert()
    eng = engine()
    await eng.tick(now=utcnow())
    finished_a, finished_b = future(5), future(6)
    run_a = await completed_run(tracker_a, finished_at=finished_a)
    run_b = await completed_run(tracker_b, finished_at=finished_b)
    (run,) = await eng.tick(now=minutes_ahead(10))
    refreshed = await reloaded(run)
    assert refreshed.workflow_id == downstream.workflow_id
    assert completion_firings(refreshed) == {
        tracker_a.workflow_id: [str(run_a.uid)],
        tracker_b.workflow_id: [str(run_b.uid)],
    }
    watermarks = {
        s.key: s.watermark
        for s in await Subscription.find(Subscription.workflow_id == downstream.workflow_id).to_list()
    }
    assert watermarks == {str(tracker_a.workflow_id): finished_a, str(tracker_b.workflow_id): finished_b}
    assert await Run.find_all().count() == 3


async def test_multiple_completions_of_same_upstream_one_firing(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(task_id=upstream.task_id, name="decider", triggers=[completion_trigger(upstream)])
    await upstream.insert()
    await downstream.insert()
    eng = engine()
    await eng.tick(now=utcnow())
    earlier, later = future(5), future(6)
    second = await completed_run(upstream, finished_at=later)
    first = await completed_run(upstream, finished_at=earlier)
    (run,) = await eng.tick(now=minutes_ahead(10))
    assert completion_firings(await reloaded(run)) == {upstream.workflow_id: [str(first.uid), str(second.uid)]}
    assert (await subscription_for(downstream)).watermark == later


async def test_settle_window_holds_then_fires(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(task_id=upstream.task_id, name="decider", triggers=[completion_trigger(upstream)])
    await upstream.insert()
    await downstream.insert()
    eng = engine()
    await eng.tick(now=utcnow())
    seeded = (await subscription_for(downstream)).watermark
    await completed_run(upstream, finished_at=future(60))
    assert await eng.tick(now=minutes_ahead(2)) == []
    assert (await subscription_for(downstream)).watermark == seeded
    assert await Run.find_all().count() == 1
    (run,) = await eng.tick(now=minutes_ahead(10))
    assert (await reloaded(run)).workflow_id == downstream.workflow_id


async def test_chatty_upstream_fires_after_max_hold(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(task_id=upstream.task_id, name="decider", triggers=[completion_trigger(upstream)])
    await upstream.insert()
    await downstream.insert()
    eng = engine()
    await eng.tick(now=utcnow())
    first = await completed_run(upstream, finished_at=future(5))
    second = await completed_run(upstream, finished_at=(utcnow() + timedelta(minutes=14)).replace(microsecond=0))
    (run,) = await eng.tick(now=minutes_ahead(16))
    assert completion_firings(await reloaded(run)) == {upstream.workflow_id: [str(first.uid), str(second.uid)]}


async def test_completion_during_materializing_tick_not_lost(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(task_id=upstream.task_id, name="decider", triggers=[completion_trigger(upstream)])
    await upstream.insert()
    await downstream.insert()
    tick_now = utcnow() - timedelta(minutes=2)
    finished = (utcnow() - timedelta(minutes=1)).replace(microsecond=0)
    completion = await completed_run(upstream, finished_at=finished)
    eng = engine()
    assert await eng.tick(now=tick_now) == []
    (run,) = await eng.tick(now=minutes_ahead(10))
    assert completion_firings(await reloaded(run)) == {upstream.workflow_id: [str(completion.uid)]}


async def test_concurrent_materialize_tolerates_duplicate_insert(
    mongo: AsyncMongoClient[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(task_id=upstream.task_id, name="decider", triggers=[completion_trigger(upstream)])
    await upstream.insert()
    await downstream.insert()
    eng = engine()
    await eng.tick(now=utcnow())

    async def stale_find_one(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(Subscription, "find_one", stale_find_one)
    assert await eng.tick(now=utcnow()) == []
    assert await Subscription.find_all().count() == 1


async def test_fresh_completion_holds_older_sibling_completion(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    tracker_a = make_workflow(name="tracker-a")
    tracker_b = make_workflow(task_id=tracker_a.task_id, name="tracker-b")
    downstream = make_workflow(
        task_id=tracker_a.task_id,
        name="decider",
        triggers=[completion_trigger(tracker_a), completion_trigger(tracker_b)],
    )
    for workflow in (tracker_a, tracker_b, downstream):
        await workflow.insert()
    eng = engine()
    await eng.tick(now=utcnow())
    run_a = await completed_run(tracker_a, finished_at=future(5))
    run_b = await completed_run(tracker_b, finished_at=future(9 * 60))
    assert await eng.tick(now=minutes_ahead(10)) == []
    (run,) = await eng.tick(now=minutes_ahead(20))
    assert completion_firings(await reloaded(run)) == {
        tracker_a.workflow_id: [str(run_a.uid)],
        tracker_b.workflow_id: [str(run_b.uid)],
    }


async def test_settle_does_not_hold_gmail_news(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(
        task_id=upstream.task_id, name="decider", triggers=[QUERY_TRIGGER, completion_trigger(upstream)]
    )
    await upstream.insert()
    await downstream.insert()
    eng = engine(gmail)
    await eng.tick(now=utcnow())
    gmail.add(
        EmailMessage(
            id="m1",
            thread_id="t1",
            sender="alice@example.com",
            to="me@example.com",
            subject="subj",
            body="hello",
            date=future(5),
        )
    )
    completion = await completed_run(upstream, finished_at=future(9 * 60))
    (mail_run,) = await eng.tick(now=minutes_ahead(10))
    assert (await reloaded(mail_run)).fired_by == [Firing(trigger=QUERY_TRIGGER, occurrence_ids=["m1"])]
    (completion_run,) = await eng.tick(now=minutes_ahead(20))
    assert (await reloaded(completion_run)).fired_by == [
        Firing(trigger=completion_trigger(upstream), occurrence_ids=[str(completion.uid)])
    ]


async def test_failed_downstream_run_keeps_watermark_and_refires(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(task_id=upstream.task_id, name="decider", triggers=[completion_trigger(upstream)])
    await upstream.insert()
    await downstream.insert()
    failing = engine(ok=False)
    await failing.tick(now=utcnow())
    seeded = (await subscription_for(downstream)).watermark
    finished = future(5)
    completion = await completed_run(upstream, finished_at=finished)
    (failed,) = await failing.tick(now=minutes_ahead(10))
    assert (await reloaded(failed)).status == "failed"
    assert (await subscription_for(downstream)).watermark == seeded
    (retried,) = await engine().tick(now=minutes_ahead(10))
    refreshed = await reloaded(retried)
    assert refreshed.status == "succeeded"
    assert refreshed.fired_by == [Firing(trigger=completion_trigger(upstream), occurrence_ids=[str(completion.uid)])]
    assert (await subscription_for(downstream)).watermark == finished


async def test_failed_upstream_run_does_not_fire(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(task_id=upstream.task_id, name="decider", triggers=[completion_trigger(upstream)])
    await upstream.insert()
    await downstream.insert()
    eng = engine()
    await eng.tick(now=utcnow())
    seeded = (await subscription_for(downstream)).watermark
    await completed_run(upstream, finished_at=future(5), status="failed")
    assert await eng.tick(now=minutes_ahead(10)) == []
    assert (await subscription_for(downstream)).watermark == seeded
    assert await Run.find_all().count() == 1


async def test_running_upstream_not_observed(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    upstream = make_workflow(name="tracker")
    downstream = make_workflow(task_id=upstream.task_id, name="decider", triggers=[completion_trigger(upstream)])
    await upstream.insert()
    await downstream.insert()
    eng = engine()
    await eng.tick(now=utcnow())
    await completed_run(upstream, finished_at=None, status="running")
    assert await eng.tick(now=minutes_ahead(10)) == []


async def test_unknown_subscription_source_crashes(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    await Subscription(
        workflow_id=workflow.workflow_id,
        source="slack",
        event="message",
        key="#general",
        watermark=utcnow(),
        origin="agent",
    ).insert()
    with pytest.RaisesGroup(pytest.RaisesExc(ValueError, match="unknown subscription source")):
        await engine().tick(now=utcnow())
