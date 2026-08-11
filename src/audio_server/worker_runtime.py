from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from audio_server.core.config import Settings, get_settings
from audio_server.core.database import create_database
from audio_server.db.models import (
    Analysis,
    AnalysisStatus,
    Recording,
    TranscriptSegment,
)
from audio_server.jobs.queue import ClaimedJob, JobQueue, ResultPersister, RetryPolicy
from audio_server.jobs.worker import (
    JobProcessor,
    ProgressCallback,
    Worker,
    WorkerIntervals,
    make_worker_id,
)
from audio_server.processing.analysis import DisabledAnalysisProvider
from audio_server.processing.audio import AudioProcessor, FFmpegSettings
from audio_server.processing.contracts import DiarizationProvider, PipelineResult
from audio_server.processing.diarization import (
    DisabledDiarizationProvider,
    PyannoteDiarizationProvider,
    PyannoteSettings,
)
from audio_server.processing.errors import PermanentProcessingError, ProviderConfigurationError
from audio_server.processing.pipeline import ProcessingPipeline
from audio_server.processing.transcription import FasterWhisperProvider, FasterWhisperSettings
from audio_server.services.storage import LocalStorageBackend, StorageBackend

logger = logging.getLogger(__name__)


class PipelineJobProcessor:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        storage: StorageBackend,
        pipeline: ProcessingPipeline,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage
        self._pipeline = pipeline

    def __call__(self, claim: ClaimedJob, progress: ProgressCallback) -> ResultPersister:
        with self._session_factory() as session:
            recording = session.scalar(select(Recording).where(Recording.id == claim.recording_id))
            if recording is None:
                raise PermanentProcessingError(
                    code="recording_missing",
                    safe_message="The recording metadata is unavailable.",
                )
            storage_key = recording.storage_key

        work_dir = self._storage.work_directory(claim.id, claim.claim_token)
        try:
            with self._storage.materialize(storage_key) as source_path:
                result = self._pipeline.run(
                    recording_id=claim.recording_id,
                    source_path=source_path,
                    work_dir=work_dir,
                    stage_callback=progress,
                )
        except FileNotFoundError as exc:
            raise PermanentProcessingError(
                code="original_audio_missing",
                safe_message="The original audio file is unavailable.",
            ) from exc
        finally:
            try:
                shutil.rmtree(work_dir)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    "processing workspace cleanup failed",
                    extra={
                        "recording_id": str(claim.recording_id),
                        "job_id": str(claim.id),
                        "error_type": type(exc).__name__,
                    },
                )

        return _result_persister(claim, result)


def _result_persister(claim: ClaimedJob, result: PipelineResult) -> ResultPersister:
    def persist(session: Session) -> None:
        session.execute(
            delete(TranscriptSegment).where(TranscriptSegment.recording_id == claim.recording_id)
        )
        session.add_all(
            [
                TranscriptSegment(
                    recording_id=claim.recording_id,
                    job_id=claim.id,
                    sequence=segment.sequence,
                    speaker_label=segment.speaker_label,
                    start_time=segment.start,
                    end_time=segment.end,
                    text=segment.text,
                    language=segment.language,
                    confidence=segment.confidence,
                    has_overlap=segment.has_overlap,
                )
                for segment in result.transcript
            ]
        )
        analysis_status = AnalysisStatus(result.analysis.status.value)
        session.add(
            Analysis(
                recording_id=claim.recording_id,
                job_id=claim.id,
                provider=result.analysis.provider,
                model=result.analysis.model,
                schema_version=str(result.analysis.schema_version),
                status=analysis_status,
                result=dict(result.analysis.data) if result.analysis.data is not None else None,
                error_code=result.analysis.error_code,
                error_message=result.analysis.error_message,
                completed_at=datetime.now(UTC),
            )
        )

    return persist


def build_worker(worker_index: int) -> Worker:
    settings = get_settings()
    database = create_database(settings.database_url)
    storage = LocalStorageBackend(settings.storage_path)
    queue = JobQueue(
        database.session_factory,
        worker_id=make_worker_id(worker_index),
        lease_duration=timedelta(seconds=settings.job_lease_seconds),
        retry_policy=RetryPolicy(
            base_delay=timedelta(seconds=settings.retry_base_seconds),
            max_delay=timedelta(seconds=settings.retry_max_seconds),
        ),
    )

    def processor_factory() -> JobProcessor:
        pipeline = _build_pipeline(settings)
        pipeline.load_providers()
        return PipelineJobProcessor(
            session_factory=database.session_factory,
            storage=storage,
            pipeline=pipeline,
        )

    return Worker(
        queue,
        processor_factory,
        intervals=WorkerIntervals(
            poll_seconds=settings.job_poll_seconds,
            heartbeat_seconds=settings.job_heartbeat_seconds,
            recovery_seconds=settings.job_recovery_seconds,
        ),
    )


def _build_pipeline(settings: Settings) -> ProcessingPipeline:
    audio_processor = AudioProcessor(
        FFmpegSettings(
            ffmpeg_binary=settings.ffmpeg_binary,
            ffprobe_binary=settings.ffprobe_binary,
            conversion_timeout_seconds=settings.ffmpeg_timeout_seconds,
        )
    )
    transcription_provider = FasterWhisperProvider(
        FasterWhisperSettings(
            model=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            language=settings.whisper_language or None,
            download_root=settings.whisper_cache_dir,
            cpu_threads=settings.whisper_cpu_threads,
        )
    )
    if settings.diarization_enabled:
        token = settings.huggingface_token.get_secret_value() or None
        diarization_provider: DiarizationProvider = PyannoteDiarizationProvider(
            PyannoteSettings(
                model=settings.diarization_model,
                token=token,
                device=settings.diarization_device,
                disable_telemetry=True,
            )
        )
    else:
        diarization_provider = DisabledDiarizationProvider()

    if settings.llm_enabled:
        raise ProviderConfigurationError(
            code="analysis_provider_unavailable",
            safe_message="The configured LLM analysis provider is not implemented in this MVP.",
        )
    return ProcessingPipeline(
        audio_processor=audio_processor,
        transcription_provider=transcription_provider,
        diarization_provider=diarization_provider,
        analysis_provider=DisabledAnalysisProvider(),
    )
