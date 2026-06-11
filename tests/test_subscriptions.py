from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient

from dailies.agent import AgentRequest, AgentResult
from dailies.documents import Run, Subscription, Workflow
from dailies.engine import Engine
from dailies.gmail import EmailMessage, ThreadNotFound
from dailies.models import (
    EventTrigger,
    Firing,
    PromptStr,
    SchemaStr,
    TaskId,
    TaskStatus,
    Trigger,
    WorkflowDefinition,
    WorkflowId,
    utcnow,
)
from dailies.runtime import RunContext
from dailies.storage import state_storage
from dailies.tools.inputs import EmailToolSet, SubscriptionInfo, SubscriptionNotFound, SubscriptionUpdate
from tests.fakes import FakeGmail, FakeProvider, ScriptedProvider

pytestmark = pytest.mark.integration

QUERY_TRIGGER = EventTrigger(source="gmail", event="query", key="alice@")
THREAD_TRIGGER = EventTrigger(source="gmail", event="thread", key="t-watch")
WATCHES = [("thread", "t1"), ("query", "alice@")]
WATCH_IDS = ["thread", "query"]


def make_workflow(
    *,
    workflow_id: WorkflowId | None = None,
    version: int = 1,
    status: TaskStatus = "active",
    triggers: list[Trigger] | None = None,
) -> Workflow:
    return Workflow(
        task_id=TaskId(uuid4()),
        workflow_id=workflow_id or WorkflowId(uuid4()),
        version=version,
        name="wf",
        definition=WorkflowDefinition(summary="s", prompt=PromptStr("p")),
        ddl=SchemaStr("CREATE TABLE t (id TEXT)"),
        status=status,
        triggers=triggers or [],
    )


def email(message_id: str, *, date: datetime, thread_id: str = "t1", sender: str = "alice@example.com") -> EmailMessage:
    return EmailMessage(
        id=message_id,
        thread_id=thread_id,
        sender=sender,
        to="me@example.com",
        subject="subj",
        body="hello",
        date=date,
    )


def engine(gmail: FakeGmail, *, ok: bool = True) -> Engine:
    return Engine(provider=FakeProvider(AgentResult("done", ok=ok)), storage=state_storage(), gmail=gmail)


def email_tools(workflow: Workflow, gmail: FakeGmail) -> EmailToolSet:
    return EmailToolSet(
        context=RunContext(
            workflow_id=workflow.workflow_id, workflow_doc_id=workflow.uid, task_id=workflow.task_id, run_id=uuid4()
        ),
        gmail=gmail,
    )


def floor_ms(moment: datetime) -> datetime:
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def future(seconds: int) -> datetime:
    return (utcnow() + timedelta(seconds=seconds)).replace(microsecond=0)


def past(seconds: int = 3_600) -> datetime:
    return (utcnow() - timedelta(seconds=seconds)).replace(microsecond=0)


def stored_info(info: SubscriptionInfo) -> SubscriptionInfo:
    return info.model_copy(update={"watermark": floor_ms(info.watermark)})


def event_firings(run: Run) -> dict[tuple[str, str], list[str]]:
    return {
        (firing.trigger.event, firing.trigger.key): firing.occurrence_ids
        for firing in run.fired_by
        if isinstance(firing.trigger, EventTrigger)
    }


async def subscription_for(workflow: Workflow) -> Subscription:
    (subscription,) = await Subscription.find(Subscription.workflow_id == workflow.workflow_id).to_list()
    return subscription


async def watermarks(workflow: Workflow) -> dict[tuple[str, str], datetime]:
    return {
        (subscription.event, subscription.key): subscription.watermark
        for subscription in await Subscription.find(Subscription.workflow_id == workflow.workflow_id).to_list()
    }


async def reloaded(run: Run) -> Run:
    refreshed = await Run.get(run.uid)
    assert refreshed is not None
    return refreshed


async def subscribe(tools: EmailToolSet, event: str, key: str) -> SubscriptionInfo:
    match event:
        case "thread":
            return await tools.subscribe_to_thread(key)
        case "query":
            return await tools.subscribe_to_query(key)
        case _:
            raise AssertionError(event)


async def unsubscribe(tools: EmailToolSet, event: str, key: str) -> None:
    match event:
        case "thread":
            await tools.unsubscribe_from_thread(key)
        case "query":
            await tools.unsubscribe_from_query(key)
        case _:
            raise AssertionError(event)


@dataclass(frozen=True, slots=True)
class InjectingProvider:
    """Provider that lands a new matching message while the run executes."""

    gmail: FakeGmail
    message: EmailMessage

    async def run(self, request: AgentRequest) -> AgentResult:
        self.gmail.add(self.message)
        return AgentResult("done", ok=True)


