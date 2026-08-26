"""Add a human-set checked flag to recordings."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_recording_checked"
down_revision: str | None = "0005_bookmarks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A literal server default backfills every existing row in one statement,
    # so the column can be NOT NULL immediately.
    op.add_column(
        "recordings",
        sa.Column("checked", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_index("ix_recordings_checked_created", "recordings", ["checked", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_recordings_checked_created", table_name="recordings")
    op.drop_column("recordings", "checked")
