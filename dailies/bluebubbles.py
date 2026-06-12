"""Async iMessage client backed by a paired BlueBubbles server."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

import httpx

from dailies.models import FrozenModel


class MessageSendFailed(RuntimeError):
    """BlueBubbles reported a non-200 envelope status for a send."""


class SentMessage(FrozenModel):
    guid: str


class IMessageClient(Protocol):
    """Async iMessage surface shared by agent tools; BlueBubbles-backed in production."""

    async def send(self, *, to: str, text: str) -> SentMessage: ...

    async def ping(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class BlueBubblesClient:
    """IMessageClient calling a BlueBubbles server's REST API.

    Construction performs no I/O and reads no environment; ``BLUEBUBBLES_URL``
    and ``BLUEBUBBLES_PASSWORD`` are resolved per call so unconfigured machines
    only fail when a tool is actually used. The envelope ``status`` is
    authoritative for sends — BlueBubbles reports failures both via HTTP status
    and envelope status, so the body is parsed without ``raise_for_status``.
    """

    transport: httpx.AsyncBaseTransport | None = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            base_url=os.environ["BLUEBUBBLES_URL"],
            params={"password": os.environ["BLUEBUBBLES_PASSWORD"]},
            transport=self.transport,
        ) as client:
            yield client

    async def send(self, *, to: str, text: str) -> SentMessage:
        async with self.session() as client:
            data = (
                await client.post(
                    "/api/v1/message/text",
                    json={"chatGuid": f"iMessage;-;{to}", "tempGuid": f"dly-{uuid4().hex}", "message": text},
                )
            ).json()
        if data["status"] != 200:
            raise MessageSendFailed(f"BlueBubbles returned {data['status']} sending to {to}: {data['message']}")
        return SentMessage(guid=data["data"]["guid"])

    async def ping(self) -> bool:
        async with self.session() as client:
            response = await client.get("/api/v1/ping")
        return response.status_code == 200 and response.json()["status"] == 200


def imessage_client() -> IMessageClient:
    return BlueBubblesClient()
