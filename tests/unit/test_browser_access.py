from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from audio_server.db.models import JobStatus, ProcessingJob, Recording, RecordingStatus
from tests.conftest import TEST_WEB_SETUP_TOKEN, make_upload

ORIGIN = "http://testserver"
PASSWORD = "a synthetic admin password"


def _setup_and_login(client: TestClient) -> None:
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


def _mark_terminal(client: TestClient, recording_id: uuid.UUID) -> None:
    with client.app.state.test_session_factory.begin() as session:
        recording = session.get(Recording, recording_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and job is not None
        recording.processing_status = RecordingStatus.COMPLETED
        job.status = JobStatus.COMPLETED


def test_browser_session_can_read_recordings_but_cannot_upload(
    app_client: TestClient,
    wav_bytes: bytes,
) -> None:
    _setup_and_login(app_client)
    files, headers, metadata = make_upload(wav_bytes)

    uploaded = app_client.post("/api/v1/recordings", files=files, headers=headers)

    assert uploaded.status_code == 201
    listing = app_client.get("/api/v1/recordings")
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "no-store"
    assert listing.json()["items"][0]["id"] == metadata["id"]
    assert app_client.get(f"/api/v1/recordings/{metadata['id']}/status").status_code == 200
    assert app_client.get(f"/api/v1/recordings/{metadata['id']}/activity").status_code == 200

    next_files, next_headers, _ = make_upload(wav_bytes + b"different")
    next_headers.pop("Authorization")
    rejected = app_client.post(
        "/api/v1/recordings",
        files=next_files,
        headers=next_headers,
    )
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "authentication_required"


def test_browser_session_cannot_retry_and_invalid_bearer_does_not_fall_back(
    app_client: TestClient,
    wav_bytes: bytes,
) -> None:
    _setup_and_login(app_client)
    files, headers, metadata = make_upload(wav_bytes)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201

    retry = app_client.post(f"/api/v1/recordings/{metadata['id']}/retry")
    assert retry.status_code == 401

    wrong_bearer = app_client.get(
        "/api/v1/recordings",
        headers={"Authorization": "Bearer deliberately-wrong"},
    )
    assert wrong_bearer.status_code == 401

    unsupported_scheme = app_client.get(
        "/api/v1/recordings",
        headers={"Authorization": "Basic deliberately-wrong"},
    )
    assert unsupported_scheme.status_code == 401


def test_logout_revokes_browser_access(app_client: TestClient) -> None:
    _setup_and_login(app_client)
    csrf_token = app_client.cookies.get("audio_server_csrf")
    assert csrf_token

    logout = app_client.post(
        "/api/v1/auth/logout",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token},
    )

    assert logout.status_code == 200
    assert app_client.get("/api/v1/recordings").status_code == 401


def test_browser_session_can_reprocess_and_delete_with_origin_and_csrf(
    app_client: TestClient,
    wav_bytes: bytes,
) -> None:
    _setup_and_login(app_client)
    csrf_token = app_client.cookies.get("audio_server_csrf")
    assert csrf_token
    mutation_headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf_token}

    first_files, first_headers, first_metadata = make_upload(wav_bytes)
    assert (
        app_client.post("/api/v1/recordings", files=first_files, headers=first_headers).status_code
        == 201
    )
    first_id = uuid.UUID(first_metadata["id"])
    _mark_terminal(app_client, first_id)
    reprocess = app_client.post(
        f"/api/v1/recordings/{first_id}/reprocess", headers=mutation_headers
    )
    assert reprocess.status_code == 202

    second_files, second_headers, second_metadata = make_upload(wav_bytes + b"another-recording")
    assert (
        app_client.post(
            "/api/v1/recordings", files=second_files, headers=second_headers
        ).status_code
        == 201
    )
    second_id = uuid.UUID(second_metadata["id"])
    _mark_terminal(app_client, second_id)
    deleted = app_client.delete(f"/api/v1/recordings/{second_id}", headers=mutation_headers)
    assert deleted.status_code == 204
    assert app_client.get(f"/api/v1/recordings/{second_id}").status_code == 404


def test_browser_mutations_require_exact_origin_and_csrf(
    app_client: TestClient,
    wav_bytes: bytes,
) -> None:
    _setup_and_login(app_client)
    files, headers, metadata = make_upload(wav_bytes)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201
    recording_id = uuid.UUID(metadata["id"])
    _mark_terminal(app_client, recording_id)
    csrf_token = app_client.cookies.get("audio_server_csrf")
    assert csrf_token

    no_origin = app_client.post(
        f"/api/v1/recordings/{recording_id}/reprocess",
        headers={"X-CSRF-Token": csrf_token},
    )
    wrong_csrf = app_client.delete(
        f"/api/v1/recordings/{recording_id}",
        headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong-token"},
    )

    assert no_origin.status_code == 403
    assert no_origin.json()["error"]["code"] == "origin_not_allowed"
    assert wrong_csrf.status_code == 403
    assert wrong_csrf.json()["error"]["code"] == "csrf_validation_failed"
