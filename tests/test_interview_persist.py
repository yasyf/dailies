from __future__ import annotations

from typing import Any

import pytest
from pymongo import AsyncMongoClient

from dailies.documents import Task, Workflow
from dailies.interview import persist_proposal
from dailies.models import CronExpr, CronTrigger, TaskDraft, TaskProposal, WorkflowDraft

pytestmark = pytest.mark.integration


def make_proposal() -> TaskProposal:
    return TaskProposal(
        task=TaskDraft(name="Digest", description="Daily digest", user_input="email me a digest", prompt="summarize"),
        workflows=[
            WorkflowDraft(
                name="send",
                prompt="send the digest",
                rules=["be brief"],
                ddl="CREATE TABLE sent (day TEXT)",
                cron_expression="0 9 * * *",
            )
        ],
    )


async def test_persist_proposal_active(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    task = await persist_proposal(make_proposal(), status="active")
    assert task.status == "active"

    stored = await Task.get(task.uid)
    assert stored is not None
    assert stored.definition.user_input == "email me a digest"
    assert stored.definition.prompt == "summarize"

    workflows = await Workflow.find(Workflow.task_id == task.uid).to_list()
    assert len(workflows) == 1
    workflow = workflows[0]
    assert workflow.status == "active"
    assert workflow.version == 1
    assert workflow.definition.rules == ["be brief"]
    assert workflow.ddl == "CREATE TABLE sent (day TEXT)"
    assert workflow.triggers == [CronTrigger(cron_expression=CronExpr("0 9 * * *"))]


async def test_persist_proposal_draft(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    task = await persist_proposal(make_proposal(), status="draft")
    assert task.status == "draft"
    workflows = await Workflow.find(Workflow.task_id == task.uid).to_list()
    assert [w.status for w in workflows] == ["draft"]
