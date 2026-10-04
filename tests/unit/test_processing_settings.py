"""The administrator's switches for the LLM work queued behind a transcript.

Transcription always runs; analysis and the Cantonese translation can each be
switched off for the whole system, and the queue reads the switches at the
moment a finished transcript is handed on.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from audio_server.db.models import (
    JobKind,
    JobStatus,
    ProcessingJob,
    ProcessingSettings,
    Recording,
    RecordingStatus,
)
from audio_server.jobs.queue import AutomaticFollowUps, JobQueue, read_automatic_follow_ups
from tests.conftest import TEST_API_TOKEN, TEST_WEB_SETUP_TOKEN, make_upload

ORIGIN = "http://testserver"
PASSWORD = "a synthetic admin password"
BEARER = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
URL = "/api/v1/settings/processing"
BOTH_ON = {"auto_analysis": True, "auto_translation": True}


def _login(client: TestClient) -> str:
    setup = client.post(
        "/api/v1/auth/setup",
        headers={"Origin": ORIGIN, "X-Setup-Token": TEST_WEB_SETUP_TOKEN},
        json={"username": "admin", "password": PASSWORD},
    )
    assert setup.status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": "admin", "password": PASSWORD},
    )
    assert login.status_code == 200
    token = client.cookies.get("audio_server_csrf")
    assert token
    return token


def _completed_recording(session_factory: sessionmaker[Session]) -> uuid.UUID:
    recording_id = uuid.uuid4()
    started = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        session.add(
            Recording(
                id=recording_id,
                device_id="pi-recorder-01",
                original_filename="meeting.wav",
                storage_key=f"recordings/{recording_id}/original.wav",
                mime_type="audio/wav",
                audio_format="wav",
                file_size=1024,
                sha256=recording_id.hex * 2,
                started_at=started,
                ended_at=started + timedelta(seconds=1),
                duration_seconds=1.0,
                processing_status=RecordingStatus.COMPLETED,
            )
        )
    return recording_id


def test_both_steps_start_switched_on(app_client: TestClient) -> None:
    response = app_client.get(URL, headers=BEARER)

    assert response.status_code == 200
    assert response.json() == BOTH_ON
    assert response.headers["cache-control"] == "no-store"


def test_a_browser_session_flips_one_switch_without_touching_the_other(
    app_client: TestClient,
) -> None:
    csrf = _login(app_client)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}

    analysis_off = app_client.patch(URL, headers=headers, json={"auto_analysis": False})
    assert analysis_off.status_code == 200
    assert analysis_off.json() == {"auto_analysis": False, "auto_translation": True}

    # A second tab that still shows analysis on flips only the other switch,
    # so it cannot turn analysis back on behind the first tab's back.
    translation_off = app_client.patch(URL, headers=headers, json={"auto_translation": False})
    assert translation_off.json() == {"auto_analysis": False, "auto_translation": False}
    assert app_client.get(URL).json() == {"auto_analysis": False, "auto_translation": False}

    both_on = app_client.patch(URL, headers=headers, json=BOTH_ON)
    assert both_on.status_code == 200
    assert both_on.json() == BOTH_ON


def test_a_machine_client_may_change_the_switches_with_its_bearer_token(
    app_client: TestClient,
) -> None:
    response = app_client.patch(URL, headers=BEARER, json={"auto_translation": False})

    assert response.status_code == 200
    assert app_client.get(URL, headers=BEARER).json() == {
        "auto_analysis": True,
        "auto_translation": False,
    }


def test_changing_a_switch_requires_the_exact_origin_and_csrf_token(
    app_client: TestClient,
) -> None:
    csrf = _login(app_client)
    body = {"auto_analysis": False}

    no_origin = app_client.patch(URL, headers={"X-CSRF-Token": csrf}, json=body)
    other_origin = app_client.patch(
        URL, headers={"Origin": "http://attacker.example", "X-CSRF-Token": csrf}, json=body
    )
    wrong_csrf = app_client.patch(
        URL, headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"}, json=body
    )

    assert no_origin.status_code == 403
    assert no_origin.json()["error"]["code"] == "origin_not_allowed"
    assert other_origin.status_code == 403
    assert other_origin.json()["error"]["code"] == "origin_not_allowed"
    assert wrong_csrf.status_code == 403
    assert wrong_csrf.json()["error"]["code"] == "csrf_validation_failed"
    assert app_client.get(URL).json() == BOTH_ON


def test_the_switches_require_authentication(app_client: TestClient) -> None:
    read = app_client.get(URL)
    write = app_client.patch(URL, headers={"Origin": ORIGIN}, json={"auto_analysis": False})

    assert read.status_code == 401
    assert write.status_code == 401
    assert app_client.get(URL, headers=BEARER).json() == BOTH_ON


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"auto_analysis": None},
        {"auto_analysis": "false"},
        {"auto_analysis": 0},
        # Transcription has no switch: every recording is transcribed.
        {"auto_transcription": False},
        {"auto_analysis": False, "auto_transcription": False},
    ],
)
def test_a_change_must_set_a_known_switch_to_true_or_false(
    app_client: TestClient, body: dict[str, Any]
) -> None:
    response = app_client.patch(URL, headers=BEARER, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert app_client.get(URL, headers=BEARER).json() == BOTH_ON


def test_a_switched_off_step_can_still_be_queued_by_hand(
    app_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    recording_id = _completed_recording(session_factory)
    app_client.patch(URL, headers=BEARER, json={"auto_analysis": False})

    queued = app_client.post(
        f"/api/v1/recordings/{recording_id}/analysis/reprocess", headers=BEARER
    )

    assert queued.status_code == 202
    with session_factory() as session:
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
    assert job is not None and job.kind is JobKind.ANALYSIS


def test_the_switch_the_api_writes_is_the_one_the_queue_reads(
    app_client: TestClient, session_factory: sessionmaker[Session], wav_bytes: bytes
) -> None:
    assert app_client.patch(URL, headers=BEARER, json={"auto_analysis": False}).status_code == 200
    files, headers, metadata = make_upload(wav_bytes)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201
    queue = JobQueue(session_factory, worker_id="settings-test-worker")

    claim = queue.claim_next()
    assert claim is not None and claim.kind is JobKind.FULL
    queue.complete(claim)

    with session_factory() as session:
        jobs = list(
            session.scalars(
                select(ProcessingJob)
                .where(ProcessingJob.recording_id == uuid.UUID(metadata["id"]))
                .order_by(ProcessingJob.available_at, ProcessingJob.created_at)
            )
        )
    assert [job.kind for job in jobs] == [JobKind.FULL, JobKind.TRANSLATION]
    assert jobs[1].status is JobStatus.QUEUED


def test_the_migration_seeds_one_row_with_both_steps_on() -> None:
    migration = importlib.import_module("migrations.versions.0012_processing_settings")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        with engine.begin() as connection, Operations.context(
            MigrationContext.configure(connection)
        ):
            migration.upgrade()

        with factory() as session:
            assert read_automatic_follow_ups(session) == AutomaticFollowUps(
                analysis=True, translation=True
            )
        # The table holds one answer only.
        with pytest.raises(IntegrityError), factory.begin() as session:
            session.add(ProcessingSettings(id=2, auto_analysis=False, auto_translation=False))

        with engine.begin() as connection, Operations.context(
            MigrationContext.configure(connection)
        ):
            migration.downgrade()
        assert "processing_settings" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
