"""Cantonese translation reuses the analysis client, so it must fail the same way."""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from audio_server.processing import translation as translation_module
from audio_server.processing.analysis import LMStudioSettings
from audio_server.processing.contracts import AnalysisStatus, MergedTranscriptSegment
from audio_server.processing.errors import (
    PermanentProcessingError,
    ProviderConfigurationError,
    RetryableProcessingError,
)
from audio_server.processing.translation import LMStudioTranslationProvider

SETTINGS = LMStudioSettings(host="127.0.0.1:1234", api_key="", timeout_seconds=30)


class FakeModel:
    identifier = "loaded/model-instance"

    def __init__(self, replies: list[dict[str, object]]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    def respond(self, prompt: str, **kwargs: object) -> object:
        self.prompts.append(prompt)
        del kwargs
        return SimpleNamespace(parsed=self.replies[len(self.prompts) - 1], structured=True)


class FakeClient:
    def __init__(self, models: list[FakeModel]) -> None:
        self.models = models

    def list_loaded_models(self, namespace: str | None = None) -> list[FakeModel]:
        del namespace
        return self.models


class FakeContext(AbstractContextManager[Any]):
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def __enter__(self) -> FakeClient:
        return self.client

    def __exit__(self, *args: object) -> None:
        del args


class FailingContext(AbstractContextManager[Any]):
    def __init__(self, error: Exception) -> None:
        self.error = error

    def __enter__(self) -> Any:
        raise self.error

    def __exit__(self, *args: object) -> None:
        del args


def _segment(sequence: int, text: str, speaker: str = "SPEAKER_00") -> MergedTranscriptSegment:
    return MergedTranscriptSegment(
        sequence=sequence,
        speaker_label=speaker,
        start=float(sequence),
        end=float(sequence) + 0.9,
        text=text,
    )


def _provider(client: FakeClient) -> LMStudioTranslationProvider:
    return LMStudioTranslationProvider(
        SETTINGS, client_factory=lambda _settings: FakeContext(client)
    )


def test_each_sentence_is_translated_once_and_keeps_its_own_span() -> None:
    model = FakeModel([{"zh_hk": "尋日去咗開會。"}, {"zh_hk": "好耐。"}])
    segments = [_segment(0, "昨日は"), _segment(1, "会議に行きました。"), _segment(2, "長かった。")]

    result = _provider(FakeClient([model])).translate("recording-1", segments)

    assert result.status is AnalysisStatus.COMPLETED
    assert result.model == "loaded/model-instance"
    assert [(item.start_sequence, item.end_sequence) for item in result.translations] == [
        (0, 1),
        (2, 2),
    ]
    # One request per sentence: the model never chooses the boundaries.
    assert len(model.prompts) == 2
    assert "昨日は会議に行きました。" in model.prompts[0]


def test_an_empty_transcript_makes_no_request() -> None:
    client = FakeClient([FakeModel([])])

    result = _provider(client).translate("recording-1", [])

    assert result.status is AnalysisStatus.COMPLETED
    assert result.translations == ()


def test_exactly_one_loaded_model_is_required() -> None:
    segments = [_segment(0, "はい。")]

    with pytest.raises(ProviderConfigurationError) as empty:
        _provider(FakeClient([])).translate("recording-1", segments)
    with pytest.raises(ProviderConfigurationError) as several:
        _provider(FakeClient([FakeModel([]), FakeModel([])])).translate("recording-1", segments)

    assert empty.value.code == "lmstudio_model_not_loaded"
    assert several.value.code == "lmstudio_multiple_models"


def test_a_timeout_is_retryable_like_analysis() -> None:
    provider = LMStudioTranslationProvider(
        SETTINGS, client_factory=lambda _settings: FailingContext(TimeoutError("slow"))
    )

    with pytest.raises(RetryableProcessingError) as caught:
        provider.translate("recording-1", [_segment(0, "はい。")])

    assert caught.value.code == "lmstudio_unavailable"


def test_authentication_failure_is_a_configuration_error() -> None:
    provider = LMStudioTranslationProvider(
        SETTINGS, client_factory=lambda _settings: FailingContext(RuntimeError("unauthorized"))
    )

    with pytest.raises(ProviderConfigurationError) as caught:
        provider.translate("recording-1", [_segment(0, "はい。")])

    assert caught.value.code == "lmstudio_authentication_failed"


def test_an_oversized_reply_is_rejected_after_generation() -> None:
    model = FakeModel([{"zh_hk": "字" * (translation_module.MAX_TRANSLATION_CHARS + 1)}])

    with pytest.raises(PermanentProcessingError) as caught:
        _provider(FakeClient([model])).translate("recording-1", [_segment(0, "はい。")])

    assert caught.value.code == "lmstudio_translation_schema_invalid"


def test_an_unreadable_response_is_named_the_same_way_analysis_names_it() -> None:
    class UnreadableModel(FakeModel):
        def respond(self, prompt: str, **kwargs: object) -> object:
            self.prompts.append(prompt)
            del kwargs
            return SimpleNamespace(parsed=None, structured=True)

    with pytest.raises(PermanentProcessingError) as caught:
        _provider(FakeClient([UnreadableModel([])])).translate(
            "recording-1", [_segment(0, "はい。")]
        )

    # One broken response shape used to surface here as lmstudio_translation_failed
    # and in analysis as lmstudio_schema_invalid, which read as two unrelated
    # faults while it was a single one.
    assert caught.value.code == "lmstudio_translation_schema_invalid"


def test_violations_never_carry_model_output() -> None:
    leaked = "モデルが返した本文"
    with pytest.raises(ValidationError) as caught:
        translation_module._DraftTranslation.model_validate({"zh_hk": "", leaked: "x"})

    violations = translation_module._safe_violations(caught.value)

    assert "zh_hk:string_too_short" in violations
    assert all(leaked not in line for line in violations)
