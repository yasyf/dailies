from __future__ import annotations

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from typing import Any

import httpx
import pytest

from dailies.connections import NangoCredential, NotConnected
from dailies.gmail import (
    MAX_BODY,
    EmailMessage,
    GmailProfile,
    MessageMeta,
    NangoGmailClient,
    SentEmail,
    ThreadNotFound,
    parse_message,
    truncate,
)
from tests.fakes import FakeCredentialStore

pytestmark = pytest.mark.unit

PROXY_PREFIX = "/proxy/gmail/v1/users/me"
CREDENTIAL = NangoCredential(connection_id="conn-1", provider_config_key="google-mail")
HEADERS = [
    {"name": "From", "value": "alice@example.com"},
    {"name": "To", "value": "bob@example.com"},
    {"name": "Subject", "value": "Greetings"},
]


@pytest.fixture(autouse=True)
def nango_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANGO_SECRET_KEY", "secret-key")


def b64(text: str) -> str:
    return urlsafe_b64encode(text.encode()).decode()


def part(mime: str, text: str) -> dict[str, Any]:
    return {"mimeType": mime, "body": {"data": b64(text)}}


def resource(payload: dict[str, Any], *, id: str = "m1", internal_date: str = "1767225600000") -> dict[str, Any]:
    return {"id": id, "threadId": "t1", "internalDate": internal_date, "payload": payload}


def full(id: str, body: str, *, internal_date: str = "1767225600000") -> dict[str, Any]:
    payload = {"mimeType": "text/plain", "headers": HEADERS, "body": {"data": b64(body)}}
    return resource(payload, id=id, internal_date=internal_date)


def minimal(id: str, internal_date: str, *, thread_id: str = "t1") -> dict[str, Any]:
    return {"id": id, "threadId": thread_id, "internalDate": internal_date, "labelIds": ["INBOX"]}


def proxied(routes: dict[str, Any]) -> tuple[NangoGmailClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if (route := routes.get(request.url.path.removeprefix(PROXY_PREFIX))) is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
        return httpx.Response(200, json=route)

    store = FakeCredentialStore(credentials={"gmail": CREDENTIAL})
    return NangoGmailClient(credentials=store, transport=httpx.MockTransport(handler)), requests


RENDER_CASES = {
    "plain-only": (
        {"mimeType": "text/plain", "headers": HEADERS, "body": {"data": b64("hello plain")}},
        "hello plain",
    ),
    "alternative-prefers-plain": (
        {
            "mimeType": "multipart/alternative",
            "headers": HEADERS,
            "body": {},
            "parts": [part("text/plain", "the plain one"), part("text/html", "<p>the html one</p>")],
        },
        "the plain one",
    ),
    "html-only-converts": (
        {
            "mimeType": "multipart/alternative",
            "headers": HEADERS,
            "body": {},
            "parts": [part("text/html", "<p>Hello <b>world</b></p>")],
        },
        "Hello **world**\n\n",
    ),
    "nested-mixed": (
        {
            "mimeType": "multipart/mixed",
            "headers": HEADERS,
            "body": {},
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "body": {},
                    "parts": [part("text/plain", "nested plain"), part("text/html", "<p>nested html</p>")],
                },
                {"mimeType": "application/pdf", "body": {"attachmentId": "a1"}},
            ],
        },
        "nested plain",
    ),
    "stripped-padding": (
        {"mimeType": "text/plain", "headers": HEADERS, "body": {"data": b64("padded!").rstrip("=")}},
        "padded!",
    ),
}


