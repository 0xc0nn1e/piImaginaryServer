from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid


class Base(DeclarativeBase):
    pass


class RecordingStatus(enum.StrEnum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobKind(enum.StrEnum):
    FULL = "full"
    ANALYSIS = "analysis"


class JobStage(enum.StrEnum):
    QUEUED = "queued"
    PREPROCESSING = "preprocessing"
    TRANSCRIBING = "transcribing"
    DIARIZING = "diarizing"
    MERGING = "merging"
    ANALYZING = "analyzing"
    COMPLETED = "completed"


class AnalysisStatus(enum.StrEnum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    STALE = "stale"


class BookmarkKind(enum.StrEnum):
    EXPRESSION = "expression"
    HIGHLIGHT = "highlight"


def enum_column(enum_type: type[enum.Enum], *, name: str) -> Enum:
    return Enum(
        enum_type,
        name=name,
        native_enum=False,
        create_constraint=False,
        values_callable=lambda members: [member.value for member in members],
    )


json_type = JSON().with_variant(JSONB(), "postgresql")


class Recording(Base):
    __tablename__ = "recordings"
    __table_args__ = (
        CheckConstraint(
            "processing_status IN ('uploaded', 'queued', 'processing', 'completed', 'failed')",
            name="recording_status",
        ),
        UniqueConstraint("device_id", "sha256", name="uq_recordings_device_sha256"),
        UniqueConstraint("storage_key", name="uq_recordings_storage_key"),
        Index("ix_recordings_created_at", "created_at"),
        Index("ix_recordings_status_created", "processing_status", "created_at"),
        Index("ix_recordings_checked_created", "checked", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    audio_format: Mapped[str] = mapped_column(String(32), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[int | None] = mapped_column(Integer)
    client_metadata: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict)
    processing_status: Mapped[RecordingStatus] = mapped_column(
        enum_column(RecordingStatus, name="recording_status"),
        default=RecordingStatus.UPLOADED,
        nullable=False,
    )
    audio_delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transcript_delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The only human-set column on a recording: everything else is ingest
    # identity or pipeline output.
    checked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    transcript_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    analysis_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )
    transcript_segments: Mapped[list[TranscriptSegment]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )
    analyses: Mapped[list[Analysis]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed')",
            name="job_status",
        ),
        CheckConstraint(
            "stage IN ('queued', 'preprocessing', 'transcribing', 'diarizing', "
            "'merging', 'analyzing', 'completed')",
            name="job_stage",
        ),
        CheckConstraint(
            "failed_stage IN ('queued', 'preprocessing', 'transcribing', 'diarizing', "
            "'merging', 'analyzing', 'completed')",
            name="failed_job_stage",
        ),
        CheckConstraint("kind IN ('full', 'analysis')", name="processing_job_kind"),
        Index(
            "ix_processing_jobs_claim",
            "available_at",
            "created_at",
            "id",
            postgresql_where=text("status = 'queued'"),
            sqlite_where=text("status = 'queued'"),
        ),
        Index(
            "ix_processing_jobs_expired",
            "lease_expires_at",
            postgresql_where=text("status = 'processing'"),
            sqlite_where=text("status = 'processing'"),
        ),
        Index(
            "uq_processing_jobs_one_active_recording",
            "recording_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'processing')"),
            sqlite_where=text("status IN ('queued', 'processing')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[JobKind] = mapped_column(
        enum_column(JobKind, name="processing_job_kind"),
        default=JobKind.FULL,
        server_default="full",
        nullable=False,
    )
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus, name="job_status"), default=JobStatus.QUEUED, nullable=False
    )
    stage: Mapped[JobStage] = mapped_column(
        enum_column(JobStage, name="job_stage"), default=JobStage.QUEUED, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    worker_id: Mapped[str | None] = mapped_column(String(128))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_stage: Mapped[JobStage | None] = mapped_column(
        enum_column(JobStage, name="failed_job_stage")
    )
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    recording: Mapped[Recording] = relationship(back_populates="jobs")
    transcript_segments: Mapped[list[TranscriptSegment]] = relationship(back_populates="job")
    analyses: Mapped[list[Analysis]] = relationship(back_populates="job")


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint("recording_id", "sequence", name="uq_transcript_recording_sequence"),
        Index("ix_transcript_segments_recording_sequence", "recording_id", "sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_label: Mapped[str] = mapped_column(String(64), nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(Float)
    has_overlap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    recording: Mapped[Recording] = relationship(back_populates="transcript_segments")
    job: Mapped[ProcessingJob] = relationship(back_populates="transcript_segments")


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        CheckConstraint(
            "status IN ('completed', 'skipped', 'failed', 'stale')",
            name="analysis_status",
        ),
        UniqueConstraint("job_id", name="uq_analyses_job"),
        UniqueConstraint("recording_id", name="uq_analyses_recording"),
        Index("ix_analyses_recording_created", "recording_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    schema_version: Mapped[str] = mapped_column(String(32), default="1", nullable=False)
    status: Mapped[AnalysisStatus] = mapped_column(
        enum_column(AnalysisStatus, name="analysis_status"), nullable=False
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(json_type)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    recording: Mapped[Recording] = relationship(back_populates="analyses")
    job: Mapped[ProcessingJob] = relationship(back_populates="analyses")


class Bookmark(Base):
    """A saved analysis quote, stored as an independent snapshot.

    Analysis items live inside ``Analysis.result`` and have no stable identity:
    reprocessing regenerates them, transcript edits renumber their segments, and
    deleting a recording removes them. A bookmark therefore keeps its own copy
    of the text so a personal study list survives all of those, and links back
    to its recording only as a soft reference.
    """

    __tablename__ = "bookmarks"
    __table_args__ = (
        CheckConstraint("kind IN ('expression', 'highlight')", name="bookmark_kind"),
        UniqueConstraint("user_id", "source_digest", name="uq_bookmarks_user_source"),
        Index("ix_bookmarks_user_created", "user_id", "created_at"),
        Index("ix_bookmarks_recording", "recording_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[BookmarkKind] = mapped_column(
        enum_column(BookmarkKind, name="bookmark_kind"), nullable=False
    )
    # Stable identity of the saved quote, so re-saving the same item is a no-op
    # and the source card can show whether it is already bookmarked. It is
    # computed once and never recomputed, because the recording may later go.
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    original_ja: Mapped[str] = mapped_column(Text, nullable=False)
    translation_zh_hk: Mapped[str] = mapped_column(Text, nullable=False)
    # ``usage`` for an expression, ``reason`` for a highlight.
    note_ja: Mapped[str] = mapped_column(Text, nullable=False)
    note_zh_hk: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_label: Mapped[str] = mapped_column(String(64), nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float | None] = mapped_column(Float)
    recording_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recordings.id", ondelete="SET NULL")
    )
    # Kept so the list still shows where a quote came from after its recording
    # has been deleted.
    source_label: Mapped[str] = mapped_column(String(255), nullable=False)
    source_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
