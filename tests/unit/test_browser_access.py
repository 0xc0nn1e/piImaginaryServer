from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from audio_server.db.models import JobStatus, ProcessingJob, Recording, RecordingStatus
from audio_server.processing.contracts import AudioProbe
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
    audio = app_client.get(
        f"/api/v1/recordings/{metadata['id']}/audio", headers={"Range": "bytes=0-15"}
    )
    assert audio.status_code == 206
    assert audio.content == wav_bytes[:16]

    next_files, next_headers, _ = make_upload(wav_bytes + b"different")
    next_headers.pop("Authorization")
    rejected = app_client.post(
        "/api/v1/recordings",
        files=next_files,
        headers=next_headers,
    )
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "authentication_required"


def test_browser_session_uploads_wav_idempotently_with_started_at(
    app_client: TestClient,
    wav_bytes: bytes,
) -> None:
    _setup_and_login(app_client)
    csrf_token = app_client.cookies.get("audio_server_csrf")
    assert csrf_token
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf_token}
    files = {"audio": ("meeting.wav", wav_bytes, "audio/wav")}

    response = app_client.post(
        "/api/v1/web/recordings",
        headers=headers,
        files=files,
        data={"started_at": "2026-08-13T09:30:00+09:00"},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "queued"
    recording_id = uuid.UUID(response.json()["recording_id"])
    with app_client.app.state.test_session_factory() as session:
        recording = session.get(Recording, recording_id)
        assert recording is not None
        assert recording.device_id == "web-upload"
        assert recording.original_filename == "meeting.wav"
        assert recording.duration_seconds == 1
        assert (recording.ended_at - recording.started_at).total_seconds() == 1

    duplicate = app_client.post(
        "/api/v1/web/recordings",
        headers=headers,
        files={"audio": ("renamed.wav", wav_bytes, "audio/wav")},
    )
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "recording_id": str(recording_id),
        "status": "queued",
        "duplicate": True,
    }


def test_browser_upload_requires_origin_and_csrf_before_acceptance(
    app_client: TestClient,
    wav_bytes: bytes,
) -> None:
    _setup_and_login(app_client)
    csrf_token = app_client.cookies.get("audio_server_csrf")
    assert csrf_token

    no_origin = app_client.post(
        "/api/v1/web/recordings",
        headers={"X-CSRF-Token": csrf_token},
        files={"audio": ("meeting.wav", wav_bytes, "audio/wav")},
    )
    wrong_csrf = app_client.post(
        "/api/v1/web/recordings",
        headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
        files={"audio": ("meeting.wav", wav_bytes, "audio/wav")},
    )

    assert no_origin.status_code == 403
    assert no_origin.json()["error"]["code"] == "origin_not_allowed"
    assert wrong_csrf.status_code == 403
    assert wrong_csrf.json()["error"]["code"] == "csrf_validation_failed"
    with app_client.app.state.test_session_factory() as session:
        assert session.scalar(select(Recording)) is None


def test_browser_upload_rejects_bad_time_unsupported_media_and_declared_size(
    app_client: TestClient,
    wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_and_login(app_client)
    csrf_token = app_client.cookies.get("audio_server_csrf")
    assert csrf_token
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf_token}

    bad_time = app_client.post(
        "/api/v1/web/recordings",
        headers=headers,
        files={"audio": ("meeting.wav", wav_bytes, "audio/wav")},
        data={"started_at": "2026-08-13T09:30:00"},
    )
    assert bad_time.status_code == 422
    assert bad_time.json()["error"]["code"] == "started_at_invalid"

    def unsupported_probe(_source: Path) -> AudioProbe:
        return AudioProbe(
            duration_seconds=1,
            codec_name="aac",
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            sample_rate=16_000,
            channels=1,
            mime_type="audio/mp4",
            preferred_extension=".m4a",
        )

    monkeypatch.setattr(app_client.app.state.recording_service._audio, "probe", unsupported_probe)
    unsupported = app_client.post(
        "/api/v1/web/recordings",
        headers=headers,
        files={"audio": ("renamed.mp3", wav_bytes, "audio/mpeg")},
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["error"]["code"] == "web_audio_type_unsupported"

    request_limit = (
        app_client.app.state.settings.max_upload_bytes
        + app_client.app.state.settings.max_metadata_bytes
        + 1024 * 1024
    )
    too_large = app_client.post(
        "/api/v1/web/recordings",
        headers={**headers, "Content-Length": str(request_limit + 1)},
        files={"audio": ("meeting.wav", wav_bytes, "audio/wav")},
    )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "upload_request_too_large"


def test_browser_upload_does_not_ack_or_leave_original_when_job_commit_fails(
    app_client: TestClient,
    wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_and_login(app_client)
    csrf_token = app_client.cookies.get("audio_server_csrf")
    assert csrf_token

    def fail_commit(**_kwargs: object) -> None:
        raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(
        app_client.app.state.recording_service,
        "_create_web_recording_and_job",
        fail_commit,
    )
    response = app_client.post(
        "/api/v1/web/recordings",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token},
        files={"audio": ("meeting.wav", wav_bytes, "audio/wav")},
    )

    assert response.status_code == 500
    with app_client.app.state.test_session_factory() as session:
        assert session.scalar(select(Recording)) is None
    assert not list(app_client.app.state.storage.recordings_root.rglob("original.*"))


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
