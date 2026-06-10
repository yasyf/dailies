from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from dailies.models import SchemaStr, TaskId, WorkflowId
from dailies.runtime import RunContext
from dailies.state import (
    MAX_ROWS,
    StateDatabaseExists,
    apply_ddl,
    dump_state,
    state_session,
    task_db_key,
    workflow_db_key,
)
from dailies.storage import StateStorage, state_storage

pytestmark = pytest.mark.unit


def make_context(task_id: TaskId | None = None) -> RunContext:
    return RunContext(
        workflow_id=WorkflowId(uuid4()),
        workflow_doc_id=uuid4(),
        task_id=task_id or TaskId(uuid4()),
        run_id=uuid4(),
    )


async def provision(
    storage: StateStorage,
    context: RunContext,
    *,
    ddl: str = "CREATE TABLE t (v INTEGER)",
    shared_ddl: str | None = None,
) -> None:
    await apply_ddl(storage, workflow_db_key(context.workflow_id), SchemaStr(ddl))
    await apply_ddl(storage, task_db_key(context.task_id), SchemaStr(shared_ddl) if shared_ddl else None)


async def test_apply_ddl_creates_schema_in_wal_mode() -> None:
    storage = state_storage()
    context = make_context()
    await provision(storage, context)
    async with storage.lease(workflow_db_key(context.workflow_id)) as path:
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            assert db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == [("t",)]


async def test_apply_ddl_none_creates_empty_database() -> None:
    storage = state_storage()
    key = task_db_key(TaskId(uuid4()))
    await apply_ddl(storage, key, None)
    assert await dump_state(storage, key) == {}


async def test_apply_ddl_refuses_existing_database() -> None:
    storage = state_storage()
    key = workflow_db_key(WorkflowId(uuid4()))
    await apply_ddl(storage, key, SchemaStr("CREATE TABLE t (v INTEGER)"))
    with pytest.raises(StateDatabaseExists):
        await apply_ddl(storage, key, SchemaStr("CREATE TABLE u (v INTEGER)"))


async def test_session_requires_existing_databases() -> None:
    storage = state_storage()
    context = make_context()
    with pytest.raises(sqlite3.OperationalError):
        async with state_session(storage, context):
            pass
    await apply_ddl(storage, workflow_db_key(context.workflow_id), None)
    with pytest.raises(sqlite3.OperationalError):
        async with state_session(storage, context):
            pass


async def test_session_commits_on_clean_exit() -> None:
    storage = state_storage()
    context = make_context()
    await provision(storage, context)
    async with state_session(storage, context) as db:
        await db.execute("INSERT INTO t VALUES (1)")
    async with state_session(storage, context, readonly=True) as db:
        assert [tuple(r) for r in await db.execute_fetchall("SELECT v FROM t")] == [(1,)]


async def test_session_rolls_back_on_error() -> None:
    storage = state_storage()
    context = make_context()
    await provision(storage, context)
    with pytest.raises(RuntimeError):
        async with state_session(storage, context) as db:
            await db.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
    async with state_session(storage, context, readonly=True) as db:
        assert await db.execute_fetchall("SELECT v FROM t") == []


async def test_readonly_session_blocks_writes() -> None:
    storage = state_storage()
    context = make_context()
    await provision(storage, context)
    async with state_session(storage, context, readonly=True) as db:
        with pytest.raises(sqlite3.OperationalError):
            await db.execute("INSERT INTO t VALUES (1)")


async def test_shared_database_visible_across_sibling_workflows() -> None:
    storage = state_storage()
    task_id = TaskId(uuid4())
    a, b = make_context(task_id), make_context(task_id)
    await provision(storage, a, shared_ddl="CREATE TABLE counter (n INTEGER)")
    await apply_ddl(storage, workflow_db_key(b.workflow_id), SchemaStr("CREATE TABLE t (v INTEGER)"))

    async with state_session(storage, a) as db:
        await db.execute("INSERT INTO shared.counter VALUES (1)")
    async with state_session(storage, b) as db:
        assert [tuple(r) for r in await db.execute_fetchall("SELECT n FROM shared.counter")] == [(1,)]


async def test_dump_state_caps_rows() -> None:
    storage = state_storage()
    context = make_context()
    await provision(storage, context)
    async with state_session(storage, context) as db:
        await db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(MAX_ROWS + 5)])
    dump = await dump_state(storage, workflow_db_key(context.workflow_id))
    assert list(dump) == ["t"]
    assert len(dump["t"]) == MAX_ROWS
    assert dump["t"][0] == {"v": 0}
