from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue

from dailies.runtime import RunContext
from dailies.tools.base import ToolSet, tool

__all__ = ["StateToolSet"]


@dataclass(frozen=True, slots=True)
class StateToolSet(ToolSet):
    context: RunContext

    @tool
    async def read_state(self) -> dict[str, JsonValue]:
        """Return the full stored state for the current workflow."""
        raise NotImplementedError

    @tool
    async def get_state_value(self, key: str) -> JsonValue:
        """Return a single stored state value by key."""
        raise NotImplementedError

    @tool
    async def set_state_value(self, key: str, value: JsonValue) -> None:
        """Set a single stored state value."""
        raise NotImplementedError

    @tool
    async def merge_state(self, patch: dict[str, JsonValue]) -> None:
        """Shallow-merge a patch into the stored state."""
        raise NotImplementedError

    @tool
    async def clear_state(self) -> None:
        """Remove all stored state for the current workflow."""
        raise NotImplementedError
