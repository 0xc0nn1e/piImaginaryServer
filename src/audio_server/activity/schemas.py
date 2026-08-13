"""Public recording activity response shapes."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from audio_server.db.activity_models import ProcessingActivityType
from audio_server.db.models import JobKind, JobStage, JobStatus


class ProcessingActivityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    job_kind: JobKind
    event_type: ProcessingActivityType
    job_status: JobStatus | None
    stage: JobStage | None
    attempt_count: int
    max_attempts: int
    error_code: str | None
    error_type: str | None
    message: str | None = Field(validation_alias="safe_message")
    retry_scheduled: bool
    next_attempt_at: datetime | None
    occurred_at: datetime


class RecordingActivityResponse(BaseModel):
    items: list[ProcessingActivityResponse]
    limit: int
    offset: int
