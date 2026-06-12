from __future__ import annotations

import inspect

from dailies.bluebubbles import IMessageClient
from dailies.browser import BrowserBackend
from dailies.gmail import GmailClient
from dailies.onepassword import VaultClient
from dailies.runtime import RunContext
from dailies.storage import StateStorage
from dailies.tools.action import ActionReader, ActionRecorder, ActionToolSet
from dailies.tools.base import Tool, ToolSet, ToolSpec, model_for, tool
from dailies.tools.inputs import BrowseToolSet, EmailToolSet, WebToolSet
from dailies.tools.profile import ProfileToolSet
from dailies.tools.spend import SpendToolSet
from dailies.tools.state import StateToolSet
from dailies.tools.vault import VaultToolSet
from dailies.web import WebClient

TOOLSETS: tuple[type[ToolSet], ...] = (
    StateToolSet,
    ActionToolSet,
    EmailToolSet,
    WebToolSet,
    BrowseToolSet,
    ProfileToolSet,
    VaultToolSet,
    SpendToolSet,
)


def toolset_header(toolset: type[ToolSet]) -> str:
    name = toolset.__name__.removesuffix("ToolSet")
    return f"{name} (requires {', '.join(toolset.integrations)}):" if toolset.integrations else f"{name}:"


def render_catalog() -> str:
    """Render the runtime tool catalog (name: one-line description, grouped by toolset) for prompts.

    Each toolset header names the integrations the toolset requires, e.g.
    ``Email (requires gmail):``, so prompts and the activation checker share
    one tool-to-integration mapping.
    """
    return "\n\n".join(
        "\n".join(
            [
                toolset_header(toolset),
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
    imessage: IMessageClient,
    web: WebClient,
    browser: BrowserBackend,
    vault: VaultClient,
    chrome: bool,
    record: ActionRecorder,
    recorded: ActionReader,
) -> tuple[ToolSet, ...]:
    return (
        StateToolSet(context, storage),
        ActionToolSet(context, gmail, imessage, record, recorded),
        EmailToolSet(context, gmail),
        WebToolSet(web),
        ProfileToolSet(),
        VaultToolSet(context, vault),
        SpendToolSet(context, record),
        *(() if chrome else (BrowseToolSet(context, browser, storage),)),
    )
