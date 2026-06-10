from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from dailies.models import SchemaStr, TaskId, WorkflowId
from dailies.runtime import RunContext
from dailies.state import MAX_ROWS, apply_ddl, state_session, task_db_key, workflow_db_key
from dailies.storage import StateStorage, state_storage
from dailies.tools.state import ExecuteResult, StateToolSet

pytestmark = pytest.mark.unit

ITEMS_DDL = "CREATE TABLE items (name TEXT, qty INTEGER)"


def make_context(task_id: TaskId | None = None) -> RunContext:
    return RunContext(
        workflow_id=WorkflowId(uuid4()),
        workflow_doc_id=uuid4(),
        task_id=task_id or TaskId(uuid4()),
        run_id=uuid4(),
    )


async def make_tools(
    storage: StateStorage,
    *,
    task_id: TaskId | None = None,
    ddl: str = ITEMS_DDL,
    shared_ddl: str | None = None,
) -> StateToolSet:
    context = make_context(task_id)
    await apply_ddl(storage, workflow_db_key(context.workflow_id), SchemaStr(ddl))
    if task_id is None:
        await apply_ddl(storage, task_db_key(context.task_id), SchemaStr(shared_ddl) if shared_ddl else None)
    return StateToolSet(context, storage)


async def test_execute_and_query_roundtrip() -> None:
    tools = await make_tools(state_storage())
    result = await tools.execute_state("INSERT INTO items VALUES (?, ?)", ["kibble", 3])
    assert result == ExecuteResult(rows_affected=1, last_insert_rowid=1)

    query = await tools.query_state("SELECT name, qty FROM items WHERE qty > ?", [1])
    assert query.rows == [{"name": "kibble", "qty": 3}]
    assert query.truncated is False


async def test_query_truncates_at_max_rows() -> None:
    tools = await make_tools(state_storage())
    async with state_session(tools.storage, tools.context) as db:
        await db.executemany("INSERT INTO items VALUES (?, ?)", [("x", i) for i in range(MAX_ROWS + 1)])
    query = await tools.query_state("SELECT * FROM items")
    assert len(query.rows) == MAX_ROWS
    assert query.truncated is True


async def test_query_rejects_writes() -> None:
    tools = await make_tools(state_storage())
    with pytest.raises(sqlite3.OperationalError):
        await tools.query_state("INSERT INTO items VALUES ('x', 1)")


async def test_shared_counter_across_sibling_workflows() -> None:
    storage = state_storage()
    task_id = TaskId(uuid4())
    await apply_ddl(
        storage,
        task_db_key(task_id),
        SchemaStr("CREATE TABLE penalty_state (penalty_counter INTEGER NOT NULL DEFAULT 0)"),
    )
    a = await make_tools(storage, task_id=task_id)
    b = await make_tools(storage, task_id=task_id)

    await a.execute_state("INSERT INTO shared.penalty_state (penalty_counter) VALUES (1)")
    await b.execute_state("UPDATE shared.penalty_state SET penalty_counter = penalty_counter + 1")
    query = await a.query_state("SELECT penalty_counter FROM shared.penalty_state")
    assert query.rows == [{"penalty_counter": 2}]


async def test_alter_table_reflected_in_describe() -> None:
    tools = await make_tools(state_storage())
    result = await tools.execute_state("ALTER TABLE items ADD COLUMN price REAL")
    assert result.rows_affected == -1

    described = await tools.describe_state()
    assert described["main"] == [f"{ITEMS_DDL[:-1]}, price REAL)"]
    assert described["shared"] == []


async def test_describe_lists_main_and_shared_schemas() -> None:
    tools = await make_tools(state_storage(), shared_ddl="CREATE TABLE totals (n INTEGER)")
    assert await tools.describe_state() == {"main": [ITEMS_DDL], "shared": ["CREATE TABLE totals (n INTEGER)"]}


async def test_connection_state_does_not_survive_across_calls() -> None:
    tools = await make_tools(state_storage())
    await tools.execute_state("CREATE TEMP TABLE scratch (v INTEGER)")
    with pytest.raises(sqlite3.OperationalError):
        await tools.query_state("SELECT * FROM scratch")
