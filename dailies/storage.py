"""Pluggable storage for state databases: local filesystem now, blob backends later."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class StateStorage(Protocol):
    """Materializes named state databases as local files for the duration of a lease.

    Keys are relative POSIX paths owned by the state layer. A remote backend
    downloads the database on lease entry and uploads it on clean exit; the
    local backend yields the file in place.
    """

    def lease(self, key: str) -> AbstractAsyncContextManager[Path]: ...

    async def delete(self, key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class LocalStorage:
    root: Path

    @asynccontextmanager
    async def lease(self, key: str) -> AsyncIterator[Path]:
        (path := self.root / key).parent.mkdir(parents=True, exist_ok=True)
        yield path

    async def delete(self, key: str) -> None:
        for suffix in ("", "-wal", "-shm"):
            (self.root / f"{key}{suffix}").unlink(missing_ok=True)


def state_storage() -> StateStorage:
    return LocalStorage(root=Path(os.environ["DAILIES_STATE_DIR"]))
