from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic import context as alembic_context
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import audio_server.jobs.queue as queue_module
import audio_server.services.recording_service as recording_service_module
from audio_server.activity.repository import list_recording_activity
from audio_server.api.activity import router as activity_router
from audio_server.db.activity_models import ProcessingActivity, ProcessingActivityType
from audio_server.db.models import (
    JobStage,
    JobStatus,
    ProcessingJob,
    Recording,
    RecordingStatus,
)
from audio_server.jobs.queue import (
    JobFailure,
    JobQueue,
    RetryPolicy,
    create_processing_job,
    retry_failed_recording,
)
from audio_server.jobs.worker import Worker, WorkerIntervals
from tests.conftest import TEST_API_TOKEN, make_upload

BASE_TIME = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)


def _add_recording(session: Session, recording_id: uuid.UUID) -> Recording:
    recording = Recording(
        id=recording_id,
        device_id="test-device",
        original_filename="recording.wav",
        storage_key=f"recordings/{recording_id}/original.wav",
        mime_type="audio/wav",
        audio_format="wav",
        file_size=1024,
        sha256=uuid.uuid4().hex * 2,
        started_at=BASE_TIME,
        ended_at=BASE_TIME + timedelta(seconds=1),
        duration_seconds=1,
        sample_rate=16_000,
        channels=1,
        client_metadata={},
        processing_status=RecordingStatus.UPLOADED,
    )
    session.add(recording)
    return recording


def _create_job(
    session_factory: sessionmaker[Session],
    *,
    max_attempts: int = 3,
) -> tuple[uuid.UUID, uuid.UUID]:
    recording_id = uuid.uuid4()
    with session_factory.begin() as session:
        _add_recording(session, recording_id)
        job = create_processing_job(
            session,
            recording_id=recording_id,
            max_attempts=max_attempts,
            available_at=BASE_TIME,
        )
        job_id = job.id
    return recording_id, job_id


def _queue(
    session_factory: sessionmaker[Session],
    *,
    lease_seconds: int = 60,
) -> JobQueue:
    return JobQueue(
        session_factory,
        worker_id="activity-test-worker",
        lease_duration=timedelta(seconds=lease_seconds),
        retry_policy=RetryPolicy(
            base_delay=timedelta(seconds=30),
            max_delay=timedelta(seconds=120),
        ),
    )


def test_queue_events_are_chronological_idempotent_and_exclude_heartbeats(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, _job_id = _create_job(session_factory)
    queue = _queue(session_factory)
    claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=1))
    assert claim is not None

    queue.heartbeat(claim, now=BASE_TIME + timedelta(seconds=2))
    queue.advance_stage(
        claim,
        JobStage.TRANSCRIBING,
        now=BASE_TIME + timedelta(seconds=3),
    )
    # Repeating a stage update does not create a duplicate UI event.
    queue.advance_stage(
        claim,
        JobStage.TRANSCRIBING,
        now=BASE_TIME + timedelta(seconds=4),
    )
    queue.complete(claim, now=BASE_TIME + timedelta(seconds=5))

    with session_factory() as session:
        events = list_recording_activity(
            session,
            recording_id=recording_id,
            limit=100,
            offset=0,
        )
    assert events is not None
    # The transcription job finishes by queueing the analysis that reads its
    # transcript, so the recording's timeline carries that hand-off too.
    assert [event.event_type for event in events] == [
        ProcessingActivityType.JOB_QUEUED,
        ProcessingActivityType.PROCESSING_STARTED,
        ProcessingActivityType.STAGE_STARTED,
        ProcessingActivityType.PROCESSING_COMPLETED,
        ProcessingActivityType.JOB_QUEUED,
    ]
    assert [event.occurred_at for event in events] == sorted(event.occurred_at for event in events)
    assert all(event.event_type.value != "heartbeat" for event in events)


