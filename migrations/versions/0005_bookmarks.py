"""Add per-user snapshot bookmarks for analysis expressions and highlights."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_bookmarks"
down_revision: str | None = "0004_web_upload_analysis_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bookmarks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column("original_ja", sa.Text(), nullable=False),
        sa.Column("translation_zh_hk", sa.Text(), nullable=False),
        sa.Column("note_ja", sa.Text(), nullable=False),
        sa.Column("note_zh_hk", sa.Text(), nullable=False),
        sa.Column("speaker_label", sa.String(length=64), nullable=False),
        sa.Column("start_time", sa.Float(), nullable=False),
        sa.Column("end_time", sa.Float(), nullable=True),
        # A deleted recording detaches its bookmarks instead of removing them,
        # so a personal study list survives the source audio.
        sa.Column("recording_id", sa.Uuid(), nullable=True),
        sa.Column("source_label", sa.String(length=255), nullable=False),
        sa.Column("source_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="SET NULL"),
        sa.CheckConstraint("kind IN ('expression', 'highlight')", name="bookmark_kind"),
        sa.UniqueConstraint("user_id", "source_digest", name="uq_bookmarks_user_source"),
    )
    op.create_index("ix_bookmarks_user_created", "bookmarks", ["user_id", "created_at"])
    op.create_index("ix_bookmarks_recording", "bookmarks", ["recording_id"])


def downgrade() -> None:
    op.drop_index("ix_bookmarks_recording", table_name="bookmarks")
    op.drop_index("ix_bookmarks_user_created", table_name="bookmarks")
    op.drop_table("bookmarks")
