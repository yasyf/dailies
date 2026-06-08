from __future__ import annotations

from dailies.runtime import RunContext
from dailies.tools.action import ActionToolSet, Notification
from dailies.tools.base import Tool, ToolSet, ToolSpec, model_for, tool
from dailies.tools.inputs import BrowserToolSet, EmailMessage, EmailToolSet
from dailies.tools.state import StateToolSet

__all__ = [
    "ActionToolSet",
    "BrowserToolSet",
    "EmailMessage",
    "EmailToolSet",
    "Notification",
    "StateToolSet",
    "Tool",
    "ToolSet",
    "ToolSpec",
    "build_toolsets",
    "model_for",
    "tool",
]


def build_toolsets(context: RunContext) -> tuple[ToolSet, ...]:
    return (
        StateToolSet(context),
        ActionToolSet(context),
        EmailToolSet(context),
        BrowserToolSet(context),
    )
