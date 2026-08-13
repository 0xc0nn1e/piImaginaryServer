"""Persistence helpers for allowlisted processing activity events."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from audio_server.db.activity_models import ProcessingActivity, ProcessingActivityType
from audio_server.db.models import JobKind, JobStage, JobStatus, Recording

_SAFE_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_.-]+")


def append_activity(
    session: Session,
    *,
    recording_id: uuid.UUID,
    job_id: uuid.UUID,
    job_kind: JobKind = JobKind.FULL,
    event_type: ProcessingActivityType,
    job_status: JobStatus | None,
    stage: JobStage | None,
    attempt_count: int,
    max_attempts: int,
    occurred_at: datetime,
    error_code: str | None = None,
    error_type: str | None = None,
    safe_message: str | None = None,
    retry_scheduled: bool = False,
    next_attempt_at: datetime | None = None,
) -> ProcessingActivity:
    """Append a deliberately narrow event inside the caller's transaction."""

    if attempt_count < 0 or max_attempts < 1:
        raise ValueError("invalid processing activity attempt counts")
    if retry_scheduled and next_attempt_at is None:
        raise ValueError("a scheduled retry requires its next attempt time")
    stage_clause = (
        ProcessingActivity.stage.is_(None) if stage is None else ProcessingActivity.stage == stage
    )
    existing = session.scalar(
        select(ProcessingActivity)
        .where(
            ProcessingActivity.job_id == job_id,
            ProcessingActivity.attempt_count == attempt_count,
            ProcessingActivity.event_type == event_type,
            stage_clause,
        )
        .limit(1)
    )
    if existing is not None:
        return existing
    activity = ProcessingActivity(
        recording_id=recording_id,
        job_id=job_id,
        job_kind=job_kind,
        event_type=event_type,
        job_status=job_status,
        stage=stage,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        error_code=_bounded_identifier(error_code) if error_code is not None else None,
        error_type=_bounded_identifier(error_type) if error_type is not None else None,
        safe_message=_bounded_message(safe_message) if safe_message is not None else None,
        retry_scheduled=retry_scheduled,
        next_attempt_at=next_attempt_at,
        occurred_at=occurred_at,
    )
    session.add(activity)
    return activity


def list_recording_activity(
    session: Session,
    *,
    recording_id: uuid.UUID,
    limit: int,
    offset: int,
) -> list[ProcessingActivity] | None:
    if limit < 1 or limit > 100 or offset < 0:
        raise ValueError("invalid activity pagination")
    if session.get(Recording, recording_id) is None:
        return None
    return list(
        session.scalars(
            select(ProcessingActivity)
            .where(ProcessingActivity.recording_id == recording_id)
            .order_by(ProcessingActivity.occurred_at, ProcessingActivity.id)
            .limit(limit)
            .offset(offset)
        )
    )


def _bounded_identifier(value: str) -> str:
    normalized = _SAFE_IDENTIFIER.sub("_", value.strip())[:128]
    return normalized or "processing_error"


def _bounded_message(value: str) -> str:
    return " ".join(value.split())[:1000] or "Processing failed."
