"""Lazy pyannote.audio diarization providers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audio_server.processing.contracts import (
    DiarizationResult,
    ProcessingStage,
    SpeakerTurn,
)
from audio_server.processing.errors import (
    PermanentProcessingError,
    ProcessingError,
    ProviderConfigurationError,
    RetryableProcessingError,
)


@dataclass(frozen=True, slots=True)
class PyannoteSettings:
    model: str = "pyannote/speaker-diarization-community-1"
    token: str | None = None
    device: str = "cpu"
    disable_telemetry: bool = True

    def __post_init__(self) -> None:
        if not self.model.strip() or not self.device.strip():
            raise ValueError("pyannote model and device are required")


@dataclass(frozen=True, slots=True)
class _RawTurn:
    start: float
    end: float
    label: str


class PyannoteDiarizationProvider:
    """Returns exclusive turns for assignment and regular turns for overlap."""

    def __init__(self, settings: PyannoteSettings) -> None:
        self._settings = settings
        self._pipeline: Any | None = None

    @property
    def name(self) -> str:
        return "pyannote.audio"

    def load(self) -> None:
        """Warm the model before a worker claims jobs."""

        self._get_pipeline()

    def diarize(self, audio_path: Path) -> DiarizationResult:
        pipeline = self._get_pipeline()
        try:
            output = pipeline(str(audio_path))
            regular_annotation = getattr(output, "speaker_diarization", output)
            exclusive_annotation = getattr(output, "exclusive_speaker_diarization", None)
            regular_raw = tuple(_read_annotation(regular_annotation))
            exclusive_raw = (
                tuple(_read_annotation(exclusive_annotation))
                if exclusive_annotation is not None
                else regular_raw
            )
            labels = sorted(
                {turn.label for turn in regular_raw}.union(turn.label for turn in exclusive_raw)
            )
            label_map = {label: f"SPEAKER_{index:02d}" for index, label in enumerate(labels)}
            return DiarizationResult(
                exclusive_turns=_convert_turns(exclusive_raw, label_map),
                regular_turns=_convert_turns(regular_raw, label_map),
            )
        except ProcessingError:
            raise
        except _InvalidDiarizationResult as exc:
            raise PermanentProcessingError(
                code="diarization_invalid_result",
                safe_message="The diarization provider returned invalid timestamps.",
                stage=ProcessingStage.DIARIZING,
            ) from exc
        except Exception as exc:
            raise _runtime_failure(exc) from exc

    def _get_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        if (
            self._settings.model == "pyannote/speaker-diarization-community-1"
            and not (self._settings.token or "").strip()
        ):
            raise ProviderConfigurationError(
                code="diarization_token_missing",
                safe_message=(
                    "A Hugging Face token is required for the configured diarization model."
                ),
                stage=ProcessingStage.DIARIZING,
            )
        try:
            import torch
            from pyannote.audio import Pipeline

            if self._settings.disable_telemetry:
                from pyannote.audio.telemetry import set_telemetry_metrics

                set_telemetry_metrics(False)
        except ImportError as exc:
            raise ProviderConfigurationError(
                code="diarization_dependency_missing",
                safe_message="The pyannote.audio dependency is not installed.",
                stage=ProcessingStage.DIARIZING,
            ) from exc

        try:
            pipeline = Pipeline.from_pretrained(
                self._settings.model,
                token=self._settings.token,
            )
            if pipeline is None:
                raise RuntimeError("pipeline loader returned no pipeline")
            pipeline.to(torch.device(self._settings.device))
            self._pipeline = pipeline
        except Exception as exc:
            raise ProviderConfigurationError(
                code="diarization_model_load_failed",
                safe_message=(
                    "The diarization model could not be loaded; check model access, "
                    "device, and model cache settings."
                ),
                stage=ProcessingStage.DIARIZING,
            ) from exc
        return self._pipeline


class DisabledDiarizationProvider:
    """Explicit no-op used only when diarization is disabled by configuration."""

    @property
    def name(self) -> str:
        return "disabled"

    def diarize(self, audio_path: Path) -> DiarizationResult:
        del audio_path
        return DiarizationResult(exclusive_turns=(), regular_turns=())


class _InvalidDiarizationResult(ValueError):
    pass


def _read_annotation(annotation: Any) -> Iterable[_RawTurn]:
    try:
        if hasattr(annotation, "itertracks"):
            for segment, _track, label in annotation.itertracks(yield_label=True):
                yield _raw_turn(segment, label)
            return

        for item in annotation:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise _InvalidDiarizationResult
            segment, label = item
            yield _raw_turn(segment, label)
    except _InvalidDiarizationResult:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise _InvalidDiarizationResult from exc


def _raw_turn(segment: Any, label: Any) -> _RawTurn:
    try:
        raw = _RawTurn(
            start=float(segment.start),
            end=float(segment.end),
            label=str(label),
        )
        if raw.start < 0 or raw.end <= raw.start or not raw.label.strip():
            raise ValueError("invalid turn")
        return raw
    except (AttributeError, TypeError, ValueError) as exc:
        raise _InvalidDiarizationResult from exc


def _convert_turns(
    turns: tuple[_RawTurn, ...],
    label_map: dict[str, str],
) -> tuple[SpeakerTurn, ...]:
    converted = tuple(
        SpeakerTurn(
            start=turn.start,
            end=turn.end,
            speaker_label=label_map[turn.label],
        )
        for turn in turns
    )
    return tuple(
        sorted(
            converted,
            key=lambda turn: (turn.start, turn.end, turn.speaker_label),
        )
    )


def _runtime_failure(exc: Exception) -> RetryableProcessingError:
    description = str(exc).lower()
    if "out of memory" in description or "cuda oom" in description:
        return RetryableProcessingError(
            code="gpu_out_of_memory",
            safe_message="The diarization device ran out of memory and may be retried.",
            stage=ProcessingStage.DIARIZING,
        )
    return RetryableProcessingError(
        code="diarization_failed",
        safe_message="Speaker diarization failed and may be retried.",
        stage=ProcessingStage.DIARIZING,
    )
