from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import JsonValue

from dailies.models import FrozenModel
from dailies.state import MAX_ROWS, row_dict, state_session
from dailies.tools.base import ToolSet, tool

if TYPE_CHECKING:
    from dailies.runtime import RunContext
    from dailies.storage import StateStorage

type SqlParam = str | int | float | bool | None


class QueryResult(FrozenModel):
    rows: list[dict[str, JsonValue]]
    truncated: bool


class ExecuteResult(FrozenModel):
    rows_affected: int
    last_insert_rowid: int


@dataclass(frozen=True, slots=True)
class StateToolSet(ToolSet):
    context: RunContext
    storage: StateStorage

    @tool
    async def query_state(self, sql: str, params: list[SqlParam] | None = None) -> QueryResult:
        """Run a read-only SQL query against this workflow's state database.

        Private tables live in the main database; tables shared across the task's
        workflows live in the attached `shared` database (e.g. SELECT * FROM
        shared.totals). Use ? placeholders with params for values. At most 200 rows
        are returned; truncated=true means the query matched more — narrow it with
        WHERE or LIMIT/OFFSET.
        """
        async with state_session(self.storage, self.context, readonly=True) as db:
            cursor = await db.execute(sql, params or [])
            rows = list(await cursor.fetchmany(MAX_ROWS + 1))
        return QueryResult(rows=[row_dict(row) for row in rows[:MAX_ROWS]], truncated=len(rows) > MAX_ROWS)

    @tool
    async def execute_state(self, sql: str, params: list[SqlParam] | None = None) -> ExecuteResult:
        """Execute one SQL write statement against this workflow's state database.

        Accepts INSERT, UPDATE, DELETE, and schema changes (CREATE TABLE, ALTER
        TABLE, DROP, CREATE INDEX). Address shared tables with the shared. prefix.
        One statement per call; use ? placeholders with params for values. Returns
        the affected row count (-1 for schema changes) and the rowid of the last
        insert.
        """
        async with state_session(self.storage, self.context) as db:
            cursor = await db.execute(sql, params or [])
            return ExecuteResult(rows_affected=cursor.rowcount, last_insert_rowid=cursor.lastrowid or 0)

    @tool
    async def describe_state(self) -> dict[str, list[str]]:
        """Return the current state schema as CREATE statements, keyed by database:
        "main" for this workflow's private tables, "shared" for tables shared
        across the task's workflows."""
        async with state_session(self.storage, self.context, readonly=True) as db:
            return {
                schema: [
                    sql
                    for (sql,) in await db.execute_fetchall(
                        f"SELECT sql FROM {schema}.sqlite_master "
                        "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
                    )
                ]
                for schema in ("main", "shared")
            }
