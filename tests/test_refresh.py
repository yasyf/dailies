from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pymongo import AsyncMongoClient

from dailies.activation import activate_task
from dailies.connections import NangoCredential, credential_store
from dailies.documents import Task, Workflow
from dailies.models import CronExpr, CronTrigger
from dailies.refresh import REFRESH_TASK_ID, REFRESH_WORKFLOW_ID, seed_refresh_task
from tests.factories import seed_profile

pytestmark = pytest.mark.integration


async def test_seed_refresh_task_is_idempotent_and_creates_one_pair(
    mongo: AsyncMongoClient[dict[str, Any]], state_dir: Path
) -> None:
    first = await seed_refresh_task()
    second = await seed_refresh_task()

    assert first.uid == second.uid == REFRESH_TASK_ID
    assert await Task.find(Task.id == REFRESH_TASK_ID).count() == 1
    assert await Workflow.find(Workflow.workflow_id == REFRESH_WORKFLOW_ID).count() == 1


async def test_seed_refresh_task_seeds_the_weekly_draft(
    mongo: AsyncMongoClient[dict[str, Any]], state_dir: Path
) -> None:
    task = await seed_refresh_task()

    assert task.name == "Profile refresh"
    assert task.gaps == []
    assert task.status == "draft"

    workflow = await Workflow.find_one(Workflow.workflow_id == REFRESH_WORKFLOW_ID)
    assert workflow is not None
    assert workflow.triggers == [CronTrigger(cron_expression=CronExpr("0 7 * * 1"))]
    assert workflow.requires == ["gmail"]
    assert workflow.status == "draft"


async def test_seed_then_activate_flips_task_and_workflow_active(
    mongo: AsyncMongoClient[dict[str, Any]], state_dir: Path
) -> None:
    await seed_refresh_task()
    await seed_profile()
    await credential_store().save(
        "gmail", NangoCredential(connection_id="conn-1", provider_config_key="google-mail")
    )

    await activate_task(REFRESH_TASK_ID, ack_gaps=True, spend_policy=None)

    task = await Task.get(REFRESH_TASK_ID)
    assert task is not None
    assert task.status == "active"
    workflow = await Workflow.find_one(Workflow.workflow_id == REFRESH_WORKFLOW_ID)
    assert workflow is not None
    assert workflow.status == "active"
