"""Keep the live captions an iPhone recorder transcribed while recording."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_recording_captions"
down_revision: str | None = "0012_processing_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recording_captions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "recording_id",
            sa.Uuid(),
            sa.ForeignKey("recordings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("locale", sa.String(length=35), nullable=False),
        sa.Column(
            "lines",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("recording_id", name="uq_recording_captions_recording"),
    )


def downgrade() -> None:
    # Older code neither stores nor reads captions, so they go with the table.
    # The analyses written from them stay as they are.
    op.drop_table("recording_captions")
