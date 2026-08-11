from __future__ import annotations

import io
import os
import stat
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from audio_server.services.storage import (
    LocalStorageBackend,
    StorageConflictError,
    StorageError,
    UploadTooLargeError,
    original_storage_key,
)


def test_local_storage_streams_and_preserves_original(tmp_path: Path) -> None:
    storage = LocalStorageBackend(tmp_path / "data")
    staged = storage.create_staged_upload(io.BytesIO(b"audio-bytes"), max_bytes=100)
    key = original_storage_key(
        uuid.UUID("b7c53889-f3f0-4ae5-9e00-b329efe61176"),
        datetime(2026, 8, 10, tzinfo=UTC),
        ".wav",
    )
    stored = storage.put_original(staged, key)
    assert stored.key == key
    with storage.materialize(key) as path:
        assert path.read_bytes() == b"audio-bytes"


def test_local_storage_rejects_oversized_stream(tmp_path: Path) -> None:
    storage = LocalStorageBackend(tmp_path / "data")
    with pytest.raises(UploadTooLargeError):
        storage.create_staged_upload(io.BytesIO(b"too-large"), max_bytes=3)
    assert not list(storage.staging_root.iterdir())


def test_local_storage_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalStorageBackend(tmp_path / "data")
    with pytest.raises(StorageError):
        storage.exists("../private.wav")


def test_local_storage_never_overwrites_conflicting_original(tmp_path: Path) -> None:
    storage = LocalStorageBackend(tmp_path / "data")
    key = original_storage_key(
        uuid.UUID("b7c53889-f3f0-4ae5-9e00-b329efe61176"),
        datetime(2026, 8, 10, tzinfo=UTC),
        ".wav",
    )
    first = storage.create_staged_upload(io.BytesIO(b"first-audio"), max_bytes=100)
    storage.put_original(first, key)
    conflicting = storage.create_staged_upload(io.BytesIO(b"different-audio"), max_bytes=100)

    with pytest.raises(StorageConflictError):
        storage.put_original(conflicting, key)

    with storage.materialize(key) as path:
        assert path.read_bytes() == b"first-audio"


def test_concurrent_publication_never_overwrites_winner(tmp_path: Path) -> None:
    storage = LocalStorageBackend(tmp_path / "data")
    key = original_storage_key(
        uuid.UUID("b7c53889-f3f0-4ae5-9e00-b329efe61176"),
        datetime(2026, 8, 10, tzinfo=UTC),
        ".wav",
    )
    staged = {
        "first": storage.create_staged_upload(io.BytesIO(b"first-audio"), max_bytes=100),
        "second": storage.create_staged_upload(io.BytesIO(b"second-audio"), max_bytes=100),
    }
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def publish(name: str) -> None:
        barrier.wait()
        try:
            storage.put_original(staged[name], key)
            outcome = "stored"
        except StorageConflictError:
            outcome = "conflict"
        with lock:
            outcomes.append((name, outcome))

    threads = [threading.Thread(target=publish, args=(name,)) for name in staged]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcome for _name, outcome in outcomes) == ["conflict", "stored"]
    winner = next(name for name, outcome in outcomes if outcome == "stored")
    with storage.materialize(key) as path:
        assert path.read_bytes() == f"{winner}-audio".encode()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes")
def test_local_storage_uses_private_modes(tmp_path: Path) -> None:
    storage = LocalStorageBackend(tmp_path / "data")
    staged = storage.create_staged_upload(io.BytesIO(b"private-audio"), max_bytes=100)
    key = original_storage_key(
        uuid.UUID("b7c53889-f3f0-4ae5-9e00-b329efe61176"),
        datetime(2026, 8, 10, tzinfo=UTC),
        ".wav",
    )
    storage.put_original(staged, key)
    work = storage.work_directory(uuid.uuid4(), uuid.uuid4())

    assert stat.S_IMODE(storage.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    with storage.materialize(key) as path:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_new_recording_directory_chain_is_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalStorageBackend(tmp_path / "data")
    synced: list[Path] = []
    monkeypatch.setattr(storage, "_fsync_directory", synced.append)
    staged = storage.create_staged_upload(io.BytesIO(b"audio"), max_bytes=100)
    key = original_storage_key(
        uuid.UUID("b7c53889-f3f0-4ae5-9e00-b329efe61176"),
        datetime(2026, 8, 10, tzinfo=UTC),
        ".wav",
    )

    storage.put_original(staged, key)

    current = (storage.root / key).parent
    while True:
        assert current in synced
        if current == storage.root:
            break
        current = current.parent
