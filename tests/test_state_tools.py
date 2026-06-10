from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient

from dailies.documents import Task, TaskState, Workflow, WorkflowState
from dailies.models import (
    PromptStr,
    SchemaStr,
    TaskDefinition,
    TaskId,
    WorkflowDefinition,
    WorkflowId,
)
from dailies.runtime import RunContext
from dailies.tools.state import StateToolSet, TaskStateToolSet

pytestmark = pytest.mark.integration


async def make_workflow(task_id: TaskId, *, ddl: str = "CREATE TABLE s (k TEXT)") -> Workflow:
    return await Workflow(
        task_id=task_id,
        workflow_id=WorkflowId(uuid4()),
        version=1,
        name="wf",
        definition=WorkflowDefinition(prompt=PromptStr("p")),
        ddl=SchemaStr(ddl),
        status="active",
    ).insert()


async def make_task(shared_ddl: str | None) -> Task:
    return await Task(
        name="t",
        definition=TaskDefinition(user_input="i", description="d", prompt=PromptStr("p")),
        shared_ddl=SchemaStr(shared_ddl) if shared_ddl is not None else None,
    ).insert()


def ctx(workflow: Workflow) -> RunContext:
    return RunContext(
        workflow_id=workflow.workflow_id, workflow_doc_id=workflow.uid, task_id=workflow.task_id, run_id=uuid4()
    )


async def test_workflow_state_roundtrip(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    workflow = await make_workflow(TaskId(uuid4()))
    tools = StateToolSet(ctx(workflow))

    assert await tools.read_state() == {}
    assert await WorkflowState.find(WorkflowState.workflow_id == workflow.workflow_id).first_or_none() is None
    assert await tools.get_state_value("missing") is None

    await tools.set_state_value("count", 1)
    assert await tools.get_state_value("count") == 1
    assert await tools.read_state() == {"count": 1}

    await tools.merge_state({"count": 2, "label": "x"})
    assert await tools.read_state() == {"count": 2, "label": "x"}

    row = await WorkflowState.find(WorkflowState.workflow_id == workflow.workflow_id).first_or_none()
    assert row is not None and row.ddl == workflow.ddl

    await tools.clear_state()
    assert await tools.read_state() == {}
    assert await WorkflowState.find(WorkflowState.workflow_id == workflow.workflow_id).count() == 0


async def test_task_state_roundtrip(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    task = await make_task("CREATE TABLE shared (k TEXT)")
    workflow = await make_workflow(task.uid)
    tools = TaskStateToolSet(ctx(workflow))

    assert await tools.read_task_state() == {}
    await tools.set_task_state_value("count", 1)
    await tools.merge_task_state({"label": "x"})
    assert await tools.read_task_state() == {"count": 1, "label": "x"}

    row = await TaskState.find(TaskState.task_id == task.uid).first_or_none()
    assert row is not None and row.ddl == task.shared_ddl

    await tools.clear_task_state()
    assert await tools.read_task_state() == {}


async def test_task_state_ddl_none_when_unscoped(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    task = await make_task(None)
    workflow = await make_workflow(task.uid)
    tools = TaskStateToolSet(ctx(workflow))

    await tools.set_task_state_value("k", 1)
    row = await TaskState.find(TaskState.task_id == task.uid).first_or_none()
    assert row is not None and row.ddl is None and row.data == {"k": 1}


async def test_scenario5_shared_counter(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    task = await make_task("CREATE TABLE penalty_state (penalty_counter INTEGER NOT NULL DEFAULT 0)")
    a = TaskStateToolSet(ctx(await make_workflow(task.uid)))
    b = TaskStateToolSet(ctx(await make_workflow(task.uid)))

    await a.set_task_state_value("penalty_counter", 1)
    assert await b.get_task_state_value("penalty_counter") == 1
    await b.set_task_state_value("penalty_counter", await b.get_task_state_value("penalty_counter") + 1)
    assert await a.get_task_state_value("penalty_counter") == 2

    await a.set_task_state_value("from_a", "x")
    await b.set_task_state_value("from_b", "y")
    assert await a.read_task_state() == {"penalty_counter": 2, "from_a": "x", "from_b": "y"}
    assert await TaskState.find(TaskState.task_id == task.uid).count() == 1
