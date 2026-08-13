from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile

from audio_server.api.dependencies import (
    get_recording_service,
    require_mutation_principal,
    require_principal,
)
from audio_server.api.schemas import UploadRecordingResponse
from audio_server.services.recording_service import RecordingService

router = APIRouter(
    prefix="/api/v1/web/recordings",
    tags=["browser recordings"],
    dependencies=[Depends(require_principal)],
)


@router.post("", response_model=UploadRecordingResponse, status_code=201)
def upload_web_recording(
    response: Response,
    audio: Annotated[UploadFile, File()],
    _principal: Annotated[object, Depends(require_mutation_principal)],
    service: Annotated[RecordingService, Depends(get_recording_service)],
    started_at: Annotated[datetime | None, Form()] = None,
) -> UploadRecordingResponse:
    result = service.ingest_web(
        source=audio.file,
        filename=audio.filename or "audio",
        started_at=started_at,
    )
    if result.duplicate:
        response.status_code = 200
    response.headers["Location"] = f"/api/v1/recordings/{result.recording.id}"
    return UploadRecordingResponse(
        recording_id=result.recording.id,
        status=result.recording.processing_status,
        duplicate=result.duplicate,
    )
