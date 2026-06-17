from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient

from dailies.documents import Run, Task
from dailies.models import (
    Action,
    Firing,
    ManualTrigger,
    SpendPolicy,
    TaskId,
    WorkflowId,
)
from dailies.runtime import RunContext
from dailies.spend import weekly_spent_cents
from dailies.tools.base import ToolError, ToolSpec
from dailies.tools.spend import SpendToolSet
from tests.factories import make_task, seed_profile

pytestmark = pytest.mark.integration

WEEK = datetime(2026, 6, 8, 0, 0, tzinfo=UTC)


def make_run(task_id: TaskId, actions: list[Action]) -> Run:
    return Run(
        workflow_doc_id=uuid4(),
        workflow_id=WorkflowId(uuid4()),
        task_id=task_id,
        fired_by=[Firing(trigger=ManualTrigger())],
        actions=actions,
    )


def spend(amount_cents: int, *, created_at: datetime) -> Action:
    return Action(
        kind="spend",
        target="doordash.com",
        payload={"amount_cents": amount_cents, "reason": "dinner"},
        created_at=created_at,
    )


def authorize_spend_spec(task_id: TaskId, recorded: list[Action]) -> ToolSpec:
    async def record(action: Action) -> None:
        recorded.append(action)

    context = RunContext(workflow_id=WorkflowId(uuid4()), workflow_doc_id=uuid4(), task_id=task_id, run_id=uuid4())
    return next(t.to_spec() for t in SpendToolSet(context, record).get_tools() if t.name == "authorize_spend")


async def test_weekly_spent_cents_counts_only_this_tasks_spend_inside_window(
    mongo: AsyncMongoClient[dict[str, Any]],
) -> None:
    task = make_task(spend_policy=SpendPolicy(per_order_cents=1000, weekly_cents=5000))
    await task.insert()
    await make_run(
        task.uid,
        [
            spend(700, created_at=WEEK),
            spend(250, created_at=WEEK + timedelta(days=2)),
            spend(999, created_at=WEEK - timedelta(seconds=1)),
            Action(kind="email", target="a@b.com", payload={"subject": "receipt"}, created_at=WEEK + timedelta(days=1)),
        ],
    ).insert()
    await make_run(task.uid, [spend(50, created_at=WEEK + timedelta(days=3))]).insert()
    await make_run(TaskId(uuid4()), [spend(10_000, created_at=WEEK + timedelta(days=2))]).insert()
    assert await weekly_spent_cents(task.uid, since=WEEK) == 1000


async def test_authorize_spend_denied_raises_tool_error_and_records_nothing(
    mongo: AsyncMongoClient[dict[str, Any]],
) -> None:
    await seed_profile()
    task = make_task(spend_policy=None)
    await task.insert()
    recorded: list[Action] = []
    with pytest.raises(ToolError) as excinfo:
        await authorize_spend_spec(task.uid, recorded).invoke(
            {"amount_cents": 500, "merchant": "doordash.com", "reason": "dinner"}
        )
    assert excinfo.value.error_type == "spend_denied"
    assert excinfo.value.detail == (
        "task has no spend policy — activate with --per-order-cap/--weekly-cap to allow spending"
    )
    assert excinfo.value.fix == (
        "ask the user via the email approval gate: send_email the request, "
        "subscribe_to_thread(receipt.thread_id), record the pending decision in state, and end the run"
    )
    assert recorded == []


async def test_authorize_spend_approval_records_the_ledger_action(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    await seed_profile(timezone="America/Los_Angeles")
    task = make_task(spend_policy=SpendPolicy(per_order_cents=2000, weekly_cents=10_000))
    await task.insert()
    recorded: list[Action] = []
    action_id = await authorize_spend_spec(task.uid, recorded).invoke(
        {"amount_cents": 1500, "merchant": "doordash.com", "reason": "weekly groceries"}
    )
    assert [action.id for action in recorded] == [action_id]
    assert (recorded[0].kind, recorded[0].target) == ("spend", "doordash.com")
    assert recorded[0].payload == {"amount_cents": 1500, "reason": "weekly groceries"}


async def test_spend_policy_round_trips_through_bson(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    capped = make_task(spend_policy=SpendPolicy(per_order_cents=2500, weekly_cents=10_000))
    uncapped = make_task(spend_policy=None)
    await capped.insert()
    await uncapped.insert()
    reloaded_capped = await Task.get(capped.uid)
    reloaded_uncapped = await Task.get(uncapped.uid)
    assert reloaded_capped is not None
    assert reloaded_capped.spend_policy == SpendPolicy(per_order_cents=2500, weekly_cents=10_000)
    assert reloaded_uncapped is not None
    assert reloaded_uncapped.spend_policy is None
