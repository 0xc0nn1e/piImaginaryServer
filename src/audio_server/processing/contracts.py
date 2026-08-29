"""Typed, provider-neutral values used by the processing pipeline."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID


class ProcessingStage(StrEnum):
    """Durable processing stages exposed to the job worker."""

    PREPROCESSING = "preprocessing"
    TRANSCRIBING = "transcribing"
    DIARIZING = "diarizing"
    MERGING = "merging"
    TRANSLATING = "translating"
    ANALYZING = "analyzing"
    COMPLETED = "completed"


class AnalysisStatus(StrEnum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


def _validate_interval(start: float, end: float, *, allow_empty: bool = False) -> None:
    if not math.isfinite(start) or not math.isfinite(end):
        raise ValueError("timestamps must be finite")
    if start < 0:
        raise ValueError("timestamps cannot be negative")
    if end < start or (not allow_empty and end == start):
        raise ValueError("end timestamp must be after start timestamp")


def _validate_probability(value: float | None) -> None:
    if value is not None and (not math.isfinite(value) or value < 0 or value > 1):
        raise ValueError("probability must be between zero and one")


@dataclass(frozen=True, slots=True)
class AudioProbe:
    duration_seconds: float
    codec_name: str
    format_name: str
    sample_rate: int
    channels: int
    mime_type: str
    preferred_extension: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0:
            raise ValueError("audio duration must be positive and finite")
        if self.sample_rate <= 0 or self.channels <= 0:
            raise ValueError("sample rate and channel count must be positive")
        if not self.codec_name or not self.format_name:
            raise ValueError("audio codec and format are required")
        if not self.preferred_extension.startswith("."):
            raise ValueError("preferred extension must start with a dot")


@dataclass(frozen=True, slots=True)
class WordTiming:
    start: float
    end: float
    text: str
    probability: float | None = None

    def __post_init__(self) -> None:
        _validate_interval(self.start, self.end, allow_empty=True)
        if not self.text:
            raise ValueError("word text cannot be empty")
        _validate_probability(self.probability)


@dataclass(frozen=True, slots=True)
class TranscriptionSegment:
    start: float
    end: float
    text: str
    words: tuple[WordTiming, ...] = ()
    language: str | None = None
    average_log_probability: float | None = None
    no_speech_probability: float | None = None

    def __post_init__(self) -> None:
        _validate_interval(self.start, self.end)
        if self.no_speech_probability is not None:
            _validate_probability(self.no_speech_probability)
        if self.average_log_probability is not None and not math.isfinite(
            self.average_log_probability
        ):
            raise ValueError("average log probability must be finite")


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    segments: tuple[TranscriptionSegment, ...]
    language: str | None = None
    language_probability: float | None = None

    def __post_init__(self) -> None:
        _validate_probability(self.language_probability)


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    start: float
    end: float
    speaker_label: str

    def __post_init__(self) -> None:
        _validate_interval(self.start, self.end)
        if not self.speaker_label.strip():
            raise ValueError("speaker label cannot be empty")


@dataclass(frozen=True, slots=True)
class DiarizationResult:
    """Both timelines returned by pyannote.

    Exclusive turns are used to choose one primary speaker. Regular turns retain
    simultaneous speech and are used to flag overlap in the merged transcript.
    """

    exclusive_turns: tuple[SpeakerTurn, ...]
    regular_turns: tuple[SpeakerTurn, ...] = ()

    @property
    def primary_turns(self) -> tuple[SpeakerTurn, ...]:
        return self.exclusive_turns or self.regular_turns


@dataclass(frozen=True, slots=True)
class MergedTranscriptSegment:
    sequence: int
    speaker_label: str
    start: float
    end: float
    text: str
    language: str | None = None
    confidence: float | None = None
    has_overlap: bool = False

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("sequence cannot be negative")
        _validate_interval(self.start, self.end, allow_empty=True)
        if not self.speaker_label.strip() or not self.text.strip():
            raise ValueError("merged speaker and text cannot be empty")
        _validate_probability(self.confidence)


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    status: AnalysisStatus
    provider: str
    model: str | None = None
    schema_version: int = 1
    data: Mapping[str, object] | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("analysis provider cannot be empty")
        if self.schema_version < 1:
            raise ValueError("analysis schema version must be positive")
        if self.status is AnalysisStatus.FAILED and not self.error_code:
            raise ValueError("failed analysis requires an error code")


@dataclass(frozen=True, slots=True)
class PipelineResult:
    recording_id: str
    audio: AudioProbe
    transcript: tuple[MergedTranscriptSegment, ...]
    analysis: AnalysisResult
    translation: TranslationResult | None = None
    transcription_language: str | None = None
    transcription_language_probability: float | None = None


class AudioPreprocessor(Protocol):
    def probe(self, source: Path) -> AudioProbe:
        """Validate and inspect an original audio file."""

    def normalize(self, source: Path, destination: Path) -> None:
        """Create a mono 16 kHz PCM WAV without changing the original."""


class TranscriptionProvider(Protocol):
    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        """Transcribe normalized audio and include word timestamps when possible."""


class DiarizationProvider(Protocol):
    def diarize(self, audio_path: Path) -> DiarizationResult:
        """Return anonymous speaker turns for normalized audio."""


@dataclass(frozen=True, slots=True)
class SentenceTranslation:
    """A Cantonese rendering tied to the segment span it came from."""

    start_sequence: int
    end_sequence: int
    source_ja: str
    text_zh_hk: str

    def __post_init__(self) -> None:
        if self.end_sequence < self.start_sequence:
            raise ValueError("translation end cannot precede its start")
        if not self.source_ja.strip():
            raise ValueError("translation source text cannot be empty")
        if not self.text_zh_hk.strip():
            raise ValueError("translation text cannot be empty")


@dataclass(frozen=True, slots=True)
class TranslationResult:
    status: AnalysisStatus
    provider: str
    model: str | None = None
    translations: tuple[SentenceTranslation, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class TranslationProvider(Protocol):
    @property
    def name(self) -> str:
        """Stable provider identifier stored with a translation."""

    def translate(
        self,
        recording_id: str,
        segments: Sequence[MergedTranscriptSegment],
    ) -> TranslationResult:
        """Render each sentence of a transcript in Cantonese."""


class AnalysisProvider(Protocol):
    @property
    def name(self) -> str:
        """Stable provider identifier stored with an analysis."""

    def analyze(
        self,
        recording_id: str,
        segments: Sequence[MergedTranscriptSegment],
    ) -> AnalysisResult:
        """Analyze a transcript without depending on transcription internals."""


@dataclass(frozen=True, slots=True)
class DailyRecordingDigest:
    """One analysed recording of a day, reduced to what a day summary reads.

    The digest is built from the committed analysis only. No audio and no
    transcript text reaches the day summary, and the model sees ``index``
    rather than the recording id, so a reply can never invent an identifier.
    """

    index: int
    recording_id: str
    time_label: str
    description_ja: str
    description_zh_hk: str
    summary_ja: str = ""
    summary_zh_hk: str = ""
    tags_ja: tuple[str, ...] = ()
    highlights_ja: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("digest index cannot be negative")
        if not self.recording_id.strip():
            raise ValueError("digest recording id cannot be empty")


class DailySummaryProvider(Protocol):
    @property
    def name(self) -> str:
        """Stable provider identifier stored with a day summary."""

    def summarize(
        self,
        summary_date: str,
        digests: Sequence[DailyRecordingDigest],
    ) -> AnalysisResult:
        """Summarize one day from the analyses its recordings already produced."""


StageCallback = Callable[[ProcessingStage], None]
RecordingIdentifier = str | UUID
