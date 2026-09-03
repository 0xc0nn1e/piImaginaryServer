"""Chain a recording's LLM work behind its transcription job."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_job_follow_up"
down_revision: str | None = "0010_recordings_started_at_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FOLLOW_UP_KINDS = "follow_up_kind IS NULL OR follow_up_kind IN ('analysis', 'translation')"


def upgrade() -> None:
    op.add_column(
        "processing_jobs",
        sa.Column("follow_up_kind", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "processing_job_follow_up_kind", "processing_jobs", _FOLLOW_UP_KINDS
    )

    # Analysis and translation used to run inside the transcription job. A job
    # that is still queued or running when this lands would otherwise commit a
    # transcript under the new code and never queue the LLM work that the old
    # code would have done inline, so those rows are given the chain here.
    # Finished jobs keep NULL: their recordings already hold whatever result
    # the inline run produced.
    op.execute(
        sa.text(
            "UPDATE processing_jobs SET follow_up_kind = 'analysis' "
            "WHERE kind = 'full' AND status IN ('queued', 'processing')"
        )
    )


def downgrade() -> None:
    # A rollback returns to code that runs the LLM steps inside the
    # transcription job, so a recording whose transcript was already committed
    # by the newer code will not be analysed or translated by the older code
    # either -- with or without this column. What the drop removes is the
    # hand-off still pending on a queued or running job, and it cannot be
    # rescued here: a recording is allowed one queued or running job at a time
    # (uq_processing_jobs_one_active_recording), so the successor cannot be
    # inserted while the job holding the hand-off is still active.
    #
    # Let the queue drain before rolling back. Anything caught mid-chain keeps
    # its transcript, and its analysis or Cantonese translation is re-requested
    # from the recording page.
    op.drop_constraint("processing_job_follow_up_kind", "processing_jobs", type_="check")
    op.drop_column("processing_jobs", "follow_up_kind")
