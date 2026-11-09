"""Live captions an iPhone recorder sends beside its audio."""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from audio_server.db.activity_models import ProcessingActivity, ProcessingActivityType
from audio_server.db.models import (
    PROCESSING_SETTINGS_ID,
    Analysis,
    AnalysisStatus,
    JobKind,
    JobStage,
    JobStatus,
    ProcessingJob,
    ProcessingSettings,
    Recording,
    RecordingCaptions,
    RecordingStatus,
    TranscriptSegment,
)
from audio_server.jobs.queue import JobQueue
from audio_server.jobs.worker import Worker, WorkerIntervals
from audio_server.processing.contracts import (
    AnalysisResult,
    CaptionLine,
    MergedTranscriptSegment,
)
from audio_server.processing.contracts import AnalysisStatus as PipelineAnalysisStatus
from audio_server.worker_runtime import PipelineJobProcessor
from tests.conftest import TEST_API_TOKEN, make_upload

AUTH = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
DEVICE = "iphone-recorder-01"
LINES: list[dict[str, Any]] = [
    {"start": 4.5, "end": 6.0, "text": "来週までにお願いします。"},
    {"start": 0.2, "end": 2.0, "text": "  一旦持ち帰ります。  "},
]


def _upload(app_client: TestClient, audio: bytes) -> uuid.UUID:
    files, headers, metadata = make_upload(audio, device_id=DEVICE)
    response = app_client.post("/api/v1/recordings", files=files, headers=headers)
    assert response.status_code == 201
    return uuid.UUID(metadata["id"])


def _put_captions(
    app_client: TestClient,
    recording_id: uuid.UUID,
    lines: list[dict[str, Any]],
    *,
    locale: str = "ja-JP",
    headers: dict[str, str] | None = None,
) -> Response:
    return app_client.put(
        f"/api/v1/recordings/{recording_id}/captions",
        headers={**AUTH, "X-Device-ID": DEVICE} if headers is None else headers,
        json={"locale": locale, "lines": lines},
    )


def _factory(app_client: TestClient) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app_client.app.state.test_session_factory
    return factory


def _finish_transcription(
    app_client: TestClient, recording_id: uuid.UUID, *, analysed: bool
) -> None:
    """Leave a recording as the chain does once its transcript is committed."""

    with _factory(app_client).begin() as session:
        recording = session.get(Recording, recording_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and job is not None
        recording.processing_status = RecordingStatus.COMPLETED
        job.status = JobStatus.COMPLETED
        if analysed:
            session.add(
                Analysis(
                    recording_id=recording_id,
                    job_id=job.id,
                    provider="lmstudio",
                    schema_version="2",
                    status=AnalysisStatus.COMPLETED,
                    result={"description": {"ja": "打ち合わせです。", "zh_hk": "會議。"}},
                )
            )


def _analysis_jobs(app_client: TestClient, recording_id: uuid.UUID) -> list[ProcessingJob]:
    with _factory(app_client)() as session:
        return list(
            session.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.recording_id == recording_id,
                    ProcessingJob.kind == JobKind.ANALYSIS,
                )
            )
        )


