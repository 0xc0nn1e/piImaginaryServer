from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from audio_server.api.schemas import ClientRecordingMetadata
from audio_server.db.models import (
    Analysis,
    AnalysisStatus,
    JobStatus,
    ProcessingJob,
    Recording,
    RecordingStatus,
    TranscriptSegment,
)
from audio_server.processing.contracts import ProcessingStage
from audio_server.processing.errors import (
    ProviderConfigurationError,
    RetryableProcessingError,
)
from tests.conftest import TEST_API_TOKEN, make_upload


def test_upload_creates_recording_and_durable_job(app_client: TestClient, wav_bytes: bytes) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    response = app_client.post("/api/v1/recordings", files=files, headers=headers)
    assert response.status_code == 201
    body = response.json()
    assert body == {
        "recording_id": metadata["id"],
        "status": "queued",
        "duplicate": False,
    }
    assert response.headers["location"].endswith(metadata["id"])

    session_factory = app_client.app.state.test_session_factory
    with session_factory() as session:
        recording = session.get(Recording, uuid.UUID(metadata["id"]))
        assert recording is not None
        assert recording.storage_key.endswith("/original.wav")
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording.id)
        )
        assert job is not None
        assert job.status is JobStatus.QUEUED
        assert app_client.app.state.storage.exists(recording.storage_key)


def test_exact_upload_replay_is_idempotent(app_client: TestClient, wav_bytes: bytes) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201
    replay_files, replay_headers, _ = make_upload(wav_bytes, recording_id=uuid.UUID(metadata["id"]))
    response = app_client.post("/api/v1/recordings", files=replay_files, headers=replay_headers)
    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert response.json()["recording_id"] == metadata["id"]


