"""Recording activity query service."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from audio_server.activity.repository import list_recording_activity
from audio_server.db.activity_models import ProcessingActivity
from audio_server.services.recording_service import RecordingServiceError


class ActivityService:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def list_recording_activity(
        self,
        recording_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
    ) -> list[ProcessingActivity]:
        with self._session_factory() as session:
            activity = list_recording_activity(
                session,
                recording_id=recording_id,
                limit=limit,
                offset=offset,
            )
            if activity is None:
                raise RecordingServiceError(
                    "recording_not_found",
                    "Recording was not found.",
                    status_code=404,
                )
            return activity
