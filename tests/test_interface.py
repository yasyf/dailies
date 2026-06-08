from __future__ import annotations

import inspect
from typing import Any
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient

from dailies.documents import Run, Task, Workflow
from dailies.interface.presenter import Presenter
from dailies.interface.textual_app import TextualPresenter
from dailies.models import (
    Action,
    ManualTrigger,
    PromptStr,
    SchemaStr,
    StatusUpdate,
    TaskDefinition,
    WorkflowDefinition,
    WorkflowId,
)

pytestmark = pytest.mark.integration


def test_textual_presenter_is_a_presenter() -> None:
    assert isinstance(TextualPresenter(), Presenter)


@pytest.mark.parametrize("name", ["list_tasks", "list_workflows", "list_runs", "get_run", "get_state"])
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
    assert await presenter.get_state(workflow_id) == {}
