from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pymongo import AsyncMongoClient

from dailies.documents import Run, Task, Workflow
from dailies.interface.presenter import BlastRadius, Presenter
from dailies.interface.textual_app import TextualPresenter
from dailies.models import (
    Action,
    Firing,
    ManualTrigger,
    SchemaStr,
    StatusUpdate,
    TaskId,
)
from dailies.state import apply_ddl, task_db_key, workflow_db_key
from dailies.storage import state_storage
from tests.factories import make_run, make_task, make_workflow

pytestmark = pytest.mark.integration


async def provision_task(name: str) -> Task:
    task = await make_task(name=name, shared_ddl=SchemaStr("CREATE TABLE shared (k TEXT)")).insert()
    await apply_ddl(state_storage(), task_db_key(task.uid), task.shared_ddl)
    return task


async def provision_workflow(task_id: TaskId) -> Workflow:
    wf = await make_workflow(task_id=task_id, status="draft").insert()
    await apply_ddl(state_storage(), workflow_db_key(wf.workflow_id), wf.ddl)
    return wf


async def provision_run(workflow: Workflow) -> Run:
    return await make_run(workflow).insert()


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
    task = await provision_task("t")
    workflow = await provision_workflow(task.uid)
    run = Run(
        workflow_doc_id=workflow.uid,
        workflow_id=workflow.workflow_id,
        task_id=task.uid,
        fired_by=[Firing(trigger=ManualTrigger())],
        status_updates=[StatusUpdate(title="s1"), StatusUpdate(title="s2")],
        actions=[Action(kind="email", target="a@b.com")],
    )
    await run.insert()

    presenter = TextualPresenter()
    assert [t.name for t in await presenter.list_tasks()] == ["t"]
    assert [w.workflow_id for w in await presenter.list_workflows(task.uid)] == [workflow.workflow_id]
    assert len(await presenter.list_runs(workflow.workflow_id)) == 1

    fetched = await presenter.get_run(run.uid)
    assert [s.title for s in fetched.status_updates] == ["s1", "s2"]
    assert [a.kind for a in fetched.actions] == ["email"]
    assert await presenter.get_state(workflow.workflow_id) == {"t": []}


async def test_delete_task_cascades(mongo: AsyncMongoClient[dict[str, Any]], state_dir: Path) -> None:
    a = await provision_task("a")
    a_workflows = [await provision_workflow(a.uid), await provision_workflow(a.uid)]
    for workflow in a_workflows:
        await provision_run(workflow)

    b = await provision_task("b")
    b_workflow = await provision_workflow(b.uid)
    await provision_run(b_workflow)

    presenter = TextualPresenter()
    assert await presenter.blast_radius(a.uid) == BlastRadius(workflows=2, runs=2)

    await presenter.delete_task(a.uid)

    assert await Task.get(a.uid) is None
    assert await Workflow.find(Workflow.task_id == a.uid).count() == 0
    assert await Run.find(Run.task_id == a.uid).count() == 0
    assert not (state_dir / task_db_key(a.uid)).exists()
    assert not any((state_dir / workflow_db_key(w.workflow_id)).exists() for w in a_workflows)

    assert await Task.get(b.uid) is not None
    assert await Workflow.find(Workflow.task_id == b.uid).count() == 1
    assert await Run.find(Run.task_id == b.uid).count() == 1
    assert (state_dir / task_db_key(b.uid)).exists()
    assert (state_dir / workflow_db_key(b_workflow.workflow_id)).exists()


async def test_get_state_dumps_live_tables(mongo: AsyncMongoClient[dict[str, Any]], state_dir: Path) -> None:
    task = await provision_task("t")
    workflow = await provision_workflow(task.uid)

    db = sqlite3.connect(state_dir / workflow_db_key(workflow.workflow_id))
    with db:
        db.execute("INSERT INTO t VALUES ('a'), ('b')")
    db.close()
    db = sqlite3.connect(state_dir / task_db_key(task.uid))
    with db:
        db.execute("INSERT INTO shared VALUES ('x')")
    db.close()

    presenter = TextualPresenter()
    assert await presenter.get_state(workflow.workflow_id) == {"t": [{"id": "a"}, {"id": "b"}]}
    assert await presenter.get_task_state(task.uid) == {"shared": [{"k": "x"}]}
