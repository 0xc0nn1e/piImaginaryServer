from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from audio_server.api.schemas import ClientRecordingMetadata, TranscriptSegmentUpdate
from audio_server.db.activity_models import ProcessingActivity
from audio_server.db.models import (
    Analysis,
    AnalysisStatus,
    JobKind,
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
from audio_server.services.recording_service import _overlap_flags
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


def test_dense_conversational_transcript_is_editable_end_to_end(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    """A transcript far larger than one Whisper segment per speaker turn saves.

    The merge stage emits one segment per contiguous same-speaker word run, so a
    multi-hour recording with rapid speaker changes reaches tens of thousands of
    segments, and an edit must resubmit every one of them. This drives the real
    request path rather than the schema alone, so a segment-count ceiling or a
    quadratic save would both fail here.
    """

    segment_count = 12_000
    files, headers, metadata = make_upload(wav_bytes)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201
    recording_id = uuid.UUID(metadata["id"])
    with app_client.app.state.test_session_factory.begin() as session:
        recording = session.get(Recording, recording_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and job is not None
        recording.processing_status = RecordingStatus.COMPLETED
        recording.duration_seconds = segment_count
        job.status = JobStatus.COMPLETED
        session.add_all(
            TranscriptSegment(
                recording_id=recording_id,
                job_id=job.id,
                sequence=index,
                speaker_label=f"SPEAKER_0{index % 2}",
                start_time=index * 0.5,
                end_time=index * 0.5 + 0.4,
                text="はい",
                language="ja",
                has_overlap=False,
            )
            for index in range(segment_count)
        )
        session.flush()
        segment_ids = list(
            session.scalars(
                select(TranscriptSegment.id)
                .where(TranscriptSegment.recording_id == recording_id)
                .order_by(TranscriptSegment.sequence)
            )
        )

    assert len(segment_ids) == segment_count
    response = app_client.put(
        f"/api/v1/recordings/{recording_id}/transcript",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
        json={
            "expected_revision": 0,
            "segments": [
                {
                    "id": str(segment_id),
                    "speaker_label": "SPEAKER_00",
                    # The last pair is made to overlap so the sweep is exercised.
                    "start_time": index * 0.5,
                    "end_time": index * 0.5 + (0.9 if index == segment_count - 2 else 0.4),
                    "text": "はい、そうですね。",
                }
                for index, segment_id in enumerate(segment_ids)
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 1
    assert len(body["segments"]) == segment_count
    overlapping = [item["sequence"] for item in body["segments"] if item["has_overlap"]]
    assert overlapping == [segment_count - 2, segment_count - 1]


def test_overlap_flags_match_the_pairwise_definition() -> None:
    """The linear sweep must stay equivalent to comparing every pair."""

    def pairwise(ordered: list[TranscriptSegmentUpdate]) -> list[bool]:
        return [
            any(
                other.id != item.id
                and item.start_time < other.end_time
                and other.start_time < item.end_time
                for other in ordered
            )
            for item in ordered
        ]

    def build(spans: list[tuple[float, float]]) -> list[TranscriptSegmentUpdate]:
        items = [
            TranscriptSegmentUpdate(
                id=uuid.uuid4(), speaker_label="S", start_time=start, end_time=end, text="x"
            )
            for start, end in spans
        ]
        return sorted(items, key=lambda item: (item.start_time, item.end_time, str(item.id)))

    cases: list[list[tuple[float, float]]] = [
        [(0.0, 1.0)],
        [(0.0, 1.0), (1.0, 2.0)],
        [(0.0, 1.5), (1.0, 2.0)],
        [(0.0, 1.0), (0.0, 1.0)],
        [(0.0, 9.0), (2.0, 3.0)],
        [(0.0, 9.0), (1.0, 2.0), (5.0, 6.0)],
        [(0.0, 1.0), (5.0, 6.5), (6.0, 7.0)],
        [(0.0, 1.0), (0.0, 4.0), (2.0, 3.0)],
    ]
    random = Random(20260814)
    for _ in range(200):
        count = random.randint(1, 12)
        spans = []
        for _ in range(count):
            start = random.randint(0, 12) * 0.5 + random.choice([0.0, 0.0, 0.25])
            spans.append((start, start + random.choice([0.25, 0.5, 1.0, 3.0])))
        cases.append(spans)

    for spans in cases:
        ordered = build(spans)
        assert _overlap_flags(ordered) == pairwise(ordered), spans


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


def test_original_audio_stream_supports_authenticated_byte_ranges(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    uploaded = app_client.post("/api/v1/recordings", files=files, headers=headers)
    assert uploaded.status_code == 201
    recording_id = metadata["id"]
    auth = {"Authorization": f"Bearer {TEST_API_TOKEN}"}

    full = app_client.get(f"/api/v1/recordings/{recording_id}/audio", headers=auth)
    assert full.status_code == 200
    assert full.content == wav_bytes
    assert full.headers["content-type"].startswith("audio/wav")
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["cache-control"] == "private, no-store"

    ranged = app_client.get(
        f"/api/v1/recordings/{recording_id}/audio",
        headers={**auth, "Range": "bytes=0-15"},
    )
    assert ranged.status_code == 206
    assert ranged.content == wav_bytes[:16]
    assert ranged.headers["content-range"] == f"bytes 0-15/{len(wav_bytes)}"

    unauthenticated = app_client.get(f"/api/v1/recordings/{recording_id}/audio")
    assert unauthenticated.status_code == 401


def test_analysis_only_reprocess_keeps_recording_completed(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201
    recording_id = uuid.UUID(metadata["id"])
    with app_client.app.state.test_session_factory.begin() as session:
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
                start_time=0,
                end_time=1,
                text="確認します。",
                language="ja",
                confidence=0.9,
                has_overlap=False,
            )
        )

    response = app_client.post(
        f"/api/v1/recordings/{recording_id}/analysis/reprocess",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )

    assert response.status_code == 202
    auth = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
    transcript = app_client.get(
        f"/api/v1/recordings/{recording_id}/transcript", headers=auth
    ).json()
    transcript_edit = app_client.put(
        f"/api/v1/recordings/{recording_id}/transcript",
        headers=auth,
        json={
            "expected_revision": transcript["revision"],
            "segments": [
                {
                    "id": transcript["segments"][0]["id"],
                    "speaker_label": "SPEAKER_00",
                    "start_time": 0,
                    "end_time": 1,
                    "text": "変更しません。",
                }
            ],
        },
    )
    assert transcript_edit.status_code == 409
    assert transcript_edit.json()["error"]["code"] == "job_already_active"

    analysis_edit = app_client.put(
        f"/api/v1/recordings/{recording_id}/analysis",
        headers=auth,
        json={
            "expected_revision": 0,
            "result": {
                "description": {"ja": "確認です。", "zh_hk": "呢段係確認。"},
                "tags": [],
                "natural_expressions": [],
                "highlights": [],
            },
        },
    )
    assert analysis_edit.status_code == 409
    assert analysis_edit.json()["error"]["code"] == "job_already_active"

    with app_client.app.state.test_session_factory() as session:
        recording = session.get(Recording, recording_id)
        analysis_job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.kind == JobKind.ANALYSIS)
        )
        assert recording is not None
        assert recording.processing_status is RecordingStatus.COMPLETED
        assert analysis_job is not None and analysis_job.status is JobStatus.QUEUED


def test_transcript_and_analysis_updates_are_revisioned_and_grounded(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201
    recording_id = uuid.UUID(metadata["id"])
    with app_client.app.state.test_session_factory.begin() as session:
        recording = session.get(Recording, recording_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and job is not None
        recording.processing_status = RecordingStatus.COMPLETED
        job.status = JobStatus.COMPLETED
        segment = TranscriptSegment(
            recording_id=recording_id,
            job_id=job.id,
            sequence=0,
            speaker_label="SPEAKER_00",
            start_time=0,
            end_time=1,
            text="一旦こちらで持ち帰ります。",
            language="ja",
            confidence=0.9,
            has_overlap=False,
        )
        session.add(segment)
        session.add(
            Analysis(
                recording_id=recording_id,
                job_id=job.id,
                provider="disabled",
                schema_version="2",
                status=AnalysisStatus.SKIPPED,
            )
        )
        session.flush()
        segment_id = segment.id

    auth = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
    transcript_update = app_client.put(
        f"/api/v1/recordings/{recording_id}/transcript",
        headers=auth,
        json={
            "expected_revision": 0,
            "segments": [
                {
                    "id": str(segment_id),
                    "speaker_label": "SPEAKER_01",
                    "start_time": 0.1,
                    "end_time": 0.9,
                    "text": "一旦こちらで持ち帰ります。",
                }
            ],
        },
    )
    assert transcript_update.status_code == 200
    assert transcript_update.json()["revision"] == 1
    analysis = app_client.get(f"/api/v1/recordings/{recording_id}/analysis", headers=auth)
    assert analysis.json()["status"] == "stale"
    assert analysis.json()["revision"] == 1

    result = {
        "description": {"ja": "会議です。", "zh_hk": "呢段係會議。"},
        "tags": [{"ja": "検討", "zh_hk": "研究"}],
        "natural_expressions": [
            {
                "segment_sequence": 0,
                "start_time": 999,
                "speaker_label": "FAKE",
                "original_ja": "一旦こちらで持ち帰ります。",
                "translation_zh_hk": "我哋暫時拎返去研究。",
                "usage_ja": "職場表現です。",
                "usage_zh_hk": "職場用語。",
            }
        ],
        "highlights": [],
    }
    saved = app_client.put(
        f"/api/v1/recordings/{recording_id}/analysis",
        headers=auth,
        json={"expected_revision": 1, "result": result},
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == 2
    expression = saved.json()["result"]["natural_expressions"][0]
    assert expression["start_time"] == 0.1
    assert expression["speaker_label"] == "SPEAKER_01"

    conflict = app_client.put(
        f"/api/v1/recordings/{recording_id}/analysis",
        headers=auth,
        json={"expected_revision": 1, "result": result},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "analysis_revision_conflict"


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


def test_completed_recording_can_be_reprocessed_without_dropping_old_transcript(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201
    recording_id = uuid.UUID(metadata["id"])
    with app_client.app.state.test_session_factory.begin() as session:
        recording = session.get(Recording, recording_id)
        old_job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and old_job is not None
        recording.processing_status = RecordingStatus.COMPLETED
        old_job.status = JobStatus.COMPLETED
        session.add(
            TranscriptSegment(
                recording_id=recording_id,
                job_id=old_job.id,
                sequence=0,
                speaker_label="SPEAKER_00",
                start_time=0.0,
                end_time=1.0,
                text="existing result",
                language="en",
                confidence=0.9,
                has_overlap=False,
            )
        )

    response = app_client.post(
        f"/api/v1/recordings/{recording_id}/reprocess",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    with app_client.app.state.test_session_factory() as session:
        recording = session.get(Recording, recording_id)
        jobs = list(
            session.scalars(select(ProcessingJob).where(ProcessingJob.recording_id == recording_id))
        )
        segments = list(
            session.scalars(
                select(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)
            )
        )
        assert recording is not None
        assert recording.processing_status is RecordingStatus.QUEUED
        assert len(jobs) == 2
        assert [segment.text for segment in segments] == ["existing result"]


def test_active_recording_cannot_be_reprocessed_or_deleted(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201
    recording_id = uuid.UUID(metadata["id"])
    auth = {"Authorization": f"Bearer {TEST_API_TOKEN}"}

    reprocess = app_client.post(f"/api/v1/recordings/{recording_id}/reprocess", headers=auth)
    deleted = app_client.delete(f"/api/v1/recordings/{recording_id}", headers=auth)

    assert reprocess.status_code == 409
    assert reprocess.json()["error"]["code"] == "recording_not_reprocessable"
    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "recording_delete_active"
    with app_client.app.state.test_session_factory() as session:
        recording = session.get(Recording, recording_id)
        assert recording is not None
        assert app_client.app.state.storage.exists(recording.storage_key)


def test_delete_terminal_recording_removes_file_and_all_database_children(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201
    recording_id = uuid.UUID(metadata["id"])
    with app_client.app.state.test_session_factory.begin() as session:
        recording = session.get(Recording, recording_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and job is not None
        storage_key = recording.storage_key
        job_id = job.id
        recording.processing_status = RecordingStatus.FAILED
        job.status = JobStatus.FAILED
        session.add(
            TranscriptSegment(
                recording_id=recording_id,
                job_id=job.id,
                sequence=0,
                speaker_label="SPEAKER_00",
                start_time=0.0,
                end_time=1.0,
                text="private transcript",
                language="en",
                confidence=None,
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
    work = app_client.app.state.storage.work_directory(job_id, uuid.uuid4())
    (work / "processing.wav").write_bytes(b"derived audio")

    response = app_client.delete(
        f"/api/v1/recordings/{recording_id}",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )

    assert response.status_code == 204
    assert not app_client.app.state.storage.exists(storage_key)
    assert not (app_client.app.state.storage.work_root / str(job_id)).exists()
    with app_client.app.state.test_session_factory() as session:
        assert session.get(Recording, recording_id) is None
        assert (
            session.scalar(select(ProcessingJob).where(ProcessingJob.recording_id == recording_id))
            is None
        )
        assert (
            session.scalar(
                select(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)
            )
            is None
        )
        assert session.scalar(select(Analysis).where(Analysis.recording_id == recording_id)) is None
        assert (
            session.scalar(
                select(ProcessingActivity).where(ProcessingActivity.recording_id == recording_id)
            )
            is None
        )


def test_delete_database_failure_restores_original_audio(
    app_client: TestClient,
    wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files, headers, metadata = make_upload(wav_bytes)
    assert app_client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201
    recording_id = uuid.UUID(metadata["id"])
    with app_client.app.state.test_session_factory.begin() as session:
        recording = session.get(Recording, recording_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and job is not None
        storage_key = recording.storage_key
        recording.processing_status = RecordingStatus.FAILED
        job.status = JobStatus.FAILED

    def fail_commit(_session: Session) -> None:
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(Session, "commit", fail_commit)
    response = app_client.delete(
        f"/api/v1/recordings/{recording_id}",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_server_error"
    assert app_client.app.state.storage.exists(storage_key)
    with app_client.app.state.test_session_factory() as session:
        assert session.get(Recording, recording_id) is not None
