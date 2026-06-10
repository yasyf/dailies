from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from pydantic import JsonValue

from dailies.documents import Task, TaskState, Workflow, WorkflowState
from dailies.models import new_uuid, utcnow
from dailies.runtime import RunContext
from dailies.tools.base import ToolSet, tool


@dataclass(frozen=True, slots=True)
class StateStore:
    document: type[WorkflowState] | type[TaskState]
    filter: dict[str, UUID]
    ddl: Callable[[], Awaitable[str | None]]

    async def read(self) -> dict[str, JsonValue]:
        row = await self.document.find_one(self.filter)
        return row.data if row else {}

    async def get(self, key: str) -> JsonValue:
        return (await self.read()).get(key)

    async def write(self, fields: dict[str, JsonValue]) -> None:
        await self.document.find_one(self.filter).update(
            {
                "$set": {f"data.{key}": value for key, value in fields.items()} | {"updated_at": utcnow()},
                "$setOnInsert": {"_id": new_uuid(), "ddl": await self.ddl(), "created_at": utcnow()},
            },
            upsert=True,
        )

    async def clear(self) -> None:
        await self.document.find_one(self.filter).delete()


@dataclass(frozen=True, slots=True)
class StateToolSet(ToolSet):
    context: RunContext

    @property
    def store(self) -> StateStore:
        return StateStore(WorkflowState, {"workflow_id": self.context.workflow_id}, self.workflow_ddl)

    async def workflow_ddl(self) -> str:
        return (await Workflow.get(self.context.workflow_doc_id)).ddl

    @tool
    async def read_state(self) -> dict[str, JsonValue]:
        """Return the full stored state for the current workflow."""
        return await self.store.read()

    @tool
    async def get_state_value(self, key: str) -> JsonValue:
        """Return a single stored state value by key (null if unset)."""
        return await self.store.get(key)

    @tool
    async def set_state_value(self, key: str, value: JsonValue) -> None:
        """Set a single stored state value."""
        await self.store.write({key: value})

    @tool
    async def merge_state(self, patch: dict[str, JsonValue]) -> None:
        """Shallow-merge a patch into the stored state."""
        await self.store.write(patch)

    @tool
    async def clear_state(self) -> None:
        """Remove all stored state for the current workflow."""
        await self.store.clear()


@dataclass(frozen=True, slots=True)
class TaskStateToolSet(ToolSet):
    context: RunContext

    @property
    def store(self) -> StateStore:
        return StateStore(TaskState, {"task_id": self.context.task_id}, self.task_ddl)

    async def task_ddl(self) -> str | None:
        return (await Task.get(self.context.task_id)).shared_ddl

    @tool
    async def read_task_state(self) -> dict[str, JsonValue]:
        """Return the full stored state shared across the current task's workflows."""
        return await self.store.read()

    @tool
    async def get_task_state_value(self, key: str) -> JsonValue:
        """Return a single shared task-state value by key (null if unset)."""
        return await self.store.get(key)

    @tool
    async def set_task_state_value(self, key: str, value: JsonValue) -> None:
        """Set a single shared task-state value."""
        await self.store.write({key: value})

    @tool
    async def merge_task_state(self, patch: dict[str, JsonValue]) -> None:
        """Shallow-merge a patch into the shared task state."""
        await self.store.write(patch)

    @tool
    async def clear_task_state(self) -> None:
        """Remove all shared state for the current task."""
        await self.store.clear()
