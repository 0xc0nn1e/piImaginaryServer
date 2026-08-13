from __future__ import annotations

from contextlib import AbstractContextManager
from types import SimpleNamespace
from typing import Any

import pytest

from audio_server.processing.analysis import (
    LMStudioAnalysisProvider,
    LMStudioSettings,
    chunk_transcript,
)
from audio_server.processing.contracts import AnalysisStatus, MergedTranscriptSegment
from audio_server.processing.errors import (
    PermanentProcessingError,
    ProviderConfigurationError,
    RetryableProcessingError,
)


class FakeModel:
    identifier = "loaded/model-instance"

    def __init__(self, parsed: dict[str, object]) -> None:
        self.parsed = parsed
        self.calls: list[tuple[str, dict[str, object]]] = []

    def respond(self, prompt: str, **kwargs: object) -> object:
        self.calls.append((prompt, kwargs))
        return SimpleNamespace(parsed=self.parsed, structured=True)


class FakeClient:
    def __init__(self, models: list[FakeModel]) -> None:
        self.models = models
        self.namespaces: list[str | None] = []

    def list_loaded_models(self, namespace: str | None = None) -> list[FakeModel]:
        self.namespaces.append(namespace)
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


class AuthenticationDeniedError(Exception):
    pass


def _segment(text: str = "一旦こちらで持ち帰ります。") -> MergedTranscriptSegment:
    return MergedTranscriptSegment(
        sequence=12,
        speaker_label="SPEAKER_01",
        start=84.2,
        end=90,
        text=text,
        language="ja",
    )


def _draft(quote: str = "一旦こちらで持ち帰ります。") -> dict[str, object]:
    return {
        "description": {"ja": "打ち合わせです。", "zh_hk": "呢段係會議內容。"},
        "tags": [{"ja": "検討", "zh_hk": "研究"}],
        "natural_expressions": [
            {
                "segment_sequence": 12,
                "original_ja": quote,
                "translation_zh_hk": "我哋暫時拎返去研究。",
                "usage_ja": "職場で保留するときの表現です。",
                "usage_zh_hk": "職場表示要內部研究時使用。",
            }
        ],
        "highlights": [],
    }


def test_lmstudio_uses_exactly_one_loaded_handle_and_structured_schema() -> None:
    model = FakeModel(_draft())
    client = FakeClient([model])
    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234"),
        client_factory=lambda _settings: FakeContext(client),
    )

    result = provider.analyze("recording-id", [_segment()])

    assert result.status is AnalysisStatus.COMPLETED
    assert result.model == "loaded/model-instance"
    assert result.schema_version == 2
    assert client.namespaces == ["llm"]
    assert len(model.calls) == 1
    _prompt, kwargs = model.calls[0]
    assert "response_format" in kwargs
    assert "model" not in kwargs
    assert result.data is not None
    expression = result.data["natural_expressions"][0]  # type: ignore[index]
    assert expression["start_time"] == 84.2  # type: ignore[index]
    assert expression["speaker_label"] == "SPEAKER_01"  # type: ignore[index]


@pytest.mark.parametrize("model_count", [0, 2])
def test_lmstudio_requires_exactly_one_loaded_llm(model_count: int) -> None:
    client = FakeClient([FakeModel(_draft()) for _ in range(model_count)])
    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234"),
        client_factory=lambda _settings: FakeContext(client),
    )

    with pytest.raises(ProviderConfigurationError):
        provider.analyze("recording-id", [_segment()])


def test_lmstudio_rejects_ungrounded_japanese_quotes() -> None:
    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234"),
        client_factory=lambda _settings: FakeContext(FakeClient([FakeModel(_draft("偽の引用"))])),
    )

    with pytest.raises(PermanentProcessingError) as caught:
        provider.analyze("recording-id", [_segment()])

    assert caught.value.code == "lmstudio_quote_not_grounded"


def test_lmstudio_maps_timeout_to_retryable_failure() -> None:
    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234"),
        client_factory=lambda _settings: FailingContext(TimeoutError()),
    )

    with pytest.raises(RetryableProcessingError) as caught:
        provider.analyze("recording-id", [_segment()])

    assert caught.value.code == "lmstudio_unavailable"


def test_lmstudio_maps_authentication_failure_without_exposing_details() -> None:
    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234"),
        client_factory=lambda _settings: FailingContext(
            AuthenticationDeniedError("Authentication failed for secret-token")
        ),
    )

    with pytest.raises(ProviderConfigurationError) as caught:
        provider.analyze("recording-id", [_segment()])

    assert caught.value.code == "lmstudio_authentication_failed"
    assert "secret-token" not in caught.value.safe_message


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(parsed=None, structured=True),
        SimpleNamespace(parsed=_draft() | {"unexpected": True}, structured=True),
        SimpleNamespace(parsed=_draft(), structured=False),
    ],
)
def test_lmstudio_rejects_missing_invalid_or_unstructured_output(response: object) -> None:
    class InvalidModel(FakeModel):
        def respond(self, prompt: str, **kwargs: object) -> object:
            self.calls.append((prompt, kwargs))
            return response

    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234"),
        client_factory=lambda _settings: FakeContext(FakeClient([InvalidModel(_draft())])),
    )

    with pytest.raises(PermanentProcessingError) as caught:
        provider.analyze("recording-id", [_segment()])

    assert caught.value.code == "lmstudio_schema_invalid"


def test_lmstudio_hierarchically_reduces_and_deduplicates_all_chunks() -> None:
    quote = "一旦こちらで持ち帰ります。"
    model = FakeModel(_draft())
    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234", chunk_chars=1000),
        client_factory=lambda _settings: FakeContext(FakeClient([model])),
    )

    result = provider.analyze("recording-id", [_segment(quote + ("あ" * 1800))])

    assert len(model.calls) >= 3
    assert result.data is not None
    assert len(result.data["natural_expressions"]) == 1  # type: ignore[arg-type]


def test_chunking_covers_oversized_segment_tail() -> None:
    text = "開始" + ("あ" * 3000) + "終了"
    chunks = chunk_transcript([_segment(text)], 1000)

    assert len(chunks) > 1
    assert "開始" in chunks[0]
    assert "終了" in chunks[-1]
    assert all(len(chunk) <= 1000 for chunk in chunks)
