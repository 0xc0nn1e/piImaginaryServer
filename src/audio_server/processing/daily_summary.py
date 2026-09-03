"""Day-level summary of already-analysed recordings, through LM Studio.

The day summary reads committed analyses only. It never opens audio and never
re-reads a transcript, so a day can be summarised long after its recordings
finished without repeating any expensive work.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from audio_server.processing.analysis import (
    ClientFactory,
    LMStudioSettings,
    _LoadedModel,
    _sdk_client,
    classify_lmstudio_failure,
    require_single_loaded_model,
    structured_payload,
)
from audio_server.processing.contracts import (
    AnalysisResult,
    AnalysisStatus,
    DailyRecordingDigest,
)
from audio_server.processing.errors import PermanentProcessingError, ProcessingError

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_KEY_POINTS = 10
# A reduce pass at least halves the partial results, so this ceiling is far
# above any real day and exists only to stop a pathological loop.
MAX_REDUCE_PASSES = 6
MAX_TAGS = 12


class _GenerationText(BaseModel):
    """Generation schema.

    LM Studio turns JSON Schema length bounds into llama.cpp grammar
    repetitions, which breaks past its safety ceiling, so generation is
    unbounded and every reply is re-validated against the bounded draft below.
    """

    model_config = ConfigDict(extra="forbid")

    ja: str = Field(min_length=1)
    zh_hk: str = Field(min_length=1)


class _GenerationPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recording_index: int = Field(ge=0)
    ja: str = Field(min_length=1)
    zh_hk: str = Field(min_length=1)


class _GenerationDailyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overview: _GenerationText
    key_points: list[_GenerationPoint] = Field(max_length=MAX_KEY_POINTS)
    tags: list[_GenerationText] = Field(max_length=MAX_TAGS)


class _DraftText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ja: str = Field(min_length=1, max_length=4000)
    zh_hk: str = Field(min_length=1, max_length=4000)


class _DraftTag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ja: str = Field(min_length=1, max_length=80)
    zh_hk: str = Field(min_length=1, max_length=80)


class _DraftPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recording_index: int = Field(ge=0)
    ja: str = Field(min_length=1, max_length=1000)
    zh_hk: str = Field(min_length=1, max_length=1000)


class _DailyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overview: _DraftText
    key_points: list[_DraftPoint] = Field(max_length=MAX_KEY_POINTS)
    tags: list[_DraftTag] = Field(max_length=MAX_TAGS)


_KNOWN_DRAFT_FIELDS = frozenset(
    name
    for model in (_DailyDraft, _DraftText, _DraftTag, _DraftPoint)
    for name in model.model_fields
)


class LMStudioDailySummaryProvider:
    """Summarise one day from the analyses its recordings already produced."""

    def __init__(
        self,
        settings: LMStudioSettings,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or _sdk_client

    @property
    def name(self) -> str:
        return "lmstudio"

    def summarize(
        self,
        summary_date: str,
        digests: Sequence[DailyRecordingDigest],
    ) -> AnalysisResult:
        if not digests:
            return AnalysisResult(
                status=AnalysisStatus.SKIPPED,
                provider=self.name,
                schema_version=SCHEMA_VERSION,
            )

        try:
            with self._client_factory(self._settings) as client:
                model = require_single_loaded_model(client)
                identifier = str(model.identifier)
                drafts = [
                    self._respond(model, _map_prompt(summary_date, chunk))
                    for chunk in _chunk_digests(digests, self._settings.chunk_chars)
                ]
                for _pass in range(MAX_REDUCE_PASSES):
                    if len(drafts) == 1:
                        break
                    drafts = [
                        self._respond(model, _reduce_prompt(summary_date, group))
                        for group in _group_drafts(drafts, self._settings.chunk_chars)
                    ]
                # Judged on the result, not on having spent the budget: work
                # that converges on the final permitted pass has succeeded.
                if len(drafts) != 1:
                    raise PermanentProcessingError(
                        code="lmstudio_daily_summary_not_converging",
                        safe_message="The day summary could not be reduced to one result.",
                    )
                return AnalysisResult(
                    status=AnalysisStatus.COMPLETED,
                    provider=self.name,
                    model=identifier,
                    schema_version=SCHEMA_VERSION,
                    data=_to_data(drafts[0], digests),
                )
        except ProcessingError:
            raise
        except Exception as exc:
            raise classify_lmstudio_failure(
                exc,
                permanent_code="lmstudio_daily_summary_failed",
                permanent_message="LM Studio could not produce a valid day summary.",
            ) from exc

    def _respond(self, model: _LoadedModel, prompt: str) -> _DailyDraft:
        try:
            response = model.respond(
                prompt,
                response_format=_GenerationDailyDraft,
                config={"temperature": 0.2, "maxTokens": self._settings.max_tokens},
            )
            return structured_payload(response, _DailyDraft)
        except ValidationError as exc:
            # Field paths and rule names only. The rejected values are model
            # output and must never reach the log.
            logger.warning(
                "LM Studio day summary failed schema validation",
                extra={"violations": _safe_violations(exc)},
            )
            raise PermanentProcessingError(
                code="lmstudio_daily_summary_schema_invalid",
                safe_message="LM Studio returned an invalid day summary.",
            ) from exc
        except (TypeError, ValueError) as exc:
            raise PermanentProcessingError(
                code="lmstudio_daily_summary_schema_invalid",
                safe_message="LM Studio returned an invalid day summary.",
            ) from exc


def _digest_block(digest: DailyRecordingDigest) -> str:
    lines = [
        f"RECORDING {digest.index} | {digest.time_label}",
        f"description_ja: {digest.description_ja}",
        f"description_zh_hk: {digest.description_zh_hk}",
    ]
    if digest.summary_ja:
        lines.append(f"summary_ja: {digest.summary_ja}")
    if digest.summary_zh_hk:
        lines.append(f"summary_zh_hk: {digest.summary_zh_hk}")
    if digest.tags_ja:
        lines.append(f"tags_ja: {', '.join(digest.tags_ja)}")
    for highlight in digest.highlights_ja:
        lines.append(f"highlight_ja: {highlight}")
    return "\n".join(lines)


def _chunk_digests(
    digests: Sequence[DailyRecordingDigest], max_chars: int
) -> tuple[str, ...]:
    """Split at recording boundaries so an index never straddles two requests."""

    if max_chars < 100:
        raise ValueError("max_chars is too small")
    chunks: list[str] = []
    current = ""
    for digest in digests:
        block = _digest_block(digest)[:max_chars]
        if not current:
            current = block
            continue
        if len(current) + len(block) + 2 <= max_chars:
            current = f"{current}\n\n{block}"
            continue
        chunks.append(current)
        current = block
    if current:
        chunks.append(current)
    return tuple(chunks)


def _group_drafts(
    drafts: Sequence[_DailyDraft], max_chars: int
) -> tuple[tuple[_DailyDraft, ...], ...]:
    """Group partial day summaries so each reduce request stays within budget."""

    groups: list[tuple[_DailyDraft, ...]] = []
    current: list[_DailyDraft] = []
    size = 0
    for draft in drafts:
        rendered = len(_draft_json(draft))
        if current and size + rendered > max_chars:
            groups.append(tuple(current))
            current = []
            size = 0
        current.append(draft)
        size += rendered
    if current:
        groups.append(tuple(current))
    if len(groups) == len(drafts) and all(len(group) == 1 for group in groups):
        # Partial results too large to pair by size would each be reduced alone,
        # returning as many drafts as went in and never finishing. Exceeding the
        # size budget is the lesser problem, so pair them anyway.
        groups = [tuple(drafts[index : index + 2]) for index in range(0, len(drafts), 2)]
    return tuple(groups)


def _draft_json(draft: _DailyDraft) -> str:
    return json.dumps(draft.model_dump(mode="json"), ensure_ascii=False)


_INSTRUCTIONS = (
    "You summarise a day of Japanese conversation recordings for a Cantonese "
    "speaker who is studying Japanese.\n"
    "Write every field twice: ja in natural Japanese, zh_hk in written Hong "
    "Kong Cantonese using Traditional Chinese. Never use Simplified Chinese.\n"
    "overview: what the day was about, a few sentences.\n"
    "key_points: what actually happened or was discussed, most important "
    f"first, at most {MAX_KEY_POINTS}. Set recording_index to the RECORDING "
    "number a point came from, and use only numbers shown below.\n"
    f"tags: at most {MAX_TAGS} short topic labels for the day.\n"
    "Summarise only what the material states; invent nothing."
)


def _map_prompt(summary_date: str, block: str) -> str:
    return (
        f"{_INSTRUCTIONS}\n\n"
        f"Date: {summary_date}\n"
        "Each RECORDING below is one recording's finished analysis.\n\n"
        f"{block}"
    )


def _reduce_prompt(summary_date: str, drafts: Sequence[_DailyDraft]) -> str:
    partials = "\n\n".join(_draft_json(draft) for draft in drafts)
    return (
        f"{_INSTRUCTIONS}\n\n"
        f"Date: {summary_date}\n"
        "Merge the partial day summaries below into one. Keep every "
        "recording_index exactly as given; drop duplicates rather than "
        "inventing new material.\n\n"
        f"{partials}"
    )


def _to_data(
    draft: _DailyDraft, digests: Sequence[DailyRecordingDigest]
) -> Mapping[str, object]:
    """Resolve model-supplied indices to recording ids, dropping unknown ones.

    The stored summary names recordings by id so it stays correct when the day
    later gains or loses a recording, and a hallucinated index is discarded
    rather than pointing at somebody else's audio.
    """

    by_index = {digest.index: digest.recording_id for digest in digests}
    key_points = [
        {
            "recording_id": by_index[point.recording_index],
            "ja": point.ja.strip(),
            "zh_hk": point.zh_hk.strip(),
        }
        for point in draft.key_points
        if point.recording_index in by_index
    ]
    return {
        "overview": {"ja": draft.overview.ja.strip(), "zh_hk": draft.overview.zh_hk.strip()},
        "key_points": key_points,
        "tags": [{"ja": tag.ja.strip(), "zh_hk": tag.zh_hk.strip()} for tag in draft.tags],
    }


def _safe_violations(error: ValidationError) -> list[str]:
    """Describe a failed validation as `field.path:rule` without any values."""

    return sorted({f"{_safe_path(item['loc'])}:{item['type']}" for item in error.errors()})


def _safe_path(location: Sequence[object]) -> str:
    parts = [
        str(part) if isinstance(part, int) or part in _KNOWN_DRAFT_FIELDS else "<redacted>"
        for part in location
    ]
    return ".".join(parts) or "<root>"