@pytest.mark.parametrize(("payload", "body"), RENDER_CASES.values(), ids=RENDER_CASES.keys())
def test_parse_message_renders_body(payload: dict[str, Any], body: str) -> None:
    assert parse_message(resource(payload)) == EmailMessage(
        id="m1",
        thread_id="t1",
        sender="alice@example.com",
        to="bob@example.com",
        subject="Greetings",
        body=body,
        date=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_internal_date_parses_to_aware_datetime() -> None:
    message = parse_message(resource(RENDER_CASES["plain-only"][0], internal_date="1767225600250"))
    assert message.date == datetime(2026, 1, 1, 0, 0, 0, 250000, tzinfo=UTC)


def test_headers_are_case_insensitive() -> None:
    payload = {
        "mimeType": "text/plain",
        "body": {"data": b64("x")},
        "headers": [
            {"name": "FROM", "value": "a@example.com"},
            {"name": "to", "value": "b@example.com"},
            {"name": "SuBJeCt", "value": "s"},
        ],
    }
    message = parse_message(resource(payload))
    assert (message.sender, message.to, message.subject) == ("a@example.com", "b@example.com", "s")


def test_truncate_caps_body_and_flips_flag() -> None:
    payload = {"mimeType": "text/plain", "headers": HEADERS, "body": {"data": b64("x" * (MAX_BODY + 1))}}
    long = parse_message(resource(payload))
    cut = truncate(long)
    assert (len(cut.body), cut.truncated) == (MAX_BODY, True)
    assert cut.body == "x" * MAX_BODY


def test_truncate_keeps_short_body_untouched() -> None:
    message = parse_message(resource(RENDER_CASES["plain-only"][0]))
    assert truncate(message) is message
    assert message.truncated is False


async def test_every_request_carries_nango_headers() -> None:
    client, requests = proxied(
        {"/messages": {"messages": [{"id": "m1", "threadId": "t1"}]}, "/messages/m1": full("m1", "hi")}
    )
    await client.search("hi")
    assert len(requests) == 2
    assert all(
        (r.headers["Authorization"], r.headers["Connection-Id"], r.headers["Provider-Config-Key"])
        == ("Bearer secret-key", "conn-1", "google-mail")
        for r in requests
    )


async def test_search_lists_then_fetches_each_message() -> None:
    client, requests = proxied(
        {
            "/messages": {"messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t1"}]},
            "/messages/m1": full("m1", "first"),
            "/messages/m2": full("m2", "second"),
        }
    )
    messages = await client.search("from:alice", limit=5)
    assert [m.body for m in messages] == ["first", "second"]
    assert messages[0] == EmailMessage(
        id="m1",
        thread_id="t1",
        sender="alice@example.com",
        to="bob@example.com",
        subject="Greetings",
        body="first",
        date=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert requests[0].url.path == f"{PROXY_PREFIX}/messages"
    assert dict(requests[0].url.params) == {"q": "from:alice", "maxResults": "5"}
    assert [r.url.path for r in requests[1:]] == [f"{PROXY_PREFIX}/messages/m1", f"{PROXY_PREFIX}/messages/m2"]
    assert all(r.url.params["format"] == "full" for r in requests[1:])


async def test_search_with_no_matches_returns_empty() -> None:
    client, requests = proxied({"/messages": {"resultSizeEstimate": 0}})
    assert await client.search("nothing") == []
    assert len(requests) == 1


async def test_query_metas_floors_epoch_wraps_query_and_filters_exact() -> None:
    after = datetime(2026, 6, 1, 12, 0, 0, 250000, tzinfo=UTC)
    ms = int(after.timestamp() * 1000)
    client, requests = proxied(
        {
            "/messages": {
                "messages": [
                    {"id": "old", "threadId": "t1"},
                    {"id": "edge", "threadId": "t1"},
                    {"id": "new", "threadId": "t2"},
                ]
            },
            "/messages/old": minimal("old", str(ms - 100)),
            "/messages/edge": minimal("edge", str(ms)),
            "/messages/new": minimal("new", str(ms + 100), thread_id="t2"),
        }
    )
    metas = await client.query_metas("from:carol OR from:dave", after=after)
    assert metas == [MessageMeta(id="new", thread_id="t2", date=after + timedelta(milliseconds=100))]
    assert requests[0].url.params["q"] == f"(from:carol OR from:dave) after:{int(after.timestamp())}"
    assert all(r.url.params["format"] == "minimal" for r in requests[1:])


async def test_query_metas_paginates_via_next_page_token() -> None:
    after = datetime(2026, 6, 1, tzinfo=UTC)
    ms = int(after.timestamp() * 1000)
    query = f"(from:carol) after:{int(after.timestamp())}"
    gets = {
        "/messages/stale": minimal("stale", str(ms - 100)),
        "/messages/p1-new": minimal("p1-new", str(ms + 100)),
        "/messages/p2-new": minimal("p2-new", str(ms + 200), thread_id="t2"),
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        match request.url.path.removeprefix(PROXY_PREFIX), request.url.params.get("pageToken"):
            case "/messages", None:
                return httpx.Response(
                    200,
                    json={
                        "messages": [{"id": "stale", "threadId": "t1"}, {"id": "p1-new", "threadId": "t1"}],
                        "nextPageToken": "page-two",
                    },
                )
            case "/messages", "page-two":
                return httpx.Response(200, json={"messages": [{"id": "p2-new", "threadId": "t2"}]})
            case path, _:
                return httpx.Response(200, json=gets[path])

    store = FakeCredentialStore(credentials={"gmail": CREDENTIAL})
    client = NangoGmailClient(credentials=store, transport=httpx.MockTransport(handler))
    metas = await client.query_metas("from:carol", after=after)
    assert metas == [
        MessageMeta(id="p1-new", thread_id="t1", date=after + timedelta(milliseconds=100)),
        MessageMeta(id="p2-new", thread_id="t2", date=after + timedelta(milliseconds=200)),
    ]
    assert [r.url.path.removeprefix(PROXY_PREFIX) for r in requests] == [
        "/messages",
        "/messages",
        "/messages/stale",
        "/messages/p1-new",
        "/messages/p2-new",
    ]
    assert dict(requests[0].url.params) == {"q": query}
    assert dict(requests[1].url.params) == {"q": query, "pageToken": "page-two"}


async def test_send_builds_raw_mime_that_round_trips() -> None:
    client, requests = proxied({"/messages/send": {"id": "m9", "threadId": "t9"}})
    sent = await client.send(to="bob@example.com", subject="Hi there", body="line one\nline two\n")
    assert sent == SentEmail(message_id="m9", thread_id="t9")
    assert requests[0].method == "POST"
    raw = json.loads(requests[0].content)["raw"]
    mime = message_from_bytes(urlsafe_b64decode(raw), policy=policy.default)
    assert (str(mime["To"]), str(mime["Subject"])) == ("bob@example.com", "Hi there")
    assert mime.get_content() == "line one\nline two\n"


async def test_thread_fetches_full_messages() -> None:
    client, requests = proxied(
        {"/threads/t1": {"id": "t1", "messages": [full("m1", "one"), full("m2", "two", internal_date="1767225601000")]}}
    )
    messages = await client.thread("t1")
    assert [m.id for m in messages] == ["m1", "m2"]
    assert messages[1].date == datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert requests[0].url.params["format"] == "full"


async def test_thread_metas_uses_minimal_format() -> None:
    client, requests = proxied({"/threads/t1": {"id": "t1", "messages": [minimal("m1", "1767225600000")]}})
    metas = await client.thread_metas("t1")
    assert metas == [MessageMeta(id="m1", thread_id="t1", date=datetime(2026, 1, 1, tzinfo=UTC))]
    assert requests[0].url.params["format"] == "minimal"


async def test_missing_thread_raises_thread_not_found() -> None:
    client, _ = proxied({})
    with pytest.raises(ThreadNotFound):
        await client.thread("ghost")
    with pytest.raises(ThreadNotFound):
        await client.thread_metas("ghost")


async def test_non_404_thread_errors_propagate() -> None:
    store = FakeCredentialStore(credentials={"gmail": CREDENTIAL})
    client = NangoGmailClient(credentials=store, transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    with pytest.raises(httpx.HTTPStatusError):
        await client.thread("t1")


async def test_profile_returns_connected_address() -> None:
    client, requests = proxied({"/profile": {"emailAddress": "me@example.com", "messagesTotal": 42}})
    assert await client.profile() == GmailProfile(email="me@example.com")
    assert requests[0].url.path == f"{PROXY_PREFIX}/profile"


async def test_unconnected_store_raises_not_connected() -> None:
    client = NangoGmailClient(
        credentials=FakeCredentialStore(), transport=httpx.MockTransport(lambda _: httpx.Response(200))
    )
    with pytest.raises(NotConnected, match="dly auth gmail"):
        await client.profile()


async def test_missing_secret_key_raises_key_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NANGO_SECRET_KEY")
    client, _ = proxied({"/profile": {"emailAddress": "x@example.com"}})
    with pytest.raises(KeyError):
        await client.profile()


def test_construction_needs_no_env_or_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NANGO_SECRET_KEY")
    NangoGmailClient()
