"""DB-free orchestration for one claimed transcription job.

The pipeline stops at the committed transcript. Analysis and translation read
that transcript in jobs of their own, so an LM Studio failure costs a retry on
its own budget instead of a result the transcription job would have thrown
away on its way to reporting success.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from audio_server.processing.contracts import (
    AudioPreprocessor,
    AudioProbe,
    DiarizationProvider,
    PipelineResult,
    ProcessingStage,
    RecordingIdentifier,
    StageCallback,
    TranscriptionProvider,
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
        merger: TimestampTranscriptMerger | None = None,
    ) -> None:
        self._audio_processor = audio_processor
        self._transcription_provider = transcription_provider
        self._diarization_provider = diarization_provider
        self._merger = merger or TimestampTranscriptMerger()

    def load_providers(self) -> None:
        """Warm optional heavy providers before the worker claims a job."""

        for provider in (self._transcription_provider, self._diarization_provider):
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

        _notify(stage_callback, ProcessingStage.COMPLETED)
        return PipelineResult(
            recording_id=str(recording_id),
            audio=audio_probe,
            transcript=transcript,
            transcription_language=transcription.language,
            transcription_language_probability=transcription.language_probability,
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