def test_retry_and_terminal_failure_events_store_only_bounded_safe_fields(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, job_id = _create_job(session_factory, max_attempts=2)
    queue = _queue(session_factory)
    first_claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=1))
    assert first_claim is not None
    failure = JobFailure(
        code="temporary timeout\ninjected/value",
        error_type="Provider Error\tunsafe",
        message=" Safe retry message\n" + ("x" * 1200),
        retryable=True,
        stage=JobStage.TRANSCRIBING,
    )

    assert queue.fail(first_claim, failure, now=BASE_TIME + timedelta(seconds=2)) is True
    second_claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=32))
    assert second_claim is not None
    assert queue.fail(second_claim, failure, now=BASE_TIME + timedelta(seconds=33)) is False

    with session_factory() as session:
        events = list(
            session.scalars(
                select(ProcessingActivity)
                .where(ProcessingActivity.job_id == job_id)
                .order_by(ProcessingActivity.occurred_at)
            )
        )
        recording = session.get(Recording, recording_id)

    retry = next(
        event for event in events if event.event_type is ProcessingActivityType.RETRY_SCHEDULED
    )
    terminal = next(
        event for event in events if event.event_type is ProcessingActivityType.PROCESSING_FAILED
    )
    assert retry.retry_scheduled is True
    assert retry.next_attempt_at is not None
    assert retry.next_attempt_at.replace(tzinfo=UTC) == BASE_TIME + timedelta(seconds=32)
    assert retry.error_code == "temporary_timeout_injected_value"
    assert retry.error_type == "Provider_Error_unsafe"
    assert retry.safe_message is not None and len(retry.safe_message) == 1000
    assert "\n" not in retry.safe_message
    assert terminal.retry_scheduled is False
    assert terminal.next_attempt_at is None
    assert recording is not None
    assert recording.processing_status is RecordingStatus.FAILED


def test_expired_lease_recovery_is_recorded_with_retry_state(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, _job_id = _create_job(session_factory)
    queue = _queue(session_factory, lease_seconds=30)
    claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=1))
    assert claim is not None

    summary = queue.recover_expired(now=BASE_TIME + timedelta(seconds=31))

    assert summary.requeued == 1
    with session_factory() as session:
        events = list_recording_activity(
            session,
            recording_id=recording_id,
            limit=100,
            offset=0,
        )
    assert events is not None
    recovered = events[-1]
    assert recovered.event_type is ProcessingActivityType.LEASE_RECOVERED
    assert recovered.job_status is JobStatus.QUEUED
    assert recovered.stage is JobStage.PREPROCESSING
    assert recovered.error_code == "worker_lease_expired"
    assert recovered.retry_scheduled is True
    assert recovered.next_attempt_at is not None
    assert recovered.next_attempt_at.replace(tzinfo=UTC) == BASE_TIME + timedelta(seconds=61)


def test_event_failure_rolls_back_job_claim_state(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording_id, job_id = _create_job(session_factory)

    def reject_event(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("activity storage unavailable")

    monkeypatch.setattr(queue_module, "append_activity", reject_event)

    with pytest.raises(RuntimeError, match="activity storage unavailable"):
        _queue(session_factory).claim_next(now=BASE_TIME + timedelta(seconds=1))

    with session_factory() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, recording_id)
        events = list(session.scalars(select(ProcessingActivity)))
    assert job is not None and job.status is JobStatus.QUEUED
    assert job.attempt_count == 0
    assert job.claim_token is None
    assert recording is not None
    assert recording.processing_status is RecordingStatus.QUEUED
    assert [event.event_type for event in events] == [ProcessingActivityType.JOB_QUEUED]


def test_manual_retry_has_a_distinct_event_and_preserves_job_history(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, old_job_id = _create_job(session_factory, max_attempts=1)
    queue = _queue(session_factory)
    claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=1))
    assert claim is not None
    queue.fail(
        claim,
        JobFailure(
            code="invalid_audio",
            error_type="PermanentProcessingError",
            message="The audio is invalid.",
            retryable=False,
            stage=JobStage.PREPROCESSING,
        ),
        now=BASE_TIME + timedelta(seconds=2),
    )

    with session_factory.begin() as session:
        new_job = retry_failed_recording(
            session,
            recording_id=recording_id,
            available_at=BASE_TIME + timedelta(seconds=3),
        )
        new_job_id = new_job.id

    with session_factory() as session:
        events = list_recording_activity(
            session,
            recording_id=recording_id,
            limit=100,
            offset=0,
        )
    assert events is not None
    manual = events[-1]
    assert manual.event_type is ProcessingActivityType.MANUAL_RETRY_QUEUED
    assert manual.job_id == new_job_id
    assert manual.attempt_count == 0
    assert any(event.job_id == old_job_id for event in events[:-1])


