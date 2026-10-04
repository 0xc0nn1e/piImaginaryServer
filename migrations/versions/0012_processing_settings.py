"""Let the administrator switch off the LLM work queued behind a transcript."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_processing_settings"
down_revision: str | None = "0011_job_follow_up"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processing_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("auto_analysis", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("auto_translation", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="processing_settings_singleton"),
    )
    # Every transcript has so far been followed by both its analysis and its
    # Cantonese translation, so the one row starts with both switches on and
    # nothing changes until the administrator turns one off.
    op.execute(sa.text("INSERT INTO processing_settings (id) VALUES (1)"))


def downgrade() -> None:
    # Older code queues both steps after every transcript, which is what both
    # switches on means. A step switched off here is switched back on by the
    # rollback; there is nowhere older code could keep that choice.
    op.drop_table("processing_settings")
