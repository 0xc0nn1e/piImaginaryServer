"""A worker may be restricted to the job kinds it actually has providers for.

Analysis work is a network call to LM Studio, while a full job is hours of
CPU-bound transcription. Sharing one queue makes the cheap job wait for the
expensive one, so a worker can claim one kind only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from audio_server.db.models import JobKind, JobStatus, ProcessingJob, Recording, RecordingStatus
from audio_server.jobs.queue import JobQueue, RetryPolicy
from audio_server.processing.errors import PermanentProcessingError
from audio_server.worker_runtime import PipelineJobProcessor, _parse_job_kinds
from tests.conftest import TEST_API_TOKEN, TEST_WEB_SETUP_TOKEN


def _queue(session_factory: sessionmaker[Session]) -> JobQueue:
    return JobQueue(
        session_factory,
        worker_id="test-worker",
        lease_duration=timedelta(seconds=300),
        retry_policy=RetryPolicy(
            base_delay=timedelta(seconds=30), max_delay=timedelta(seconds=900)
        ),
    )


def _seed_job(
    session_factory: sessionmaker[Session], *, kind: JobKind, available_at: datetime
) -> uuid.UUID:
    recording_id = uuid.uuid4()
    job_id = uuid.uuid4()
    started = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        session.add(
            Recording(
                id=recording_id,
                device_id="test-device",
                original_filename="meeting.wav",
                storage_key=f"recordings/{recording_id}/original.wav",
                mime_type="audio/wav",
                audio_format="wav",
                file_size=1024,
                sha256=recording_id.hex * 2,
                started_at=started,
                ended_at=started + timedelta(seconds=1),
                duration_seconds=1.0,
                sample_rate=16_000,
                channels=1,
                processing_status=RecordingStatus.QUEUED,
            )
        )
        session.add(
            ProcessingJob(
                id=job_id,
                recording_id=recording_id,
                kind=kind,
                status=JobStatus.QUEUED,
                available_at=available_at,
            )
        )
    return job_id


def test_claim_skips_kinds_the_worker_cannot_process(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    # The full job is older, so an unfiltered claim would take it first.
    _seed_job(session_factory, kind=JobKind.FULL, available_at=now - timedelta(hours=2))
    analysis_id = _seed_job(
        session_factory, kind=JobKind.ANALYSIS, available_at=now - timedelta(minutes=5)
    )

    claim = _queue(session_factory).claim_next(now=now, kinds={JobKind.ANALYSIS})

    assert claim is not None
    assert claim.id == analysis_id
    assert claim.kind is JobKind.ANALYSIS


def test_unfiltered_claim_still_takes_the_oldest_job(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    full_id = _seed_job(session_factory, kind=JobKind.FULL, available_at=now - timedelta(hours=2))
    _seed_job(session_factory, kind=JobKind.ANALYSIS, available_at=now - timedelta(minutes=5))

    claim = _queue(session_factory).claim_next(now=now)

    assert claim is not None
    assert claim.id == full_id


def test_analysis_only_claim_finds_nothing_when_only_full_work_is_queued(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    _seed_job(session_factory, kind=JobKind.FULL, available_at=now - timedelta(hours=2))

    assert _queue(session_factory).claim_next(now=now, kinds={JobKind.ANALYSIS}) is None


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("", None),
        ("analysis", frozenset({JobKind.ANALYSIS})),
        ("full", frozenset({JobKind.FULL})),
        ("full,analysis", frozenset({JobKind.FULL, JobKind.ANALYSIS})),
    ],
)
def test_configured_kinds_are_parsed(configured: str, expected: object) -> None:
    assert _parse_job_kinds(configured) == expected


def test_a_transcription_job_without_a_pipeline_fails_loudly(
    session_factory: sessionmaker[Session],
) -> None:
    processor = PipelineJobProcessor(session_factory=session_factory, storage=object())  # type: ignore[arg-type]
    _seed_job(
        session_factory,
        kind=JobKind.FULL,
        available_at=datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
    )
    claim = _queue(session_factory).claim_next(now=datetime(2026, 8, 25, 12, 0, tzinfo=UTC))
    assert claim is not None

    with pytest.raises(PermanentProcessingError) as caught:
        processor(claim, lambda _stage: None)

    assert caught.value.code == "transcription_worker_unavailable"


def test_queue_view_lists_processing_first_then_claim_order(
    app_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    _seed_job(session_factory, kind=JobKind.ANALYSIS, available_at=now - timedelta(minutes=5))
    older = _seed_job(session_factory, kind=JobKind.FULL, available_at=now - timedelta(hours=2))
    running = _seed_job(session_factory, kind=JobKind.FULL, available_at=now - timedelta(minutes=1))
    with session_factory.begin() as session:
        session.get(ProcessingJob, running).status = JobStatus.PROCESSING

    response = app_client.get(
        "/api/v1/queue", headers={"Authorization": f"Bearer {TEST_API_TOKEN}"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["processing"] == 1
    assert body["queued"] == 2
    order = [uuid.UUID(item["job"]["id"]) for item in body["items"]]
    # What is running now leads; the rest follow in the order a worker claims.
    assert order[0] == running
    assert order[1] == older


def test_queue_view_requires_a_credential(app_client: TestClient) -> None:
    assert app_client.get("/api/v1/queue").status_code == 401


def test_queue_view_keeps_analysis_visible_behind_a_transcription_backlog(
    app_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    for minute in range(4):
        _seed_job(
            session_factory,
            kind=JobKind.FULL,
            available_at=now - timedelta(hours=3, minutes=minute),
        )
    analysis = _seed_job(session_factory, kind=JobKind.ANALYSIS, available_at=now)

    response = app_client.get(
        "/api/v1/queue?limit=2", headers={"Authorization": f"Bearer {TEST_API_TOKEN}"}
    )

    assert response.status_code == 200
    ids = [uuid.UUID(item["job"]["id"]) for item in response.json()["items"]]
    # The cap applies per kind, so a transcription backlog cannot hide analysis.
    assert analysis in ids


def test_queue_counts_report_the_whole_backlog_not_the_page(
    app_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    for minute in range(5):
        _seed_job(session_factory, kind=JobKind.FULL, available_at=now - timedelta(minutes=minute))

    response = app_client.get(
        "/api/v1/queue?limit=2", headers={"Authorization": f"Bearer {TEST_API_TOKEN}"}
    )

    body = response.json()
    # The listing is capped; the totals must still describe the real backlog.
    assert len(body["items"]) == 2
    assert body["queued"] == 5
    assert body["processing"] == 0


def test_a_browser_session_can_read_the_queue_without_a_bearer_token(
    app_client: TestClient,
) -> None:
    setup = app_client.post(
        "/api/v1/auth/setup",
        headers={"Origin": "http://testserver", "X-Setup-Token": TEST_WEB_SETUP_TOKEN},
        json={"username": "admin", "password": "a synthetic admin password"},
    )
    assert setup.status_code == 201
    login = app_client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://testserver"},
        json={"username": "admin", "password": "a synthetic admin password"},
    )
    assert login.status_code == 200

    response = app_client.get("/api/v1/queue")

    # A 401 here logs the administrator out of the whole application.
    assert response.status_code == 200
