from __future__ import annotations

import json

import httpx
import pytest

from dailies.bluebubbles import BlueBubblesClient, MessageSendFailed, SentMessage, imessage_client
from dailies.connections import NotConnected, WizardCredential
from tests.fakes import FakeCredentialStore

pytestmark = pytest.mark.unit

SENT_ENVELOPE = {"status": 200, "message": "Success", "data": {"guid": "p:0/AAA-111"}}


def bb_store(url: str = "http://mac.tailnet:1234", password: str = "hunter2") -> FakeCredentialStore:
    return FakeCredentialStore(
        credentials={"bluebubbles": WizardCredential(values={"BLUEBUBBLES_URL": url, "BLUEBUBBLES_PASSWORD": password})}
    )


def recording(response: httpx.Response, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    return httpx.MockTransport(handler)


async def test_send_posts_text_with_password_and_chat_guid() -> None:
    seen: list[httpx.Request] = []
    transport = recording(httpx.Response(200, json=SENT_ENVELOPE), seen)
    client = BlueBubblesClient(credentials=bb_store(), transport=transport)
    sent = await client.send(to="+15551234567", text="dinner at 7?")
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "http://mac.tailnet:1234/api/v1/message/text?password=hunter2"
    body = json.loads(request.content)
    assert body["chatGuid"] == "iMessage;-;+15551234567"
    assert body["message"] == "dinner at 7?"
    assert body["tempGuid"].startswith("dly-")
    assert sent == SentMessage(guid="p:0/AAA-111")


async def test_each_send_mints_a_distinct_temp_guid() -> None:
    seen: list[httpx.Request] = []
    client = BlueBubblesClient(
        credentials=bb_store(), transport=recording(httpx.Response(200, json=SENT_ENVELOPE), seen)
    )
    await client.send(to="+15551234567", text="one")
    await client.send(to="+15551234567", text="two")
    first, second = (json.loads(request.content)["tempGuid"] for request in seen)
    assert first != second
    assert first.startswith("dly-") and second.startswith("dly-")


@pytest.mark.parametrize("http_status", [200, 500], ids=["http-200", "http-500"])
async def test_send_envelope_failure_raises(http_status: int) -> None:
    envelope = {"status": 500, "message": "iMessage not available", "error": {"type": "Server Error"}}
    transport = httpx.MockTransport(lambda request: httpx.Response(http_status, json=envelope))
    with pytest.raises(MessageSendFailed, match=r"500 sending to \+15551234567: iMessage not available"):
        await BlueBubblesClient(credentials=bb_store(), transport=transport).send(to="+15551234567", text="hi")


async def test_ping_true_when_server_responds() -> None:
    seen: list[httpx.Request] = []
    transport = recording(httpx.Response(200, json={"status": 200, "message": "pong", "data": "pong"}), seen)
    assert await BlueBubblesClient(credentials=bb_store(), transport=transport).ping() is True
    (request,) = seen
    assert request.method == "GET"
    assert str(request.url) == "http://mac.tailnet:1234/api/v1/ping?password=hunter2"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(401, json={"status": 401, "message": "bad password"}), httpx.Response(200, json={"status": 500})],
    ids=["http-error", "envelope-error"],
)
async def test_ping_false_when_unhealthy(response: httpx.Response) -> None:
    client = BlueBubblesClient(credentials=bb_store(), transport=httpx.MockTransport(lambda request: response))
    assert await client.ping() is False


async def test_missing_credentials_raise_not_connected() -> None:
    client = BlueBubblesClient(credentials=FakeCredentialStore())
    with pytest.raises(NotConnected):
        await client.send(to="+15551234567", text="hi")
    with pytest.raises(NotConnected):
        await client.ping()


def test_factory_returns_bluebubbles_client() -> None:
    assert isinstance(imessage_client(), BlueBubblesClient)