@pytest.mark.parametrize(("event", "key"), WATCHES, ids=WATCH_IDS)
async def test_subscribe_inserts_now_watermark(mongo: AsyncMongoClient[dict[str, Any]], event: str, key: str) -> None:
    gmail = FakeGmail()
    gmail.add(email("m0", date=past()))
    workflow = make_workflow()
    await workflow.insert()
    before = floor_ms(utcnow())
    created = await subscribe(email_tools(workflow, gmail), event, key)
    stored = await subscription_for(workflow)
    assert (stored.source, stored.event, stored.key, stored.origin) == ("gmail", event, key, "agent")
    assert before <= stored.watermark <= utcnow()
    assert stored_info(created) == SubscriptionInfo(event=event, key=key, watermark=stored.watermark)


@pytest.mark.parametrize(("event", "key"), WATCHES, ids=WATCH_IDS)
async def test_duplicate_subscribe_preserves_watermark(
    mongo: AsyncMongoClient[dict[str, Any]], event: str, key: str
) -> None:
    gmail = FakeGmail()
    gmail.add(email("m0", date=past()))
    workflow = make_workflow()
    await workflow.insert()
    tools = email_tools(workflow, gmail)
    await subscribe(tools, event, key)
    backdated = past(86_400)
    await (await subscription_for(workflow)).set({Subscription.watermark: backdated})
    again = await subscribe(tools, event, key)
    assert again == SubscriptionInfo(event=event, key=key, watermark=backdated)
    assert [s.watermark for s in await Subscription.find_all().to_list()] == [backdated]


