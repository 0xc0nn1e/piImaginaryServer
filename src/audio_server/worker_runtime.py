from __future__ import annotations

import logging
import shutil
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

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
    TranscriptTranslation,
    TranslationSource,
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
    DisabledDailySummaryProvider,
    DisabledTranslationProvider,
    LMStudioAnalysisProvider,
    LMStudioSettings,
)
from audio_server.processing.audio import AudioProcessor, FFmpegSettings
from audio_server.processing.contracts import (
    AnalysisProvider,
    AnalysisResult,
    DailySummaryProvider,
    DiarizationProvider,
    MergedTranscriptSegment,
    PipelineResult,
    TranslationProvider,
    TranslationResult,
)
from audio_server.processing.contracts import (
    AnalysisStatus as PipelineAnalysisStatus,
)
from audio_server.processing.daily_summary import LMStudioDailySummaryProvider
from audio_server.processing.diarization import (
    DisabledDiarizationProvider,
    PyannoteDiarizationProvider,
    PyannoteSettings,
)
from audio_server.processing.errors import PermanentProcessingError, ProviderConfigurationError
from audio_server.processing.pipeline import ProcessingPipeline
from audio_server.processing.sentences import TranscriptSentence, group_sentences
from audio_server.processing.transcription import FasterWhisperProvider, FasterWhisperSettings
from audio_server.processing.translation import LMStudioTranslationProvider
from audio_server.services.daily_service import DailyService
from audio_server.services.storage import LocalStorageBackend, StorageBackend

logger = logging.getLogger(__name__)


def _recording_scope(claim: ClaimedJob) -> uuid.UUID:
    """Return the recording a job works on.

    Recording work always names its recording. Only a day summary leaves the
    column empty, and it is dispatched before any of these paths are reached,
    so an empty one here means a corrupt job rather than a day summary.
    """

    if claim.recording_id is None:
        raise PermanentProcessingError(
            code="recording_scope_missing",
            safe_message="The processing job does not name a recording.",
        )
    return claim.recording_id


