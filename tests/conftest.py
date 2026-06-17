from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from beanie import init_beanie
from pymongo import AsyncMongoClient

from dailies.documents import document_models

DB_NAME = "dailies_test"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DAILIES_STATE_DIR", str(state_root := tmp_path / "state"))
    return state_root


@pytest.fixture
def nango_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANGO_SECRET_KEY", "secret")


def docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def mongo_url() -> Iterator[str]:
    if not docker_available():
        pytest.skip("Docker not available")
    from testcontainers.mongodb import MongoDbContainer

    with MongoDbContainer("mongo:7.0") as container:
        yield container.get_connection_url()


@pytest.fixture
async def mongo(mongo_url: str) -> AsyncIterator[AsyncMongoClient[dict[str, Any]]]:
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(mongo_url, uuidRepresentation="standard", tz_aware=True)
    await init_beanie(database=client[DB_NAME], document_models=document_models())
    try:
        yield client
    finally:
        await client.drop_database(DB_NAME)
        await client.close()
