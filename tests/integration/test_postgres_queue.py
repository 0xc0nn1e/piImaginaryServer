from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from audio_server.db.models import (
    Base,
    JobStage,
    JobStatus,
    ProcessingJob,
    Recording,
    RecordingStatus,
)
from audio_server.jobs.queue import (
    ClaimLostError,
    InvalidRetryStateError,
    JobFailure,
    JobQueue,
    RetryPolicy,
    create_processing_job,
    retry_failed_recording,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def postgres_factory() -> Iterator[sessionmaker[Session]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL queue integration tests")
    if not database_url.startswith("postgresql+psycopg://"):
        pytest.skip("queue integration tests require postgresql+psycopg")

    schema = f"queue_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema))

    engine: Engine | None = None
    try:
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args={
                "options": f"-csearch_path={schema} -cstatement_timeout=3000",
            },
        )
        Base.metadata.create_all(engine)
        yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    finally:
        if engine is not None:
            engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


@pytest.fixture
def empty_database(postgres_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    with postgres_factory() as session, session.begin():
        for model in (ProcessingJob, Recording):
            session.query(model).delete()
    return postgres_factory


def _recording(recording_id: uuid.UUID | None = None) -> Recording:
    now = datetime.now(UTC)
    identifier = recording_id or uuid.uuid4()
    return Recording(
        id=identifier,
        device_id=f"device-{identifier}",
        original_filename="sample.flac",
        storage_key=f"recordings/{identifier}/original.flac",
        mime_type="audio/flac",
        audio_format="flac",
        file_size=1024,
        sha256=identifier.hex * 2,
        started_at=now,
        ended_at=now + timedelta(seconds=10),
        duration_seconds=10,
        client_metadata={},
    )


def _add_recording_with_job(
    factory: sessionmaker[Session],
    *,
    max_attempts: int = 3,
    available_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    recording = _recording()
    with factory() as session, session.begin():
        session.add(recording)
        job = create_processing_job(
            session,
            recording_id=recording.id,
            max_attempts=max_attempts,
            available_at=available_at,
        )
        job_id = job.id
    return recording.id, job_id


def _queue(
    factory: sessionmaker[Session],
    worker_id: str,
    *,
    retry_seconds: int = 1,
) -> JobQueue:
    return JobQueue(
        factory,
        worker_id=worker_id,
        lease_duration=timedelta(seconds=10),
        retry_policy=RetryPolicy(
            base_delay=timedelta(seconds=retry_seconds),
            max_delay=timedelta(seconds=retry_seconds),
        ),
    )


def test_skip_locked_allows_another_worker_to_claim_next_job(
    empty_database: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    _, first_job_id = _add_recording_with_job(empty_database, available_at=now)
    _, second_job_id = _add_recording_with_job(
        empty_database,
        available_at=now + timedelta(microseconds=1),
    )

    with empty_database() as locking_session, locking_session.begin():
        locked = locking_session.scalar(
            select(ProcessingJob).where(ProcessingJob.id == first_job_id).with_for_update()
        )
        assert locked is not None

        claim = _queue(empty_database, "worker-two").claim_next(now=now + timedelta(seconds=1))
        assert claim is not None
        assert claim.id == second_job_id


def test_claim_token_fences_worker_after_expired_lease_recovery(
    empty_database: sessionmaker[Session],
) -> None:
    start = datetime.now(UTC)
    recording_id, job_id = _add_recording_with_job(empty_database, available_at=start)
    first_queue = _queue(empty_database, "worker-one", retry_seconds=1)
    stale_claim = first_queue.claim_next(now=start)
    assert stale_claim is not None
    assert stale_claim.id == job_id

    recovery_time = start + timedelta(seconds=11)
    summary = _queue(empty_database, "recovery-worker", retry_seconds=1).recover_expired(
        now=recovery_time
    )
    assert summary.requeued == 1

    replacement = _queue(empty_database, "worker-two", retry_seconds=1).claim_next(
        now=recovery_time + timedelta(seconds=1)
    )
    assert replacement is not None
    assert replacement.id == job_id
    assert replacement.claim_token != stale_claim.claim_token

    with pytest.raises(ClaimLostError):
        first_queue.heartbeat(stale_claim, now=recovery_time + timedelta(seconds=2))
    with pytest.raises(ClaimLostError):
        first_queue.complete(stale_claim, now=recovery_time + timedelta(seconds=2))

    with empty_database() as session:
        recording = session.get(Recording, recording_id)
        assert recording is not None
        assert recording.processing_status is RecordingStatus.PROCESSING


def test_expired_lease_fails_after_attempt_budget_is_exhausted(
    empty_database: sessionmaker[Session],
) -> None:
    start = datetime.now(UTC)
    recording_id, job_id = _add_recording_with_job(
        empty_database,
        max_attempts=1,
        available_at=start,
    )
    queue = _queue(empty_database, "worker-one")
    claim = queue.claim_next(now=start)
    assert claim is not None

    summary = queue.recover_expired(now=start + timedelta(seconds=11))
    assert summary.failed == 1
    assert summary.requeued == 0

    with empty_database() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, recording_id)
        assert job is not None and recording is not None
        assert job.status is JobStatus.FAILED
        assert job.failed_stage is JobStage.PREPROCESSING
        assert job.error_code == "worker_lease_expired"
        assert job.claim_token is None
        assert recording.processing_status is RecordingStatus.FAILED


def test_retryable_and_permanent_failures_update_durable_state(
    empty_database: sessionmaker[Session],
) -> None:
    start = datetime.now(UTC)
    recording_id, job_id = _add_recording_with_job(empty_database, available_at=start)
    queue = _queue(empty_database, "worker-one", retry_seconds=2)
    claim = queue.claim_next(now=start)
    assert claim is not None

    retry_scheduled = queue.fail(
        claim,
        JobFailure(
            code="gpu_unavailable",
            error_type="RetryableProcessingError",
            message="The configured compute device is temporarily unavailable.",
            retryable=True,
            stage=JobStage.TRANSCRIBING,
        ),
        now=start + timedelta(seconds=1),
    )
    assert retry_scheduled is True

    with empty_database() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, recording_id)
        assert job is not None and recording is not None
        assert job.status is JobStatus.QUEUED
        assert job.stage is JobStage.QUEUED
        assert job.failed_stage is JobStage.TRANSCRIBING
        assert job.available_at == start + timedelta(seconds=3)
        assert recording.processing_status is RecordingStatus.QUEUED

    second_claim = queue.claim_next(now=start + timedelta(seconds=3))
    assert second_claim is not None
    retry_scheduled = queue.fail(
        second_claim,
        JobFailure(
            code="audio_conversion_failed",
            error_type="PermanentProcessingError",
            message="The uploaded audio cannot be decoded.",
            retryable=False,
            stage=JobStage.PREPROCESSING,
        ),
        now=start + timedelta(seconds=4),
    )
    assert retry_scheduled is False

    with empty_database() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, recording_id)
        assert job is not None and recording is not None
        assert job.status is JobStatus.FAILED
        assert job.error_code == "audio_conversion_failed"
        assert recording.processing_status is RecordingStatus.FAILED


def test_completion_result_writer_rolls_back_with_job_state(
    empty_database: sessionmaker[Session],
) -> None:
    start = datetime.now(UTC)
    recording_id, job_id = _add_recording_with_job(empty_database, available_at=start)
    queue = _queue(empty_database, "worker-one")
    claim = queue.claim_next(now=start)
    assert claim is not None

    def broken_result_writer(session: Session) -> None:
        recording = session.get(Recording, recording_id)
        assert recording is not None
        recording.original_filename = "must-roll-back.flac"
        raise RuntimeError("simulated persistence failure")

    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        queue.complete(claim, persist_results=broken_result_writer)

    with empty_database() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, recording_id)
        assert job is not None and recording is not None
        assert job.status is JobStatus.PROCESSING
        assert job.claim_token == claim.claim_token
        assert recording.original_filename == "sample.flac"
        assert recording.processing_status is RecordingStatus.PROCESSING


def test_manual_retry_creates_new_job_only_for_failed_recording(
    empty_database: sessionmaker[Session],
) -> None:
    start = datetime.now(UTC)
    recording_id, first_job_id = _add_recording_with_job(empty_database, available_at=start)
    queue = _queue(empty_database, "worker-one")
    claim = queue.claim_next(now=start)
    assert claim is not None
    queue.fail(
        claim,
        JobFailure(
            code="invalid_audio",
            error_type="PermanentProcessingError",
            message="The uploaded file is not supported audio.",
            retryable=False,
        ),
        now=start + timedelta(seconds=1),
    )

    with empty_database() as session, session.begin():
        new_job = retry_failed_recording(
            session,
            recording_id=recording_id,
            max_attempts=2,
            available_at=start + timedelta(seconds=2),
        )
        new_job_id = new_job.id
    assert new_job_id != first_job_id

    with empty_database() as session, session.begin(), pytest.raises(InvalidRetryStateError):
        retry_failed_recording(session, recording_id=recording_id)
