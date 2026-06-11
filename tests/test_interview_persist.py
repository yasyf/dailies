from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pymongo import AsyncMongoClient

from dailies.documents import Task, Workflow
from dailies.interview import persist_proposal
from dailies.models import (
    CronExpr,
    CronTrigger,
    TaskDraft,
    TaskProposal,
    WorkflowDraft,
    WorkflowTrigger,
    WorkflowTriggerDraft,
)
from dailies.state import task_db_key, workflow_db_key

pytestmark = pytest.mark.integration


def make_proposal(*, ddl: str = "CREATE TABLE sent (day TEXT)") -> TaskProposal:
    return TaskProposal(
        task=TaskDraft(
            name="Digest",
            description="Daily digest",
            user_input="email me a digest",
            prompt="summarize",
            shared_ddl="CREATE TABLE totals (sent INTEGER)",
        ),
        workflows=[
            WorkflowDraft(
                name="send",
                summary="Sends the digest each morning",
                prompt="send the digest",
                rules=["be brief"],
                ddl=ddl,
                triggers=[CronTrigger(cron_expression=CronExpr("0 9 * * *"))],
            )
        ],
    )


def table_names(path: Path) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return [name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    finally:
        db.close()


async def test_persist_proposal_active(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    task = await persist_proposal(make_proposal(), status="active")
    assert task.status == "active"

    stored = await Task.get(task.uid)
    assert stored is not None
    assert stored.definition.user_input == "email me a digest"
    assert stored.definition.prompt == "summarize"
    assert stored.shared_ddl == "CREATE TABLE totals (sent INTEGER)"

    workflows = await Workflow.find(Workflow.task_id == task.uid).to_list()
    assert len(workflows) == 1
    workflow = workflows[0]
    assert workflow.status == "active"
    assert workflow.version == 1
    assert workflow.definition.summary == "Sends the digest each morning"
    assert workflow.definition.rules == ["be brief"]
    assert workflow.ddl == "CREATE TABLE sent (day TEXT)"
    assert workflow.triggers == [CronTrigger(cron_expression=CronExpr("0 9 * * *"))]


async def test_persist_proposal_draft(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    task = await persist_proposal(make_proposal(), status="draft")
    assert task.status == "draft"
    workflows = await Workflow.find(Workflow.task_id == task.uid).to_list()
    assert [w.status for w in workflows] == ["draft"]


async def test_persist_proposal_creates_state_databases(
    mongo: AsyncMongoClient[dict[str, Any]], state_dir: Path
) -> None:
    task = await persist_proposal(make_proposal(), status="active")
    workflow = await Workflow.find(Workflow.task_id == task.uid).first_or_none()
    assert workflow is not None
    assert table_names(state_dir / task_db_key(task.uid)) == ["totals"]
    assert table_names(state_dir / workflow_db_key(workflow.workflow_id)) == ["sent"]


async def test_persist_proposal_invalid_ddl_persists_nothing(
    mongo: AsyncMongoClient[dict[str, Any]], state_dir: Path
) -> None:
    with pytest.raises(sqlite3.OperationalError):
        await persist_proposal(make_proposal(ddl="CREATE TABEL nope (day TEXT)"), status="draft")
    assert await Task.find_all().count() == 0
    assert await Workflow.find_all().count() == 0
    assert list(state_dir.rglob("*.sqlite")) == []


async def test_persist_resolves_workflow_trigger_to_sibling_id(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    fan_in = TaskProposal(
        task=TaskDraft(name="Scout", description="d", user_input="u", prompt="p"),
        workflows=[
            WorkflowDraft(
                name="tracker",
                summary="s",
                prompt="p",
                ddl="CREATE TABLE t (x TEXT)",
                triggers=[CronTrigger(cron_expression=CronExpr("0 7 * * *"))],
            ),
            WorkflowDraft(
                name="decider",
                summary="s",
                prompt="p",
                ddl="CREATE TABLE d (x TEXT)",
                triggers=[WorkflowTriggerDraft(workflow="tracker")],
            ),
        ],
    )
    task = await persist_proposal(fan_in, status="draft")
    workflows = {w.name: w for w in await Workflow.find(Workflow.task_id == task.uid).to_list()}
    assert workflows["decider"].triggers == [WorkflowTrigger(workflow_id=workflows["tracker"].workflow_id)]