def test_same_device_and_checksum_returns_existing_recording(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    first_files, first_headers, first_metadata = make_upload(wav_bytes)
    assert (
        app_client.post("/api/v1/recordings", files=first_files, headers=first_headers).status_code
        == 201
    )
    second_files, second_headers, _ = make_upload(wav_bytes)
    response = app_client.post("/api/v1/recordings", files=second_files, headers=second_headers)
    assert response.status_code == 200
    assert response.json()["recording_id"] == first_metadata["id"]


def test_checksum_mismatch_is_rejected_without_database_rows(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    incorrect = "0" * 64
    files, headers, _ = make_upload(wav_bytes, checksum=incorrect)
    response = app_client.post("/api/v1/recordings", files=files, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "checksum_mismatch"
    with app_client.app.state.test_session_factory() as session:
        assert session.scalar(select(Recording)) is None
        assert session.scalar(select(ProcessingJob)) is None


def test_non_finite_extra_metadata_is_rejected_before_storage(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    metadata["extra"] = {"invalid": float("nan")}
    files["metadata"] = (None, json.dumps(metadata), "application/json")

    response = app_client.post("/api/v1/recordings", files=files, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "metadata_invalid"
    assert not list(app_client.app.state.storage.recordings_root.rglob("*.*"))


def test_reused_recording_id_with_different_audio_conflicts(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    identifier = uuid.uuid4()
    files, headers, _ = make_upload(wav_bytes, recording_id=identifier)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201

    changed_audio = wav_bytes + b"changed"
    changed_hash = hashlib.sha256(changed_audio).hexdigest()
    changed_files, changed_headers, _ = make_upload(
        changed_audio, recording_id=identifier, checksum=changed_hash
    )
    response = app_client.post("/api/v1/recordings", files=changed_files, headers=changed_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "recording_identity_conflict"


def test_concurrent_identity_conflict_removes_unmanaged_published_audio(
    app_client: TestClient,
    wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = uuid.uuid4()
    files, headers, _ = make_upload(wav_bytes, recording_id=identifier)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201

    service = app_client.app.state.recording_service
    find_duplicate = service._find_duplicate
    calls = 0

    def simulate_concurrent_insert(
        session: Session, metadata: ClientRecordingMetadata
    ) -> Recording | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return find_duplicate(session, metadata)

    monkeypatch.setattr(service, "_find_duplicate", simulate_concurrent_insert)
    changed_audio = wav_bytes + b"different"
    changed_hash = hashlib.sha256(changed_audio).hexdigest()
    next_day = datetime(2026, 8, 11, tzinfo=UTC)
    changed_files, changed_headers, _ = make_upload(
        changed_audio,
        recording_id=identifier,
        checksum=changed_hash,
        metadata_overrides={
            "recording_start_time": next_day.isoformat(),
            "recording_end_time": (next_day + timedelta(seconds=1)).isoformat(),
        },
    )

    response = app_client.post("/api/v1/recordings", files=changed_files, headers=changed_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "recording_identity_conflict"
    originals = list(app_client.app.state.storage.recordings_root.rglob("original.wav"))
    assert len(originals) == 1
    assert originals[0].read_bytes() == wav_bytes


@pytest.mark.parametrize(
    "probe_error",
    [
        ProviderConfigurationError(
            code="ffprobe_unavailable",
            safe_message="server path must not leak",
            stage=ProcessingStage.PREPROCESSING,
        ),
        RetryableProcessingError(
            code="audio_probe_timeout",
            safe_message="provider output must not leak",
            stage=ProcessingStage.PREPROCESSING,
        ),
    ],
)
def test_probe_server_failures_are_not_misclassified_as_unsupported_media(
    app_client: TestClient,
    wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
    probe_error: Exception,
) -> None:
    def fail_probe(_source: Path) -> None:
        raise probe_error

    monkeypatch.setattr(app_client.app.state.recording_service._audio, "probe", fail_probe)
    files, headers, _ = make_upload(wav_bytes)

    response = app_client.post("/api/v1/recordings", files=files, headers=headers)

    assert response.status_code == 503
    assert response.json()["error"]["code"] in {
        "ffprobe_unavailable",
        "audio_probe_timeout",
    }


def test_recording_status_and_list_are_retrievable(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    app_client.post("/api/v1/recordings", files=files, headers=headers)
    auth = {"Authorization": f"Bearer {TEST_API_TOKEN}"}

    listing = app_client.get("/api/v1/recordings", headers=auth)
    assert listing.status_code == 200
    assert listing.json()["items"][0]["id"] == metadata["id"]

    status = app_client.get(f"/api/v1/recordings/{metadata['id']}/status", headers=auth)
    assert status.status_code == 200
    assert status.json()["job"]["stage"] == "queued"


def test_transcript_and_analysis_retrieval(app_client: TestClient, wav_bytes: bytes) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    app_client.post("/api/v1/recordings", files=files, headers=headers)
    recording_id = uuid.UUID(metadata["id"])
    session_factory = app_client.app.state.test_session_factory
    with session_factory.begin() as session:
        recording = session.get(Recording, recording_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and job is not None
        recording.processing_status = RecordingStatus.COMPLETED
        job.status = JobStatus.COMPLETED
        session.add(
            TranscriptSegment(
                recording_id=recording_id,
                job_id=job.id,
                sequence=0,
                speaker_label="SPEAKER_00",
                start_time=5.0,
                end_time=7.0,
                text="来週までにお願いします。",
                language="ja",
                confidence=0.9,
                has_overlap=False,
            )
        )
        session.add(
            Analysis(
                recording_id=recording_id,
                job_id=job.id,
                provider="disabled",
                schema_version="1",
                status=AnalysisStatus.SKIPPED,
                result=None,
            )
        )

    auth = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
    transcript = app_client.get(f"/api/v1/recordings/{recording_id}/transcript", headers=auth)
    assert transcript.status_code == 200
    assert "[00:00:05.000] SPEAKER_00" in transcript.json()["text"]
    assert transcript.json()["segments"][0]["language"] == "ja"

    analysis = app_client.get(f"/api/v1/recordings/{recording_id}/analysis", headers=auth)
    assert analysis.status_code == 200
    assert analysis.json()["status"] == "skipped"


def test_completed_silent_recording_returns_empty_transcript(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    app_client.post("/api/v1/recordings", files=files, headers=headers)
    recording_id = uuid.UUID(metadata["id"])
    with app_client.app.state.test_session_factory.begin() as session:
        recording = session.get(Recording, recording_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and job is not None
        recording.processing_status = RecordingStatus.COMPLETED
        job.status = JobStatus.COMPLETED

    response = app_client.get(
        f"/api/v1/recordings/{recording_id}/transcript",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json()["segments"] == []
    assert response.json()["text"] == ""


def test_failed_recording_can_be_retried(app_client: TestClient, wav_bytes: bytes) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    app_client.post("/api/v1/recordings", files=files, headers=headers)
    recording_id = uuid.UUID(metadata["id"])
    with app_client.app.state.test_session_factory.begin() as session:
        recording = session.get(Recording, recording_id)
        old_job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and old_job is not None
        recording.processing_status = RecordingStatus.FAILED
        old_job.status = JobStatus.FAILED

    response = app_client.post(
        f"/api/v1/recordings/{recording_id}/retry",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    with app_client.app.state.test_session_factory() as session:
        jobs = list(
            session.scalars(select(ProcessingJob).where(ProcessingJob.recording_id == recording_id))
        )
        assert len(jobs) == 2
