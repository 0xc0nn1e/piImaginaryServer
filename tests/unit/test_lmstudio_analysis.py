from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from audio_server.processing import analysis
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
        "summary": {
            "ja": "来週の対応について確認した打ち合わせです。担当者は内容を持ち帰って検討します。",
            "zh_hk": "呢段會議確認咗下星期嘅處理安排。負責人會將內容帶返去再研究。",
        },
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
    response_format = kwargs["response_format"]
    assert isinstance(response_format, type)
    schema = response_format.model_json_schema()
    assert '"maxLength":' not in json.dumps(schema)
    assert result.data is not None
    assert result.data["summary"]["ja"].startswith("来週")  # type: ignore[index]
    expression = result.data["natural_expressions"][0]  # type: ignore[index]
    assert expression["start_time"] == 84.2  # type: ignore[index]
    assert expression["end_time"] == 90  # type: ignore[index]
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


def test_lmstudio_discards_ungrounded_quotes_without_losing_valid_analysis() -> None:
    draft = _draft("偽の引用")
    draft["highlights"] = [
        {
            "segment_sequence": 12,
            "original_ja": "存在しないハイライト",
            "translation_zh_hk": "不存在嘅重點",
            "reason_ja": "重要です。",
            "reason_zh_hk": "呢點重要。",
        }
    ]
    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234"),
        client_factory=lambda _settings: FakeContext(FakeClient([FakeModel(draft)])),
    )

    result = provider.analyze("recording-id", [_segment()])

    assert result.status is AnalysisStatus.COMPLETED
    assert result.data is not None
    assert result.data["description"]["ja"] == "打ち合わせです。"  # type: ignore[index]
    assert result.data["summary"]["ja"].startswith("来週")  # type: ignore[index]
    assert result.data["tags"] == [{"ja": "検討", "zh_hk": "研究"}]
    assert result.data["natural_expressions"] == []
    assert result.data["highlights"] == []


def test_lmstudio_unwraps_quote_marks_only_when_inner_text_is_exactly_grounded() -> None:
    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234"),
        client_factory=lambda _settings: FakeContext(
            FakeClient([FakeModel(_draft(" 「一旦こちらで持ち帰ります。」 "))])
        ),
    )

    result = provider.analyze("recording-id", [_segment()])

    assert result.data is not None
    expression = result.data["natural_expressions"][0]  # type: ignore[index]
    assert expression["original_ja"] == "一旦こちらで持ち帰ります。"  # type: ignore[index]


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
        SimpleNamespace(parsed="not json at all", content=None, structured=False),
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


@pytest.mark.parametrize(
    "response_for",
    [
        # A mapping, the shape the SDK returned when this was written.
        lambda draft: SimpleNamespace(parsed=draft, structured=True),
        # The parsed instance of the response format, which a later SDK hands
        # back instead.
        lambda draft: SimpleNamespace(
            parsed=analysis._GenerationAnalysisDraft.model_validate(draft), structured=True
        ),
        # The raw JSON text.
        lambda draft: SimpleNamespace(parsed=json.dumps(draft), structured=True),
        # No parsed field at all, with the JSON only on the content.
        lambda draft: SimpleNamespace(parsed=None, content=json.dumps(draft), structured=True),
        # A complete result the SDK nonetheless flagged as unstructured. The
        # flag is not the payload, and refusing over it is what took analysis
        # down; the strict draft schema still decides what may be stored.
        lambda draft: SimpleNamespace(parsed=draft, structured=False),
    ],
)
def test_lmstudio_reads_every_shape_a_structured_response_has_come_back_as(
    response_for: Callable[[dict[str, object]], object],
) -> None:
    class ShapedModel(FakeModel):
        def respond(self, prompt: str, **kwargs: object) -> object:
            self.calls.append((prompt, kwargs))
            return response_for(_draft())

    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234"),
        client_factory=lambda _settings: FakeContext(FakeClient([ShapedModel(_draft())])),
    )

    # An SDK upgrade that moves the payload must not read as every analysis and
    # translation on the server suddenly returning invalid output.
    result = provider.analyze("recording-id", [_segment()])

    assert result.status is AnalysisStatus.COMPLETED
    assert result.data is not None


def test_an_unreadable_response_names_its_shape_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class UnreadableModel(FakeModel):
        def respond(self, prompt: str, **kwargs: object) -> object:
            self.calls.append((prompt, kwargs))
            return SimpleNamespace(parsed=object(), content=None, structured=False)

    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234"),
        client_factory=lambda _settings: FakeContext(FakeClient([UnreadableModel(_draft())])),
    )

    with (
        caplog.at_level(logging.WARNING, logger="audio_server.processing.analysis"),
        pytest.raises(PermanentProcessingError),
    ):
        provider.analyze("recording-id", [_segment()])

    # Without this the failure is silent, which is how an SDK whose reply
    # changed shape read as "invalid analysis" for days with nothing to chase.
    record = next(item for item in caplog.records if "unreadable shape" in item.getMessage())
    assert record.parsed_type == "object"
    assert record.content_type == "NoneType"
    assert record.structured_flag is False
    # The reply itself is model output and must never reach the log.
    assert not hasattr(record, "parsed")


def test_lmstudio_enforces_string_limits_after_structured_generation() -> None:
    oversized = _draft()
    oversized["description"] = {"ja": "あ" * 4001, "zh_hk": "有效內容"}
    provider = LMStudioAnalysisProvider(
        LMStudioSettings(host="lmstudio.test:1234"),
        client_factory=lambda _settings: FakeContext(FakeClient([FakeModel(oversized)])),
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


def test_safe_violations_reports_paths_and_rules_without_model_content() -> None:
    secret = "機密の発話内容"
    draft = {
        "description": {"ja": "あ", "zh_hk": "a"},
        "summary": {"ja": "あ", "zh_hk": "a"},
        "tags": [{"ja": secret, "zh_hk": "a"} for _ in range(15)],
        "natural_expressions": [
            {
                "segment_sequence": 0,
                "original_ja": "",
                "translation_zh_hk": "a",
                "usage_ja": "a",
                "usage_zh_hk": "a",
            }
        ],
        "highlights": [],
    }

    with pytest.raises(ValidationError) as caught:
        analysis._AnalysisDraft.model_validate(draft)

    violations = analysis._safe_violations(caught.value)
    assert "tags:too_long" in violations
    assert "natural_expressions.0.original_ja:string_too_short" in violations
    # The rejected values are model output and must never reach the log.
    assert all(secret not in line for line in violations)


def test_safe_violations_redacts_model_invented_keys() -> None:
    leaked_key = "モデルが勝手に作ったキー"
    draft = {
        "description": {"ja": "あ", "zh_hk": "a"},
        "summary": {"ja": "あ", "zh_hk": "a"},
        "tags": [],
        "natural_expressions": [],
        "highlights": [],
        leaked_key: "value",
    }

    with pytest.raises(ValidationError) as caught:
        analysis._AnalysisDraft.model_validate(draft)

    violations = analysis._safe_violations(caught.value)
    # extra="forbid" puts the model's own key in the path; it must not be logged.
    assert violations == ["<redacted>:extra_forbidden"]