def test_captions_are_kept_in_spoken_order_and_read_back(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    recording_id = _upload(app_client, wav_bytes)

    response = _put_captions(app_client, recording_id, LINES)

    assert response.status_code == 200
    # The transcript is still to come, and the analysis it chains reads these.
    assert response.json() == {
        "recording_id": str(recording_id),
        "locale": "ja-JP",
        "line_count": 2,
        "analysis_queued": False,
    }
    read = app_client.get(f"/api/v1/recordings/{recording_id}/captions", headers=AUTH)
    assert read.status_code == 200
    body = read.json()
    assert body["locale"] == "ja-JP"
    assert body["updated_at"] is not None
    assert body["lines"] == [
        {"sequence": 0, "start_time": 0.2, "end_time": 2.0, "text": "一旦持ち帰ります。"},
        {"sequence": 1, "start_time": 4.5, "end_time": 6.0, "text": "来週までにお願いします。"},
    ]
    # Japanese captions carry readings, as the transcript does.
    assert "来週までにお願いします。" in body["furigana"]


def test_captions_heard_as_another_language_carry_no_japanese_readings(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    recording_id = _upload(app_client, wav_bytes)
    lines = [{"start": 0, "end": 1, "text": "今日天氣好好"}]

    assert _put_captions(app_client, recording_id, lines, locale="yue-Hant-HK").status_code == 200

    body = app_client.get(f"/api/v1/recordings/{recording_id}/captions", headers=AUTH).json()
    assert body["locale"] == "yue-Hant-HK"
    assert body["furigana"] == {}


def test_a_recording_without_captions_reads_as_empty(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    recording_id = _upload(app_client, wav_bytes)

    read = app_client.get(f"/api/v1/recordings/{recording_id}/captions", headers=AUTH)

    # Every recorder but the iPhone sends none, so this is not an error.
    assert read.status_code == 200
    assert read.json() == {
        "recording_id": str(recording_id),
        "locale": None,
        "updated_at": None,
        "lines": [],
        "furigana": {},
    }


def test_captions_need_the_recorder_credential(app_client: TestClient, wav_bytes: bytes) -> None:
    recording_id = _upload(app_client, wav_bytes)

    anonymous = _put_captions(app_client, recording_id, LINES, headers={"X-Device-ID": DEVICE})
    wrong = _put_captions(
        app_client,
        recording_id,
        LINES,
        headers={"Authorization": "Bearer not-the-recorder-token", "X-Device-ID": DEVICE},
    )
    unread = app_client.get(f"/api/v1/recordings/{recording_id}/captions")

    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert unread.status_code == 401
    with _factory(app_client)() as session:
        assert session.scalar(select(RecordingCaptions)) is None


def test_captions_from_another_device_are_refused(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    recording_id = _upload(app_client, wav_bytes)

    response = _put_captions(
        app_client, recording_id, LINES, headers={**AUTH, "X-Device-ID": "someone-else"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "captions_device_mismatch"
    with _factory(app_client)() as session:
        assert session.scalar(select(RecordingCaptions)) is None


def test_captions_for_an_unknown_recording_are_not_found(app_client: TestClient) -> None:
    unknown = uuid.uuid4()

    stored = _put_captions(app_client, unknown, LINES)
    read = app_client.get(f"/api/v1/recordings/{unknown}/captions", headers=AUTH)

    assert stored.status_code == 404
    assert stored.json()["error"]["code"] == "recording_not_found"
    assert read.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"locale": "ja-JP", "lines": [{"start": -0.1, "end": 1, "text": "あ"}]},
        {"locale": "ja-JP", "lines": [{"start": 2, "end": 1, "text": "あ"}]},
        {"locale": "ja-JP", "lines": [{"start": 0, "end": 1, "text": "   "}]},
        {"locale": "ja-JP", "lines": [{"start": 0, "end": 1, "text": "あ", "speaker": "A"}]},
        {"locale": "日本語", "lines": []},
        {"lines": []},
    ],
)
def test_malformed_captions_are_rejected(
    app_client: TestClient, wav_bytes: bytes, payload: dict[str, Any]
) -> None:
    recording_id = _upload(app_client, wav_bytes)

    response = app_client.put(
        f"/api/v1/recordings/{recording_id}/captions",
        headers={**AUTH, "X-Device-ID": DEVICE},
        json=payload,
    )

    assert response.status_code == 422


def test_captions_without_a_device_header_are_rejected(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    recording_id = _upload(app_client, wav_bytes)

    response = _put_captions(app_client, recording_id, LINES, headers=AUTH)

    assert response.status_code == 422


def test_captions_after_the_analysis_flag_it_and_queue_a_fresh_one(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    recording_id = _upload(app_client, wav_bytes)
    _finish_transcription(app_client, recording_id, analysed=True)

    response = _put_captions(app_client, recording_id, LINES)

    assert response.status_code == 200
    assert response.json()["analysis_queued"] is True
    with _factory(app_client)() as session:
        recording = session.get(Recording, recording_id)
        analysis = session.scalar(select(Analysis).where(Analysis.recording_id == recording_id))
        job = session.scalar(select(ProcessingJob).where(ProcessingJob.kind == JobKind.ANALYSIS))
        assert job is not None
        events = list(
            session.scalars(
                select(ProcessingActivity.event_type).where(ProcessingActivity.job_id == job.id)
            )
        )
    assert recording is not None and analysis is not None
    # The reading stays visible but is marked out of date until it is redone.
    assert analysis.status is AnalysisStatus.STALE
    assert recording.analysis_revision == 1
    assert recording.processing_status is RecordingStatus.COMPLETED
    assert job.status is JobStatus.QUEUED
    # Captions change what the analysis reads, not the transcript, so the
    # sentence translation is not redone behind it.
    assert job.follow_up_kind is None
    assert events == [ProcessingActivityType.JOB_QUEUED]


def test_resending_the_same_captions_changes_nothing(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    recording_id = _upload(app_client, wav_bytes)
    _finish_transcription(app_client, recording_id, analysed=True)
    assert _put_captions(app_client, recording_id, LINES).json()["analysis_queued"] is True
    with _factory(app_client).begin() as session:
        # The analysis those captions asked for has since been written.
        job = session.scalar(select(ProcessingJob).where(ProcessingJob.kind == JobKind.ANALYSIS))
        analysis = session.scalar(select(Analysis).where(Analysis.recording_id == recording_id))
        assert job is not None and analysis is not None
        job.status = JobStatus.COMPLETED
        analysis.status = AnalysisStatus.COMPLETED

    # A device that never saw the first answer sends the same captions again.
    replay = _put_captions(app_client, recording_id, LINES)

    assert replay.status_code == 200
    assert replay.json()["analysis_queued"] is False
    assert len(_analysis_jobs(app_client, recording_id)) == 1
    with _factory(app_client)() as session:
        recording = session.get(Recording, recording_id)
        analysis = session.scalar(select(Analysis).where(Analysis.recording_id == recording_id))
    assert recording is not None and analysis is not None
    assert analysis.status is AnalysisStatus.COMPLETED
    assert recording.analysis_revision == 1


def test_captions_during_transcription_are_left_to_the_chained_analysis(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    recording_id = _upload(app_client, wav_bytes)

    response = _put_captions(app_client, recording_id, LINES)

    assert response.json()["analysis_queued"] is False
    with _factory(app_client)() as session:
        jobs = list(
            session.scalars(select(ProcessingJob).where(ProcessingJob.recording_id == recording_id))
        )
    assert [job.kind for job in jobs] == [JobKind.FULL]
    assert jobs[0].follow_up_kind is JobKind.ANALYSIS


def test_with_automatic_analysis_off_captions_only_flag_the_analysis(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    recording_id = _upload(app_client, wav_bytes)
    _finish_transcription(app_client, recording_id, analysed=True)
    with _factory(app_client).begin() as session:
        session.add(
            ProcessingSettings(
                id=PROCESSING_SETTINGS_ID, auto_analysis=False, auto_translation=True
            )
        )

    response = _put_captions(app_client, recording_id, LINES)

    assert response.json()["analysis_queued"] is False
    assert _analysis_jobs(app_client, recording_id) == []
    with _factory(app_client)() as session:
        analysis = session.scalar(select(Analysis).where(Analysis.recording_id == recording_id))
    assert analysis is not None and analysis.status is AnalysisStatus.STALE


def test_captions_that_arrive_while_another_job_runs_are_still_kept(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    recording_id = _upload(app_client, wav_bytes)
    _finish_transcription(app_client, recording_id, analysed=True)
    with _factory(app_client).begin() as session:
        session.add(
            ProcessingJob(
                recording_id=recording_id,
                kind=JobKind.TRANSLATION,
                status=JobStatus.PROCESSING,
                stage=JobStage.TRANSLATING,
                available_at=datetime.now(UTC),
            )
        )

    response = _put_captions(app_client, recording_id, LINES)

    assert response.status_code == 200
    assert response.json()["analysis_queued"] is False
    assert _analysis_jobs(app_client, recording_id) == []
    with _factory(app_client)() as session:
        stored = session.scalar(select(RecordingCaptions))
        analysis = session.scalar(select(Analysis).where(Analysis.recording_id == recording_id))
    assert stored is not None and len(stored.lines) == 2
    assert analysis is not None and analysis.status is AnalysisStatus.STALE


def test_deleting_a_recording_removes_its_captions(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    recording_id = _upload(app_client, wav_bytes)
    assert _put_captions(app_client, recording_id, LINES).status_code == 200
    _finish_transcription(app_client, recording_id, analysed=False)

    deleted = app_client.delete(f"/api/v1/recordings/{recording_id}", headers=AUTH)

    assert deleted.status_code == 204
    with _factory(app_client)() as session:
        assert session.scalar(select(RecordingCaptions)) is None


class CapturingAnalysisProvider:
    name = "capturing"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], tuple[CaptionLine, ...]]] = []

    def analyze(
        self,
        recording_id: str,
        segments: Sequence[MergedTranscriptSegment],
        captions: Sequence[CaptionLine] = (),
    ) -> AnalysisResult:
        self.calls.append(
            (recording_id, tuple(segment.text for segment in segments), tuple(captions))
        )
        return AnalysisResult(
            status=PipelineAnalysisStatus.SKIPPED, provider=self.name, schema_version=2
        )


def test_the_analysis_worker_reads_the_captions_beside_the_transcript(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    recording_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.add(
            Recording(
                id=recording_id,
                device_id=DEVICE,
                original_filename="chunk.m4a",
                storage_key=f"recordings/{recording_id}/original.m4a",
                mime_type="audio/mp4",
                audio_format="m4a",
                file_size=1024,
                sha256="b" * 64,
                started_at=now,
                ended_at=now + timedelta(seconds=10),
                duration_seconds=10,
                client_metadata={},
                processing_status=RecordingStatus.COMPLETED,
            )
        )
        job = ProcessingJob(recording_id=recording_id, kind=JobKind.ANALYSIS, available_at=now)
        session.add(job)
        session.flush()
        session.add(
            TranscriptSegment(
                recording_id=recording_id,
                job_id=job.id,
                sequence=0,
                speaker_label="SPEAKER_00",
                start_time=0.0,
                end_time=2.0,
                text="一旦持ち帰ります。",
                language="ja",
                confidence=0.9,
                has_overlap=False,
            )
        )
        session.add(
            RecordingCaptions(
                recording_id=recording_id,
                locale="ja-JP",
                lines=[{"start": 0.1, "end": 1.9, "text": "一旦持ち帰ります"}],
            )
        )
    provider = CapturingAnalysisProvider()
    processor = PipelineJobProcessor(
        session_factory=session_factory,
        storage=object(),  # type: ignore[arg-type]
        analysis_provider=provider,
    )
    queue = JobQueue(
        session_factory, worker_id="captions-worker", lease_duration=timedelta(seconds=10)
    )
    worker = Worker(
        queue,
        lambda: processor,
        intervals=WorkerIntervals(poll_seconds=0.01, heartbeat_seconds=1, recovery_seconds=1),
    )

    assert worker.run_once() is True

    assert provider.calls == [
        (
            str(recording_id),
            ("一旦持ち帰ります。",),
            (CaptionLine(start=0.1, end=1.9, text="一旦持ち帰ります"),),
        )
    ]


def test_the_migration_adds_and_removes_the_captions_table() -> None:
    migration = importlib.import_module("migrations.versions.0013_recording_captions")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        with engine.begin() as connection, Operations.context(
            MigrationContext.configure(connection)
        ):
            migration.upgrade()
        columns = sa.inspect(engine).get_columns("recording_captions")
        assert {column["name"] for column in columns} == {
            "id",
            "recording_id",
            "locale",
            "lines",
            "created_at",
            "updated_at",
        }

        with engine.begin() as connection, Operations.context(
            MigrationContext.configure(connection)
        ):
            migration.downgrade()
        assert "recording_captions" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
