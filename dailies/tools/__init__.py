from __future__ import annotations

from dailies.gmail import GmailClient
from dailies.runtime import RunContext
from dailies.storage import StateStorage
from dailies.tools.action import ActionRecorder, ActionToolSet, Notification
from dailies.tools.base import Tool, ToolSet, ToolSpec, model_for, tool
from dailies.tools.inputs import BrowseToolSet, EmailToolSet, WebToolSet
from dailies.tools.state import StateToolSet
from dailies.web import BrowserClient, WebClient


def build_toolsets(
    context: RunContext,
    *,
    storage: StateStorage,
    gmail: GmailClient,
    web: WebClient,
    browser: BrowserClient,
    chrome: bool,
    record: ActionRecorder,
) -> tuple[ToolSet, ...]:
    return (
        StateToolSet(context, storage),
        ActionToolSet(context, gmail, record),
        EmailToolSet(context, gmail),
        WebToolSet(context, web),
        *(() if chrome else (BrowseToolSet(context, browser),)),
    )
