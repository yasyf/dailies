from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pymongo import AsyncMongoClient

from dailies.documents import Task, Workflow
from dailies.models import CronExpr, CronTrigger
from dailies.refresh import REFRESH_TASK_ID, REFRESH_WORKFLOW_ID, seed_refresh_task

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
