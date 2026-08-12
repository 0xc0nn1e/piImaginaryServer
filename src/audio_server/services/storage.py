from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol


class StorageError(Exception):
    """Base error for durable storage operations."""


class UploadTooLargeError(StorageError):
    pass


class StorageConflictError(StorageError):
    pass


@dataclass(frozen=True)
class StagedUpload:
    path: Path
    file_size: int
    sha256: str


@dataclass(frozen=True)
class StoredObject:
    key: str
    file_size: int
    sha256: str


@dataclass(frozen=True)
class StagedDeletion:
    """An original moved out of its public key until the DB transaction commits."""

    key: str
    quarantine_path: Path | None


class StorageBackend(Protocol):
    def create_staged_upload(self, source: BinaryIO, *, max_bytes: int) -> StagedUpload: ...

    def put_original(self, staged: StagedUpload, key: str) -> StoredObject: ...

    def discard_staged(self, staged: StagedUpload) -> None: ...

    def exists(self, key: str) -> bool: ...

    def materialize(self, key: str) -> contextlib.AbstractContextManager[Path]: ...

    def delete(self, key: str) -> None: ...

    def stage_delete(self, key: str) -> StagedDeletion: ...

    def restore_staged_delete(self, deletion: StagedDeletion) -> None: ...

    def finalize_staged_delete(self, deletion: StagedDeletion) -> None: ...

    def delete_workspaces(self, job_ids: list[uuid.UUID]) -> None: ...

    def work_directory(self, job_id: uuid.UUID, claim_token: uuid.UUID) -> Path: ...


