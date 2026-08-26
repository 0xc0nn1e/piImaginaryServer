"""DB-free orchestration for one claimed processing job."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from audio_server.processing.analysis import (
    DisabledAnalysisProvider,
    DisabledTranslationProvider,
)
from audio_server.processing.contracts import (
    AnalysisProvider,
    AnalysisResult,
    AnalysisStatus,
    AudioPreprocessor,
    AudioProbe,
    DiarizationProvider,
    MergedTranscriptSegment,
    PipelineResult,
    ProcessingStage,
    RecordingIdentifier,
    StageCallback,
    TranscriptionProvider,
    TranslationProvider,
    TranslationResult,
)
from audio_server.processing.errors import (
    PermanentProcessingError,
    ProcessingError,
    RetryableProcessingError,
)
from audio_server.processing.merge import TimestampTranscriptMerger

ResultT = TypeVar("ResultT")


class ProcessingPipeline:
    """Runs providers synchronously inside a dedicated worker process.

    Persistence and lease fencing remain the worker's responsibility. A stage
    callback should extend/check the job lease before each expensive operation.
    """

    def __init__(
        self,
        *,
        audio_processor: AudioPreprocessor,
        transcription_provider: TranscriptionProvider,
        diarization_provider: DiarizationProvider,
        analysis_provider: AnalysisProvider | None = None,
        translation_provider: TranslationProvider | None = None,
        merger: TimestampTranscriptMerger | None = None,
    ) -> None:
        self._audio_processor = audio_processor
        self._transcription_provider = transcription_provider
        self._diarization_provider = diarization_provider
        self._analysis_provider = analysis_provider or DisabledAnalysisProvider()
        self._translation_provider = translation_provider or DisabledTranslationProvider()
        self._merger = merger or TimestampTranscriptMerger()

    def load_providers(self) -> None:
        """Warm optional heavy providers before the worker claims a job."""

        for provider in (
            self._transcription_provider,
            self._diarization_provider,
            self._translation_provider,
            self._analysis_provider,
        ):
            load = getattr(provider, "load", None)
            if callable(load):
                load()

    def run(
        self,
        *,
        recording_id: RecordingIdentifier,
        source_path: Path,
        work_dir: Path,
        stage_callback: StageCallback | None = None,
    ) -> PipelineResult:
        normalized_path = work_dir / "processing.wav"

        _notify(stage_callback, ProcessingStage.PREPROCESSING)

        def preprocess() -> AudioProbe:
            work_dir.mkdir(parents=True, exist_ok=True)
            probe = self._audio_processor.probe(source_path)
            self._audio_processor.normalize(source_path, normalized_path)
            return probe

        audio_probe = _execute_stage(
            ProcessingStage.PREPROCESSING,
            preprocess,
            retryable_unknown=True,
        )

        _notify(stage_callback, ProcessingStage.TRANSCRIBING)
        transcription = _execute_stage(
            ProcessingStage.TRANSCRIBING,
            lambda: self._transcription_provider.transcribe(normalized_path),
            retryable_unknown=True,
        )

        _notify(stage_callback, ProcessingStage.DIARIZING)
        diarization = _execute_stage(
            ProcessingStage.DIARIZING,
            lambda: self._diarization_provider.diarize(normalized_path),
            retryable_unknown=True,
        )

        _notify(stage_callback, ProcessingStage.MERGING)
        transcript = _execute_stage(
            ProcessingStage.MERGING,
            lambda: self._merger.merge(transcription, diarization),
            retryable_unknown=False,
        )

        _notify(stage_callback, ProcessingStage.TRANSLATING)
        translation = self._translate_without_breaking_transcript(str(recording_id), transcript)

        _notify(stage_callback, ProcessingStage.ANALYZING)
        analysis = self._analyze_without_breaking_transcript(str(recording_id), transcript)

        _notify(stage_callback, ProcessingStage.COMPLETED)
        return PipelineResult(
            recording_id=str(recording_id),
            audio=audio_probe,
            transcript=transcript,
            analysis=analysis,
            translation=translation,
            transcription_language=transcription.language,
            transcription_language_probability=transcription.language_probability,
        )

    def _translate_without_breaking_transcript(
        self,
        recording_id: str,
        transcript: tuple[MergedTranscriptSegment, ...],
    ) -> TranslationResult:
        """Translation is optional, so a failure must never cost the transcript."""

        try:
            return self._translation_provider.translate(recording_id, transcript)
        except ProcessingError as exc:
            return TranslationResult(
                status=AnalysisStatus.FAILED,
                provider=self._translation_provider.name,
                error_code=exc.code,
                error_message=exc.safe_message,
            )
        except Exception:
            return TranslationResult(
                status=AnalysisStatus.FAILED,
                provider=self._translation_provider.name,
                error_code="translation_failed",
                error_message="Transcript translation failed.",
            )

    def _analyze_without_breaking_transcript(
        self,
        recording_id: str,
        transcript: tuple[MergedTranscriptSegment, ...],
    ) -> AnalysisResult:
        try:
            return self._analysis_provider.analyze(recording_id, transcript)
        except ProcessingError as exc:
            return AnalysisResult(
                status=AnalysisStatus.FAILED,
                provider=self._analysis_provider.name,
                error_code=exc.code,
                error_message=exc.safe_message,
            )
        except Exception:
            return AnalysisResult(
                status=AnalysisStatus.FAILED,
                provider=self._analysis_provider.name,
                error_code="analysis_failed",
                error_message="Transcript analysis failed.",
            )


def _notify(
    callback: StageCallback | None,
    stage: ProcessingStage,
) -> None:
    if callback is not None:
        # Lease/fencing errors must reach the worker unchanged.
        callback(stage)


def _execute_stage(
    stage: ProcessingStage,
    operation: Callable[[], ResultT],
    *,
    retryable_unknown: bool,
) -> ResultT:
    try:
        return operation()
    except ProcessingError as exc:
        exc.at_stage(stage)
        raise
    except Exception as exc:
        error_code = {
            ProcessingStage.PREPROCESSING: "audio_preprocessing_failed",
            ProcessingStage.TRANSCRIBING: "transcription_failed",
            ProcessingStage.DIARIZING: "diarization_failed",
            ProcessingStage.MERGING: "merge_failed",
            ProcessingStage.ANALYZING: "analysis_failed",
            ProcessingStage.COMPLETED: "processing_completion_failed",
        }[stage]
        if retryable_unknown:
            raise RetryableProcessingError(
                code=error_code,
                safe_message=f"The {stage.value} stage failed.",
                stage=stage,
            ) from exc
        raise PermanentProcessingError(
            code=error_code,
            safe_message=f"The {stage.value} stage failed.",
            stage=stage,
        ) from exc
