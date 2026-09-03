"""Privacy-safe transcript analysis providers."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    DailyRecordingDigest,
    MergedTranscriptSegment,
    TranslationResult,
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


class DisabledDailySummaryProvider:
    """Stands in when no LLM is configured, so a day simply has no summary."""

    @property
    def name(self) -> str:
        return "disabled"

    def summarize(
        self,
        summary_date: str,
        digests: Sequence[DailyRecordingDigest],
    ) -> AnalysisResult:
        del summary_date, digests
        return AnalysisResult(status=AnalysisStatus.SKIPPED, provider=self.name)


class DisabledTranslationProvider:
    """Stands in when no LLM is configured, so the pipeline stays uniform."""

    @property
    def name(self) -> str:
        return "disabled"

    def translate(
        self,
        recording_id: str,
        segments: Sequence[MergedTranscriptSegment],
    ) -> TranslationResult:
        del recording_id, segments
        return TranslationResult(status=AnalysisStatus.SKIPPED, provider=self.name)


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


logger = logging.getLogger(__name__)


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
    summary: _DraftDescription
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
    summary: _GenerationDescription
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
                summary=BilingualDescription(
                    ja="文字起こしに分析できる発話がないため、詳しい内容要約はありません。",
                    zh_hk="逐字稿沒有可供分析的語音內容，因此暫時沒有詳細內容摘要。",
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
                draft = _ground_draft(drafts[0], segments)
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
        except Exception as exc:
            raise classify_lmstudio_failure(
                exc,
                permanent_code="lmstudio_analysis_failed",
                permanent_message="LM Studio could not produce a valid structured analysis.",
            ) from exc

    def _respond(self, model: _LoadedModel, prompt: str) -> _AnalysisDraft:
        try:
            response = model.respond(
                prompt,
                response_format=_GenerationAnalysisDraft,
                config={"temperature": 0.2, "maxTokens": self._settings.max_tokens},
            )
            return structured_payload(response, _AnalysisDraft)
        except ValidationError as exc:
            # Field paths and rule names only. The rejected values are model
            # output and must never reach the log.
            logger.warning(
                "LM Studio analysis failed schema validation",
                extra={"violations": _safe_violations(exc)},
            )
            raise PermanentProcessingError(
                code="lmstudio_schema_invalid",
                safe_message="LM Studio returned an invalid structured analysis.",
            ) from exc
        except (TypeError, ValueError) as exc:
            raise PermanentProcessingError(
                code="lmstudio_schema_invalid",
                safe_message="LM Studio returned an invalid structured analysis.",
            ) from exc


_KNOWN_DRAFT_FIELDS = frozenset(
    name
    for model in (
        _AnalysisDraft,
        _DraftDescription,
        _DraftTag,
        _DraftExpression,
        _DraftHighlight,
    )
    for name in model.model_fields
)


def _safe_violations(error: ValidationError) -> list[str]:
    """Describe a failed validation as `field.path:rule` without any values."""

    return sorted({f"{_safe_path(item['loc'])}:{item['type']}" for item in error.errors()})


def _safe_path(location: Sequence[object]) -> str:
    """Render a validation path from known names and indices only.

    An `extra_forbidden` violation puts the model-invented key straight into
    the path, so anything the schema does not declare is redacted rather than
    logged.
    """

    parts = [
        str(part) if isinstance(part, int) or part in _KNOWN_DRAFT_FIELDS else "<redacted>"
        for part in location
    ]
    return ".".join(parts) or "<root>"


ModelT = TypeVar("ModelT", bound=BaseModel)


def structured_payload(response: object, schema: type[ModelT]) -> ModelT:
    """Return the reply an LM Studio response carries, read as ``schema``.

    Two things about a reply are unstable. What the SDK puts in ``parsed`` has
    changed across releases -- a mapping, the parsed response format, or the
    raw text -- and the text itself is only JSON when the model answers with
    nothing else: a reasoning model narrates first, and some models fence their
    reply, in which case the SDK's own ``json.loads`` fails and it hands back
    the text as though no schema had been asked for. Reading one shape only is
    what stopped every analysis and translation at once.

    So each shape is offered to ``schema``, and the schema decides. Candidates
    run best-first -- what the SDK parsed, then objects embedded in the text
    from the end backwards, since the answer follows the narration -- but
    position only orders the attempts and never settles which one is the
    answer. Nothing the schema rejects is accepted, so a trailing remark that
    happens to be an object cannot displace the reply it follows.
    """

    candidates = _candidates(response)
    first_error: ValidationError | None = None
    for candidate in candidates:
        try:
            return schema.model_validate(candidate)
        except ValidationError as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        # Every candidate was readable and none fitted. The caller reports the
        # violations, which name the fields rather than the model's words.
        raise first_error
    parsed = getattr(response, "parsed", None)
    content = getattr(response, "content", None)
    # The values are model output and must never be logged; their types, and
    # the flag the SDK set, are not.
    logger.warning(
        "LM Studio structured response has an unreadable shape",
        extra={
            "response_type": type(response).__name__,
            "parsed_type": type(parsed).__name__,
            "content_type": type(content).__name__,
            "structured_flag": bool(getattr(response, "structured", True)),
        },
    )
    raise ValueError("LM Studio structured response is missing parsed data")


def _candidates(response: object) -> tuple[Mapping[str, object], ...]:
    """Every object a reply might be, most likely first."""

    found: list[Mapping[str, object]] = []
    for value in (getattr(response, "parsed", None), getattr(response, "content", None)):
        if isinstance(value, Mapping):
            found.append(cast(Mapping[str, object], value))
        elif isinstance(value, BaseModel):
            found.append(value.model_dump())
        elif isinstance(value, str):
            found.extend(_json_objects_in(_after_reasoning(value)))
    return tuple(found)


# What a model writes to mark where it stops thinking aloud and answers.
_REASONING_MARKS = (
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<reasoning>", "</reasoning>"),
)


def _after_reasoning(text: str) -> str:
    """Return what the model wrote after it finished thinking aloud.

    A reasoning block holds attempts the model went on to revise, so an object
    left inside one is not the reply even when it satisfies the schema.

    Only a reply that opens with such a block is trimmed. The marks are
    ordinary characters that a transcript can quote and an answer can therefore
    contain, so searching the whole reply for them would cut a sound reply in
    half over its own content. A block that opens and never closes means the
    reply ran out before an answer was reached, which is reported rather than
    answered from an unfinished draft.
    """

    answer = text.lstrip()
    for opener, closer in _REASONING_MARKS:
        if not answer.startswith(opener):
            continue
        closed = answer.find(closer, len(opener))
        if closed == -1:
            return ""
        return answer[closed + len(closer) :]
    return text


def _json_objects_in(text: str) -> list[Mapping[str, object]]:
    """Return the complete JSON objects embedded in a reply, last one first."""

    decoder = json.JSONDecoder()
    found: list[Mapping[str, object]] = []
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, Mapping):
            found.append(cast(Mapping[str, object], value))
        index = text.find("{", max(end, index + 1))
    found.reverse()
    return found


def classify_lmstudio_failure(
    exc: Exception, *, permanent_code: str, permanent_message: str
) -> ProcessingError:
    """Map an SDK failure onto the retry policy.

    Every LM Studio call has to classify failures the same way, or one feature
    would retry a misconfiguration the other treats as fatal.
    """

    if isinstance(exc, TimeoutError | ConnectionError | OSError):
        return RetryableProcessingError(
            code="lmstudio_unavailable",
            safe_message="LM Studio is temporarily unavailable.",
        )
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
        return ProviderConfigurationError(
            code="lmstudio_authentication_failed",
            safe_message="LM Studio rejected the configured API token.",
        )
    if "timeout" in error_name or "websocket" in error_name or "client" in error_name:
        return RetryableProcessingError(
            code="lmstudio_unavailable",
            safe_message="LM Studio is temporarily unavailable.",
        )
    return PermanentProcessingError(code=permanent_code, safe_message=permanent_message)


def require_single_loaded_model(client: _LMStudioClient) -> _LoadedModel:
    """Refuse to guess which model to use when the operator loaded several."""

    loaded = list(client.list_loaded_models("llm"))
    if len(loaded) == 1:
        return loaded[0]
    code = "lmstudio_model_not_loaded" if not loaded else "lmstudio_multiple_models"
    message = (
        "Load exactly one LLM in LM Studio before running this step."
        if not loaded
        else "Unload extra LLMs in LM Studio so exactly one remains loaded."
    )
    raise ProviderConfigurationError(code=code, safe_message=message)


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
        "Write every field in both Japanese and natural Hong Kong Cantonese. Make description a "
        "single concise sentence. Make summary a substantially more detailed 4-8 sentence account "
        "covering context, main topics, conclusions, decisions, and follow-up actions actually "
        "present in the transcript; do not invent missing details. Select at most 12 "
        "tags, 20 useful natural expressions, and 12 highlights. For expressions/highlights, "
        "copy original_ja exactly from the referenced SEGMENT without adding quotation marks, "
        "and use its sequence number. "
        "If no Japanese quote is available, return an empty corresponding array.\n\n"
        + transcript
    )


def _reduce_prompt(drafts: Sequence[_AnalysisDraft]) -> str:
    payload = [draft.model_dump(mode="json") for draft in drafts]
    return (
        "Merge these partial bilingual transcript analyses into one structured response. "
        "Keep description to one concise sentence and write summary as a detailed 4-8 sentence "
        "account of the full transcript, including grounded conclusions and follow-up actions. "
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


def _ground_draft(
    draft: _AnalysisDraft, segments: Sequence[MergedTranscriptSegment]
) -> _AnalysisDraft:
    """Keep only quotes grounded in their designated transcript segment.

    A model may wrap a verbatim quote in quotation marks even when instructed
    not to. Strip only surrounding whitespace and a single matching quote pair;
    never fuzzy-match or accept paraphrases. The retained ``original_ja`` is
    therefore always an exact substring of the transcript.
    """

    text_by_sequence = {segment.sequence: segment.text for segment in segments}
    expressions: list[_DraftExpression] = []
    for expression_item in draft.natural_expressions:
        text = text_by_sequence.get(expression_item.segment_sequence)
        grounded = _exact_grounded_quote(expression_item.original_ja, text)
        if grounded is not None:
            expressions.append(
                expression_item.model_copy(update={"original_ja": grounded})
            )

    highlights: list[_DraftHighlight] = []
    for highlight_item in draft.highlights:
        text = text_by_sequence.get(highlight_item.segment_sequence)
        grounded = _exact_grounded_quote(highlight_item.original_ja, text)
        if grounded is not None:
            highlights.append(
                highlight_item.model_copy(update={"original_ja": grounded})
            )
    return draft.model_copy(
        update={"natural_expressions": expressions, "highlights": highlights}
    )


def _exact_grounded_quote(quote: str, transcript: str | None) -> str | None:
    if transcript is None:
        return None
    candidate = quote.strip()
    if candidate in transcript:
        return candidate
    for opening, closing in (
        ("「", "」"),
        ("『", "』"),
        ('"', '"'),
        ("“", "”"),
        ("‘", "’"),
        ("'", "'"),
    ):
        if candidate.startswith(opening) and candidate.endswith(closing):
            unwrapped = candidate[len(opening) : len(candidate) - len(closing)].strip()
            if unwrapped and unwrapped in transcript:
                return unwrapped
    return None


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
                end_time=segment.end,
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
                end_time=segment.end,
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
        summary=BilingualDescription.model_validate(draft.summary.model_dump()),
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
