from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from dailies.web import BrowserUseClient, LiveWebClient, SearchResult, browser_client, chrome_available, web_client

pytestmark = pytest.mark.unit

EXA_RESPONSE = {
    "results": [
        {"title": "Example", "url": "https://example.com", "text": "snippet", "publishedDate": "2026-01-01"},
        {"url": "https://no-title.com"},
    ]
}


def config_for(target: Path, *, enabled: bool) -> Path:
    target.write_text(json.dumps({"claudeInChromeDefaultEnabled": enabled}))
    return target


def test_chrome_available_false_without_claude_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("dailies.web.CLAUDE_CONFIG", tmp_path / "missing-claude.json")
    assert chrome_available() is False


def test_chrome_available_false_when_not_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("dailies.web.CLAUDE_CONFIG", config_for(tmp_path / "claude.json", enabled=False))
    assert chrome_available() is False


def test_chrome_available_true_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("dailies.web.CLAUDE_CONFIG", config_for(tmp_path / "claude.json", enabled=True))
    assert chrome_available() is True


async def test_search_sends_exa_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=EXA_RESPONSE)

    results = await LiveWebClient(transport=httpx.MockTransport(handler)).search("anthropic", limit=2)
    (request,) = seen
    assert str(request.url) == "https://api.exa.ai/search"
    assert request.headers["x-api-key"] == "exa-key"
    assert json.loads(request.content) == {
        "query": "anthropic",
        "numResults": 2,
        "contents": {"text": {"maxCharacters": LiveWebClient.SNIPPET_CHARS}},
    }
    assert results == [
        SearchResult(title="Example", url="https://example.com", snippet="snippet", published="2026-01-01"),
        SearchResult(title=None, url="https://no-title.com", snippet="", published=None),
    ]


async def test_search_reads_key_only_when_called(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    client = LiveWebClient()
    with pytest.raises(KeyError):
        await client.search("q")


async def test_fetch_returns_plain_text() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="plain body"))
    assert await LiveWebClient(transport=transport).fetch("https://example.com/raw") == "plain body"


async def test_fetch_renders_html_to_markdown() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=b"<html><body><h1>Title</h1></body></html>", headers={"content-type": "text/html"}
        )
    )
    assert (await LiveWebClient(transport=transport).fetch("https://example.com")).strip() == "# Title"


async def test_fetch_truncates_at_limit() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="x" * (LiveWebClient.FETCH_LIMIT + 1)))
    assert len(await LiveWebClient(transport=transport).fetch("https://example.com")) == LiveWebClient.FETCH_LIMIT


def test_factories_return_live_clients() -> None:
    assert isinstance(web_client(), LiveWebClient)
    assert isinstance(browser_client(), BrowserUseClient)
