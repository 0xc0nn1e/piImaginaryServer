"""Sentence-level Cantonese translation through the same LM Studio client.

Only the transcript text ever leaves this process. Audio never does.
"""

from __future__ import annotations

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
)
from audio_server.processing.contracts import (
    AnalysisStatus,
    MergedTranscriptSegment,
    SentenceTranslation,
    TranslationResult,
)
from audio_server.processing.errors import PermanentProcessingError, ProcessingError
from audio_server.processing.sentences import TranscriptSentence, group_sentences

logger = logging.getLogger(__name__)

MAX_TRANSLATION_CHARS = 4000


class _GenerationTranslation(BaseModel):
    """Generation schema.

    LM Studio turns JSON Schema length bounds into llama.cpp grammar
    repetitions, which breaks past its safety ceiling, so generation is
    unbounded and the reply is re-validated below.
    """

    model_config = ConfigDict(extra="forbid")

    zh_hk: str = Field(min_length=1)


class _DraftTranslation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zh_hk: str = Field(min_length=1, max_length=MAX_TRANSLATION_CHARS)


_KNOWN_TRANSLATION_FIELDS = frozenset(_DraftTranslation.model_fields)


class LMStudioTranslationProvider:
    """Translates one sentence per request so a reply cannot be misaligned."""

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

    def translate(
        self,
        recording_id: str,
        segments: Sequence[MergedTranscriptSegment],
    ) -> TranslationResult:
        del recording_id
        sentences = group_sentences(segments)
        if not sentences:
            return TranslationResult(status=AnalysisStatus.COMPLETED, provider=self.name)

        try:
            with self._client_factory(self._settings) as client:
                model = require_single_loaded_model(client)
                identifier = str(model.identifier)
                translations = tuple(
                    SentenceTranslation(
                        start_sequence=sentence.start_sequence,
                        end_sequence=sentence.end_sequence,
                        source_ja=sentence.text,
                        text_zh_hk=self._render(model, sentence),
                    )
                    for sentence in sentences
                )
                return TranslationResult(
                    status=AnalysisStatus.COMPLETED,
                    provider=self.name,
                    model=identifier,
                    translations=translations,
                )
        except ProcessingError:
            raise
        except Exception as exc:
            raise classify_lmstudio_failure(
                exc,
                permanent_code="lmstudio_translation_failed",
                permanent_message="LM Studio could not produce a valid translation.",
            ) from exc

    def _render(self, model: _LoadedModel, sentence: TranscriptSentence) -> str:
        try:
            response = model.respond(
                _translate_prompt(sentence),
                response_format=_GenerationTranslation,
                config={"temperature": 0.2, "maxTokens": self._settings.max_tokens},
            )
            if not bool(getattr(response, "structured", True)):
                raise ValueError("LM Studio returned an unstructured response")
            parsed = getattr(response, "parsed", None)
            if not isinstance(parsed, Mapping):
                raise ValueError("LM Studio structured response is missing parsed data")
            return _DraftTranslation.model_validate(parsed).zh_hk.strip()
        except ValidationError as exc:
            # Field paths and rule names only; the rejected value is model
            # output and must never reach the log.
            logger.warning(
                "LM Studio translation failed schema validation",
                extra={"violations": _safe_violations(exc)},
            )
            raise PermanentProcessingError(
                code="lmstudio_translation_schema_invalid",
                safe_message="LM Studio returned an invalid translation.",
            ) from exc


def _translate_prompt(sentence: TranscriptSentence) -> str:
    return (
        "You translate Japanese conversation into natural Hong Kong Cantonese.\n"
        "Translate the sentence below. Keep the speaker's register and tone.\n"
        "Use written Cantonese as spoken in Hong Kong, in Traditional Chinese.\n"
        "Never use Simplified Chinese. Translate only; add no commentary.\n"
        "Return JSON with a single zh_hk field.\n\n"
        f"Sentence: {sentence.text}"
    )


def _safe_violations(error: ValidationError) -> list[str]:
    return sorted(
        {
            f"{_safe_path(item['loc'])}:{item['type']}"
            for item in error.errors()
        }
    )


def _safe_path(location: Sequence[object]) -> str:
    parts = [
        str(part)
        if isinstance(part, int) or part in _KNOWN_TRANSLATION_FIELDS
        else "<redacted>"
        for part in location
    ]
    return ".".join(parts) or "<root>"
