from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from math import isfinite
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from audio_server.db.models import (
    AnalysisStatus,
    BookmarkKind,
    JobKind,
    JobStage,
    JobStatus,
    RecordingStatus,
    TranslationSource,
)

_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class FuriganaToken(BaseModel):
    """One run of Japanese text; ``reading`` is set only over kanji."""

    model_config = ConfigDict(from_attributes=True)

    text: str
    reading: str | None


# Responses carry one reading map keyed by the Japanese string rather than a
# parallel field beside every translated value, so repeated quotes cost nothing
# and the existing response shapes stay unchanged.
FuriganaMap = dict[str, list[FuriganaToken]]


class ClientRecordingMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    recording_start_time: datetime
    recording_end_time: datetime
    duration_seconds: float | None = Field(default=None, ge=0)
    filename: str = Field(min_length=1, max_length=1024)
    file_size: int = Field(gt=0)
    checksum_sha256: str
    device_id: str = Field(min_length=1, max_length=128)
    audio_format: str | None = Field(default=None, max_length=32)
    sample_rate: int | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0, le=64)
    upload_status: str | None = Field(default=None, max_length=64)
    retry_count: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("checksum_sha256")
    @classmethod
    def normalize_checksum(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("checksum_sha256 must be a SHA-256 hex digest")
        return value.lower()

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        if not _DEVICE_ID_PATTERN.fullmatch(value):
            raise ValueError("device_id contains unsupported characters")
        return value

    @field_validator("extra")
    @classmethod
    def validate_extra_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _require_finite_json(value)
        return value

    @field_validator("recording_start_time", "recording_end_time", "created_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamp must include a timezone offset")
        return value

    @model_validator(mode="after")
    def validate_time_order(self) -> ClientRecordingMetadata:
        if self.recording_end_time <= self.recording_start_time:
            raise ValueError("recording_end_time must be after recording_start_time")
        return self


class UploadRecordingResponse(BaseModel):
    recording_id: uuid.UUID
    status: RecordingStatus
    duplicate: bool


class RecordingSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: str
    original_filename: str
    mime_type: str
    audio_format: str
    file_size: int
    sha256: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    sample_rate: int | None
    channels: int | None
    processing_status: RecordingStatus
    checked: bool
    created_at: datetime
    updated_at: datetime


class RecordingCheckedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked: bool


class RecordingListResponse(BaseModel):
    items: list[RecordingSummary]
    limit: int
    offset: int


class JobError(BaseModel):
    code: str
    type: str | None
    message: str
    stage: JobStage | None
    at: datetime | None


class JobStatusResponse(BaseModel):
    id: uuid.UUID
    kind: JobKind
    status: JobStatus
    stage: JobStage
    attempt_count: int
    max_attempts: int
    available_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: JobError | None = None


class QueueEntryResponse(BaseModel):
    recording_id: uuid.UUID
    original_filename: str
    job: JobStatusResponse


class QueueResponse(BaseModel):
    """Everything a worker could claim right now, in claim order."""

    items: list[QueueEntryResponse]
    processing: int
    queued: int


class RecordingStatusResponse(BaseModel):
    recording_id: uuid.UUID
    status: RecordingStatus
    job: JobStatusResponse | None


class TranscriptSegmentResponse(BaseModel):
    id: uuid.UUID
    sequence: int
    speaker_label: str
    start_time: float
    end_time: float
    text: str
    language: str | None
    confidence: float | None
    has_overlap: bool


class TranslationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_segment_id: uuid.UUID
    text_zh_hk: str = Field(min_length=1, max_length=4000)

    @field_validator("text_zh_hk")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("translation text cannot be blank")
        return stripped


class TranslationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    translations: list[TranslationUpdate]


class TranscriptTranslationResponse(BaseModel):
    """A Cantonese sentence and the transcript segments it covers."""

    id: uuid.UUID
    start_segment_id: uuid.UUID | None
    end_segment_id: uuid.UUID | None
    source_ja: str
    text_zh_hk: str
    source: TranslationSource
    stale: bool


class TranscriptResponse(BaseModel):
    recording_id: uuid.UUID
    status: RecordingStatus
    revision: int
    text: str
    segments: list[TranscriptSegmentResponse]
    translations: list[TranscriptTranslationResponse] = Field(default_factory=list)
    translation_revision: int = 0
    furigana: FuriganaMap = Field(default_factory=dict)


class TranscriptSegmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    speaker_label: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=100_000)

    @field_validator("speaker_label", "text")
    @classmethod
    def strip_nonempty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_interval(self) -> TranscriptSegmentUpdate:
        if not isfinite(self.start_time) or not isfinite(self.end_time):
            raise ValueError("timestamps must be finite")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class TranscriptUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    # Deliberately unbounded in length. An edit must carry the complete current
    # segment list, and the merge stage emits one segment per speaker run --
    # which is per word when attribution alternates -- so a long conversational
    # recording legitimately produces tens of thousands of segments. Any fixed
    # ceiling would make those transcripts permanently uneditable. Total
    # validation work is bounded by MAX_MUTATION_REQUEST_BYTES instead, which
    # caps the body before it is parsed.
    segments: list[TranscriptSegmentUpdate]


class BilingualDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ja: str = Field(min_length=1, max_length=4000)
    zh_hk: str = Field(min_length=1, max_length=4000)


class BilingualTag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ja: str = Field(min_length=1, max_length=80)
    zh_hk: str = Field(min_length=1, max_length=80)


class NaturalExpression(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_sequence: int = Field(ge=0)
    start_time: float = Field(ge=0)
    end_time: float | None = Field(default=None, gt=0)
    speaker_label: str = Field(min_length=1, max_length=64)
    original_ja: str = Field(min_length=1, max_length=1000)
    translation_zh_hk: str = Field(min_length=1, max_length=1000)
    usage_ja: str = Field(min_length=1, max_length=2000)
    usage_zh_hk: str = Field(min_length=1, max_length=2000)


class AnalysisHighlight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_sequence: int = Field(ge=0)
    start_time: float = Field(ge=0)
    end_time: float | None = Field(default=None, gt=0)
    speaker_label: str = Field(min_length=1, max_length=64)
    original_ja: str = Field(min_length=1, max_length=1000)
    translation_zh_hk: str = Field(min_length=1, max_length=1000)
    reason_ja: str = Field(min_length=1, max_length=2000)
    reason_zh_hk: str = Field(min_length=1, max_length=2000)


class AnalysisResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: BilingualDescription
    summary: BilingualDescription | None = None
    tags: list[BilingualTag] = Field(max_length=12)
    natural_expressions: list[NaturalExpression] = Field(max_length=20)
    highlights: list[AnalysisHighlight] = Field(max_length=12)


class DailyKeyPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Null once the recording a point came from has been deleted; the point
    # still describes the day, it just no longer links anywhere.
    recording_id: uuid.UUID | None = None
    ja: str = Field(min_length=1, max_length=1000)
    zh_hk: str = Field(min_length=1, max_length=1000)


class DailySummaryResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overview: BilingualDescription
    key_points: list[DailyKeyPoint] = Field(max_length=10)
    tags: list[BilingualTag] = Field(max_length=12)


class DayListEntryResponse(BaseModel):
    day: date
    recording_count: int
    analysed_count: int
    summary_status: AnalysisStatus | None
    summary_stale: bool


class DayListResponse(BaseModel):
    items: list[DayListEntryResponse]
    limit: int
    offset: int


class DayDetailResponse(BaseModel):
    day: date
    recordings: list[RecordingSummary]
    analysed_recording_ids: list[uuid.UUID]
    status: AnalysisStatus | None
    provider: str | None
    model: str | None
    schema_version: str | None
    summary: DailySummaryResultV1 | None
    stale: bool
    job: JobStatusResponse | None = None
    error: dict[str, str] | None = None
    furigana: FuriganaMap = Field(default_factory=dict)


class DaySummaryQueuedResponse(BaseModel):
    day: date
    job_id: uuid.UUID
    status: JobStatus


class AnalysisUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    result: AnalysisResultV2


class AnalysisResponse(BaseModel):
    recording_id: uuid.UUID
    status: AnalysisStatus
    provider: str
    model: str | None
    schema_version: str
    revision: int
    result: AnalysisResultV2 | None
    job: JobStatusResponse | None = None
    error: dict[str, str] | None = None
    furigana: FuriganaMap = Field(default_factory=dict)


class RetryResponse(BaseModel):
    recording_id: uuid.UUID
    job_id: uuid.UUID
    status: JobStatus


class BookmarkCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: BookmarkKind
    recording_id: uuid.UUID | None = None
    original_ja: str = Field(min_length=1, max_length=1000)
    translation_zh_hk: str = Field(min_length=1, max_length=1000)
    note_ja: str = Field(min_length=1, max_length=2000)
    note_zh_hk: str = Field(min_length=1, max_length=2000)
    speaker_label: str = Field(min_length=1, max_length=64)
    start_time: float = Field(ge=0)
    end_time: float | None = Field(default=None, gt=0)

    @field_validator("original_ja", "translation_zh_hk", "note_ja", "note_zh_hk")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_times(self) -> BookmarkCreateRequest:
        if not isfinite(self.start_time):
            raise ValueError("start_time must be finite")
        if self.end_time is not None:
            if not isfinite(self.end_time):
                raise ValueError("end_time must be finite")
            if self.end_time <= self.start_time:
                raise ValueError("end_time must be after start_time")
        return self


class BookmarkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: BookmarkKind
    source_digest: str
    original_ja: str
    translation_zh_hk: str
    note_ja: str
    note_zh_hk: str
    speaker_label: str
    start_time: float
    end_time: float | None
    recording_id: uuid.UUID | None
    source_label: str
    source_deleted_at: datetime | None
    created_at: datetime


class BookmarkListResponse(BaseModel):
    items: list[BookmarkResponse]
    furigana: FuriganaMap = Field(default_factory=dict)


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


def _require_finite_json(value: object) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("metadata JSON numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _require_finite_json(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _require_finite_json(item)
        return
    raise ValueError("metadata extra must contain only JSON values")