class LocalStorageBackend:
    _CHUNK_SIZE = 1024 * 1024

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        root_was_missing = not self.root.exists()
        self.staging_root = self.root / "staging"
        self.deletion_root = self.staging_root / "deletions"
        self.recordings_root = self.root / "recordings"
        self.work_root = self.root / "work"
        for directory in (
            self.root,
            self.staging_root,
            self.recordings_root,
            self.work_root,
        ):
            self._ensure_private_directory(directory)
        if root_was_missing:
            self._fsync_directory(self.root.parent)

    def create_staged_upload(self, source: BinaryIO, *, max_bytes: int) -> StagedUpload:
        descriptor, raw_path = tempfile.mkstemp(
            prefix="upload-", suffix=".part", dir=self.staging_root
        )
        path = Path(raw_path)
        digest = hashlib.sha256()
        file_size = 0
        try:
            with os.fdopen(descriptor, "wb") as destination:
                while chunk := source.read(self._CHUNK_SIZE):
                    file_size += len(chunk)
                    if file_size > max_bytes:
                        raise UploadTooLargeError(
                            "uploaded audio exceeds the configured size limit"
                        )
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            self._fsync_directory(self.staging_root)
            return StagedUpload(path=path, file_size=file_size, sha256=digest.hexdigest())
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def put_original(self, staged: StagedUpload, key: str) -> StoredObject:
        destination = self._resolve_key(key)
        # Fsync every level through the durable storage root so a newly created
        # YYYY/MM/DD/UUID directory cannot disappear after an acknowledged
        # upload if the host loses power.
        self._ensure_private_directory(destination.parent)

        try:
            # Staging and recording directories share one filesystem. A hard
            # link publishes the already-fsynced inode atomically and, unlike
            # os.replace(), can never overwrite a concurrently accepted upload.
            os.link(staged.path, destination)
        except FileExistsError:
            existing_size, existing_hash = self._file_identity(destination)
            if existing_size != staged.file_size or existing_hash != staged.sha256:
                raise StorageConflictError(
                    "existing storage object has different content"
                ) from None
            self._set_private_file_mode(destination)
            self.discard_staged(staged)
            return StoredObject(key=key, file_size=existing_size, sha256=existing_hash)

        self.discard_staged(staged)
        self._fsync_directory(destination.parent)
        return StoredObject(key=key, file_size=staged.file_size, sha256=staged.sha256)

    def exists(self, key: str) -> bool:
        return self._resolve_key(key).is_file()

    @contextlib.contextmanager
    def materialize(self, key: str) -> Iterator[Path]:
        path = self._resolve_key(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        yield path

    def delete(self, key: str) -> None:
        path = self._resolve_key(key)
        path.unlink(missing_ok=True)
        self._fsync_directory(path.parent)

    def stage_delete(self, key: str) -> StagedDeletion:
        source = self._resolve_key(key)
        try:
            source_stat = source.lstat()
        except FileNotFoundError:
            return StagedDeletion(key=key, quarantine_path=None)
        if not stat.S_ISREG(source_stat.st_mode):
            raise StorageError("stored object is not a regular file")

        self._ensure_private_directory(self.deletion_root)
        quarantine_path = self.deletion_root / f"{uuid.uuid4()}.delete"
        os.replace(source, quarantine_path)
        self._set_private_file_mode(quarantine_path)
        self._fsync_directory(source.parent)
        self._fsync_directory(self.deletion_root)
        return StagedDeletion(key=key, quarantine_path=quarantine_path)

    def restore_staged_delete(self, deletion: StagedDeletion) -> None:
        if deletion.quarantine_path is None:
            return
        destination = self._resolve_key(deletion.key)
        self._ensure_private_directory(destination.parent)
        try:
            os.link(deletion.quarantine_path, destination)
        except FileExistsError as exc:
            raise StorageConflictError(
                "cannot restore a deleted object over existing data"
            ) from exc
        deletion.quarantine_path.unlink()
        self._fsync_directory(destination.parent)
        self._fsync_directory(self.deletion_root)

    def finalize_staged_delete(self, deletion: StagedDeletion) -> None:
        if deletion.quarantine_path is None:
            return
        deletion.quarantine_path.unlink(missing_ok=True)
        self._fsync_directory(self.deletion_root)

    def delete_workspaces(self, job_ids: list[uuid.UUID]) -> None:
        for job_id in job_ids:
            path = self._resolve_key(f"work/{job_id}")
            try:
                if path.is_symlink():
                    path.unlink()
                else:
                    shutil.rmtree(path, ignore_errors=False)
            except FileNotFoundError:
                continue
            self._fsync_directory(self.work_root)

    def discard_staged(self, staged: StagedUpload) -> None:
        try:
            staged.path.unlink()
        except FileNotFoundError:
            return
        self._fsync_directory(self.staging_root)

    def work_directory(self, job_id: uuid.UUID, claim_token: uuid.UUID) -> Path:
        path = self._resolve_key(f"work/{job_id}/{claim_token}")
        self._ensure_private_directory(path)
        return path

    def _ensure_private_directory(self, directory: Path) -> None:
        if directory != self.root and self.root not in directory.parents:
            raise StorageError("storage directory escapes the configured root")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

        current = directory
        while True:
            if os.name != "nt":
                current.chmod(0o700)
            self._fsync_directory(current)
            if current == self.root:
                break
            current = current.parent

    @staticmethod
    def _set_private_file_mode(path: Path) -> None:
        if os.name == "nt":
            return
        path.chmod(0o600)
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _resolve_key(self, key: str) -> Path:
        pure_key = PurePosixPath(key)
        if pure_key.is_absolute() or ".." in pure_key.parts:
            raise StorageError("storage key is invalid")
        resolved = (self.root / Path(*pure_key.parts)).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise StorageError("storage key escapes the configured root")
        return resolved

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(LocalStorageBackend._CHUNK_SIZE):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def original_storage_key(
    recording_id: uuid.UUID, started_at: datetime, detected_extension: str
) -> str:
    utc_start = started_at.astimezone(UTC)
    extension = detected_extension.lower().lstrip(".")
    if not extension.isalnum() or len(extension) > 8:
        raise StorageError("detected audio extension is invalid")
    return f"recordings/{utc_start:%Y/%m/%d}/{recording_id}/original.{extension}"