async def test_subscribe_to_unknown_thread_raises(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    with pytest.raises(ThreadNotFound):
        await email_tools(workflow, FakeGmail()).subscribe_to_thread("missing")
    assert await Subscription.find_all().count() == 0


@pytest.mark.parametrize(("event", "key"), WATCHES, ids=WATCH_IDS)
async def test_unsubscribe_deletes(mongo: AsyncMongoClient[dict[str, Any]], event: str, key: str) -> None:
    gmail = FakeGmail()
    gmail.add(email("m0", date=past()))
    workflow = make_workflow()
    await workflow.insert()
    tools = email_tools(workflow, gmail)
    await subscribe(tools, event, key)
    await unsubscribe(tools, event, key)
    assert await Subscription.find_all().count() == 0


@pytest.mark.parametrize(("event", "key"), WATCHES, ids=WATCH_IDS)
async def test_unsubscribe_absent_raises(mongo: AsyncMongoClient[dict[str, Any]], event: str, key: str) -> None:
    workflow = make_workflow()
    await workflow.insert()
    with pytest.raises(SubscriptionNotFound, match=f"not watching {event}"):
        await unsubscribe(email_tools(workflow, FakeGmail()), event, key)


@pytest.mark.parametrize(("event", "key"), WATCHES, ids=WATCH_IDS)
async def test_unsubscribe_trigger_origin_raises_and_keeps_subscription(
    mongo: AsyncMongoClient[dict[str, Any]], event: str, key: str
) -> None:
    workflow = make_workflow()
    await workflow.insert()
    seeded = past(86_400)
    await Subscription(
        workflow_id=workflow.workflow_id, source="gmail", event=event, key=key, watermark=seeded, origin="trigger"
    ).insert()
    with pytest.raises(SubscriptionNotFound, match="declared by the workflow; it cannot be unsubscribed"):
        await unsubscribe(email_tools(workflow, FakeGmail()), event, key)
    stored = await subscription_for(workflow)
    assert (stored.source, stored.event, stored.key, stored.origin, stored.watermark) == (
        "gmail",
        event,
        key,
        "trigger",
        seeded,
    )


async def test_list_subscriptions_scoped_to_workflow(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    gmail.add(email("m0", date=past()))
    workflow = make_workflow()
    await workflow.insert()
    tools = email_tools(workflow, gmail)
    thread_info = await tools.subscribe_to_thread("t1")
    query_info = await tools.subscribe_to_query("alice@")
    await Subscription(
        workflow_id=WorkflowId(uuid4()), source="gmail", event="query", key="other", watermark=utcnow(), origin="agent"
    ).insert()
    listed = await tools.list_subscriptions()
    expected = [stored_info(info) for info in (query_info, thread_info)]
    assert sorted(listed, key=lambda info: info.event) == sorted(expected, key=lambda info: info.event)


async def test_poll_materializes_declared_trigger(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow(triggers=[QUERY_TRIGGER])
    await workflow.insert()
    before = floor_ms(utcnow())
    assert await engine(FakeGmail()).tick(now=utcnow()) == []
    stored = await subscription_for(workflow)
    assert (stored.source, stored.event, stored.key, stored.origin) == ("gmail", "query", "alice@", "trigger")
    assert before <= stored.watermark <= utcnow()
    assert await Run.find_all().count() == 0


async def test_new_version_dropping_declaration_prunes(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    workflow = make_workflow(triggers=[QUERY_TRIGGER])
    await workflow.insert()
    await engine(gmail).tick(now=utcnow())
    assert await Subscription.find_all().count() == 1
    await make_workflow(workflow_id=workflow.workflow_id, version=2).insert()
    await engine(gmail).tick(now=utcnow())
    assert await Subscription.find_all().count() == 0


async def test_agent_origin_subscription_survives_pruning(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = make_workflow()
    await workflow.insert()
    await Subscription(
        workflow_id=workflow.workflow_id,
        source="gmail",
        event="query",
        key="alice@",
        watermark=utcnow(),
        origin="agent",
    ).insert()
    assert await engine(FakeGmail()).tick(now=utcnow()) == []
    assert (await subscription_for(workflow)).origin == "agent"


@pytest.mark.parametrize(
    "trigger",
    [
        EventTrigger(source="slack", event="message", key="#general"),
        EventTrigger(source="gmail", event="label", key="inbox"),
    ],
    ids=["unknown-source", "unknown-event"],
)
async def test_unknown_declaration_crashes(mongo: AsyncMongoClient[dict[str, Any]], trigger: EventTrigger) -> None:
    await make_workflow(triggers=[trigger]).insert()
    with pytest.RaisesGroup(pytest.RaisesExc(ValueError, match="unknown event trigger")):
        await engine(FakeGmail()).tick(now=utcnow())


async def test_poll_without_news_fires_nothing(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    gmail.add(email("m-old", date=past()))
    workflow = make_workflow(triggers=[QUERY_TRIGGER])
    await workflow.insert()
    eng = engine(gmail)
    assert await eng.tick(now=utcnow()) == []
    assert await eng.tick(now=utcnow()) == []
    assert await Run.find_all().count() == 0


async def test_new_message_fires_one_run_and_promotes_watermark(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    workflow = make_workflow(triggers=[QUERY_TRIGGER])
    await workflow.insert()
    eng = engine(gmail)
    await eng.tick(now=utcnow())
    arrival = future(5)
    gmail.add(email("m1", date=arrival))
    (run,) = await eng.tick(now=utcnow())
    refreshed = await reloaded(run)
    assert refreshed.workflow_id == workflow.workflow_id
    assert refreshed.status == "succeeded"
    assert refreshed.fired_by == [Firing(trigger=QUERY_TRIGGER, occurrence_ids=["m1"])]
    assert (await subscription_for(workflow)).watermark == arrival
    assert await eng.tick(now=utcnow()) == []
    assert await Run.find_all().count() == 1


async def test_failed_run_keeps_watermark_and_refires(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    workflow = make_workflow(triggers=[QUERY_TRIGGER])
    await workflow.insert()
    failing = engine(gmail, ok=False)
    await failing.tick(now=utcnow())
    seeded = (await subscription_for(workflow)).watermark
    arrival = future(5)
    gmail.add(email("m1", date=arrival))
    (failed,) = await failing.tick(now=utcnow())
    assert (await reloaded(failed)).status == "failed"
    assert (await subscription_for(workflow)).watermark == seeded
    (retried,) = await engine(gmail).tick(now=utcnow())
    refreshed = await reloaded(retried)
    assert refreshed.status == "succeeded"
    assert refreshed.fired_by == [Firing(trigger=QUERY_TRIGGER, occurrence_ids=["m1"])]
    assert (await subscription_for(workflow)).watermark == arrival


async def test_two_newsy_watches_batch_into_one_run(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    gmail.add(email("m-seed", thread_id="t-watch", date=past()))
    workflow = make_workflow(triggers=[QUERY_TRIGGER, THREAD_TRIGGER])
    await workflow.insert()
    eng = engine(gmail)
    await eng.tick(now=utcnow())
    query_arrival, thread_arrival = future(5), future(6)
    gmail.add(email("m-query", thread_id="t-other", date=query_arrival))
    gmail.add(email("m-thread", thread_id="t-watch", sender="bob@example.com", date=thread_arrival))
    (run,) = await eng.tick(now=utcnow())
    refreshed = await reloaded(run)
    assert refreshed.status == "succeeded"
    assert len(refreshed.fired_by) == 2
    assert event_firings(refreshed) == {("query", "alice@"): ["m-query"], ("thread", "t-watch"): ["m-thread"]}
    assert await watermarks(workflow) == {("query", "alice@"): query_arrival, ("thread", "t-watch"): thread_arrival}
    assert await Run.find_all().count() == 1


async def test_same_key_on_two_workflows_keeps_independent_watermarks(
    mongo: AsyncMongoClient[dict[str, Any]],
) -> None:
    gmail = FakeGmail()
    first, second = make_workflow(triggers=[QUERY_TRIGGER]), make_workflow(triggers=[QUERY_TRIGGER])
    await first.insert()
    await second.insert()
    eng = Engine(
        provider=ScriptedProvider([AgentResult("done", ok=True), AgentResult("oops", ok=False)]),
        storage=state_storage(),
        gmail=gmail,
    )
    await eng.tick(now=utcnow())
    seeds = {workflow.workflow_id: (await subscription_for(workflow)).watermark for workflow in (first, second)}
    arrival = future(5)
    gmail.add(email("m1", date=arrival))
    runs = [await reloaded(run) for run in await eng.tick(now=utcnow())]
    assert {run.workflow_id for run in runs} == {first.workflow_id, second.workflow_id}
    assert sorted(run.status for run in runs) == ["failed", "succeeded"]
    assert [run.fired_by for run in runs] == [[Firing(trigger=QUERY_TRIGGER, occurrence_ids=["m1"])]] * 2
    statuses = {run.workflow_id: run.status for run in runs}
    for workflow in (first, second):
        promoted = statuses[workflow.workflow_id] == "succeeded"
        assert (await subscription_for(workflow)).watermark == (arrival if promoted else seeds[workflow.workflow_id])


async def test_inactive_workflow_not_polled(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    workflow = make_workflow(status="inactive", triggers=[QUERY_TRIGGER])
    await workflow.insert()
    seeded = past()
    await Subscription(
        workflow_id=workflow.workflow_id,
        source="gmail",
        event="query",
        key="alice@",
        watermark=seeded,
        origin="agent",
    ).insert()
    gmail.add(email("m1", date=future(5)))
    assert await engine(gmail).tick(now=utcnow()) == []
    assert await Run.find_all().count() == 0
    assert (await subscription_for(workflow)).watermark == seeded


async def test_thread_gone_drops_agent_skips_trigger_others_fire(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    arrival = future(5)
    gmail.add(email("m1", date=arrival))
    gone = EventTrigger(source="gmail", event="thread", key="t-gone")
    workflow = make_workflow(triggers=[QUERY_TRIGGER, gone])
    await workflow.insert()
    await Subscription(
        workflow_id=workflow.workflow_id,
        source="gmail",
        event="thread",
        key="t-agent-gone",
        watermark=past(),
        origin="agent",
    ).insert()
    (run,) = await engine(gmail).tick(now=utcnow())
    refreshed = await reloaded(run)
    assert refreshed.status == "succeeded"
    assert refreshed.fired_by == [Firing(trigger=QUERY_TRIGGER, occurrence_ids=["m1"])]
    remaining = {(s.event, s.key, s.origin) for s in await Subscription.find_all().to_list()}
    assert remaining == {("query", "alice@", "trigger"), ("thread", "t-gone", "trigger")}
    assert (await watermarks(workflow))["query", "alice@"] == arrival


async def test_check_subscriptions_returns_only_past_watermark(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    gmail.add(email("m-old", date=past()))
    workflow = make_workflow()
    await workflow.insert()
    tools = email_tools(workflow, gmail)
    await tools.subscribe_to_query("alice@")
    await tools.subscribe_to_query("bob@")
    older, newer = email("m-new-1", date=future(5)), email("m-new-2", date=future(6))
    gmail.add(newer)
    gmail.add(older)
    before = await watermarks(workflow)
    assert await tools.check_subscriptions() == [
        SubscriptionUpdate(event="query", key="alice@", messages=[older, newer])
    ]
    assert await watermarks(workflow) == before


async def test_message_arriving_mid_run_refires_next_poll(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    gmail = FakeGmail()
    workflow = make_workflow(triggers=[QUERY_TRIGGER])
    await workflow.insert()
    fired_at, mid_run = future(5), future(10)
    injecting = Engine(
        provider=InjectingProvider(gmail, email("m-mid-run", date=mid_run)), storage=state_storage(), gmail=gmail
    )
    await injecting.tick(now=utcnow())
    gmail.add(email("m1", date=fired_at))
    (run,) = await injecting.tick(now=utcnow())
    assert (await reloaded(run)).fired_by == [Firing(trigger=QUERY_TRIGGER, occurrence_ids=["m1"])]
    assert (await subscription_for(workflow)).watermark == fired_at
    (refire,) = await engine(gmail).tick(now=utcnow())
    assert (await reloaded(refire)).fired_by == [Firing(trigger=QUERY_TRIGGER, occurrence_ids=["m-mid-run"])]
    assert (await subscription_for(workflow)).watermark == mid_run
