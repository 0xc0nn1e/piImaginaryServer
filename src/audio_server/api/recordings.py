from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Query, Response, UploadFile
from pydantic import ValidationError

from audio_server.api.dependencies import get_recording_service, require_principal
from audio_server.api.schemas import (
    AnalysisResponse,
    ClientRecordingMetadata,
    JobError,
    JobStatusResponse,
    RecordingListResponse,
    RecordingStatusResponse,
    RecordingSummary,
    RetryResponse,
    TranscriptResponse,
    TranscriptSegmentResponse,
    UploadRecordingResponse,
)
from audio_server.db.models import RecordingStatus
from audio_server.services.recording_service import RecordingService, RecordingServiceError

router = APIRouter(
    prefix="/api/v1/recordings",
    tags=["recordings"],
    dependencies=[Depends(require_principal)],
)


@router.post("", response_model=UploadRecordingResponse, status_code=201)
def upload_recording(
    response: Response,
    audio: Annotated[UploadFile, File()],
    metadata: Annotated[str, Form()],
    checksum: Annotated[str, Form()],
    idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
    device_id: Annotated[str, Header(alias="X-Device-ID")],
    content_sha256: Annotated[str, Header(alias="X-Content-SHA256")],
    service: Annotated[RecordingService, Depends(get_recording_service)],
) -> UploadRecordingResponse:
    if len(metadata.encode("utf-8")) > service_max_metadata_bytes(service):
        raise RecordingServiceError(
            "metadata_too_large", "Upload metadata exceeds the configured limit.", status_code=413
        )
    try:
        parsed_metadata = ClientRecordingMetadata.model_validate_json(metadata)
    except ValidationError as exc:
        raise RecordingServiceError(
            "metadata_invalid", "Upload metadata is invalid.", status_code=422
        ) from exc

    result = service.ingest(
        source=audio.file,
        metadata=parsed_metadata,
        idempotency_key=idempotency_key,
        header_device_id=device_id,
        header_sha256=content_sha256,
        form_checksum=checksum,
    )
    if result.duplicate:
        response.status_code = 200
    response.headers["Location"] = f"/api/v1/recordings/{result.recording.id}"
    return UploadRecordingResponse(
        recording_id=result.recording.id,
        status=result.recording.processing_status,
        duplicate=result.duplicate,
    )


@router.get("", response_model=RecordingListResponse)
def list_recordings(
    service: Annotated[RecordingService, Depends(get_recording_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    device_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    status: Annotated[RecordingStatus | None, Query()] = None,
) -> RecordingListResponse:
    recordings = service.list_recordings(
        limit=limit, offset=offset, device_id=device_id, status=status
    )
    return RecordingListResponse(
        items=[RecordingSummary.model_validate(recording) for recording in recordings],
        limit=limit,
        offset=offset,
    )


@router.get("/{recording_id}", response_model=RecordingSummary)
def get_recording(
    recording_id: uuid.UUID,
    service: Annotated[RecordingService, Depends(get_recording_service)],
) -> RecordingSummary:
    return RecordingSummary.model_validate(service.get_recording(recording_id))


@router.get("/{recording_id}/status", response_model=RecordingStatusResponse)
def get_recording_status(
    recording_id: uuid.UUID,
    service: Annotated[RecordingService, Depends(get_recording_service)],
) -> RecordingStatusResponse:
    recording, job = service.get_status(recording_id)
    job_response = None
    if job is not None:
        error = None
        if job.error_code and job.error_message:
            error = JobError(
                code=job.error_code,
                type=job.error_type,
                message=job.error_message,
                stage=job.failed_stage,
                at=job.error_at,
            )
        job_response = JobStatusResponse(
            id=job.id,
            status=job.status,
            stage=job.stage,
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            available_at=job.available_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            error=error,
        )
    return RecordingStatusResponse(
        recording_id=recording.id,
        status=recording.processing_status,
        job=job_response,
    )


@router.get("/{recording_id}/transcript", response_model=TranscriptResponse)
def get_transcript(
    recording_id: uuid.UUID,
    service: Annotated[RecordingService, Depends(get_recording_service)],
) -> TranscriptResponse:
    recording, segments = service.get_transcript(recording_id)
    segment_responses = [
        TranscriptSegmentResponse(
            sequence=segment.sequence,
            speaker_label=segment.speaker_label,
            start_time=segment.start_time,
            end_time=segment.end_time,
            text=segment.text,
            language=segment.language,
            confidence=segment.confidence,
            has_overlap=segment.has_overlap,
        )
        for segment in segments
    ]
    return TranscriptResponse(
        recording_id=recording.id,
        status=recording.processing_status,
        text=_format_transcript(segment_responses),
        segments=segment_responses,
    )


@router.get("/{recording_id}/analysis", response_model=AnalysisResponse)
def get_analysis(
    recording_id: uuid.UUID,
    service: Annotated[RecordingService, Depends(get_recording_service)],
) -> AnalysisResponse:
    analysis = service.get_analysis(recording_id)
    error = None
    if analysis.error_code and analysis.error_message:
        error = {"code": analysis.error_code, "message": analysis.error_message}
    return AnalysisResponse(
        recording_id=recording_id,
        status=analysis.status,
        provider=analysis.provider,
        model=analysis.model,
        schema_version=analysis.schema_version,
        result=analysis.result,
        error=error,
    )


@router.post("/{recording_id}/retry", response_model=RetryResponse, status_code=202)
def retry_recording(
    recording_id: uuid.UUID,
    service: Annotated[RecordingService, Depends(get_recording_service)],
) -> RetryResponse:
    job = service.retry(recording_id)
    return RetryResponse(recording_id=recording_id, job_id=job.id, status=job.status)


def service_max_metadata_bytes(service: RecordingService) -> int:
    return service.max_metadata_bytes


def _format_transcript(segments: list[TranscriptSegmentResponse]) -> str:
    lines: list[str] = []
    for segment in segments:
        total_milliseconds = round(segment.start_time * 1000)
        hours, remainder = divmod(total_milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        timestamp = f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
        lines.append(f"[{timestamp}] {segment.speaker_label}:\n{segment.text}")
    return "\n\n".join(lines)
