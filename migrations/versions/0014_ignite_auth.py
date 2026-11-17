"""Add browser-bound OIDC attempts and session authentication identity."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_ignite_auth"
down_revision: str | None = "0013_recording_captions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL preserves existing sessions in local mode; Ignite mode rejects them.
    op.add_column("web_sessions", sa.Column("identity_digest", sa.String(64), nullable=True))
    op.create_table(
        "oidc_login_flows",
        sa.Column("state_digest", sa.String(64), primary_key=True),
        sa.Column("browser_digest", sa.String(64), nullable=False),
        sa.Column("identity_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_oidc_login_flows_expires_at", "oidc_login_flows", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_oidc_login_flows_expires_at", table_name="oidc_login_flows")
    op.drop_table("oidc_login_flows")
    op.drop_column("web_sessions", "identity_digest")
