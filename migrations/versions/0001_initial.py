"""Create the initial durable audio processing schema."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


recording_status = sa.Enum(
    "uploaded",
    "queued",
    "processing",
    "completed",
    "failed",
    name="recording_status",
    native_enum=False,
    create_constraint=True,
)
job_status = sa.Enum(
    "queued",
    "processing",
    "completed",
    "failed",
    name="job_status",
    native_enum=False,
    create_constraint=True,
)
job_stage = sa.Enum(
    "queued",
    "preprocessing",
    "transcribing",
    "diarizing",
    "merging",
    "analyzing",
    "completed",
    name="job_stage",
    native_enum=False,
    create_constraint=True,
)
failed_job_stage = sa.Enum(
    "queued",
    "preprocessing",
    "transcribing",
    "diarizing",
    "merging",
    "analyzing",
    "completed",
    name="failed_job_stage",
    native_enum=False,
    create_constraint=True,
)
analysis_status = sa.Enum(
    "completed",
    "skipped",
    "failed",
    name="analysis_status",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "recordings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("audio_format", sa.String(length=32), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("channels", sa.Integer(), nullable=True),
        sa.Column("client_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("processing_status", recording_status, nullable=False),
        sa.Column("audio_delete_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transcript_delete_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id", "sha256", name="uq_recordings_device_sha256"),
        sa.UniqueConstraint("storage_key", name="uq_recordings_storage_key"),
    )
    op.create_index("ix_recordings_created_at", "recordings", ["created_at"])
    op.create_index(
        "ix_recordings_status_created", "recordings", ["processing_status", "created_at"]
    )

    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recording_id", sa.Uuid(), nullable=False),
        sa.Column("status", job_status, nullable=False),
        sa.Column("stage", job_stage, nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_stage", failed_job_stage, nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_processing_jobs_claim",
        "processing_jobs",
        ["available_at", "created_at", "id"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_processing_jobs_expired",
        "processing_jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.create_index(
        "uq_processing_jobs_one_active_recording",
        "processing_jobs",
        ["recording_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'processing')"),
    )

    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recording_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("speaker_label", sa.String(length=64), nullable=False),
        sa.Column("start_time", sa.Float(), nullable=False),
        sa.Column("end_time", sa.Float(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("has_overlap", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["processing_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recording_id", "sequence", name="uq_transcript_recording_sequence"),
    )
    op.create_index(
        "ix_transcript_segments_recording_sequence",
        "transcript_segments",
        ["recording_id", "sequence"],
    )

    op.create_table(
        "analyses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recording_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("status", analysis_status, nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["processing_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_analyses_job"),
    )
    op.create_index("ix_analyses_recording_created", "analyses", ["recording_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_analyses_recording_created", table_name="analyses")
    op.drop_table("analyses")
    op.drop_index("ix_transcript_segments_recording_sequence", table_name="transcript_segments")
    op.drop_table("transcript_segments")
    op.drop_index("uq_processing_jobs_one_active_recording", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_expired", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_claim", table_name="processing_jobs")
    op.drop_table("processing_jobs")
    op.drop_index("ix_recordings_status_created", table_name="recordings")
    op.drop_index("ix_recordings_created_at", table_name="recordings")
    op.drop_table("recordings")
