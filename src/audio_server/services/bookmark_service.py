"""Per-user snapshot bookmarks for analysis expressions and highlights."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from audio_server.db.models import Bookmark, BookmarkKind, Recording


class BookmarkServiceError(Exception):
    def __init__(self, code: str, safe_message: str, *, status_code: int) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.status_code = status_code


def source_digest(
    *, kind: BookmarkKind, recording_id: uuid.UUID | None, original_ja: str
) -> str:
    """Identify a saved quote without depending on its segment numbering.

    Transcript edits renumber segments, so the sequence cannot be part of the
    identity. Recording plus exact Japanese text is stable, and collapsing a
    repeated quote within one recording is the behaviour a study list wants.
    """

    payload = f"{kind.value}|{recording_id or ''}|{original_ja.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BookmarkService:
    def __init__(self, *, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        user_id: uuid.UUID,
        kind: BookmarkKind,
        recording_id: uuid.UUID | None,
        original_ja: str,
        translation_zh_hk: str,
        note_ja: str,
        note_zh_hk: str,
        speaker_label: str,
        start_time: float,
        end_time: float | None,
    ) -> Bookmark:
        """Save a quote, returning the existing row when it is already saved."""

        digest = source_digest(
            kind=kind, recording_id=recording_id, original_ja=original_ja
        )
        existing = self._find(user_id=user_id, digest=digest)
        if existing is not None:
            return existing

        try:
            return self._insert(
                user_id=user_id,
                kind=kind,
                digest=digest,
                recording_id=recording_id,
                original_ja=original_ja,
                translation_zh_hk=translation_zh_hk,
                note_ja=note_ja,
                note_zh_hk=note_zh_hk,
                speaker_label=speaker_label,
                start_time=start_time,
                end_time=end_time,
            )
        except IntegrityError:
            # A save that raced ours won the unique index. Each step runs in its
            # own transaction so the failed insert is already rolled back, and
            # this lookup gets a usable connection; recovering inside the failed
            # transaction would instead abort on the next statement.
            concurrent = self._find(user_id=user_id, digest=digest)
            if concurrent is None:
                raise
            return concurrent

    def _insert(
        self,
        *,
        user_id: uuid.UUID,
        kind: BookmarkKind,
        digest: str,
        recording_id: uuid.UUID | None,
        original_ja: str,
        translation_zh_hk: str,
        note_ja: str,
        note_zh_hk: str,
        speaker_label: str,
        start_time: float,
        end_time: float | None,
    ) -> Bookmark:
        with self._session_factory.begin() as session:
            source_label = "—"
            if recording_id is not None:
                recording = session.get(Recording, recording_id)
                if recording is None:
                    raise BookmarkServiceError(
                        "recording_not_found",
                        "Recording was not found.",
                        status_code=404,
                    )
                source_label = recording.original_filename

            bookmark = Bookmark(
                user_id=user_id,
                kind=kind,
                source_digest=digest,
                original_ja=original_ja.strip(),
                translation_zh_hk=translation_zh_hk.strip(),
                note_ja=note_ja.strip(),
                note_zh_hk=note_zh_hk.strip(),
                speaker_label=speaker_label,
                start_time=start_time,
                end_time=end_time,
                recording_id=recording_id,
                source_label=source_label,
            )
            session.add(bookmark)
            session.flush()
            return bookmark

    def list_for_user(
        self, *, user_id: uuid.UUID, kind: BookmarkKind | None = None
    ) -> list[Bookmark]:
        statement = select(Bookmark).where(Bookmark.user_id == user_id)
        if kind is not None:
            statement = statement.where(Bookmark.kind == kind)
        statement = statement.order_by(Bookmark.created_at.desc(), Bookmark.id.desc())
        with self._session_factory() as session:
            return list(session.scalars(statement))

    def delete(self, *, user_id: uuid.UUID, bookmark_id: uuid.UUID) -> None:
        with self._session_factory.begin() as session:
            deleted = session.scalar(
                sql_delete(Bookmark)
                .where(
                    Bookmark.id == bookmark_id,
                    # Scoping the delete by owner keeps one account from
                    # removing another account's saved quotes.
                    Bookmark.user_id == user_id,
                )
                .returning(Bookmark.id)
            )
            if deleted is None:
                raise BookmarkServiceError(
                    "bookmark_not_found",
                    "Bookmark was not found.",
                    status_code=404,
                )

    def _find(self, *, user_id: uuid.UUID, digest: str) -> Bookmark | None:
        with self._session_factory() as session:
            return session.scalar(
                select(Bookmark).where(
                    Bookmark.user_id == user_id,
                    Bookmark.source_digest == digest,
                )
            )


def detach_recording_bookmarks(
    session: Session, *, recording_id: uuid.UUID, now: datetime | None = None
) -> None:
    """Keep saved quotes after their recording is deleted.

    This runs explicitly rather than relying on ``ON DELETE SET NULL`` because
    SQLite does not enforce foreign keys unless they are enabled per connection.
    """

    detached_at = now or datetime.now(UTC)
    for bookmark in session.scalars(
        select(Bookmark).where(Bookmark.recording_id == recording_id)
    ):
        bookmark.recording_id = None
        bookmark.source_deleted_at = detached_at
