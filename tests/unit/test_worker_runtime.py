from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from audio_server.db.models import (
    Analysis,
    JobStatus,
    ProcessingJob,
    Recording,
    RecordingStatus,
    TranscriptSegment,
)
from audio_server.db.models import (
    AnalysisStatus as DatabaseAnalysisStatus,
)
from audio_server.jobs.queue import JobQueue
from audio_server.jobs.worker import Worker, WorkerIntervals
from audio_server.processing.contracts import (
    AnalysisResult,
    AudioProbe,
    MergedTranscriptSegment,
    PipelineResult,
    ProcessingStage,
    StageCallback,
)
from audio_server.processing.contracts import (
    AnalysisStatus as ProcessingAnalysisStatus,
)
from audio_server.services.storage import LocalStorageBackend
from audio_server.worker_runtime import PipelineJobProcessor


class FakePipeline:
    def run(
        self,
        *,
        recording_id: str | uuid.UUID,
        source_path: Path,
        work_dir: Path,
        stage_callback: StageCallback | None = None,
    ) -> PipelineResult:
        assert source_path.read_bytes() == b"original-audio"
        work_dir.mkdir(parents=True, exist_ok=True)
        for stage in ProcessingStage:
            if stage_callback is not None:
                stage_callback(stage)
        return PipelineResult(
            recording_id=str(recording_id),
            audio=AudioProbe(
                duration_seconds=2,
                codec_name="flac",
                format_name="flac",
                sample_rate=16_000,
                channels=1,
                mime_type="audio/flac",
                preferred_extension=".flac",
            ),
            transcript=(
                MergedTranscriptSegment(
                    sequence=0,
                    speaker_label="SPEAKER_00",
                    start=0,
                    end=2,
                    text="integration result",
                    language="en",
                    confidence=0.9,
                ),
            ),
            analysis=AnalysisResult(
                status=ProcessingAnalysisStatus.SKIPPED,
                provider="disabled",
            ),
        )


def test_worker_pipeline_results_commit_atomically(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    storage = LocalStorageBackend(tmp_path / "data")
    recording_id = uuid.uuid4()
    storage_key = f"recordings/2026/08/10/{recording_id}/original.flac"
    staged = storage.create_staged_upload(io.BytesIO(b"original-audio"), max_bytes=100)
    storage.put_original(staged, storage_key)
    now = datetime.now(UTC)

    with session_factory.begin() as session:
        recording = Recording(
            id=recording_id,
            device_id="worker-runtime-test",
            original_filename="sample.flac",
            storage_key=storage_key,
            mime_type="audio/flac",
            audio_format="flac",
            file_size=len(b"original-audio"),
            sha256="a" * 64,
            started_at=now,
            ended_at=now + timedelta(seconds=2),
            duration_seconds=2,
            client_metadata={},
            processing_status=RecordingStatus.QUEUED,
        )
        job = ProcessingJob(recording_id=recording_id, available_at=now)
        session.add_all([recording, job])

    queue = JobQueue(
        session_factory,
        worker_id="runtime-test-worker",
        lease_duration=timedelta(seconds=10),
    )
    processor = PipelineJobProcessor(
        session_factory=session_factory,
        storage=storage,
        pipeline=FakePipeline(),  # type: ignore[arg-type]
    )
    worker = Worker(
        queue,
        lambda: processor,
        intervals=WorkerIntervals(
            poll_seconds=0.01,
            heartbeat_seconds=1,
            recovery_seconds=1,
        ),
    )

    assert worker.run_once() is True

    with session_factory() as session:
        stored_recording = session.get(Recording, recording_id)
        stored_job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        segment = session.scalar(
            select(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)
        )
        analysis = session.scalar(select(Analysis).where(Analysis.recording_id == recording_id))

    assert stored_recording is not None
    assert stored_recording.processing_status is RecordingStatus.COMPLETED
    assert stored_job is not None and stored_job.status is JobStatus.COMPLETED
    assert segment is not None and segment.text == "integration result"
    assert analysis is not None and analysis.status is DatabaseAnalysisStatus.SKIPPED
    assert not list(storage.work_root.rglob("processing.wav"))
