from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from beanie import init_beanie
from pymongo import AsyncMongoClient

from dailies.documents import document_models

__all__ = ["init_db", "lifespan"]


async def init_db() -> AsyncMongoClient[dict[str, Any]]:
    """Connect to MongoDB and initialise beanie.

    Reads ``MONGODB_URI`` and ``MONGODB_DB`` from the environment (fail-loud).
    ``uuidRepresentation="standard"`` is mandatory — pymongo's default raises on
    the first UUID ``_id`` insert. ``tz_aware=True`` keeps stored UTC datetimes
    tz-aware on read so cron windows compare cleanly. Returns the client for
    caller-owned lifecycle.
    """
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
        os.environ["MONGODB_URI"], uuidRepresentation="standard", tz_aware=True
    )
    await init_beanie(database=client[os.environ["MONGODB_DB"]], document_models=document_models())
    return client


@asynccontextmanager
async def lifespan() -> AsyncIterator[AsyncMongoClient[dict[str, Any]]]:
    """Connect, yield the client, and always close it on exit.

    The single connect+close codepath every CLI command runs inside one event loop.
    """
    client = await init_db()
    try:
        yield client
    finally:
        await client.close()
