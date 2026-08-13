"""Privacy-safe transcript analysis providers."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from audio_server.api.schemas import (
    AnalysisHighlight,
    AnalysisResultV2,
    BilingualDescription,
    BilingualTag,
    NaturalExpression,
)
from audio_server.processing.contracts import (
    AnalysisResult,
    AnalysisStatus,
    MergedTranscriptSegment,
)
from audio_server.processing.errors import (
    PermanentProcessingError,
    ProcessingError,
    ProviderConfigurationError,
    RetryableProcessingError,
)


class DisabledAnalysisProvider:
    """Records an explicit skip without making transcription depend on an LLM."""

    @property
    def name(self) -> str:
        return "disabled"

    def analyze(
        self,
        recording_id: str,
        segments: Sequence[MergedTranscriptSegment],
    ) -> AnalysisResult:
        del recording_id, segments
        return AnalysisResult(
            status=AnalysisStatus.SKIPPED,
            provider=self.name,
            schema_version=2,
        )


@dataclass(frozen=True, slots=True)
class LMStudioSettings:
    host: str
    api_key: str = ""
    timeout_seconds: int = 600
    chunk_chars: int = 12_000
    max_tokens: int = 4096

    def __post_init__(self) -> None:
        if not self.host.strip() or ":" not in self.host:
            raise ValueError("LM Studio host must use host:port syntax")
        if self.timeout_seconds <= 0:
            raise ValueError("LM Studio timeout must be positive")
        if not 1000 <= self.chunk_chars <= 100_000:
            raise ValueError("LM Studio chunk size is out of range")
        if not 256 <= self.max_tokens <= 32_768:
            raise ValueError("LM Studio max tokens is out of range")


class _DraftDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ja: str = Field(min_length=1, max_length=4000)
    zh_hk: str = Field(min_length=1, max_length=4000)


class _DraftTag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ja: str = Field(min_length=1, max_length=80)
    zh_hk: str = Field(min_length=1, max_length=80)


class _DraftExpression(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_sequence: int = Field(ge=0)
    original_ja: str = Field(min_length=1, max_length=1000)
    translation_zh_hk: str = Field(min_length=1, max_length=1000)
    usage_ja: str = Field(min_length=1, max_length=2000)
    usage_zh_hk: str = Field(min_length=1, max_length=2000)


class _DraftHighlight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_sequence: int = Field(ge=0)
    original_ja: str = Field(min_length=1, max_length=1000)
    translation_zh_hk: str = Field(min_length=1, max_length=1000)
    reason_ja: str = Field(min_length=1, max_length=2000)
    reason_zh_hk: str = Field(min_length=1, max_length=2000)


class _AnalysisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: _DraftDescription
    tags: list[_DraftTag] = Field(max_length=12)
    natural_expressions: list[_DraftExpression] = Field(max_length=20)
    highlights: list[_DraftHighlight] = Field(max_length=12)


class _GenerationDescription(BaseModel):
    """Grammar-safe description schema used only during constrained generation."""

    model_config = ConfigDict(extra="forbid")

    ja: str = Field(min_length=1)
    zh_hk: str = Field(min_length=1)


class _GenerationTag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ja: str = Field(min_length=1)
    zh_hk: str = Field(min_length=1)


class _GenerationExpression(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_sequence: int = Field(ge=0)
    original_ja: str = Field(min_length=1)
    translation_zh_hk: str = Field(min_length=1)
    usage_ja: str = Field(min_length=1)
    usage_zh_hk: str = Field(min_length=1)


class _GenerationHighlight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_sequence: int = Field(ge=0)
    original_ja: str = Field(min_length=1)
    translation_zh_hk: str = Field(min_length=1)
    reason_ja: str = Field(min_length=1)
    reason_zh_hk: str = Field(min_length=1)


class _GenerationAnalysisDraft(BaseModel):
    """Pydantic response schema without large string repetition bounds.

    LM Studio translates JSON Schema string ``maxLength`` values into llama.cpp
    grammar repetitions. The strict limits used by ``_AnalysisDraft`` are much
    larger than llama.cpp's grammar safety ceiling, so generation uses this
    otherwise-identical schema and the server validates the parsed result with
    ``_AnalysisDraft`` before accepting it.
    """

    model_config = ConfigDict(extra="forbid")

    description: _GenerationDescription
    tags: list[_GenerationTag] = Field(max_length=12)
    natural_expressions: list[_GenerationExpression] = Field(max_length=20)
    highlights: list[_GenerationHighlight] = Field(max_length=12)


class _LoadedModel(Protocol):
    identifier: object

    def respond(self, prompt: str, **kwargs: object) -> object: ...


class _LMStudioClient(Protocol):
    def list_loaded_models(self, namespace: str | None = None) -> Sequence[_LoadedModel]: ...


ClientFactory = Callable[[LMStudioSettings], AbstractContextManager[_LMStudioClient]]


class LMStudioAnalysisProvider:
    """Analyze every transcript chunk using the one LLM already loaded in LM Studio."""

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

    def load(self) -> None:
        """Intentionally performs no network/model operation during worker startup."""

    def analyze(
        self,
        recording_id: str,
        segments: Sequence[MergedTranscriptSegment],
    ) -> AnalysisResult:
        del recording_id
        if not segments:
            empty = AnalysisResultV2(
                description=BilingualDescription(
                    ja="分析できる発話はありません。",
                    zh_hk="沒有可供分析的語音內容。",
                ),
                tags=[],
                natural_expressions=[],
                highlights=[],
            )
            return AnalysisResult(
                status=AnalysisStatus.COMPLETED,
                provider=self.name,
                schema_version=2,
                data=empty.model_dump(mode="json"),
            )

        try:
            with self._client_factory(self._settings) as client:
                loaded = list(client.list_loaded_models("llm"))
                if len(loaded) != 1:
                    code = "lmstudio_model_not_loaded" if not loaded else "lmstudio_multiple_models"
                    message = (
                        "Load exactly one LLM in LM Studio before running analysis."
                        if not loaded
                        else "Unload extra LLMs in LM Studio so exactly one remains loaded."
                    )
                    raise ProviderConfigurationError(code=code, safe_message=message)
                model = loaded[0]
                identifier = str(model.identifier)
                drafts = [
                    self._respond(model, _map_prompt(chunk))
                    for chunk in chunk_transcript(segments, self._settings.chunk_chars)
                ]
                while len(drafts) > 1:
                    drafts = [
                        self._respond(model, _reduce_prompt(group))
                        for group in _group_drafts(drafts, self._settings.chunk_chars)
                    ]
                draft = drafts[0]
                _validate_grounding(draft, segments)
                result = _to_result(draft, segments)
                return AnalysisResult(
                    status=AnalysisStatus.COMPLETED,
                    provider=self.name,
                    model=identifier,
                    schema_version=2,
                    data=result.model_dump(mode="json"),
                )
        except ProcessingError:
            raise
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise RetryableProcessingError(
                code="lmstudio_unavailable",
                safe_message="LM Studio is temporarily unavailable.",
            ) from exc
        except Exception as exc:
            error_name = type(exc).__name__.lower()
            error_text = str(exc).casefold()
            if (
                "auth" in error_name
                or "permission" in error_name
                or "auth" in error_text
                or "permission" in error_text
                or "unauthorized" in error_text
                or "forbidden" in error_text
            ):
                raise ProviderConfigurationError(
                    code="lmstudio_authentication_failed",
                    safe_message="LM Studio rejected the configured API token.",
                ) from exc
            if "timeout" in error_name or "websocket" in error_name or "client" in error_name:
                raise RetryableProcessingError(
                    code="lmstudio_unavailable",
                    safe_message="LM Studio is temporarily unavailable.",
                ) from exc
            raise PermanentProcessingError(
                code="lmstudio_analysis_failed",
                safe_message="LM Studio could not produce a valid structured analysis.",
            ) from exc

    def _respond(self, model: _LoadedModel, prompt: str) -> _AnalysisDraft:
        try:
            response = model.respond(
                prompt,
                response_format=_GenerationAnalysisDraft,
                config={"temperature": 0.2, "maxTokens": self._settings.max_tokens},
            )
            if not bool(getattr(response, "structured", True)):
                raise ValueError("LM Studio returned an unstructured response")
            parsed = getattr(response, "parsed", None)
            if not isinstance(parsed, Mapping):
                raise ValueError("LM Studio structured response is missing parsed data")
            return _AnalysisDraft.model_validate(parsed)
        except (TypeError, ValueError) as exc:
            raise PermanentProcessingError(
                code="lmstudio_schema_invalid",
                safe_message="LM Studio returned an invalid structured analysis.",
            ) from exc


def chunk_transcript(
    segments: Sequence[MergedTranscriptSegment], max_chars: int
) -> tuple[str, ...]:
    """Split at segment boundaries, covering oversized segments in consecutive parts."""

    if max_chars < 100:
        raise ValueError("max_chars is too small")
    chunks: list[str] = []
    current = ""
    for segment in segments:
        prefix = f"SEGMENT {segment.sequence} | {segment.speaker_label} | {segment.start:.3f}s\n"
        room = max_chars - len(prefix) - 1
        if room <= 0:
            raise ValueError("max_chars cannot fit segment metadata")
        parts = [segment.text[index : index + room] for index in range(0, len(segment.text), room)]
        for part in parts or [""]:
            rendered = f"{prefix}{part}\n"
            if current and len(current) + len(rendered) > max_chars:
                chunks.append(current.rstrip())
                current = ""
            current += rendered
    if current:
        chunks.append(current.rstrip())
    return tuple(chunks)


def _map_prompt(transcript: str) -> str:
    return (
        "Analyze the Japanese transcript below. Return only the required structured response. "
        "Write every field in both Japanese and natural Hong Kong Cantonese. Select at most 12 "
        "tags, 20 useful natural expressions, and 12 highlights. For expressions/highlights, "
        "copy original_ja exactly from the referenced SEGMENT and use its sequence number. "
        "If no Japanese quote is available, return an empty corresponding array.\n\n"
        + transcript
    )


def _reduce_prompt(drafts: Sequence[_AnalysisDraft]) -> str:
    payload = [draft.model_dump(mode="json") for draft in drafts]
    return (
        "Merge these partial bilingual transcript analyses into one structured response. "
        "Preserve only grounded original_ja quotes and their segment_sequence. Deduplicate items. "
        "Keep at most 12 tags, 20 natural expressions, and 12 highlights. Return only the required "
        "structured response.\n\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _group_drafts(
    drafts: Sequence[_AnalysisDraft], max_chars: int
) -> tuple[tuple[_AnalysisDraft, ...], ...]:
    groups: list[tuple[_AnalysisDraft, ...]] = []
    current: list[_AnalysisDraft] = []
    current_size = 0
    for draft in drafts:
        size = len(draft.model_dump_json())
        if current and current_size + size > max_chars:
            groups.append(tuple(current))
            current = []
            current_size = 0
        current.append(draft)
        current_size += size
        if len(current) >= 8:
            groups.append(tuple(current))
            current = []
            current_size = 0
    if current:
        groups.append(tuple(current))
    if len(groups) == len(drafts) and all(len(group) == 1 for group in groups):
        # Ensure hierarchical reduction still makes progress for unusually large partial results.
        groups = [tuple(drafts[index : index + 2]) for index in range(0, len(drafts), 2)]
    return tuple(groups)


def _validate_grounding(
    draft: _AnalysisDraft, segments: Sequence[MergedTranscriptSegment]
) -> None:
    text_by_sequence = {segment.sequence: segment.text for segment in segments}
    candidates: Sequence[_DraftExpression | _DraftHighlight] = (
        *draft.natural_expressions,
        *draft.highlights,
    )
    for item in candidates:
        text = text_by_sequence.get(item.segment_sequence)
        if text is None or item.original_ja not in text:
            raise PermanentProcessingError(
                code="lmstudio_quote_not_grounded",
                safe_message="LM Studio returned a quote that is not present in the transcript.",
            )


def _to_result(
    draft: _AnalysisDraft, segments: Sequence[MergedTranscriptSegment]
) -> AnalysisResultV2:
    segment_by_sequence = {segment.sequence: segment for segment in segments}
    expressions: list[NaturalExpression] = []
    seen_expressions: set[tuple[int, str]] = set()
    for expression in draft.natural_expressions:
        expression_key = (expression.segment_sequence, expression.original_ja)
        if expression_key in seen_expressions:
            continue
        seen_expressions.add(expression_key)
        segment = segment_by_sequence[expression.segment_sequence]
        expressions.append(
            NaturalExpression(
                **expression.model_dump(),
                start_time=segment.start,
                speaker_label=segment.speaker_label,
            )
        )
        if len(expressions) == 20:
            break

    highlights: list[AnalysisHighlight] = []
    seen_highlights: set[tuple[int, str]] = set()
    for draft_highlight in draft.highlights:
        highlight_key = (draft_highlight.segment_sequence, draft_highlight.original_ja)
        if highlight_key in seen_highlights:
            continue
        seen_highlights.add(highlight_key)
        segment = segment_by_sequence[draft_highlight.segment_sequence]
        highlights.append(
            AnalysisHighlight(
                **draft_highlight.model_dump(),
                start_time=segment.start,
                speaker_label=segment.speaker_label,
            )
        )
        if len(highlights) == 12:
            break

    tags: list[BilingualTag] = []
    seen_tags: set[tuple[str, str]] = set()
    for draft_tag in draft.tags:
        tag_key = (draft_tag.ja.casefold(), draft_tag.zh_hk.casefold())
        if tag_key not in seen_tags:
            seen_tags.add(tag_key)
            tags.append(BilingualTag.model_validate(draft_tag.model_dump()))
        if len(tags) == 12:
            break
    return AnalysisResultV2(
        description=BilingualDescription.model_validate(draft.description.model_dump()),
        tags=tags,
        natural_expressions=expressions,
        highlights=highlights,
    )


class _SDKClientManager(AbstractContextManager[_LMStudioClient]):
    def __init__(self, settings: LMStudioSettings) -> None:
        self._settings = settings
        self._client: AbstractContextManager[_LMStudioClient] | None = None

    def __enter__(self) -> _LMStudioClient:
        try:
            import lmstudio as lms
        except ImportError as exc:
            raise ProviderConfigurationError(
                code="lmstudio_sdk_missing",
                safe_message="The LM Studio Python SDK is not installed in the worker.",
            ) from exc
        lms.set_sync_api_timeout(float(self._settings.timeout_seconds))
        client = cast(
            AbstractContextManager[_LMStudioClient],
            lms.Client(
                self._settings.host,
                api_token=self._settings.api_key or None,
            ),
        )
        self._client = client
        return client.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        client = self._client
        if client is not None:
            exit_method = client.__exit__
            return exit_method(exc_type, exc_value, traceback)
        return None


def _sdk_client(settings: LMStudioSettings) -> AbstractContextManager[_LMStudioClient]:
    return _SDKClientManager(settings)
