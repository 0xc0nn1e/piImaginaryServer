"""Authenticated, recording-scoped processing activity API."""

from __future__ import annotations

import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request, Response

from audio_server.activity.schemas import (
    ProcessingActivityResponse,
    RecordingActivityResponse,
)
from audio_server.activity.service import ActivityService
from audio_server.api.dependencies import require_principal
from audio_server.core.database import Database

router = APIRouter(
    prefix="/api/v1/recordings",
    tags=["recording activity"],
    dependencies=[Depends(require_principal)],
)


def get_activity_service(request: Request) -> ActivityService:
    database = cast(Database, request.app.state.database)
    return ActivityService(database.session_factory)


@router.get("/{recording_id}/activity", response_model=RecordingActivityResponse)
def get_recording_activity(
    recording_id: uuid.UUID,
    response: Response,
    service: Annotated[ActivityService, Depends(get_activity_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RecordingActivityResponse:
    response.headers["Cache-Control"] = "no-store"
    items = service.list_recording_activity(recording_id, limit=limit, offset=offset)
    return RecordingActivityResponse(
        items=[ProcessingActivityResponse.model_validate(item) for item in items],
        limit=limit,
        offset=offset,
    )
