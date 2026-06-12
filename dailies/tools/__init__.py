from __future__ import annotations

import inspect

from dailies.browser import BrowserBackend
from dailies.gmail import GmailClient
from dailies.runtime import RunContext
from dailies.storage import StateStorage
from dailies.tools.action import ActionRecorder, ActionToolSet
from dailies.tools.base import Tool, ToolSet, ToolSpec, model_for, tool
from dailies.tools.inputs import BrowseToolSet, EmailToolSet, WebToolSet
from dailies.tools.state import StateToolSet
from dailies.web import WebClient

TOOLSETS: tuple[type[ToolSet], ...] = (StateToolSet, ActionToolSet, EmailToolSet, WebToolSet, BrowseToolSet)


def render_catalog() -> str:
    """Render the runtime tool catalog (name: one-line description, grouped by toolset) for prompts."""
    return "\n\n".join(
        "\n".join(
            [
                f"{toolset.__name__.removesuffix('ToolSet')}:",
                *(
                    f"- {fn.__name__}: {doc.splitlines()[0]}"
                    for fn in vars(toolset).values()
                    if inspect.isfunction(fn) and getattr(fn, "__tool__", False) and (doc := inspect.getdoc(fn))
                ),
            ]
        )
        for toolset in TOOLSETS
    )


def build_toolsets(
    context: RunContext,
    *,
    storage: StateStorage,
    gmail: GmailClient,
    web: WebClient,
    browser: BrowserBackend,
    chrome: bool,
    record: ActionRecorder,
) -> tuple[ToolSet, ...]:
    return (
        StateToolSet(context, storage),
        ActionToolSet(context, gmail, record),
        EmailToolSet(context, gmail),
        WebToolSet(context, web),
        *(() if chrome else (BrowseToolSet(context, browser, storage),)),
    )
