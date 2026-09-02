"""Day-scoped reads and the manual day summary trigger."""

from __future__ import annotations

from datetime import date
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request, Response

from audio_server.api.dependencies import (
    get_recording_service,
    require_mutation_principal,
    require_principal,
)
from audio_server.api.recordings import job_response
from audio_server.api.schemas import (
    DailySummaryResultV1,
    DayAnalysisQueuedResponse,
    DayDetailResponse,
    DayListEntryResponse,
    DayListResponse,
    DaySummaryQueuedResponse,
    FuriganaToken,
    RecordingSummary,
)
from audio_server.core.furigana import annotate_all
from audio_server.db.models import AnalysisStatus, DailySummary
from audio_server.services.daily_service import DailyService
from audio_server.services.recording_service import RecordingService

router = APIRouter(
    prefix="/api/v1/days",
    tags=["days"],
    dependencies=[Depends(require_principal)],
)


def get_daily_service(request: Request) -> DailyService:
    return cast(DailyService, request.app.state.daily_service)


@router.get("", response_model=DayListResponse)
def list_days(
    response: Response,
    service: Annotated[DailyService, Depends(get_daily_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DayListResponse:
    response.headers["Cache-Control"] = "no-store"
    entries = service.list_days(limit=limit, offset=offset)
    return DayListResponse(
        items=[
            DayListEntryResponse(
                day=entry.day,
                recording_count=entry.recording_count,
                analysed_count=entry.analysed_count,
                summary_status=entry.summary_status,
                summary_stale=entry.summary_stale,
            )
            for entry in entries
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/{day}", response_model=DayDetailResponse)
def get_day(
    day: date,
    response: Response,
    service: Annotated[DailyService, Depends(get_daily_service)],
) -> DayDetailResponse:
    response.headers["Cache-Control"] = "no-store"
    detail = service.get_day(day)
    summary = detail.summary
    structured = _structured_summary(summary)
    return DayDetailResponse(
        day=day,
        recordings=[RecordingSummary.model_validate(item) for item in detail.recordings],
        analysed_recording_ids=sorted(detail.analysed_ids, key=str),
        active_job_recording_ids=sorted(detail.active_job_ids, key=str),
        status=summary.status if summary else None,
        provider=summary.provider if summary else None,
        model=summary.model if summary else None,
        schema_version=summary.schema_version if summary else None,
        summary=structured,
        stale=detail.stale,
        job=job_response(detail.job) if detail.job else None,
        error=_summary_error(summary),
        furigana=_summary_furigana(structured),
    )


@router.post("/{day}/summary/reprocess", response_model=DaySummaryQueuedResponse, status_code=202)
def reprocess_day_summary(
    day: date,
    _principal: Annotated[object, Depends(require_mutation_principal)],
    service: Annotated[DailyService, Depends(get_daily_service)],
) -> DaySummaryQueuedResponse:
    job = service.queue_summary(day)
    return DaySummaryQueuedResponse(day=day, job_id=job.id, status=job.status)


@router.post(
    "/{day}/analysis/reprocess", response_model=DayAnalysisQueuedResponse, status_code=202
)
def reprocess_day_analyses(
    day: date,
    _principal: Annotated[object, Depends(require_mutation_principal)],
    service: Annotated[DailyService, Depends(get_daily_service)],
    recordings: Annotated[RecordingService, Depends(get_recording_service)],
) -> DayAnalysisQueuedResponse:
    """Queue analysis for every recording of the day that still lacks one.

    Queueing is per recording rather than one day-wide job: the worker already
    knows how to analyse a recording, and a day whose recordings are half
    analysed should only pay for the half that is missing.
    """

    jobs, skipped = recordings.reprocess_analyses(service.pending_analysis_recording_ids(day))
    return DayAnalysisQueuedResponse(
        day=day,
        queued_recording_ids=[job.recording_id for job in jobs if job.recording_id is not None],
        skipped=skipped,
    )


def _structured_summary(summary: DailySummary | None) -> DailySummaryResultV1 | None:
    """Return the stored summary only when it still matches the schema.

    A stored payload that no longer validates is dropped rather than served
    half-formed; the day simply reads as not summarised yet.
    """

    if summary is None or not isinstance(summary.result, dict):
        return None
    try:
        return DailySummaryResultV1.model_validate(summary.result)
    except ValueError:
        return None


def _summary_error(summary: DailySummary | None) -> dict[str, str] | None:
    if summary is None or summary.status is not AnalysisStatus.FAILED:
        return None
    return {
        "code": summary.error_code or "daily_summary_failed",
        "message": summary.error_message or "The day summary could not be produced.",
    }


def _summary_furigana(
    summary: DailySummaryResultV1 | None,
) -> dict[str, list[FuriganaToken]]:
    if summary is None:
        return {}
    japanese = [summary.overview.ja]
    japanese.extend(point.ja for point in summary.key_points)
    japanese.extend(tag.ja for tag in summary.tags)
    return {
        text: [FuriganaToken(text=token.text, reading=token.reading) for token in tokens]
        for text, tokens in annotate_all(japanese).items()
    }

