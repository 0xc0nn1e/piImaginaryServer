"""The day summary reuses the analysis client, so it must fail the same way.

It also resolves the model's recording numbers itself: the reply names an
index, never an identifier, so an invented number is dropped instead of
pointing a key point at somebody else's recording.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import SimpleNamespace
from typing import Any

import pytest

from audio_server.processing import daily_summary
from audio_server.processing.analysis import LMStudioSettings
from audio_server.processing.contracts import AnalysisStatus, DailyRecordingDigest
from audio_server.processing.daily_summary import LMStudioDailySummaryProvider
from audio_server.processing.errors import (
    PermanentProcessingError,
    ProviderConfigurationError,
    RetryableProcessingError,
)

SETTINGS = LMStudioSettings(host="127.0.0.1:1234", api_key="", timeout_seconds=30)
RECORDING_A = "11111111-1111-4111-8111-111111111111"
RECORDING_B = "22222222-2222-4222-8222-222222222222"


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


def _digest(index: int, recording_id: str) -> DailyRecordingDigest:
    return DailyRecordingDigest(
        index=index,
        recording_id=recording_id,
        time_label=f"0{index + 9}:00",
        description_ja="朝の打ち合わせ。",
        description_zh_hk="朝早開會。",
        summary_ja="進捗の共有。",
        summary_zh_hk="分享進度。",
        tags_ja=("会議",),
        highlights_ja=("よろしくお願いします。",),
    )


def _reply(points: list[dict[str, object]]) -> dict[str, object]:
    return {
        "overview": {"ja": "落ち着いた一日。", "zh_hk": "平靜嘅一日。"},
        "key_points": points,
        "tags": [{"ja": "会議", "zh_hk": "會議"}],
    }


def _provider(client: FakeClient) -> LMStudioDailySummaryProvider:
    return LMStudioDailySummaryProvider(
        SETTINGS, client_factory=lambda _settings: FakeContext(client)
    )


def test_a_day_without_analyses_is_skipped_without_calling_the_model() -> None:
    model = FakeModel([])
    result = _provider(FakeClient([model])).summarize("2026-08-27", [])

    assert result.status is AnalysisStatus.SKIPPED
    assert model.prompts == []


def test_key_points_resolve_to_recording_ids() -> None:
    model = FakeModel([_reply([{"recording_index": 1, "ja": "進捗。", "zh_hk": "進度。"}])])

    result = _provider(FakeClient([model])).summarize(
        "2026-08-27", [_digest(0, RECORDING_A), _digest(1, RECORDING_B)]
    )

    assert result.status is AnalysisStatus.COMPLETED
    assert result.model == "loaded/model-instance"
    assert result.data is not None
    assert result.data["key_points"] == [
        {"recording_id": RECORDING_B, "ja": "進捗。", "zh_hk": "進度。"}
    ]


def test_an_invented_recording_number_is_dropped() -> None:
    model = FakeModel(
        [
            _reply(
                [
                    {"recording_index": 0, "ja": "進捗。", "zh_hk": "進度。"},
                    {"recording_index": 7, "ja": "作り話。", "zh_hk": "作故仔。"},
                ]
            )
        ]
    )

    result = _provider(FakeClient([model])).summarize("2026-08-27", [_digest(0, RECORDING_A)])

    assert result.data is not None
    assert [point["recording_id"] for point in result.data["key_points"]] == [RECORDING_A]


def test_the_prompt_carries_no_transcript_or_audio_reference() -> None:
    model = FakeModel([_reply([])])

    _provider(FakeClient([model])).summarize("2026-08-27", [_digest(0, RECORDING_A)])

    prompt = model.prompts[0]
    assert "RECORDING 0" in prompt
    assert RECORDING_A not in prompt
    assert ".wav" not in prompt


class CyclingModel(FakeModel):
    """Answers every prompt with the same oversized draft."""

    def respond(self, prompt: str, **kwargs: object) -> object:
        self.prompts.append(prompt)
        del kwargs
        return SimpleNamespace(parsed=self.replies[0], structured=True)


def test_partial_results_too_large_to_pair_still_reduce_to_one() -> None:
    # Two partial summaries that each exceed the request budget cannot be
    # grouped by size. Reducing them one at a time would return as many drafts
    # as went in, so the pass has to pair them anyway rather than loop forever.
    long_text = "あ" * 900
    oversized = {
        "overview": {"ja": long_text, "zh_hk": long_text},
        "key_points": [],
        "tags": [],
    }
    model = CyclingModel([oversized])
    settings = LMStudioSettings(host="127.0.0.1:1234", chunk_chars=1000)
    provider = LMStudioDailySummaryProvider(
        settings, client_factory=lambda _settings: FakeContext(FakeClient([model]))
    )
    # Long enough that the two recordings cannot share one request either.
    wordy = [
        DailyRecordingDigest(
            index=index,
            recording_id=recording_id,
            time_label="10:00",
            description_ja="あ" * 600,
            description_zh_hk="啊" * 600,
        )
        for index, recording_id in enumerate((RECORDING_A, RECORDING_B))
    ]

    result = provider.summarize("2026-08-27", wordy)

    assert result.status is AnalysisStatus.COMPLETED
    # Two map requests and one reduce request, not an unbounded stream of them.
    assert len(model.prompts) == 3


def test_work_that_converges_on_the_last_permitted_pass_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Spending the whole reduce budget is not a failure. Four oversized partial
    # summaries halve twice, so with a budget of two passes the run finishes on
    # the last one and must be accepted rather than thrown away.
    monkeypatch.setattr(daily_summary, "MAX_REDUCE_PASSES", 2)
    long_text = "あ" * 900
    oversized = {
        "overview": {"ja": long_text, "zh_hk": long_text},
        "key_points": [],
        "tags": [],
    }
    model = CyclingModel([oversized])
    settings = LMStudioSettings(host="127.0.0.1:1234", chunk_chars=1000)
    provider = LMStudioDailySummaryProvider(
        settings, client_factory=lambda _settings: FakeContext(FakeClient([model]))
    )
    wordy = [
        DailyRecordingDigest(
            index=index,
            recording_id=f"{index}" * 8,
            time_label="10:00",
            description_ja="あ" * 600,
            description_zh_hk="啊" * 600,
        )
        for index in range(4)
    ]

    result = provider.summarize("2026-08-27", wordy)

    assert result.status is AnalysisStatus.COMPLETED
    # Four map requests, then two reduce passes of two and one request.
    assert len(model.prompts) == 7


def test_a_reduce_that_never_converges_fails_instead_of_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daily_summary, "MAX_REDUCE_PASSES", 2)
    monkeypatch.setattr(daily_summary, "_group_drafts", lambda drafts, _max: tuple(
        (draft,) for draft in drafts
    ))
    long_text = "あ" * 900
    model = CyclingModel(
        [{"overview": {"ja": long_text, "zh_hk": long_text}, "key_points": [], "tags": []}]
    )
    settings = LMStudioSettings(host="127.0.0.1:1234", chunk_chars=1000)
    provider = LMStudioDailySummaryProvider(
        settings, client_factory=lambda _settings: FakeContext(FakeClient([model]))
    )
    wordy = [
        DailyRecordingDigest(
            index=index,
            recording_id=f"{index}" * 8,
            time_label="10:00",
            description_ja="あ" * 600,
            description_zh_hk="啊" * 600,
        )
        for index in range(2)
    ]

    with pytest.raises(PermanentProcessingError) as caught:
        provider.summarize("2026-08-27", wordy)

    assert caught.value.code == "lmstudio_daily_summary_not_converging"


def test_an_invalid_structured_reply_fails_permanently() -> None:
    model = FakeModel([{"overview": {"ja": "", "zh_hk": ""}, "key_points": [], "tags": []}])

    with pytest.raises(PermanentProcessingError) as caught:
        _provider(FakeClient([model])).summarize("2026-08-27", [_digest(0, RECORDING_A)])

    assert caught.value.code == "lmstudio_daily_summary_schema_invalid"
    # The rejected model output must not reach the caller.
    assert "ja" not in caught.value.safe_message


def test_two_loaded_models_are_a_configuration_error() -> None:
    client = FakeClient([FakeModel([]), FakeModel([])])

    with pytest.raises(ProviderConfigurationError) as caught:
        _provider(client).summarize("2026-08-27", [_digest(0, RECORDING_A)])

    assert caught.value.code == "lmstudio_multiple_models"


def test_an_unreachable_lm_studio_is_retryable() -> None:
    provider = LMStudioDailySummaryProvider(
        SETTINGS, client_factory=lambda _settings: FailingContext(ConnectionError("refused"))
    )

    with pytest.raises(RetryableProcessingError) as caught:
        provider.summarize("2026-08-27", [_digest(0, RECORDING_A)])

    assert caught.value.code == "lmstudio_unavailable"
