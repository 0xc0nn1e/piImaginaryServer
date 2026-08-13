"""Add browser upload and revisioned analysis-only processing support."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_web_upload_analysis_v2"
down_revision: str | None = "0003_processing_activity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recordings",
        sa.Column("transcript_revision", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "recordings",
        sa.Column("analysis_revision", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("kind", sa.String(length=32), server_default="full", nullable=False),
    )
    op.create_check_constraint(
        "processing_job_kind", "processing_jobs", "kind IN ('full', 'analysis')"
    )
    op.add_column(
        "processing_activity",
        sa.Column("job_kind", sa.String(length=32), server_default="full", nullable=False),
    )
    op.create_check_constraint(
        "processing_activity_job_kind",
        "processing_activity",
        "job_kind IN ('full', 'analysis')",
    )

    # Keep the newest current analysis before enforcing one row per recording.
    op.execute(
        sa.text(
            """
            DELETE FROM analyses AS older
            USING analyses AS newer
            WHERE older.recording_id = newer.recording_id
              AND (older.created_at, older.id) < (newer.created_at, newer.id)
            """
        )
    )
    op.drop_constraint("analysis_status", "analyses", type_="check")
    op.create_check_constraint(
        "analysis_status",
        "analyses",
        "status IN ('completed', 'skipped', 'failed', 'stale')",
    )
    op.create_unique_constraint("uq_analyses_recording", "analyses", ["recording_id"])


def downgrade() -> None:
    op.drop_constraint("uq_analyses_recording", "analyses", type_="unique")
    op.execute(sa.text("UPDATE analyses SET status = 'failed' WHERE status = 'stale'"))
    op.drop_constraint("analysis_status", "analyses", type_="check")
    op.create_check_constraint(
        "analysis_status", "analyses", "status IN ('completed', 'skipped', 'failed')"
    )
    op.drop_constraint("processing_activity_job_kind", "processing_activity", type_="check")
    op.drop_column("processing_activity", "job_kind")
    op.drop_constraint("processing_job_kind", "processing_jobs", type_="check")
    op.drop_column("processing_jobs", "kind")
    op.drop_column("recordings", "analysis_revision")
    op.drop_column("recordings", "transcript_revision")
