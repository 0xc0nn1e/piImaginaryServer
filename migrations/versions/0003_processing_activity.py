"""Add a durable, privacy-safe processing activity timeline.

This revision requires an online database connection because it backfills
truthful lifecycle events from processing jobs that already exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import context, op

revision: str = "0003_processing_activity"
down_revision: str | None = "0002_web_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_TYPES = (
    "job_queued",
    "processing_started",
    "stage_started",
    "retry_scheduled",
    "lease_recovered",
    "processing_completed",
    "processing_failed",
    "manual_retry_queued",
)
_JOB_STATUSES = ("queued", "processing", "completed", "failed")
_JOB_STAGES = (
    "queued",
    "preprocessing",
    "transcribing",
    "diarizing",
    "merging",
    "analyzing",
    "completed",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "0003_processing_activity requires an online database connection "
            "to backfill existing processing jobs"
        )
    op.create_table(
        "processing_activity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recording_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("job_status", sa.String(length=32), nullable=True),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("safe_message", sa.String(length=1000), nullable=True),
        sa.Column("retry_scheduled", sa.Boolean(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"event_type IN ({_quoted(_EVENT_TYPES)})",
            name="processing_activity_event_type",
        ),
        sa.CheckConstraint(
            f"job_status IS NULL OR job_status IN ({_quoted(_JOB_STATUSES)})",
            name="processing_activity_job_status",
        ),
        sa.CheckConstraint(
            f"stage IS NULL OR stage IN ({_quoted(_JOB_STAGES)})",
            name="processing_activity_stage",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="processing_activity_attempt_nonnegative",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="processing_activity_max_attempts_positive",
        ),
        sa.CheckConstraint(
            "retry_scheduled = false OR next_attempt_at IS NOT NULL",
            name="processing_activity_retry_time_required",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["processing_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_processing_activity_job_attempt_type_stage",
        "processing_activity",
        ["job_id", "attempt_count", "event_type", "stage"],
        unique=True,
        postgresql_where=sa.text("stage IS NOT NULL"),
        sqlite_where=sa.text("stage IS NOT NULL"),
    )
    op.create_index(
        "uq_processing_activity_job_attempt_type_no_stage",
        "processing_activity",
        ["job_id", "attempt_count", "event_type"],
        unique=True,
        postgresql_where=sa.text("stage IS NULL"),
        sqlite_where=sa.text("stage IS NULL"),
    )
    op.create_index(
        "ix_processing_activity_recording_chronological",
        "processing_activity",
        ["recording_id", "occurred_at", "id"],
    )
    op.create_index(
        "ix_processing_activity_job_chronological",
        "processing_activity",
        ["job_id", "occurred_at", "id"],
    )
    _backfill_existing_jobs()


def _backfill_existing_jobs() -> None:
    """Use only lifecycle timestamps and state already persisted by each job."""

    connection = op.get_bind()
    jobs_table = _processing_jobs_table()
    jobs = connection.execute(sa.select(*jobs_table.c)).mappings()
    rows: list[dict[str, Any]] = []
    for job in jobs:
        job_id = job["id"]
        recording_id = job["recording_id"]
        max_attempts = job["max_attempts"]
        rows.append(
            _event_row(
                job_id=job_id,
                recording_id=recording_id,
                event_type="job_queued",
                job_status="queued",
                stage="queued",
                attempt_count=0,
                max_attempts=max_attempts,
                occurred_at=job["created_at"],
            )
        )

        if job["started_at"] is not None:
            # `started_at` identifies the first claim, but historical rows do
            # not contain exact per-stage transition timestamps.
            rows.append(
                _event_row(
                    job_id=job_id,
                    recording_id=recording_id,
                    event_type="processing_started",
                    job_status="processing",
                    stage=None,
                    attempt_count=1,
                    max_attempts=max_attempts,
                    occurred_at=job["started_at"],
                )
            )

        status = job["status"]
        error_at = job["error_at"]
        if status == "queued" and error_at is not None:
            recovered = job["error_code"] == "worker_lease_expired"
            rows.append(
                _event_row(
                    job_id=job_id,
                    recording_id=recording_id,
                    event_type="lease_recovered" if recovered else "retry_scheduled",
                    job_status="queued",
                    stage=job["failed_stage"],
                    attempt_count=job["attempt_count"],
                    max_attempts=max_attempts,
                    occurred_at=error_at,
                    error_code=job["error_code"],
                    error_type=job["error_type"],
                    safe_message=job["error_message"],
                    retry_scheduled=True,
                    next_attempt_at=job["available_at"],
                )
            )
        elif status == "completed" and job["finished_at"] is not None:
            rows.append(
                _event_row(
                    job_id=job_id,
                    recording_id=recording_id,
                    event_type="processing_completed",
                    job_status="completed",
                    stage="completed",
                    attempt_count=job["attempt_count"],
                    max_attempts=max_attempts,
                    occurred_at=job["finished_at"],
                )
            )
        elif status == "failed":
            occurred_at = job["finished_at"] or error_at or job["updated_at"]
            recovered = job["error_code"] == "worker_lease_expired"
            rows.append(
                _event_row(
                    job_id=job_id,
                    recording_id=recording_id,
                    event_type="lease_recovered" if recovered else "processing_failed",
                    job_status="failed",
                    stage=job["failed_stage"],
                    attempt_count=job["attempt_count"],
                    max_attempts=max_attempts,
                    occurred_at=occurred_at,
                    error_code=job["error_code"],
                    error_type=job["error_type"],
                    safe_message=job["error_message"],
                )
            )

    if rows:
        connection.execute(sa.insert(_activity_table()), rows)


def _event_row(**values: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "error_code": None,
        "error_type": None,
        "safe_message": None,
        "retry_scheduled": False,
        "next_attempt_at": None,
        **values,
    }


def _activity_table() -> sa.TableClause:
    return sa.table(
        "processing_activity",
        sa.column("id", sa.Uuid()),
        sa.column("recording_id", sa.Uuid()),
        sa.column("job_id", sa.Uuid()),
        sa.column("event_type", sa.String()),
        sa.column("job_status", sa.String()),
        sa.column("stage", sa.String()),
        sa.column("attempt_count", sa.Integer()),
        sa.column("max_attempts", sa.Integer()),
        sa.column("error_code", sa.String()),
        sa.column("error_type", sa.String()),
        sa.column("safe_message", sa.String()),
        sa.column("retry_scheduled", sa.Boolean()),
        sa.column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.column("occurred_at", sa.DateTime(timezone=True)),
    )


def _processing_jobs_table() -> sa.TableClause:
    """Typed lightweight table used by the cross-dialect data backfill."""

    return sa.table(
        "processing_jobs",
        sa.column("id", sa.Uuid()),
        sa.column("recording_id", sa.Uuid()),
        sa.column("status", sa.String()),
        sa.column("stage", sa.String()),
        sa.column("attempt_count", sa.Integer()),
        sa.column("max_attempts", sa.Integer()),
        sa.column("available_at", sa.DateTime(timezone=True)),
        sa.column("failed_stage", sa.String()),
        sa.column("error_code", sa.String()),
        sa.column("error_type", sa.String()),
        sa.column("error_message", sa.String()),
        sa.column("error_at", sa.DateTime(timezone=True)),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("finished_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_processing_activity_job_chronological",
        table_name="processing_activity",
    )
    op.drop_index(
        "ix_processing_activity_recording_chronological",
        table_name="processing_activity",
    )
    op.drop_index(
        "uq_processing_activity_job_attempt_type_stage",
        table_name="processing_activity",
    )
    op.drop_index(
        "uq_processing_activity_job_attempt_type_no_stage",
        table_name="processing_activity",
    )
    op.drop_table("processing_activity")
