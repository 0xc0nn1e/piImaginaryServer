"""Lazy faster-whisper provider implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audio_server.processing.contracts import (
    ProcessingStage,
    TranscriptionResult,
    TranscriptionSegment,
    WordTiming,
)
from audio_server.processing.errors import (
    PermanentProcessingError,
    ProcessingError,
    ProviderConfigurationError,
    RetryableProcessingError,
)


@dataclass(frozen=True, slots=True)
class FasterWhisperSettings:
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str | None = None
    download_root: Path | None = None
    beam_size: int = 5
    cpu_threads: int = 0
    num_workers: int = 1
    vad_filter: bool = False

    def __post_init__(self) -> None:
        if not self.model.strip() or not self.device.strip() or not self.compute_type.strip():
            raise ValueError("Whisper model, device, and compute type are required")
        if self.beam_size < 1 or self.cpu_threads < 0 or self.num_workers < 1:
            raise ValueError("Whisper worker settings must be positive")


class FasterWhisperProvider:
    """Loads the model once per worker process, never in the API process."""

    def __init__(self, settings: FasterWhisperSettings | None = None) -> None:
        self._settings = settings or FasterWhisperSettings()
        self._model: Any | None = None

    @property
    def name(self) -> str:
        return "faster-whisper"

    def load(self) -> None:
        """Warm the model before a worker claims jobs."""

        self._get_model()

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        model = self._get_model()
        try:
            raw_segments, info = model.transcribe(
                str(audio_path),
                language=self._settings.language,
                beam_size=self._settings.beam_size,
                word_timestamps=True,
                vad_filter=self._settings.vad_filter,
            )
            language = _optional_text(getattr(info, "language", None))
            segments = tuple(
                _convert_segment(segment, language=language) for segment in raw_segments
            )
            language_probability = _optional_float(getattr(info, "language_probability", None))
            return TranscriptionResult(
                segments=segments,
                language=language,
                language_probability=language_probability,
            )
        except ProcessingError:
            raise
        except _InvalidProviderResult as exc:
            raise PermanentProcessingError(
                code="transcription_invalid_result",
                safe_message="The transcription provider returned invalid timestamps.",
                stage=ProcessingStage.TRANSCRIBING,
            ) from exc
        except Exception as exc:
            raise _runtime_failure(exc) from exc

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ProviderConfigurationError(
                code="transcription_dependency_missing",
                safe_message="The faster-whisper dependency is not installed.",
                stage=ProcessingStage.TRANSCRIBING,
            ) from exc

        options: dict[str, object] = {
            "device": self._settings.device,
            "compute_type": self._settings.compute_type,
            "cpu_threads": self._settings.cpu_threads,
            "num_workers": self._settings.num_workers,
        }
        if self._settings.download_root is not None:
            options["download_root"] = str(self._settings.download_root)
        try:
            self._model = WhisperModel(self._settings.model, **options)
        except Exception as exc:
            raise ProviderConfigurationError(
                code="transcription_model_load_failed",
                safe_message=(
                    "The Whisper model could not be loaded; check the model, device, "
                    "compute type, and model cache settings."
                ),
                stage=ProcessingStage.TRANSCRIBING,
            ) from exc
        return self._model


class _InvalidProviderResult(ValueError):
    pass


def _convert_segment(segment: Any, *, language: str | None) -> TranscriptionSegment:
    try:
        words = tuple(
            converted
            for raw_word in (getattr(segment, "words", None) or ())
            if (converted := _convert_word(raw_word)) is not None
        )
        return TranscriptionSegment(
            start=float(segment.start),
            end=float(segment.end),
            text=str(segment.text or ""),
            words=words,
            language=language,
            average_log_probability=_optional_float(getattr(segment, "avg_logprob", None)),
            no_speech_probability=_optional_float(getattr(segment, "no_speech_prob", None)),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise _InvalidProviderResult from exc


def _convert_word(word: Any) -> WordTiming | None:
    start = getattr(word, "start", None)
    end = getattr(word, "end", None)
    text = str(getattr(word, "word", "") or "")
    if start is None or end is None or not text:
        return None
    try:
        return WordTiming(
            start=float(start),
            end=float(end),
            text=text,
            probability=_optional_float(getattr(word, "probability", None)),
        )
    except (TypeError, ValueError) as exc:
        raise _InvalidProviderResult from exc


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _runtime_failure(exc: Exception) -> RetryableProcessingError:
    description = str(exc).lower()
    if "out of memory" in description or "cuda oom" in description:
        return RetryableProcessingError(
            code="gpu_out_of_memory",
            safe_message="The transcription device ran out of memory and may be retried.",
            stage=ProcessingStage.TRANSCRIBING,
        )
    return RetryableProcessingError(
        code="transcription_failed",
        safe_message="Transcription failed and may be retried.",
        stage=ProcessingStage.TRANSCRIBING,
    )
