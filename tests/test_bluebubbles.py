from __future__ import annotations

import json

import httpx
import pytest

from dailies.bluebubbles import BlueBubblesClient, MessageSendFailed, SentMessage, imessage_client

pytestmark = pytest.mark.unit

SENT_ENVELOPE = {"status": 200, "message": "Success", "data": {"guid": "p:0/AAA-111"}}


@pytest.fixture
def bluebubbles_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLUEBUBBLES_URL", "http://mac.tailnet:1234")
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "hunter2")


@pytest.fixture
def no_bluebubbles_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLUEBUBBLES_URL", raising=False)
    monkeypatch.delenv("BLUEBUBBLES_PASSWORD", raising=False)


def recording(response: httpx.Response, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    return httpx.MockTransport(handler)


@pytest.mark.usefixtures("bluebubbles_env")
async def test_send_posts_text_with_password_and_chat_guid() -> None:
    seen: list[httpx.Request] = []
    transport = recording(httpx.Response(200, json=SENT_ENVELOPE), seen)
    sent = await BlueBubblesClient(transport=transport).send(to="+15551234567", text="dinner at 7?")
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "http://mac.tailnet:1234/api/v1/message/text?password=hunter2"
    body = json.loads(request.content)
    assert body["chatGuid"] == "iMessage;-;+15551234567"
    assert body["message"] == "dinner at 7?"
    assert body["tempGuid"].startswith("dly-")
    assert sent == SentMessage(guid="p:0/AAA-111")


@pytest.mark.usefixtures("bluebubbles_env")
async def test_each_send_mints_a_distinct_temp_guid() -> None:
    seen: list[httpx.Request] = []
    client = BlueBubblesClient(transport=recording(httpx.Response(200, json=SENT_ENVELOPE), seen))
    await client.send(to="+15551234567", text="one")
    await client.send(to="+15551234567", text="two")
    first, second = (json.loads(request.content)["tempGuid"] for request in seen)
    assert first != second
    assert first.startswith("dly-") and second.startswith("dly-")


@pytest.mark.usefixtures("bluebubbles_env")
@pytest.mark.parametrize("http_status", [200, 500], ids=["http-200", "http-500"])
async def test_send_envelope_failure_raises(http_status: int) -> None:
    envelope = {"status": 500, "message": "iMessage not available", "error": {"type": "Server Error"}}
    transport = httpx.MockTransport(lambda request: httpx.Response(http_status, json=envelope))
    with pytest.raises(MessageSendFailed, match=r"500 sending to \+15551234567: iMessage not available"):
        await BlueBubblesClient(transport=transport).send(to="+15551234567", text="hi")


@pytest.mark.usefixtures("bluebubbles_env")
async def test_ping_true_when_server_responds() -> None:
    seen: list[httpx.Request] = []
    transport = recording(httpx.Response(200, json={"status": 200, "message": "pong", "data": "pong"}), seen)
    assert await BlueBubblesClient(transport=transport).ping() is True
    (request,) = seen
    assert request.method == "GET"
    assert str(request.url) == "http://mac.tailnet:1234/api/v1/ping?password=hunter2"


@pytest.mark.usefixtures("bluebubbles_env")
@pytest.mark.parametrize(
    "response",
    [httpx.Response(401, json={"status": 401, "message": "bad password"}), httpx.Response(200, json={"status": 500})],
    ids=["http-error", "envelope-error"],
)
async def test_ping_false_when_unhealthy(response: httpx.Response) -> None:
    assert await BlueBubblesClient(transport=httpx.MockTransport(lambda request: response)).ping() is False


@pytest.mark.usefixtures("no_bluebubbles_env")
async def test_env_read_per_call_not_at_construction() -> None:
    client = BlueBubblesClient()
    with pytest.raises(KeyError):
        await client.send(to="+15551234567", text="hi")
    with pytest.raises(KeyError):
        await client.ping()


def test_factory_returns_bluebubbles_client() -> None:
    assert isinstance(imessage_client(), BlueBubblesClient)
