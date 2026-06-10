from __future__ import annotations

from dailies.runtime import RunContext
from dailies.storage import StateStorage
from dailies.tools.action import ActionToolSet, Notification
from dailies.tools.base import Tool, ToolSet, ToolSpec, model_for, tool
from dailies.tools.inputs import BrowserToolSet, EmailMessage, EmailToolSet
from dailies.tools.state import StateToolSet


def build_toolsets(context: RunContext, *, storage: StateStorage) -> tuple[ToolSet, ...]:
    return (
        StateToolSet(context, storage),
        ActionToolSet(context),
        EmailToolSet(context),
        BrowserToolSet(context),
    )