class PipelineJobProcessor:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        storage: StorageBackend,
        pipeline: ProcessingPipeline | None = None,
        analysis_provider: AnalysisProvider | None = None,
        translation_provider: TranslationProvider | None = None,
        daily_summary_provider: DailySummaryProvider | None = None,
        daily_service: DailyService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage
        self._pipeline = pipeline
        self._analysis_provider = analysis_provider or DisabledAnalysisProvider()
        self._translation_provider = translation_provider or DisabledTranslationProvider()
        self._daily_summary_provider = daily_summary_provider or DisabledDailySummaryProvider()
        self._daily_service = daily_service

    def __call__(self, claim: ClaimedJob, progress: ProgressCallback) -> ResultPersister:
        if claim.kind is JobKind.ANALYSIS:
            return self._process_analysis(claim)
        if claim.kind is JobKind.TRANSLATION:
            return self._process_translation(claim)
        if claim.kind is JobKind.DAILY_SUMMARY:
            return self._process_daily_summary(claim)
        if self._pipeline is None:
            # claim_next is filtered by kind, so this only fires if a worker is
            # misconfigured; fail loudly rather than silently dropping the job.
            raise PermanentProcessingError(
                code="transcription_worker_unavailable",
                safe_message="This worker does not process transcription jobs.",
            )
        recording_id = _recording_scope(claim)
        pipeline = self._pipeline
        with self._session_factory() as session:
            recording = session.scalar(select(Recording).where(Recording.id == recording_id))
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
                    recording_id=recording_id,
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
                        "recording_id": str(recording_id),
                        "job_id": str(claim.id),
                        "error_type": type(exc).__name__,
                    },
                )

        return _result_persister(claim, result)

    def _process_translation(self, claim: ClaimedJob) -> ResultPersister:
        """Read the committed transcript only; never touch audio."""

        recording_id = _recording_scope(claim)
        segments = self._load_transcript(claim)
        result = self._translation_provider.translate(str(recording_id), segments)
        if result.status is PipelineAnalysisStatus.FAILED:
            raise PermanentProcessingError(
                code=result.error_code or "translation_failed",
                safe_message=result.error_message or "Transcript translation failed.",
            )
        return _translation_result_persister(claim, result)

    def _load_transcript(self, claim: ClaimedJob) -> tuple[MergedTranscriptSegment, ...]:
        """Read the committed transcript for a job that must not touch audio."""

        recording_id = _recording_scope(claim)
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.recording_id == recording_id)
                    .order_by(TranscriptSegment.sequence)
                )
            )
        return tuple(
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

    def _process_daily_summary(self, claim: ClaimedJob) -> ResultPersister:
        """Summarise a day from committed analyses; never reads audio."""

        day = claim.summary_date
        service = self._daily_service
        if day is None:
            raise PermanentProcessingError(
                code="daily_summary_scope_missing",
                safe_message="The day summary job does not name a day.",
            )
        if service is None:
            # claim_next filters by kind, so this only fires on a misconfigured
            # worker; fail loudly rather than silently dropping the job.
            raise PermanentProcessingError(
                code="daily_summary_worker_unavailable",
                safe_message="This worker does not process day summary jobs.",
            )
        digests, revisions = service.collect_digests(day)
        result = self._daily_summary_provider.summarize(day.isoformat(), digests)
        if result.status is PipelineAnalysisStatus.FAILED:
            raise PermanentProcessingError(
                code=result.error_code or "daily_summary_failed",
                safe_message=result.error_message or "The day summary could not be produced.",
            )
        return _daily_summary_persister(service, day, result, revisions)

    def _process_analysis(self, claim: ClaimedJob) -> ResultPersister:
        recording_id = _recording_scope(claim)
        transcript = self._load_transcript(claim)
        result = self._analysis_provider.analyze(str(recording_id), transcript)
        if result.status is PipelineAnalysisStatus.FAILED:
            raise PermanentProcessingError(
                code=result.error_code or "analysis_failed",
                safe_message=result.error_message or "Transcript analysis failed.",
            )
        return _analysis_result_persister(claim, result)


def _result_persister(claim: ClaimedJob, result: PipelineResult) -> ResultPersister:
    recording_id = _recording_scope(claim)

    def persist(session: Session) -> None:
        recording = session.get(Recording, recording_id, with_for_update=True)
        if recording is None:
            raise PermanentProcessingError(
                code="recording_missing", safe_message="The recording metadata is unavailable."
            )
        # Deleting the segments cascades their translations away, so the
        # administrator's own writing is captured first.
        preserved = _capture_manual_translations(session, recording_id)
        session.execute(
            delete(TranscriptTranslation).where(
                TranscriptTranslation.recording_id == recording_id
            )
        )
        session.execute(
            delete(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)
        )
        session.add_all(
            [
                TranscriptSegment(
                    recording_id=recording_id,
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
        # The words the last analysis was written from have just been replaced.
        # The analysis job queued behind this one writes the replacement; until
        # it commits, the previous reading is flagged rather than cleared, so a
        # recording is never left with nothing while the model works.
        analysis = session.scalar(
            select(Analysis).where(Analysis.recording_id == recording_id).with_for_update()
        )
        if analysis is not None and analysis.status is AnalysisStatus.COMPLETED:
            analysis.status = AnalysisStatus.STALE
            recording.analysis_revision += 1
        _carry_manual_translations(
            session, recording_id, result.transcript, preserved
        )
        # Machine translations belonged to the segments just deleted; the
        # translation job at the end of the chain writes them again.
        recording.translation_revision += 1

    return persist


# Whisper's segment boundaries move a little between runs over the same audio,
# so a match is made within a small window rather than on an exact timestamp.
_SAME_UTTERANCE_SECONDS = 0.75


def _count_within(moments: Sequence[float], start_time: float) -> int:
    return sum(1 for moment in moments if abs(moment - start_time) <= _SAME_UTTERANCE_SECONDS)


def _unique_match(
    sentences: Sequence[TranscriptSentence], start_time: float
) -> TranscriptSentence | None:
    """Return the one sentence at this moment, or nothing if it is ambiguous.

    Picking the nearest of several candidates would let the order rows happen to
    be read in decide which line a hand-written translation lands under. Losing a
    translation is visible and can be rewritten; silently attaching it to the
    wrong sentence is neither.
    """

    within = [
        sentence
        for sentence in sentences
        if abs(sentence.start_time - start_time) <= _SAME_UTTERANCE_SECONDS
    ]
    return within[0] if len(within) == 1 else None


def _capture_manual_translations(
    session: Session, recording_id: uuid.UUID
) -> list[tuple[str, float | None, str]]:
    """Record each hand-written rendering as (sentence, when it was said, text).

    Re-transcription replaces segment ids and renumbers sequences, and it also
    changes how many times a short line is recognised -- adding or dropping an
    interjection is exactly why someone reprocesses. Neither ids nor ordinals
    survive that. The audio does: the same words spoken at the same moment are
    the same utterance.
    """

    rows = [
        row
        for row in session.scalars(
            select(TranscriptTranslation).where(
                TranscriptTranslation.recording_id == recording_id
            )
        )
        if row.source is TranslationSource.MANUAL
    ]
    if not rows:
        return []
    old_transcript = _read_transcript(session, recording_id)
    start_time_of_sentence = {
        sentence.start_sequence: sentence.start_time
        for sentence in group_sentences(old_transcript)
    }
    sequence_of_segment = {
        segment_id: sequence
        for segment_id, sequence in session.execute(
            select(TranscriptSegment.id, TranscriptSegment.sequence).where(
                TranscriptSegment.recording_id == recording_id
            )
        )
    }
    captured: list[tuple[str, float | None, str]] = []
    for row in rows:
        if row.start_segment_id is None:
            # Already detached by an earlier pass. It has no moment to match on,
            # so it is carried through untouched rather than dropped, which is
            # what skipping it here would silently do.
            captured.append((row.source_ja, None, row.text_zh_hk))
            continue
        sequence = sequence_of_segment.get(row.start_segment_id)
        start_time = start_time_of_sentence.get(sequence) if sequence is not None else None
        # A row that no longer starts a sentence has no establishable moment, so
        # it travels as detached rather than being matched by guesswork.
        captured.append((row.source_ja, start_time, row.text_zh_hk))
    return captured


def _read_transcript(
    session: Session, recording_id: uuid.UUID
) -> tuple[MergedTranscriptSegment, ...]:
    return tuple(
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
        for row in session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.recording_id == recording_id)
            .order_by(TranscriptSegment.sequence)
        )
    )


def _carry_manual_translations(
    session: Session,
    recording_id: uuid.UUID,
    transcript: Sequence[MergedTranscriptSegment],
    preserved: Sequence[tuple[str, float | None, str]],
) -> None:
    """Re-attach hand-written translations to a freshly written transcript.

    Re-transcription replaces every segment, so the rows that referenced the old
    ones are gone. A rendering whose Japanese sentence came back word for word
    still applies and is restored; one whose words changed does not, and is not
    resurrected against text it was never written for.
    """

    if not preserved:
        return
    session.flush()
    by_sequence = {
        sequence: segment_id
        for segment_id, sequence in session.execute(
            select(TranscriptSegment.id, TranscriptSegment.sequence).where(
                TranscriptSegment.recording_id == recording_id
            )
        )
    }
    # The same short line is said over and over, so identical text is not an
    # identity. The audio is unchanged by reprocessing, so a rendering is
    # restored onto the sentence with the same words at the same moment -- and
    # only when that moment identifies exactly one sentence.
    candidates: dict[str, list[TranscriptSentence]] = defaultdict(list)
    for sentence in group_sentences(transcript):
        candidates[sentence.text].append(sentence)
    written_at: dict[str, list[float]] = defaultdict(list)
    for source_ja, start_time, _text in preserved:
        if start_time is not None:
            written_at[source_ja].append(start_time)
    for source_ja, start_time, text_zh_hk in preserved:
        match = (
            _unique_match(candidates.get(source_ja, []), start_time)
            if start_time is not None
            else None
        )
        # The sentence has to point back at exactly this one rendering too. If a
        # new pass merges two lines the administrator translated separately,
        # both renderings see a single candidate and whichever row happened to
        # be read first would claim it.
        if match is not None and _count_within(written_at[source_ja], match.start_time) != 1:
            match = None
        start_id = by_sequence.get(match.start_sequence) if match else None
        end_id = by_sequence.get(match.end_sequence) if match else None
        attached = start_id is not None and end_id is not None
        session.add(
            TranscriptTranslation(
                recording_id=recording_id,
                start_segment_id=start_id if attached else None,
                end_segment_id=end_id if attached else None,
                source_ja=source_ja,
                text_zh_hk=text_zh_hk,
                source=TranslationSource.MANUAL,
                # A rendering that could not be placed is kept detached and
                # flagged. Dropping it would delete writing silently, which is
                # the one outcome the administrator cannot notice or undo.
                stale=not attached,
            )
        )


def _write_translations(
    session: Session, recording_id: uuid.UUID, result: TranslationResult | None
) -> None:
    """Store one Cantonese rendering per sentence, keyed by segment id.

    A failed or skipped run leaves whatever is already stored alone, and a
    hand-written translation is never replaced by a machine one.
    """

    if result is None or AnalysisStatus(result.status.value) is not AnalysisStatus.COMPLETED:
        return
    # Segments inserted in this transaction need ids before they can be linked.
    session.flush()
    by_sequence = {
        sequence: segment_id
        for segment_id, sequence in session.execute(
            select(TranscriptSegment.id, TranscriptSegment.sequence).where(
                TranscriptSegment.recording_id == recording_id
            )
        )
    }
    existing = {
        row.start_segment_id: row  # detached rows have no key and are left alone
        for row in session.scalars(
            select(TranscriptTranslation)
            .where(TranscriptTranslation.recording_id == recording_id)
            .with_for_update()
        )
    }
    for translation in result.translations:
        start_id = by_sequence.get(translation.start_sequence)
        end_id = by_sequence.get(translation.end_sequence)
        if start_id is None or end_id is None:
            # The transcript moved under this result; drop the orphan rather
            # than attach it to the wrong words.
            continue
        current = existing.get(start_id)
        if current is not None and current.source is TranslationSource.MANUAL:
            continue
        if current is None:
            session.add(
                TranscriptTranslation(
                    recording_id=recording_id,
                    start_segment_id=start_id,
                    end_segment_id=end_id,
                    source_ja=translation.source_ja,
                    text_zh_hk=translation.text_zh_hk,
                    source=TranslationSource.LLM,
                )
            )
            continue
        current.end_segment_id = end_id
        current.source_ja = translation.source_ja
        current.text_zh_hk = translation.text_zh_hk
        current.source = TranslationSource.LLM
        current.stale = False


def _translation_result_persister(
    claim: ClaimedJob, result: TranslationResult
) -> ResultPersister:
    recording_id = _recording_scope(claim)

    def persist(session: Session) -> None:
        recording = session.get(Recording, recording_id, with_for_update=True)
        if recording is None:
            raise PermanentProcessingError(
                code="recording_missing", safe_message="The recording metadata is unavailable."
            )
        _write_translations(session, recording_id, result)
        recording.translation_revision += 1

    return persist


def _analysis_result_persister(claim: ClaimedJob, result: AnalysisResult) -> ResultPersister:
    recording_id = _recording_scope(claim)

    def persist(session: Session) -> None:
        recording = session.get(Recording, recording_id, with_for_update=True)
        if recording is None:
            raise PermanentProcessingError(
                code="recording_missing", safe_message="The recording metadata is unavailable."
            )
        analysis = session.scalar(
            select(Analysis).where(Analysis.recording_id == recording_id).with_for_update()
        )
        status = AnalysisStatus(result.status.value)
        # A run that produced no reading must not erase the one the recording
        # already has. The job row carries why this attempt came back empty;
        # the last good analysis stands until a replacement succeeds.
        keeps_previous = analysis is not None and analysis.result is not None
        if status is not AnalysisStatus.COMPLETED and keeps_previous:
            return
        if analysis is None:
            analysis = Analysis(
                recording_id=recording_id,
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


def _daily_summary_persister(
    service: DailyService,
    day: date,
    result: AnalysisResult,
    revisions: Sequence[dict[str, Any]],
) -> ResultPersister:
    def persist(session: Session) -> None:
        if result.status is PipelineAnalysisStatus.SKIPPED:
            # Every analysis this day had has since gone. Completing without
            # writing keeps the last summary that did succeed.
            return
        # The day can change while the model works. A recording deleted in the
        # meantime must not reappear inside a summary written from it, so the
        # write only lands while the day still holds what it was built from.
        if not service.matches_revisions(session, day=day, revisions=revisions):
            raise PermanentProcessingError(
                code="daily_summary_day_changed",
                safe_message="The day changed while it was summarised; ask for it again.",
            )
        service.persist_summary(session, day=day, result=result, source_revisions=revisions)

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

    daily_service = DailyService(
        session_factory=database.session_factory,
        max_attempts=settings.processing_max_attempts,
    )

    def processor_factory() -> JobProcessor:
        analysis_provider = _build_analysis_provider(settings)
        translation_provider = _build_translation_provider(settings)
        daily_summary_provider = _build_daily_summary_provider(settings)
        if not transcribes:
            # An analysis-only worker must not pay for Whisper and pyannote:
            # loading them would cost gigabytes of memory it never uses.
            load = getattr(analysis_provider, "load", None)
            if callable(load):
                load()
            for provider in (analysis_provider, translation_provider, daily_summary_provider):
                load = getattr(provider, "load", None)
                if callable(load):
                    load()
            return PipelineJobProcessor(
                session_factory=database.session_factory,
                storage=storage,
                analysis_provider=analysis_provider,
                translation_provider=translation_provider,
                daily_summary_provider=daily_summary_provider,
                daily_service=daily_service,
            )
        pipeline = _build_pipeline(settings)
        pipeline.load_providers()
        return PipelineJobProcessor(
            session_factory=database.session_factory,
            storage=storage,
            pipeline=pipeline,
            analysis_provider=analysis_provider,
            translation_provider=translation_provider,
            daily_summary_provider=daily_summary_provider,
            daily_service=daily_service,
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


def _build_pipeline(settings: Settings) -> ProcessingPipeline:
    """Build the transcription half of the worker; the LLM half is queued."""

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
    )


def _build_translation_provider(settings: Settings) -> TranslationProvider:
    if not settings.llm_enabled:
        return DisabledTranslationProvider()
    if settings.llm_provider != "lmstudio":
        raise ProviderConfigurationError(
            code="translation_provider_invalid",
            safe_message="The configured LLM translation provider is unsupported.",
        )
    return LMStudioTranslationProvider(
        LMStudioSettings(
            host=settings.lm_studio_host,
            api_key=settings.lm_studio_api_key.get_secret_value(),
            timeout_seconds=settings.lm_studio_timeout_seconds,
            chunk_chars=settings.lm_studio_chunk_chars,
            max_tokens=settings.lm_studio_max_tokens,
        )
    )


def _build_daily_summary_provider(settings: Settings) -> DailySummaryProvider:
    if not settings.llm_enabled:
        return DisabledDailySummaryProvider()
    if settings.llm_provider != "lmstudio":
        raise ProviderConfigurationError(
            code="daily_summary_provider_invalid",
            safe_message="The configured LLM day summary provider is unsupported.",
        )
    return LMStudioDailySummaryProvider(
        LMStudioSettings(
            host=settings.lm_studio_host,
            api_key=settings.lm_studio_api_key.get_secret_value(),
            timeout_seconds=settings.lm_studio_timeout_seconds,
            chunk_chars=settings.lm_studio_chunk_chars,
            max_tokens=settings.lm_studio_max_tokens,
        )
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
