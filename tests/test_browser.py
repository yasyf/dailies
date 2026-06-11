from __future__ import annotations

import json
import os
from http.cookiejar import Cookie
from pathlib import Path

import browser_cookie3
import pytest

from dailies.browser import (
    BrowserUseBackend,
    browser_backend,
    browser_profile_key,
    domain_matches,
    import_cookies,
    jar_cookie,
    load_cookie_file,
    merge_cookies,
    read_local_cookies,
)
from dailies.models import WorkflowId, new_uuid
from dailies.storage import state_storage

pytestmark = pytest.mark.unit

NETSCAPE = """# Netscape HTTP Cookie File
.github.com\tTRUE\t/\tTRUE\t0\tsid\tabc123
api.github.com\tFALSE\t/\tFALSE\t1893456000\ttoken\txyz
.other.com\tTRUE\t/\tFALSE\t0\tjunk\tnope
"""

STORAGE_STATE = {
    "cookies": [
        {
            "name": "sid",
            "value": "abc",
            "domain": ".github.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        },
        {"name": "x", "value": "y", "domain": "example.com", "path": "/"},
    ],
    "origins": [{"origin": "https://github.com", "localStorage": [{"name": "k", "value": "v"}]}],
}


def cookie(
    name: str,
    value: str,
    domain: str,
    *,
    path: str = "/",
    secure: bool = False,
    http_only: bool = False,
    expires: int | None = None,
) -> Cookie:
    dot = domain.startswith(".")
    return Cookie(
        0,
        name,
        value,
        None,
        False,
        domain,
        dot,
        dot,
        path,
        True,
        secure,
        expires,
        False,
        None,
        None,
        {"HttpOnly": ""} if http_only else {},
    )


@pytest.mark.parametrize(
    ("domain", "wanted", "expected"),
    [
        (".github.com", ("github.com",), True),
        ("github.com", ("github.com",), True),
        ("api.github.com", ("github.com",), True),
        ("notgithub.com", ("github.com",), False),
        ("github.com.evil.com", ("github.com",), False),
        ("x.example.org", ("github.com", "example.org"), True),
    ],
)
def test_domain_matches(domain: str, wanted: tuple[str, ...], expected: bool) -> None:
    assert domain_matches(domain, wanted) is expected


def test_jar_cookie_maps_to_playwright_shape() -> None:
    assert jar_cookie(cookie("sid", "abc", ".github.com", secure=True, http_only=True, expires=1893456000)) == {
        "name": "sid",
        "value": "abc",
        "domain": ".github.com",
        "path": "/",
        "expires": 1893456000.0,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


def test_jar_cookie_session_cookie_expires_is_negative_one() -> None:
    assert jar_cookie(cookie("s", "v", ".x.com", expires=None))["expires"] == -1.0
    assert jar_cookie(cookie("s", "v", ".x.com", expires=0))["expires"] == -1.0


def test_load_cookie_file_netscape(tmp_path: Path) -> None:
    (path := tmp_path / "cookies.txt").write_text(NETSCAPE)
    cookies = load_cookie_file(path, ("github.com",))
    assert sorted(c["name"] for c in cookies) == ["sid", "token"]
    assert {c["domain"] for c in cookies} == {".github.com", "api.github.com"}


def test_load_cookie_file_storage_state_json(tmp_path: Path) -> None:
    (path := tmp_path / "state.json").write_text(json.dumps(STORAGE_STATE))
    cookies = load_cookie_file(path, ("github.com",))
    assert [c["name"] for c in cookies] == ["sid"]
    assert cookies[0]["httpOnly"] is True


def test_load_cookie_file_storage_state_normalizes_defaults(tmp_path: Path) -> None:
    (path := tmp_path / "state.json").write_text(json.dumps(STORAGE_STATE))
    only = load_cookie_file(path, ("example.com",))[0]
    assert only == {
        "name": "x",
        "value": "y",
        "domain": "example.com",
        "path": "/",
        "expires": -1.0,
        "httpOnly": False,
        "secure": False,
        "sameSite": "Lax",
    }


def test_read_local_cookies_filters_by_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        browser_cookie3,
        "chrome",
        lambda: [cookie("sid", "a", ".github.com"), cookie("junk", "b", ".other.com")],
    )
    cookies = read_local_cookies("chrome", ("github.com",))
    assert [c["name"] for c in cookies] == ["sid"]


def test_merge_cookies_dedupes_by_name_domain_path() -> None:
    old = [{"name": "sid", "value": "stale", "domain": ".x.com", "path": "/"}]
    new = [
        {"name": "sid", "value": "fresh", "domain": ".x.com", "path": "/"},
        {"name": "other", "value": "v", "domain": ".x.com", "path": "/"},
    ]
    merged = merge_cookies(old, new)
    assert {c["name"]: c["value"] for c in merged} == {"sid": "fresh", "other": "v"}


async def test_import_cookies_from_file_writes_blob(tmp_path: Path) -> None:
    (path := tmp_path / "cookies.txt").write_text(NETSCAPE)
    workflow_id = WorkflowId(new_uuid())
    count = await import_cookies(state_storage(), workflow_id, domains=("github.com",), from_file=path)
    assert count == 2
    blob = json.loads((Path(os.environ["DAILIES_STATE_DIR"]) / browser_profile_key(workflow_id)).read_text())
    assert sorted(c["name"] for c in blob["cookies"]) == ["sid", "token"]
    assert blob["origins"] == []


async def test_import_cookies_is_idempotent(tmp_path: Path) -> None:
    (path := tmp_path / "cookies.txt").write_text(NETSCAPE)
    workflow_id = WorkflowId(new_uuid())
    storage = state_storage()
    await import_cookies(storage, workflow_id, domains=("github.com",), from_file=path)
    await import_cookies(storage, workflow_id, domains=("github.com",), from_file=path)
    blob = json.loads((Path(os.environ["DAILIES_STATE_DIR"]) / browser_profile_key(workflow_id)).read_text())
    assert sorted(c["name"] for c in blob["cookies"]) == ["sid", "token"]


async def test_import_cookies_from_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_cookie3, "chrome", lambda: [cookie("sid", "a", ".github.com")])
    workflow_id = WorkflowId(new_uuid())
    count = await import_cookies(state_storage(), workflow_id, domains=("github.com",), source_browser="chrome")
    assert count == 1


def test_browser_backend_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAILIES_BROWSER_BACKEND", raising=False)
    assert isinstance(browser_backend(), BrowserUseBackend)
    monkeypatch.setenv("DAILIES_BROWSER_BACKEND", "local")
    assert isinstance(browser_backend(), BrowserUseBackend)
    monkeypatch.setenv("DAILIES_BROWSER_BACKEND", "browserbase")
    with pytest.raises(ValueError, match="browserbase"):
        browser_backend()
