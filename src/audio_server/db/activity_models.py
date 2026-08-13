"""Append-only, privacy-safe processing activity records."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from audio_server.db.models import Base, JobKind, JobStage, JobStatus, enum_column


class ProcessingActivityType(enum.StrEnum):
    JOB_QUEUED = "job_queued"
    PROCESSING_STARTED = "processing_started"
    STAGE_STARTED = "stage_started"
    RETRY_SCHEDULED = "retry_scheduled"
    LEASE_RECOVERED = "lease_recovered"
    PROCESSING_COMPLETED = "processing_completed"
    PROCESSING_FAILED = "processing_failed"
    MANUAL_RETRY_QUEUED = "manual_retry_queued"


_EVENT_VALUES = ", ".join(f"'{item.value}'" for item in ProcessingActivityType)
_STATUS_VALUES = ", ".join(f"'{item.value}'" for item in JobStatus)
_STAGE_VALUES = ", ".join(f"'{item.value}'" for item in JobStage)
_KIND_VALUES = ", ".join(f"'{item.value}'" for item in JobKind)


class ProcessingActivity(Base):
    """A durable UI event, intentionally narrower than application logs."""

    __tablename__ = "processing_activity"
    __table_args__ = (
        CheckConstraint(
            f"event_type IN ({_EVENT_VALUES})",
            name="processing_activity_event_type",
        ),
        CheckConstraint(
            f"job_status IS NULL OR job_status IN ({_STATUS_VALUES})",
            name="processing_activity_job_status",
        ),
        CheckConstraint(
            f"stage IS NULL OR stage IN ({_STAGE_VALUES})",
            name="processing_activity_stage",
        ),
        CheckConstraint(
            f"job_kind IN ({_KIND_VALUES})",
            name="processing_activity_job_kind",
        ),
        CheckConstraint("attempt_count >= 0", name="processing_activity_attempt_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="processing_activity_max_attempts_positive"),
        CheckConstraint(
            "retry_scheduled = false OR next_attempt_at IS NOT NULL",
            name="processing_activity_retry_time_required",
        ),
        # Every event emitted by the application has a stage. Keeping the
        # column nullable permits truthful historical backfills when a stage is
        # unknown, while this constraint fences normal duplicate writes.
        Index(
            "uq_processing_activity_job_attempt_type_stage",
            "job_id",
            "attempt_count",
            "event_type",
            "stage",
            unique=True,
            postgresql_where=text("stage IS NOT NULL"),
            sqlite_where=text("stage IS NOT NULL"),
        ),
        Index(
            "uq_processing_activity_job_attempt_type_no_stage",
            "job_id",
            "attempt_count",
            "event_type",
            unique=True,
            postgresql_where=text("stage IS NULL"),
            sqlite_where=text("stage IS NULL"),
        ),
        Index(
            "ix_processing_activity_recording_chronological",
            "recording_id",
            "occurred_at",
            "id",
        ),
        Index(
            "ix_processing_activity_job_chronological",
            "job_id",
            "occurred_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False
    )
    job_kind: Mapped[JobKind] = mapped_column(
        enum_column(JobKind, name="processing_activity_job_kind"),
        default=JobKind.FULL,
        server_default="full",
        nullable=False,
    )
    event_type: Mapped[ProcessingActivityType] = mapped_column(
        enum_column(ProcessingActivityType, name="processing_activity_event_type"),
        nullable=False,
    )
    job_status: Mapped[JobStatus | None] = mapped_column(
        enum_column(JobStatus, name="processing_activity_job_status")
    )
    stage: Mapped[JobStage | None] = mapped_column(
        enum_column(JobStage, name="processing_activity_stage")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_type: Mapped[str | None] = mapped_column(String(128))
    safe_message: Mapped[str | None] = mapped_column(String(1000))
    retry_scheduled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
