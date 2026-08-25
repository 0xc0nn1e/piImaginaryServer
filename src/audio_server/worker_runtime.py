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
    JobKind,
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
from audio_server.processing.analysis import (
    DisabledAnalysisProvider,
    LMStudioAnalysisProvider,
    LMStudioSettings,
)
from audio_server.processing.audio import AudioProcessor, FFmpegSettings
from audio_server.processing.contracts import (
    AnalysisProvider,
    AnalysisResult,
    DiarizationProvider,
    MergedTranscriptSegment,
    PipelineResult,
)
from audio_server.processing.contracts import (
    AnalysisStatus as PipelineAnalysisStatus,
)
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
        pipeline: ProcessingPipeline | None = None,
        analysis_provider: AnalysisProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage
        self._pipeline = pipeline
        self._analysis_provider = analysis_provider or DisabledAnalysisProvider()

    def __call__(self, claim: ClaimedJob, progress: ProgressCallback) -> ResultPersister:
        if claim.kind is JobKind.ANALYSIS:
            return self._process_analysis(claim)
        if self._pipeline is None:
            # claim_next is filtered by kind, so this only fires if a worker is
            # misconfigured; fail loudly rather than silently dropping the job.
            raise PermanentProcessingError(
                code="transcription_worker_unavailable",
                safe_message="This worker does not process transcription jobs.",
            )
        pipeline = self._pipeline
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
                result = pipeline.run(
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

    def _process_analysis(self, claim: ClaimedJob) -> ResultPersister:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.recording_id == claim.recording_id)
                    .order_by(TranscriptSegment.sequence)
                )
            )
        transcript = tuple(
            MergedTranscriptSegment(
                sequence=row.sequence,
                speaker_label=row.speaker_label,
                start=row.start_time,
                end=row.end_time,
                text=row.text,
                language=row.language,
                confidence=row.confidence,
                has_overlap=row.has_overlap,
            )
            for row in rows
        )
        result = self._analysis_provider.analyze(str(claim.recording_id), transcript)
        if result.status is PipelineAnalysisStatus.FAILED:
            raise PermanentProcessingError(
                code=result.error_code or "analysis_failed",
                safe_message=result.error_message or "Transcript analysis failed.",
            )
        return _analysis_result_persister(claim, result)


def _result_persister(claim: ClaimedJob, result: PipelineResult) -> ResultPersister:
    def persist(session: Session) -> None:
        recording = session.get(Recording, claim.recording_id, with_for_update=True)
        if recording is None:
            raise PermanentProcessingError(
                code="recording_missing", safe_message="The recording metadata is unavailable."
            )
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
        recording.transcript_revision += 1
        analysis_status = AnalysisStatus(result.analysis.status.value)
        analysis = session.scalar(
            select(Analysis).where(Analysis.recording_id == claim.recording_id).with_for_update()
        )
        if analysis is None:
            analysis = Analysis(
                recording_id=claim.recording_id,
                job_id=claim.id,
                provider=result.analysis.provider,
                status=analysis_status,
            )
            session.add(analysis)
        analysis.job_id = claim.id
        analysis.provider = result.analysis.provider
        analysis.model = result.analysis.model
        analysis.schema_version = str(result.analysis.schema_version)
        analysis.status = analysis_status
        analysis.result = dict(result.analysis.data) if result.analysis.data is not None else None
        analysis.error_code = result.analysis.error_code
        analysis.error_message = result.analysis.error_message
        analysis.completed_at = datetime.now(UTC)
        recording.analysis_revision += 1

    return persist


def _analysis_result_persister(claim: ClaimedJob, result: AnalysisResult) -> ResultPersister:
    def persist(session: Session) -> None:
        recording = session.get(Recording, claim.recording_id, with_for_update=True)
        if recording is None:
            raise PermanentProcessingError(
                code="recording_missing", safe_message="The recording metadata is unavailable."
            )
        analysis = session.scalar(
            select(Analysis).where(Analysis.recording_id == claim.recording_id).with_for_update()
        )
        status = AnalysisStatus(result.status.value)
        if analysis is None:
            analysis = Analysis(
                recording_id=claim.recording_id,
                job_id=claim.id,
                provider=result.provider,
                status=status,
            )
            session.add(analysis)
        analysis.job_id = claim.id
        analysis.provider = result.provider
        analysis.model = result.model
        analysis.schema_version = str(result.schema_version)
        analysis.status = status
        analysis.result = dict(result.data) if result.data is not None else None
        analysis.error_code = result.error_code
        analysis.error_message = result.error_message
        analysis.completed_at = datetime.now(UTC)
        recording.analysis_revision += 1

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

    job_kinds = _parse_job_kinds(settings.worker_job_kinds)
    transcribes = job_kinds is None or JobKind.FULL in job_kinds

    def processor_factory() -> JobProcessor:
        analysis_provider = _build_analysis_provider(settings)
        if not transcribes:
            # An analysis-only worker must not pay for Whisper and pyannote:
            # loading them would cost gigabytes of memory it never uses.
            load = getattr(analysis_provider, "load", None)
            if callable(load):
                load()
            return PipelineJobProcessor(
                session_factory=database.session_factory,
                storage=storage,
                analysis_provider=analysis_provider,
            )
        pipeline = _build_pipeline(settings, analysis_provider=analysis_provider)
        pipeline.load_providers()
        return PipelineJobProcessor(
            session_factory=database.session_factory,
            storage=storage,
            pipeline=pipeline,
            analysis_provider=analysis_provider,
        )

    return Worker(
        queue,
        processor_factory,
        intervals=WorkerIntervals(
            poll_seconds=settings.job_poll_seconds,
            heartbeat_seconds=settings.job_heartbeat_seconds,
            recovery_seconds=settings.job_recovery_seconds,
        ),
        job_kinds=job_kinds,
    )


def _parse_job_kinds(value: str) -> frozenset[JobKind] | None:
    """Return the job kinds this worker may claim, or None for every kind."""

    kinds = frozenset(JobKind(entry) for entry in value.split(",") if entry)
    return kinds or None


def _build_pipeline(
    settings: Settings, *, analysis_provider: AnalysisProvider | None = None
) -> ProcessingPipeline:
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

    return ProcessingPipeline(
        audio_processor=audio_processor,
        transcription_provider=transcription_provider,
        diarization_provider=diarization_provider,
        analysis_provider=analysis_provider or _build_analysis_provider(settings),
    )


def _build_analysis_provider(settings: Settings) -> AnalysisProvider:
    if not settings.llm_enabled:
        return DisabledAnalysisProvider()
    if settings.llm_provider != "lmstudio":
        raise ProviderConfigurationError(
            code="analysis_provider_invalid",
            safe_message="The configured LLM analysis provider is unsupported.",
        )
    return LMStudioAnalysisProvider(
        LMStudioSettings(
            host=settings.lm_studio_host,
            api_key=settings.lm_studio_api_key.get_secret_value(),
            timeout_seconds=settings.lm_studio_timeout_seconds,
            chunk_chars=settings.lm_studio_chunk_chars,
            max_tokens=settings.lm_studio_max_tokens,
        )
    )
