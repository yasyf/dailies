from __future__ import annotations

from pathlib import Path

import pytest

from dailies.storage import LocalStorage, state_storage

pytestmark = pytest.mark.unit


async def test_lease_yields_path_under_root_and_creates_parents(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path / "state")
    async with storage.lease("workflows/abc.sqlite") as path:
        assert path == tmp_path / "state" / "workflows" / "abc.sqlite"
        assert path.parent.is_dir()
        assert not path.exists()


async def test_lease_preserves_written_file(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path)
    async with storage.lease("tasks/t.sqlite") as path:
        path.write_bytes(b"data")
    async with storage.lease("tasks/t.sqlite") as path:
        assert path.read_bytes() == b"data"


async def test_delete_removes_db_and_sidecars(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path)
    for suffix in ("", "-wal", "-shm"):
        async with storage.lease(f"workflows/w.sqlite{suffix}") as path:
            path.write_bytes(b"x")
    await storage.delete("workflows/w.sqlite")
    assert list((tmp_path / "workflows").iterdir()) == []


async def test_delete_is_idempotent(tmp_path: Path) -> None:
    await LocalStorage(root=tmp_path).delete("workflows/missing.sqlite")


def test_state_storage_reads_env(state_dir: Path) -> None:
    storage = state_storage()
    assert isinstance(storage, LocalStorage)
    assert storage.root == state_dir


def test_state_storage_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAILIES_STATE_DIR")
    with pytest.raises(KeyError):
        state_storage()
