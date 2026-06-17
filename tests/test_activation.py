from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient

from dailies.activation import ActivationError, Problem, TaskNotFound, activate_task, latest_workflows
from dailies.connections import INTEGRATIONS, WizardCredential, credential_store, unready_fix
from dailies.documents import Task, Workflow
from dailies.models import (
    SpendPolicy,
    TaskId,
    WorkflowId,
    new_uuid,
)
from tests.factories import make_task, make_workflow, seed_profile

pytestmark = pytest.mark.integration

ACK_FIX = "review it, then re-run with --ack-gaps"
PROFILE_PROBLEM = Problem(detail="profile is not seeded", fix="run `dly profile init`")


async def test_latest_workflows_returns_max_version_per_id_regardless_of_status(
    mongo: AsyncMongoClient[dict[str, Any]],
) -> None:
    task = make_task()
    await task.insert()
    repeat_id = WorkflowId(new_uuid())
    superseded = make_workflow(task_id=task.uid, status="draft", workflow_id=repeat_id, version=1)
    newest = make_workflow(task_id=task.uid, status="draft", workflow_id=repeat_id, version=2)
    newest.status = "inactive"
    solo = make_workflow(task_id=task.uid, status="draft")
    foreign = make_workflow(task_id=TaskId(uuid4()), status="draft")
    for workflow in (superseded, newest, solo, foreign):
        await workflow.insert()
    latest = {workflow.workflow_id: workflow.version for workflow in await latest_workflows(task.uid)}
    assert latest == {repeat_id: 2, solo.workflow_id: 1}


async def test_activate_flips_only_latest_versions_and_sets_spend_policy(
    mongo: AsyncMongoClient[dict[str, Any]],
) -> None:
    await credential_store().save("onepassword", WizardCredential(values={"OP_SERVICE_ACCOUNT_TOKEN": "ops_token"}))
    await seed_profile()
    task = make_task()
    await task.insert()
    chase_id = WorkflowId(new_uuid())
    superseded = make_workflow(task_id=task.uid, status="draft", workflow_id=chase_id, version=1)
    newest = make_workflow(task_id=task.uid, status="draft", workflow_id=chase_id, version=2, requires=["onepassword"])
    solo = make_workflow(task_id=task.uid, status="draft")
    for workflow in (superseded, newest, solo):
        await workflow.insert()

    policy = SpendPolicy(per_order_cents=2000, weekly_cents=10_000)
    activated = await activate_task(task.uid, ack_gaps=False, spend_policy=policy)

    assert activated.status == "active"
    reloaded = await Task.get(task.uid)
    assert reloaded is not None
    assert reloaded.status == "active"
    assert reloaded.spend_policy == policy
    statuses = {(w.workflow_id, w.version): w.status async for w in Workflow.find(Workflow.task_id == task.uid)}
    assert statuses == {(chase_id, 1): "draft", (chase_id, 2): "active", (solo.workflow_id, 1): "active"}


async def test_activate_without_caps_keeps_existing_spend_policy(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    await seed_profile()
    existing = SpendPolicy(per_order_cents=500, weekly_cents=2_500)
    task = make_task(spend_policy=existing)
    await task.insert()
    await make_workflow(task_id=task.uid, status="draft").insert()
    await activate_task(task.uid, ack_gaps=False, spend_policy=None)
    reloaded = await Task.get(task.uid)
    assert reloaded is not None
    assert reloaded.spend_policy == existing


async def test_failure_collects_every_problem_in_order(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    task = make_task(gaps=["push notifications to a phone", "no calendar access"])
    await task.insert()
    await make_workflow(task_id=task.uid, status="draft", requires=["onepassword"]).insert()
    await make_workflow(task_id=task.uid, status="draft", requires=["gmail", "onepassword"]).insert()

    with pytest.raises(ActivationError) as excinfo:
        await activate_task(task.uid, ack_gaps=False, spend_policy=None)

    assert str(excinfo.value) == "5 problems block activation"
    assert excinfo.value.problems == [
        Problem(detail="unacknowledged gap: push notifications to a phone", fix=ACK_FIX),
        Problem(detail="unacknowledged gap: no calendar access", fix=ACK_FIX),
        PROFILE_PROBLEM,
        Problem(detail="integration gmail is not ready", fix=await unready_fix(INTEGRATIONS["gmail"])),
        Problem(detail="integration onepassword is not ready", fix=await unready_fix(INTEGRATIONS["onepassword"])),
    ]
    reloaded = await Task.get(task.uid)
    assert reloaded is not None
    assert reloaded.status == "draft"
    assert [w.status async for w in Workflow.find(Workflow.task_id == task.uid)] == ["draft", "draft"]


async def test_ack_gaps_clears_only_gap_problems(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    task = make_task(gaps=["push notifications to a phone"])
    await task.insert()
    await make_workflow(task_id=task.uid, status="draft", requires=["onepassword"]).insert()
    with pytest.raises(ActivationError) as excinfo:
        await activate_task(task.uid, ack_gaps=True, spend_policy=None)
    assert excinfo.value.problems == [
        PROFILE_PROBLEM,
        Problem(detail="integration onepassword is not ready", fix=await unready_fix(INTEGRATIONS["onepassword"])),
    ]


async def test_activation_creates_no_state_databases(
    mongo: AsyncMongoClient[dict[str, Any]], state_dir: Path
) -> None:
    await seed_profile()
    task = make_task()
    await task.insert()
    await make_workflow(task_id=task.uid, status="draft").insert()
    await activate_task(task.uid, ack_gaps=False, spend_policy=None)
    assert list(state_dir.rglob("*.sqlite")) == []


async def test_activate_unknown_task_raises_task_not_found(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    unknown = TaskId(uuid4())
    with pytest.raises(TaskNotFound, match=f"no task {unknown} — run `dly tasks` to list tasks"):
        await activate_task(unknown, ack_gaps=False, spend_policy=None)
