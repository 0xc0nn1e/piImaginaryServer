"""Read-only view of the processing queue.

The queue is the only place that explains why a recording has not progressed:
a full job can hold a worker for hours while cheap analysis work waits behind
it. Exposing the claim order makes that visible instead of inferred.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request, Response

from audio_server.api.dependencies import require_principal
from audio_server.api.schemas import (
    JobStatusResponse,
    QueueEntryResponse,
    QueueResponse,
)
from audio_server.db.models import JobKind
from audio_server.services.recording_service import RecordingService

router = APIRouter(
    prefix="/api/v1/queue",
    tags=["queue"],
    dependencies=[Depends(require_principal)],
)

MAX_QUEUE_ITEMS = 100


def get_recording_service(request: Request) -> RecordingService:
    return cast(RecordingService, request.app.state.recording_service)


@router.get("", response_model=QueueResponse)
def read_queue(
    response: Response,
    service: Annotated[RecordingService, Depends(get_recording_service)],
    limit: Annotated[int, Query(ge=1, le=MAX_QUEUE_ITEMS)] = 50,
) -> QueueResponse:
    response.headers["Cache-Control"] = "no-store"
    # Each kind is served by its own worker, so one shared cap could hide every
    # analysis job behind a backlog of transcription work.
    rows = [
        row for kind in (JobKind.FULL, JobKind.ANALYSIS, JobKind.TRANSLATION)
        for row in service.list_active_jobs(limit=limit, kind=kind)
    ]
    items = [
        QueueEntryResponse(
            recording_id=recording.id,
            original_filename=recording.original_filename,
            job=JobStatusResponse.model_validate(job, from_attributes=True),
        )
        for recording, job in rows
    ]
    processing, queued = service.count_active_jobs()
    return QueueResponse(items=items, processing=processing, queued=queued)
