"""Add day-scoped summary jobs and the daily summary table."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_daily_summaries"
down_revision: str | None = "0008_orphan_translations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = "'full', 'analysis', 'translation'"
_NEW_KINDS = "'full', 'analysis', 'translation', 'daily_summary'"
_SCOPE = (
    "(recording_id IS NOT NULL AND summary_date IS NULL) OR "
    "(recording_id IS NULL AND summary_date IS NOT NULL)"
)


def upgrade() -> None:
    # A day summary belongs to no single recording, so the queue has to carry a
    # second, mutually exclusive scope. Existing rows keep their recording and
    # get a NULL day, which already satisfies the new scope constraint.
    op.add_column("processing_jobs", sa.Column("summary_date", sa.Date(), nullable=True))
    op.alter_column("processing_jobs", "recording_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint("processing_job_scope", "processing_jobs", _SCOPE)

    op.drop_constraint("processing_job_kind", "processing_jobs", type_="check")
    op.create_check_constraint(
        "processing_job_kind", "processing_jobs", f"kind IN ({_NEW_KINDS})"
    )
    op.drop_constraint("processing_activity_job_kind", "processing_activity", type_="check")
    op.create_check_constraint(
        "processing_activity_job_kind", "processing_activity", f"job_kind IN ({_NEW_KINDS})"
    )

    # One queued or running summary per day, mirroring the per-recording guard.
    op.create_index(
        "uq_processing_jobs_one_active_day",
        "processing_jobs",
        ["summary_date"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'processing')"),
        sqlite_where=sa.text("status IN ('queued', 'processing')"),
    )

    op.create_table(
        "daily_summaries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("summary_date", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("schema_version", sa.String(length=32), server_default="1", nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_revisions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'skipped', 'failed', 'stale')", name="daily_summary_status"
        ),
        sa.UniqueConstraint("summary_date", name="uq_daily_summaries_date"),
    )


def downgrade() -> None:
    op.drop_table("daily_summaries")
    op.drop_index("uq_processing_jobs_one_active_day", table_name="processing_jobs")

    # Day-scoped rows use a vocabulary and a NULL recording the old schema
    # cannot express, so they go before the narrower rules are restored.
    op.execute(sa.text("DELETE FROM processing_activity WHERE job_kind = 'daily_summary'"))
    op.execute(sa.text("DELETE FROM processing_jobs WHERE kind = 'daily_summary'"))
    op.execute(sa.text("DELETE FROM processing_jobs WHERE recording_id IS NULL"))

    op.drop_constraint("processing_activity_job_kind", "processing_activity", type_="check")
    op.create_check_constraint(
        "processing_activity_job_kind", "processing_activity", f"job_kind IN ({_OLD_KINDS})"
    )
    op.drop_constraint("processing_job_kind", "processing_jobs", type_="check")
    op.create_check_constraint(
        "processing_job_kind", "processing_jobs", f"kind IN ({_OLD_KINDS})"
    )
    op.drop_constraint("processing_job_scope", "processing_jobs", type_="check")
    op.alter_column("processing_jobs", "recording_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("processing_jobs", "summary_date")
