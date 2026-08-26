"""Let a hand-written translation outlive the sentence it described."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_orphan_translations"
down_revision: str | None = "0007_transcript_translations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Re-transcription replaces every segment and can regroup sentences, so a
    # rendering cannot always be reattached. Detaching it keeps the writing
    # visible instead of deleting work nobody agreed to lose.
    for column in ("start_segment_id", "end_segment_id"):
        op.alter_column(
            "transcript_translations", column, existing_type=sa.Uuid(), nullable=True
        )


def downgrade() -> None:
    # Narrowing the columns again requires every detached rendering to be gone,
    # but those rows are hand-written and deleting them here would destroy work
    # without anyone asking. Refuse instead, and say what has to be resolved.
    detached = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM transcript_translations "
            "WHERE start_segment_id IS NULL OR end_segment_id IS NULL"
        )
    ).scalar_one()
    if detached:
        raise RuntimeError(
            f"{detached} detached translation(s) would be lost. "
            "Reattach or delete them deliberately before downgrading."
        )
    for column in ("start_segment_id", "end_segment_id"):
        op.alter_column(
            "transcript_translations", column, existing_type=sa.Uuid(), nullable=False
        )
