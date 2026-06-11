"""Per-workflow browser sessions: a pluggable backend plus storage_state persistence and cookie import.

The profile is a single Playwright ``storage_state`` blob (cookies + localStorage) keyed
``browser/<workflow_id>.json`` through :class:`~dailies.storage.StateStorage`, so it rides the same
abstraction as the SQLite state and a remote (Modal) backend gets it for free. The same blob is what
``dly browser import-cookies`` seeds and what a future Browserbase/Anchor backend would replay over CDP.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from http.cookiejar import Cookie, MozillaCookieJar
from typing import TYPE_CHECKING, ClassVar, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from dailies.models import WorkflowId
    from dailies.storage import StateStorage

COOKIE_BROWSERS = ("chrome", "chromium", "brave", "edge", "arc", "opera", "vivaldi", "firefox", "safari")

type StorageCookie = dict[str, str | float | bool]


class BrowseFailed(RuntimeError):
    """The browser agent finished without producing a result."""


def browser_profile_key(workflow_id: WorkflowId) -> str:
    return f"browser/{workflow_id}.json"


class BrowserBackend(Protocol):
    """Runs an autonomous multi-step browser task against a workflow's persistent profile.

    ``profile`` is the leased path to the workflow's ``storage_state`` blob; the local backend loads
    and saves it in place, while a remote backend would interpret it as a provider context handle.
    """

    async def browse(self, task: str, *, profile: Path) -> str: ...


@dataclass(frozen=True, slots=True)
class BrowserUseBackend:
    """BrowserBackend driving a headless browser-use agent, persisting cookies/localStorage per workflow."""

    model: ClassVar[str] = "claude-sonnet-4-6"
    max_steps: ClassVar[int] = 40

    async def browse(self, task: str, *, profile: Path) -> str:
        from browser_use import Agent, Browser, ChatAnthropic

        browser = Browser(headless=True, user_data_dir=None, storage_state=profile, keep_alive=True)
        try:
            history = await Agent(task=task, llm=ChatAnthropic(model=self.model), browser=browser).run(
                max_steps=self.max_steps
            )
        finally:
            await browser.kill()
        if (result := history.final_result()) is None:
            raise BrowseFailed(task)
        return result


def browser_backend() -> BrowserBackend:
    match os.environ.get("DAILIES_BROWSER_BACKEND", "local"):
        case "local":
            return BrowserUseBackend()
        case other:
            raise ValueError(f"unsupported browser backend: {other!r}")


def domain_matches(domain: str, wanted: tuple[str, ...]) -> bool:
    return any((bare := domain.lstrip(".")) == d or bare.endswith(f".{d}") for d in wanted)


def jar_cookie(cookie: Cookie) -> StorageCookie:
    return {
        "name": cookie.name,
        "value": cookie.value or "",
        "domain": cookie.domain,
        "path": cookie.path,
        "expires": float(cookie.expires) if cookie.expires else -1.0,
        "httpOnly": cookie.has_nonstandard_attr("HttpOnly"),
        "secure": cookie.secure,
        "sameSite": "Lax",
    }


def state_cookie(cookie: dict[str, object]) -> StorageCookie:
    return {
        "name": cookie["name"],
        "value": cookie["value"],
        "domain": cookie["domain"],
        "path": cookie.get("path", "/"),
        "expires": cookie.get("expires", -1.0),
        "httpOnly": cookie.get("httpOnly", False),
        "secure": cookie.get("secure", False),
        "sameSite": cookie.get("sameSite", "Lax"),
    }


def read_local_cookies(source_browser: str, domains: tuple[str, ...]) -> list[StorageCookie]:
    import browser_cookie3

    return [jar_cookie(c) for c in getattr(browser_cookie3, source_browser)() if domain_matches(c.domain, domains)]


def load_cookie_file(path: Path, domains: tuple[str, ...]) -> list[StorageCookie]:
    match json.loads(text) if (text := path.read_text()).lstrip().startswith("{") else None:
        case {"cookies": list(cookies)}:
            return [state_cookie(c) for c in cookies if domain_matches(str(c["domain"]), domains)]
        case _:
            (jar := MozillaCookieJar()).load(str(path), ignore_discard=True, ignore_expires=True)
            return [jar_cookie(c) for c in jar if domain_matches(c.domain, domains)]


def merge_cookies(existing: list[StorageCookie], incoming: list[StorageCookie]) -> list[StorageCookie]:
    return list(({(c["name"], c["domain"], c["path"]): c for c in existing + incoming}).values())


async def import_cookies(
    storage: StateStorage,
    workflow_id: WorkflowId,
    *,
    domains: tuple[str, ...],
    source_browser: str = "chrome",
    from_file: Path | None = None,
) -> int:
    cookies = load_cookie_file(from_file, domains) if from_file else read_local_cookies(source_browser, domains)
    async with storage.lease(browser_profile_key(workflow_id)) as path:
        state = json.loads(path.read_text()) if path.exists() else {"cookies": [], "origins": []}
        state["cookies"] = merge_cookies(state.get("cookies", []), cookies)
        path.write_text(json.dumps(state, indent=2))
    return len(cookies)
