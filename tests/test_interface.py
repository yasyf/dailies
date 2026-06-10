from __future__ import annotations

import inspect
from typing import Any
from uuid import uuid4

import pytest
from beanie.operators import In
from pymongo import AsyncMongoClient

from dailies.documents import Run, Task, TaskState, Workflow, WorkflowState
from dailies.interface.presenter import BlastRadius, Presenter
from dailies.interface.textual_app import TextualPresenter
from dailies.models import (
    Action,
    ManualTrigger,
    PromptStr,
    SchemaStr,
    StatusUpdate,
    TaskDefinition,
    TaskId,
    WorkflowDefinition,
    WorkflowId,
)

pytestmark = pytest.mark.integration


async def make_task(name: str) -> Task:
    return await Task(
        name=name,
        definition=TaskDefinition(user_input="u", description="d", prompt=PromptStr("p")),
        shared_ddl=SchemaStr("CREATE TABLE shared (k TEXT)"),
    ).insert()


async def make_workflow(task_id: TaskId) -> Workflow:
    return await Workflow(
        task_id=task_id,
        workflow_id=WorkflowId(uuid4()),
        version=1,
        name="w",
        definition=WorkflowDefinition(prompt=PromptStr("p")),
        ddl=SchemaStr("CREATE TABLE t (id TEXT)"),
    ).insert()


async def make_run(workflow: Workflow) -> Run:
    return await Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=workflow.task_id,
        trigger=ManualTrigger(),
    ).insert()


def test_textual_presenter_is_a_presenter() -> None:
    assert isinstance(TextualPresenter(), Presenter)


@pytest.mark.parametrize(
    "name",
    [
        "list_tasks",
        "list_workflows",
        "list_runs",
        "get_run",
        "get_state",
        "get_task_state",
        "blast_radius",
        "delete_task",
    ],
)
def test_presenter_methods_are_coroutines(name: str) -> None:
    assert inspect.iscoroutinefunction(getattr(TextualPresenter, name))


async def test_read_methods_roundtrip(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    task = Task(name="t", definition=TaskDefinition(user_input="u", description="d", prompt=PromptStr("p")))
    await task.insert()
    workflow_id = WorkflowId(uuid4())
    workflow = Workflow(
        task_id=task.uid,
        workflow_id=workflow_id,
        version=1,
        name="w",
        definition=WorkflowDefinition(prompt=PromptStr("p")),
        ddl=SchemaStr("CREATE TABLE t (id TEXT)"),
    )
    await workflow.insert()
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow_id,
        task_id=task.uid,
        trigger=ManualTrigger(),
        status_updates=[StatusUpdate(title="s1"), StatusUpdate(title="s2")],
        actions=[Action(kind="email", target="a@b.com")],
    )
    await run.insert()

    presenter = TextualPresenter()
    assert [t.name for t in await presenter.list_tasks()] == ["t"]
    assert [w.workflow_id for w in await presenter.list_workflows(task.uid)] == [workflow_id]
    assert len(await presenter.list_runs(workflow_id)) == 1

    fetched = await presenter.get_run(run.uid)
    assert [s.title for s in fetched.status_updates] == ["s1", "s2"]
    assert [a.kind for a in fetched.actions] == ["email"]
    assert await presenter.get_state(workflow_id) is None


async def test_delete_task_cascades(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    a = await make_task("a")
    a_workflows = [await make_workflow(a.uid), await make_workflow(a.uid)]
    for workflow in a_workflows:
        await make_run(workflow)
        await WorkflowState(workflow_id=workflow.workflow_id, ddl=workflow.ddl, data={"k": 1}).insert()
    await TaskState(task_id=a.uid, ddl=a.shared_ddl, data={"shared": "a"}).insert()

    b = await make_task("b")
    b_workflow = await make_workflow(b.uid)
    await make_run(b_workflow)
    await WorkflowState(workflow_id=b_workflow.workflow_id, ddl=b_workflow.ddl, data={"k": 2}).insert()
    await TaskState(task_id=b.uid, ddl=b.shared_ddl, data={"shared": "b"}).insert()

    presenter = TextualPresenter()
    assert await presenter.blast_radius(a.uid) == BlastRadius(workflows=2, runs=2)

    await presenter.delete_task(a.uid)

    a_workflow_ids = [w.workflow_id for w in a_workflows]
    assert await Task.get(a.uid) is None
    assert await Workflow.find(Workflow.task_id == a.uid).count() == 0
    assert await Run.find(Run.task_id == a.uid).count() == 0
    assert await WorkflowState.find(In(WorkflowState.workflow_id, a_workflow_ids)).count() == 0
    assert await TaskState.find(TaskState.task_id == a.uid).first_or_none() is None

    assert await Task.get(b.uid) is not None
    assert await Workflow.find(Workflow.task_id == b.uid).count() == 1
    assert await Run.find(Run.task_id == b.uid).count() == 1
    assert await WorkflowState.find(WorkflowState.workflow_id == b_workflow.workflow_id).count() == 1
    assert (b_state := await TaskState.find(TaskState.task_id == b.uid).first_or_none()) is not None
    assert b_state.data == {"shared": "b"}


async def test_get_state_returns_document(mongo: AsyncMongoClient[dict[str, Any]]) -> None:
    presenter = TextualPresenter()
    workflow_id = WorkflowId(uuid4())
    await WorkflowState(
        workflow_id=workflow_id, ddl=SchemaStr("CREATE TABLE t (id TEXT)"), data={"count": 2, "label": "x"}
    ).insert()

    state = await presenter.get_state(workflow_id)
    assert state is not None
    assert state.data == {"count": 2, "label": "x"}
    assert state.updated_at.tzinfo is not None

    task_id = TaskId(uuid4())
    assert await presenter.get_task_state(task_id) is None

    await TaskState(task_id=task_id, ddl=SchemaStr("CREATE TABLE shared (k TEXT)"), data={"shared": 1}).insert()
    task_state = await presenter.get_task_state(task_id)
    assert task_state is not None
    assert task_state.data == {"shared": 1}
