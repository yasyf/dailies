"""Async Gmail client routed through the Nango proxy, plus message rendering."""

from __future__ import annotations

import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage as MimeMessage
from typing import Any, Protocol

import html2text
import httpx

from dailies.connections import ConnectionStore, connection_store
from dailies.models import FrozenModel

NANGO_API = "https://api.nango.dev"
MAX_BODY = 4_000
SEARCH_LIMIT = 20


class ThreadNotFound(LookupError):
    """The thread id does not exist or is not visible to the connected account."""


class EmailMessage(FrozenModel):
    """A rendered Gmail message; ``truncated`` flips when the body is cut at MAX_BODY."""

    id: str
    thread_id: str
    sender: str
    to: str
    subject: str
    body: str
    date: datetime
    truncated: bool = False


class MessageMeta(FrozenModel):
    id: str
    thread_id: str
    date: datetime


class SentEmail(FrozenModel):
    message_id: str
    thread_id: str


class GmailProfile(FrozenModel):
    email: str


class GmailClient(Protocol):
    """Async Gmail surface shared by agent tools and pollers; Nango-proxied in production."""

    async def search(self, query: str, *, limit: int = SEARCH_LIMIT) -> list[EmailMessage]: ...

    async def message(self, message_id: str) -> EmailMessage: ...

    async def thread(self, thread_id: str) -> list[EmailMessage]: ...

    async def thread_metas(self, thread_id: str) -> list[MessageMeta]: ...

    async def query_metas(self, query: str, *, after: datetime) -> list[MessageMeta]: ...

    async def send(self, *, to: str, subject: str, body: str) -> SentEmail: ...

    async def profile(self) -> GmailProfile: ...


def decode_body(data: str) -> str:
    return urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode()


def part_text(part: dict[str, Any], mime: str) -> str | None:
    if part.get("mimeType") == mime and (data := part.get("body", {}).get("data")):
        return decode_body(data)
    return next((text for sub in part.get("parts", []) if (text := part_text(sub, mime)) is not None), None)


def render_body(payload: dict[str, Any]) -> str:
    if (plain := part_text(payload, "text/plain")) is not None:
        return plain
    if (html := part_text(payload, "text/html")) is not None:
        return html2text.html2text(html)
    return ""


def internal_date(resource: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(int(resource["internalDate"]) / 1000, tz=UTC)


def parse_message(resource: dict[str, Any]) -> EmailMessage:
    headers = {header["name"].lower(): header["value"] for header in resource["payload"]["headers"]}
    return EmailMessage(
        id=resource["id"],
        thread_id=resource["threadId"],
        sender=headers.get("from", ""),
        to=headers.get("to", ""),
        subject=headers.get("subject", ""),
        body=render_body(resource["payload"]),
        date=internal_date(resource),
    )


def message_meta(resource: dict[str, Any]) -> MessageMeta:
    return MessageMeta(id=resource["id"], thread_id=resource["threadId"], date=internal_date(resource))


def truncate(message: EmailMessage) -> EmailMessage:
    if len(message.body) <= MAX_BODY:
        return message
    return message.model_copy(update={"body": message.body[:MAX_BODY], "truncated": True})


def checked(response: httpx.Response) -> httpx.Response:
    response.raise_for_status()
    return response


@dataclass(frozen=True, slots=True)
class NangoGmailClient:
    """GmailClient calling the Gmail API through Nango's proxy.

    Construction performs no I/O and reads no environment; the connection and
    ``NANGO_SECRET_KEY`` are resolved per call so unconnected integrations only
    fail when actually used.
    """

    store: ConnectionStore
    transport: httpx.AsyncBaseTransport | None = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[httpx.AsyncClient]:
        connection = await self.store.load("gmail")
        async with httpx.AsyncClient(
            base_url=f"{NANGO_API}/proxy/gmail/v1/users/me",
            headers={
                "Authorization": f"Bearer {os.environ['NANGO_SECRET_KEY']}",
                "Connection-Id": connection.connection_id,
                "Provider-Config-Key": connection.provider_config_key,
            },
            transport=self.transport,
        ) as client:
            yield client

    async def fetch_thread(self, client: httpx.AsyncClient, thread_id: str, *, fmt: str) -> dict[str, Any]:
        response = await client.get(f"/threads/{thread_id}", params={"format": fmt})
        if response.status_code == 404:
            raise ThreadNotFound(thread_id)
        return checked(response).json()

    async def list_message_refs(self, client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        params = {"q": query}
        while True:
            listing = checked(await client.get("/messages", params=params)).json()
            refs.extend(listing.get("messages", []))
            if (token := listing.get("nextPageToken")) is None:
                return refs
            params = {"q": query, "pageToken": token}

    async def search(self, query: str, *, limit: int = SEARCH_LIMIT) -> list[EmailMessage]:
        async with self.session() as client:
            listing = checked(await client.get("/messages", params={"q": query, "maxResults": limit})).json()
            return [
                parse_message(checked(await client.get(f"/messages/{ref['id']}", params={"format": "full"})).json())
                for ref in listing.get("messages", [])
            ]

    async def message(self, message_id: str) -> EmailMessage:
        async with self.session() as client:
            return parse_message(checked(await client.get(f"/messages/{message_id}", params={"format": "full"})).json())

    async def thread(self, thread_id: str) -> list[EmailMessage]:
        async with self.session() as client:
            return [parse_message(m) for m in (await self.fetch_thread(client, thread_id, fmt="full"))["messages"]]

    async def thread_metas(self, thread_id: str) -> list[MessageMeta]:
        async with self.session() as client:
            return [message_meta(m) for m in (await self.fetch_thread(client, thread_id, fmt="minimal"))["messages"]]

    async def query_metas(self, query: str, *, after: datetime) -> list[MessageMeta]:
        async with self.session() as client:
            refs = await self.list_message_refs(client, f"({query}) after:{int(after.timestamp())}")
            metas = [
                message_meta(checked(await client.get(f"/messages/{ref['id']}", params={"format": "minimal"})).json())
                for ref in refs
            ]
            return [meta for meta in metas if meta.date > after]

    async def send(self, *, to: str, subject: str, body: str) -> SentEmail:
        mime = MimeMessage()
        mime["To"] = to
        mime["Subject"] = subject
        mime.set_content(body)
        async with self.session() as client:
            data = checked(
                await client.post("/messages/send", json={"raw": urlsafe_b64encode(mime.as_bytes()).decode()})
            ).json()
        return SentEmail(message_id=data["id"], thread_id=data["threadId"])

    async def profile(self) -> GmailProfile:
        async with self.session() as client:
            return GmailProfile(email=checked(await client.get("/profile")).json()["emailAddress"])


def gmail_client() -> GmailClient:
    return NangoGmailClient(store=connection_store())