def test_recording_service_retry_and_event_commit_atomically(
    app_client: TestClient,
    wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    upload = app_client.post("/api/v1/recordings", files=files, headers=headers)
    assert upload.status_code == 201
    recording_id = uuid.UUID(metadata["id"])
    session_factory = app_client.app.state.test_session_factory

    with session_factory.begin() as session:
        recording = session.get(Recording, recording_id)
        old_job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and old_job is not None
        recording.processing_status = RecordingStatus.FAILED
        old_job.status = JobStatus.FAILED
        old_job.stage = JobStage.PREPROCESSING
        old_job.finished_at = BASE_TIME
        old_job_id = old_job.id

    original_append = recording_service_module.append_activity

    def reject_event(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("activity storage unavailable")

    monkeypatch.setattr(recording_service_module, "append_activity", reject_event)
    with pytest.raises(RuntimeError, match="activity storage unavailable"):
        app_client.app.state.recording_service.retry(recording_id)

    with session_factory() as session:
        recording = session.get(Recording, recording_id)
        jobs = list(
            session.scalars(select(ProcessingJob).where(ProcessingJob.recording_id == recording_id))
        )
    assert recording is not None
    assert recording.processing_status is RecordingStatus.FAILED
    assert [job.id for job in jobs] == [old_job_id]

    monkeypatch.setattr(recording_service_module, "append_activity", original_append)
    new_job = app_client.app.state.recording_service.retry(recording_id)
    with session_factory() as session:
        events = list_recording_activity(
            session,
            recording_id=recording_id,
            limit=100,
            offset=0,
        )
    assert events is not None
    assert events[-1].event_type is ProcessingActivityType.MANUAL_RETRY_QUEUED
    assert events[-1].job_id == new_job.id


def test_migration_backfills_only_known_lifecycle_timestamps(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing_recording_id = uuid.uuid4()
    retry_recording_id = uuid.uuid4()
    processing_job_id = uuid.uuid4()
    retry_job_id = uuid.uuid4()
    with session_factory.begin() as session:
        processing_recording = _add_recording(session, processing_recording_id)
        processing_recording.processing_status = RecordingStatus.PROCESSING
        retry_recording = _add_recording(session, retry_recording_id)
        retry_recording.processing_status = RecordingStatus.QUEUED
        session.add_all(
            [
                ProcessingJob(
                    id=processing_job_id,
                    recording_id=processing_recording_id,
                    status=JobStatus.PROCESSING,
                    stage=JobStage.TRANSCRIBING,
                    attempt_count=2,
                    max_attempts=3,
                    available_at=BASE_TIME,
                    started_at=BASE_TIME + timedelta(seconds=1),
                    created_at=BASE_TIME,
                    updated_at=BASE_TIME + timedelta(seconds=4),
                ),
                ProcessingJob(
                    id=retry_job_id,
                    recording_id=retry_recording_id,
                    status=JobStatus.QUEUED,
                    stage=JobStage.QUEUED,
                    attempt_count=1,
                    max_attempts=3,
                    available_at=BASE_TIME + timedelta(seconds=30),
                    failed_stage=JobStage.DIARIZING,
                    error_code="temporary_failure",
                    error_type="ProviderUnavailable",
                    error_message="Processing is temporarily unavailable.",
                    error_at=BASE_TIME + timedelta(seconds=2),
                    started_at=BASE_TIME + timedelta(seconds=1),
                    created_at=BASE_TIME,
                    updated_at=BASE_TIME + timedelta(seconds=2),
                ),
            ]
        )

    migration = importlib.import_module("migrations.versions.0003_processing_activity")
    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration._backfill_existing_jobs()

    with session_factory() as session:
        processing_events = list(
            session.scalars(
                select(ProcessingActivity)
                .where(ProcessingActivity.job_id == processing_job_id)
                .order_by(ProcessingActivity.occurred_at, ProcessingActivity.id)
            )
        )
        retry_events = list(
            session.scalars(
                select(ProcessingActivity)
                .where(ProcessingActivity.job_id == retry_job_id)
                .order_by(ProcessingActivity.occurred_at, ProcessingActivity.id)
            )
        )

    assert [event.event_type for event in processing_events] == [
        ProcessingActivityType.JOB_QUEUED,
        ProcessingActivityType.PROCESSING_STARTED,
    ]
    assert processing_events[-1].stage is None
    assert processing_events[-1].attempt_count == 1
    assert all(
        event.event_type is not ProcessingActivityType.STAGE_STARTED for event in processing_events
    )
    assert [event.event_type for event in retry_events] == [
        ProcessingActivityType.JOB_QUEUED,
        ProcessingActivityType.PROCESSING_STARTED,
        ProcessingActivityType.RETRY_SCHEDULED,
    ]
    assert retry_events[-1].stage is JobStage.DIARIZING
    assert retry_events[-1].retry_scheduled is True
    assert retry_events[-1].next_attempt_at is not None


def test_activity_migration_rejects_offline_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("migrations.versions.0003_processing_activity")
    monkeypatch.setattr(alembic_context, "is_offline_mode", lambda: True)

    with pytest.raises(RuntimeError, match="requires an online database connection"):
        migration.upgrade()


def test_unexpected_worker_exception_never_enters_activity_message(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, _job_id = _create_job(session_factory, max_attempts=1)
    sensitive = "raw-provider-output-and-private-path"

    def processor_factory():  # type: ignore[no-untyped-def]
        def processor(job, progress):  # type: ignore[no-untyped-def]
            del job, progress
            raise RuntimeError(sensitive)

        return processor

    worker = Worker(
        _queue(session_factory),
        processor_factory,
        intervals=WorkerIntervals(
            poll_seconds=1,
            heartbeat_seconds=10,
            recovery_seconds=10,
        ),
    )

    assert worker.run_once() is True

    with session_factory() as session:
        events = list_recording_activity(
            session,
            recording_id=recording_id,
            limit=100,
            offset=0,
        )
    assert events is not None
    failed = events[-1]
    assert failed.event_type is ProcessingActivityType.PROCESSING_FAILED
    assert failed.safe_message == "Processing failed unexpectedly."
    assert sensitive not in failed.safe_message


def test_activity_api_is_authenticated_paginated_and_privacy_scoped(
    app_client: TestClient,
    wav_bytes: bytes,
) -> None:
    app_client.app.include_router(activity_router)
    files, headers, metadata = make_upload(wav_bytes)
    upload = app_client.post("/api/v1/recordings", files=files, headers=headers)
    assert upload.status_code == 201
    path = f"/api/v1/recordings/{metadata['id']}/activity"

    assert app_client.get(path).status_code == 401
    response = app_client.get(
        path,
        params={"limit": 1, "offset": 0},
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"items", "limit", "offset"}
    assert response.json()["limit"] == 1
    item = response.json()["items"][0]
    assert item["event_type"] == "job_queued"
    assert "message" in item
    assert "safe_message" not in item
    assert "recording_id" not in item
    assert "worker_id" not in item
    assert "claim_token" not in item

    missing = app_client.get(
        f"/api/v1/recordings/{uuid.uuid4()}/activity",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )
    assert missing.status_code == 404
