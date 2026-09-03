"""The LLM work a recording needs is queued, not run inside transcription."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from audio_server.db.models import (
    Analysis,
    AnalysisStatus,
    JobKind,
    JobStage,
    JobStatus,
    ProcessingJob,
    Recording,
    RecordingStatus,
)
from audio_server.jobs.queue import (
    ClaimedJob,
    JobFailure,
    JobQueue,
    RetryPolicy,
    create_processing_job,
)
from audio_server.processing.contracts import (
    AnalysisResult,
    AudioProbe,
    MergedTranscriptSegment,
    PipelineResult,
)
from audio_server.processing.contracts import AnalysisStatus as PipelineAnalysisStatus
from audio_server.worker_runtime import _analysis_result_persister, _result_persister

BASE_TIME = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)


def _add_recording(session: Session, recording_id: uuid.UUID) -> Recording:
    recording = Recording(
        id=recording_id,
        device_id="chain-test-device",
        original_filename="recording.wav",
        storage_key=f"recordings/{recording_id}/original.wav",
        mime_type="audio/wav",
        audio_format="wav",
        file_size=1024,
        sha256=uuid.uuid4().hex * 2,
        started_at=BASE_TIME,
        ended_at=BASE_TIME + timedelta(seconds=1),
        duration_seconds=1,
        client_metadata={},
        processing_status=RecordingStatus.UPLOADED,
    )
    session.add(recording)
    return recording


def _analysis_claim(job_id: uuid.UUID, recording_id: uuid.UUID) -> ClaimedJob:
    return ClaimedJob(
        id=job_id,
        recording_id=recording_id,
        kind=JobKind.ANALYSIS,
        claim_token=uuid.uuid4(),
        worker_id="chain-test-worker",
        attempt_count=1,
        max_attempts=3,
        stage=JobStage.ANALYZING,
    )


def _queue(session_factory: sessionmaker[Session]) -> JobQueue:
    return JobQueue(
        session_factory,
        worker_id="chain-test-worker",
        lease_duration=timedelta(seconds=60),
        retry_policy=RetryPolicy(
            base_delay=timedelta(seconds=30),
            max_delay=timedelta(seconds=120),
        ),
    )


def _uploaded(
    session_factory: sessionmaker[Session],
    *,
    kind: JobKind = JobKind.FULL,
    max_attempts: int = 3,
) -> uuid.UUID:
    recording_id = uuid.uuid4()
    with session_factory.begin() as session:
        _add_recording(session, recording_id)
        create_processing_job(
            session,
            recording_id=recording_id,
            kind=kind,
            max_attempts=max_attempts,
            available_at=BASE_TIME,
        )
    return recording_id


def _jobs(
    session_factory: sessionmaker[Session], recording_id: uuid.UUID
) -> list[ProcessingJob]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(ProcessingJob)
                .where(ProcessingJob.recording_id == recording_id)
                .order_by(ProcessingJob.available_at, ProcessingJob.created_at)
            )
        )


def test_transcription_queues_the_analysis_that_reads_its_transcript(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id = _uploaded(session_factory)
    queue = _queue(session_factory)

    claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=1))
    assert claim is not None
    queue.complete(claim, now=BASE_TIME + timedelta(seconds=2))

    jobs = _jobs(session_factory, recording_id)
    assert [job.kind for job in jobs] == [JobKind.FULL, JobKind.ANALYSIS]
    assert jobs[1].status is JobStatus.QUEUED
    # Translation is the far longer step, so it waits behind the analysis a
    # reviewer is actually waiting to read.
    assert jobs[1].follow_up_kind is JobKind.TRANSLATION


def test_the_chain_runs_analysis_then_translation_and_then_stops(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id = _uploaded(session_factory)
    queue = _queue(session_factory)

    for second in (1, 3, 5):
        claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=second))
        assert claim is not None
        queue.complete(claim, now=BASE_TIME + timedelta(seconds=second + 1))

    jobs = _jobs(session_factory, recording_id)
    assert [job.kind for job in jobs] == [
        JobKind.FULL,
        JobKind.ANALYSIS,
        JobKind.TRANSLATION,
    ]
    assert all(job.status is JobStatus.COMPLETED for job in jobs)
    assert queue.claim_next(now=BASE_TIME + timedelta(seconds=10)) is None


def test_analysis_giving_up_still_leaves_the_translation_queued(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id = _uploaded(session_factory, max_attempts=1)
    queue = _queue(session_factory)

    claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=1))
    assert claim is not None
    queue.complete(claim, now=BASE_TIME + timedelta(seconds=2))

    analysis_claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=3))
    assert analysis_claim is not None and analysis_claim.kind is JobKind.ANALYSIS
    retried = queue.fail(
        analysis_claim,
        JobFailure(
            code="lmstudio_schema_invalid",
            error_type="PermanentProcessingError",
            message="LM Studio returned an invalid structured analysis.",
            retryable=False,
        ),
        now=BASE_TIME + timedelta(seconds=4),
    )

    assert retried is False
    jobs = _jobs(session_factory, recording_id)
    # Analysis and translation are independent readings of the same transcript.
    assert [job.kind for job in jobs] == [
        JobKind.FULL,
        JobKind.ANALYSIS,
        JobKind.TRANSLATION,
    ]
    assert jobs[1].status is JobStatus.FAILED
    assert jobs[2].status is JobStatus.QUEUED


def test_a_retrying_analysis_does_not_queue_the_translation_early(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id = _uploaded(session_factory)
    queue = _queue(session_factory)

    claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=1))
    assert claim is not None
    queue.complete(claim, now=BASE_TIME + timedelta(seconds=2))

    analysis_claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=3))
    assert analysis_claim is not None
    retried = queue.fail(
        analysis_claim,
        JobFailure(
            code="lmstudio_unavailable",
            error_type="RetryableProcessingError",
            message="LM Studio is temporarily unavailable.",
            retryable=True,
        ),
        now=BASE_TIME + timedelta(seconds=4),
    )

    assert retried is True
    jobs = _jobs(session_factory, recording_id)
    # The analysis is coming back, so the recording still holds exactly one
    # active job and the chain continues from the attempt that succeeds.
    assert [job.kind for job in jobs] == [JobKind.FULL, JobKind.ANALYSIS]
    assert jobs[1].status is JobStatus.QUEUED
    assert jobs[1].follow_up_kind is JobKind.TRANSLATION


def test_a_failed_transcription_queues_no_analysis(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id = _uploaded(session_factory, max_attempts=1)
    queue = _queue(session_factory)

    claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=1))
    assert claim is not None
    queue.fail(
        claim,
        JobFailure(
            code="transcription_failed",
            error_type="RetryableProcessingError",
            message="The transcribing stage failed.",
            retryable=True,
        ),
        now=BASE_TIME + timedelta(seconds=2),
    )

    # There is no transcript to read, so nothing is queued to read one.
    assert [job.kind for job in _jobs(session_factory, recording_id)] == [JobKind.FULL]


def test_an_analysis_asked_for_by_hand_does_not_drag_translation_behind_it(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id = _uploaded(session_factory, kind=JobKind.ANALYSIS)
    queue = _queue(session_factory)

    claim = queue.claim_next(now=BASE_TIME + timedelta(seconds=1))
    assert claim is not None
    queue.complete(claim, now=BASE_TIME + timedelta(seconds=2))

    assert [job.kind for job in _jobs(session_factory, recording_id)] == [JobKind.ANALYSIS]


def test_a_skipped_run_keeps_the_analysis_the_recording_already_has(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id = _uploaded(session_factory, kind=JobKind.ANALYSIS)
    stored = {"description": {"ja": "説明", "zh_hk": "說明"}}
    with session_factory.begin() as session:
        earlier_job_id = session.scalar(
            select(ProcessingJob.id).where(ProcessingJob.recording_id == recording_id)
        )
        assert earlier_job_id is not None
        session.add(
            Analysis(
                recording_id=recording_id,
                job_id=earlier_job_id,
                provider="lmstudio",
                status=AnalysisStatus.COMPLETED,
                result=stored,
                completed_at=BASE_TIME,
            )
        )

    # A later run over the same recording, with the LLM turned off.
    persist = _analysis_result_persister(
        _analysis_claim(earlier_job_id, recording_id),
        AnalysisResult(status=PipelineAnalysisStatus.SKIPPED, provider="disabled"),
    )
    with session_factory.begin() as session:
        persist(session)

    with session_factory() as session:
        analysis = session.scalar(
            select(Analysis).where(Analysis.recording_id == recording_id)
        )
        recording = session.get(Recording, recording_id)
    assert analysis is not None and recording is not None
    # A run that produced nothing must never take away the reading that is
    # already on the recording.
    assert analysis.status is AnalysisStatus.COMPLETED
    assert analysis.result == stored
    assert recording.analysis_revision == 0


def test_a_fresh_transcript_flags_the_old_analysis_instead_of_erasing_it(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id = _uploaded(session_factory)
    stored = {"description": {"ja": "説明", "zh_hk": "說明"}}
    with session_factory.begin() as session:
        job_id = session.scalar(
            select(ProcessingJob.id).where(ProcessingJob.recording_id == recording_id)
        )
        assert job_id is not None
        session.add(
            Analysis(
                recording_id=recording_id,
                job_id=job_id,
                provider="lmstudio",
                status=AnalysisStatus.COMPLETED,
                result=stored,
                completed_at=BASE_TIME,
            )
        )

    persist = _result_persister(
        _analysis_claim(job_id, recording_id),
        PipelineResult(
            recording_id=str(recording_id),
            audio=AudioProbe(
                duration_seconds=1,
                codec_name="pcm_s16le",
                format_name="wav",
                sample_rate=16_000,
                channels=1,
                mime_type="audio/wav",
                preferred_extension=".wav",
            ),
            transcript=(
                MergedTranscriptSegment(
                    sequence=0,
                    speaker_label="SPEAKER_00",
                    start=0,
                    end=1,
                    text="こんにちは。",
                ),
            ),
        ),
    )
    with session_factory.begin() as session:
        persist(session)

    with session_factory() as session:
        analysis = session.scalar(
            select(Analysis).where(Analysis.recording_id == recording_id)
        )
    assert analysis is not None
    # The reading describes words that have just been replaced, so it is
    # flagged for the queued analysis to supersede. Clearing it here would
    # leave the recording with nothing to read while the model works.
    assert analysis.status is AnalysisStatus.STALE
    assert analysis.result == stored
