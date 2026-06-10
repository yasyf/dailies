"""SQLite state databases: one per workflow (private) plus one per task (attached as ``shared``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from pydantic import JsonValue

    from dailies.models import SchemaStr, TaskId, WorkflowId
    from dailies.runtime import RunContext
    from dailies.storage import StateStorage

MAX_ROWS = 200

type StateDump = dict[str, list[dict[str, JsonValue]]]


class StateDatabaseExists(FileExistsError):
    """DDL may only run against a fresh database; evolving an existing one is a migration."""


def workflow_db_key(workflow_id: WorkflowId) -> str:
    return f"workflows/{workflow_id}.sqlite"


def task_db_key(task_id: TaskId) -> str:
    return f"tasks/{task_id}.sqlite"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def row_dict(row: aiosqlite.Row) -> dict[str, JsonValue]:
    return dict(zip(row.keys(), row, strict=True))


async def apply_ddl(storage: StateStorage, key: str, ddl: SchemaStr | None) -> None:
    async with storage.lease(key) as path:
        if path.exists():
            raise StateDatabaseExists(key)
        db = await aiosqlite.connect(path)
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            if ddl is not None:
                await db.executescript(ddl)
            await db.commit()
        finally:
            await db.close()


@asynccontextmanager
async def state_session(
    storage: StateStorage, context: RunContext, *, readonly: bool = False
) -> AsyncIterator[aiosqlite.Connection]:
    async with (
        storage.lease(workflow_db_key(context.workflow_id)) as main,
        storage.lease(task_db_key(context.task_id)) as shared,
    ):
        db = await aiosqlite.connect(f"file:{main}?mode=rw", uri=True)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("ATTACH DATABASE ? AS shared", (f"file:{shared}?mode=rw",))
            if readonly:
                await db.execute("PRAGMA query_only = ON")
            yield db
            if not readonly:
                await db.commit()
        finally:
            await db.close()


async def dump_state(storage: StateStorage, key: str) -> StateDump:
    async with storage.lease(key) as path:
        db = await aiosqlite.connect(f"file:{path}?mode=rw", uri=True)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only = ON")
            tables = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            return {
                name: [row_dict(row) for row in rows]
                for (name,) in tables
                for rows in [await db.execute_fetchall(f"SELECT * FROM {quote_ident(name)} LIMIT {MAX_ROWS}")]
            }
        finally:
            await db.close()
