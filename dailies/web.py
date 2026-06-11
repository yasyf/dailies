"""Web access for agents: Exa search, plain fetch, Stagehand scrape, and a browser-use fallback."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol

import html2text
import httpx

from dailies.gmail import checked
from dailies.models import FrozenModel

SEARCH_RESULTS = 8
CHROME_MANIFEST = (
    Path("~/Library/Application Support/Google/Chrome/NativeMessagingHosts").expanduser()
    / "com.anthropic.claude_browser_extension.json"
)
CLAUDE_CONFIG = Path("~/.claude.json").expanduser()


class BrowseFailed(RuntimeError):
    """The browser agent finished without producing a result."""


class SearchResult(FrozenModel):
    title: str | None
    url: str
    snippet: str
    published: str | None = None


def chrome_available() -> bool:
    """Whether Claude-in-Chrome is set up for the Claude CLI, enabling native browser tools.

    Requires both the Chrome native messaging host (installed with the Claude desktop
    app) and Claude-in-Chrome enabled in Claude Code (the one-time interactive
    ``/chrome`` setup, recorded in ``~/.claude.json``).
    """
    return (
        CHROME_MANIFEST.exists()
        and Path(json.loads(CHROME_MANIFEST.read_text())["path"]).exists()
        and CLAUDE_CONFIG.exists()
        and bool(json.loads(CLAUDE_CONFIG.read_text()).get("claudeInChromeDefaultEnabled"))
    )


class WebClient(Protocol):
    """One-shot web surface shared by agent tools: search, fetch, and targeted scrape."""

    async def search(self, query: str, *, limit: int = SEARCH_RESULTS) -> list[SearchResult]: ...

    async def fetch(self, url: str) -> str: ...

    async def scrape(self, url: str, instruction: str) -> str: ...


class BrowserClient(Protocol):
    """Runs an autonomous multi-step browser task; the fallback when Claude-in-Chrome is absent."""

    async def browse(self, task: str) -> str: ...


@dataclass(frozen=True, slots=True)
class LiveWebClient:
    """WebClient: Exa search, httpx fetch, and a headless local Stagehand browser for scrape.

    Construction performs no I/O and reads no environment; ``EXA_API_KEY`` and
    ``ANTHROPIC_API_KEY`` are resolved per call so unconfigured machines only
    fail when a tool is actually used.
    """

    EXA_API: ClassVar[str] = "https://api.exa.ai"
    SNIPPET_CHARS: ClassVar[int] = 1_000
    FETCH_LIMIT: ClassVar[int] = 50_000
    SCRAPE_MODEL: ClassVar[str] = "anthropic/claude-haiku-4-5"

    transport: httpx.AsyncBaseTransport | None = None

    async def search(self, query: str, *, limit: int = SEARCH_RESULTS) -> list[SearchResult]:
        async with httpx.AsyncClient(
            base_url=self.EXA_API, headers={"x-api-key": os.environ["EXA_API_KEY"]}, transport=self.transport
        ) as client:
            data = checked(
                await client.post(
                    "/search",
                    json={
                        "query": query,
                        "numResults": limit,
                        "contents": {"text": {"maxCharacters": self.SNIPPET_CHARS}},
                    },
                )
            ).json()
        return [
            SearchResult(
                title=result.get("title"),
                url=result["url"],
                snippet=result.get("text", ""),
                published=result.get("publishedDate"),
            )
            for result in data["results"]
        ]

    async def fetch(self, url: str) -> str:
        async with httpx.AsyncClient(transport=self.transport, follow_redirects=True) as client:
            response = checked(await client.get(url))
        if response.headers.get("content-type", "").startswith("text/html"):
            return html2text.html2text(response.text)[: self.FETCH_LIMIT]
        return response.text[: self.FETCH_LIMIT]

    async def scrape(self, url: str, instruction: str) -> str:
        from stagehand import AsyncStagehand

        async with AsyncStagehand(
            server="local", model_api_key=os.environ["ANTHROPIC_API_KEY"], browserbase_api_key="local"
        ) as client:
            session = await client.sessions.start(
                model_name=self.SCRAPE_MODEL, browser={"type": "local", "launch_options": {"headless": True}}
            )
            try:
                await client.sessions.navigate(id=session.data.session_id, url=url)
                extracted = await client.sessions.extract(id=session.data.session_id, instruction=instruction)
            finally:
                await client.sessions.end(id=session.data.session_id)
        match extracted.data.result:
            case str(result) | {"extraction": str(result)}:
                return result
            case result:
                return json.dumps(result, default=str)


@dataclass(frozen=True, slots=True)
class BrowserUseClient:
    """BrowserClient driving an autonomous browser-use agent in a headless ephemeral chromium."""

    model: str = "claude-sonnet-4-6"
    max_steps: int = 40

    async def browse(self, task: str) -> str:
        from browser_use import Agent, Browser, ChatAnthropic

        history = await Agent(
            task=task, llm=ChatAnthropic(model=self.model), browser=Browser(headless=True, user_data_dir=None)
        ).run(max_steps=self.max_steps)
        if (result := history.final_result()) is None:
            raise BrowseFailed(task)
        return result


def web_client() -> WebClient:
    return LiveWebClient()


def browser_client() -> BrowserClient:
    return BrowserUseClient()
