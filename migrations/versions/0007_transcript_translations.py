"""Add sentence-level Cantonese translations and the translation job kind."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_transcript_translations"
down_revision: str | None = "0006_recording_checked"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STAGES = "'queued', 'preprocessing', 'transcribing', 'diarizing', 'merging', 'analyzing', 'completed'"
_NEW_STAGES = (
    "'queued', 'preprocessing', 'transcribing', 'diarizing', 'merging', "
    "'translating', 'analyzing', 'completed'"
)


def upgrade() -> None:
    op.add_column(
        "recordings",
        sa.Column("translation_revision", sa.Integer(), server_default="0", nullable=False),
    )

    # The kind and stage vocabularies are enforced by literal CHECK constraints,
    # so every table that names them has to be rewritten together.
    op.drop_constraint("processing_job_kind", "processing_jobs", type_="check")
    op.create_check_constraint(
        "processing_job_kind", "processing_jobs", "kind IN ('full', 'analysis', 'translation')"
    )
    op.drop_constraint("job_stage", "processing_jobs", type_="check")
    op.create_check_constraint("job_stage", "processing_jobs", f"stage IN ({_NEW_STAGES})")
    op.drop_constraint("failed_job_stage", "processing_jobs", type_="check")
    op.create_check_constraint(
        "failed_job_stage", "processing_jobs", f"failed_stage IN ({_NEW_STAGES})"
    )
    op.drop_constraint("processing_activity_job_kind", "processing_activity", type_="check")
    op.create_check_constraint(
        "processing_activity_job_kind",
        "processing_activity",
        "job_kind IN ('full', 'analysis', 'translation')",
    )
    op.drop_constraint("processing_activity_stage", "processing_activity", type_="check")
    op.create_check_constraint(
        "processing_activity_stage", "processing_activity", f"stage IN ({_NEW_STAGES})"
    )

    op.create_table(
        "transcript_translations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "recording_id",
            sa.Uuid(),
            sa.ForeignKey("recordings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "start_segment_id",
            sa.Uuid(),
            sa.ForeignKey("transcript_segments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "end_segment_id",
            sa.Uuid(),
            sa.ForeignKey("transcript_segments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_ja", sa.Text(), nullable=False),
        sa.Column("text_zh_hk", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=32), server_default="llm", nullable=False),
        sa.Column("stale", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("source IN ('llm', 'manual')", name="translation_source"),
        sa.UniqueConstraint(
            "recording_id", "start_segment_id", name="uq_translation_recording_start"
        ),
    )
    op.create_index(
        "ix_transcript_translations_recording", "transcript_translations", ["recording_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_transcript_translations_recording", table_name="transcript_translations")
    op.drop_table("transcript_translations")

    # Rows using the new vocabulary must go before the narrower constraints
    # can be restored, or the migration would fail against real data.
    op.execute(sa.text("DELETE FROM processing_activity WHERE job_kind = 'translation'"))
    op.execute(sa.text("DELETE FROM processing_activity WHERE stage = 'translating'"))
    op.execute(sa.text("DELETE FROM processing_jobs WHERE kind = 'translation'"))
    op.execute(sa.text("UPDATE processing_jobs SET stage = 'merging' WHERE stage = 'translating'"))
    op.execute(
        sa.text("UPDATE processing_jobs SET failed_stage = NULL WHERE failed_stage = 'translating'")
    )

    op.drop_constraint("processing_activity_stage", "processing_activity", type_="check")
    op.create_check_constraint(
        "processing_activity_stage", "processing_activity", f"stage IN ({_OLD_STAGES})"
    )
    op.drop_constraint("processing_activity_job_kind", "processing_activity", type_="check")
    op.create_check_constraint(
        "processing_activity_job_kind", "processing_activity", "job_kind IN ('full', 'analysis')"
    )
    op.drop_constraint("failed_job_stage", "processing_jobs", type_="check")
    op.create_check_constraint(
        "failed_job_stage", "processing_jobs", f"failed_stage IN ({_OLD_STAGES})"
    )
    op.drop_constraint("job_stage", "processing_jobs", type_="check")
    op.create_check_constraint("job_stage", "processing_jobs", f"stage IN ({_OLD_STAGES})")
    op.drop_constraint("processing_job_kind", "processing_jobs", type_="check")
    op.create_check_constraint(
        "processing_job_kind", "processing_jobs", "kind IN ('full', 'analysis')"
    )

    op.drop_column("recordings", "translation_revision")
