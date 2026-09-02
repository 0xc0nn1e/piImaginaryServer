"""Index recordings by capture time for day-scoped reads."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_recordings_started_at_index"
down_revision: str | None = "0009_daily_summaries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The day pages, the digests a day summary is built from, and the recording
    # list's day filter all scan a started_at range. Every existing index on
    # this table is on created_at, which is upload time and answers none of it.
    op.create_index("ix_recordings_started_at", "recordings", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_recordings_started_at", table_name="recordings")
