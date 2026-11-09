"""Strata as the local LLM server, against a stand-in that speaks its protocol."""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.request import OpenerDirector

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from audio_server.core.config import Settings
from audio_server.processing import analysis
from audio_server.processing.analysis import LMStudioAnalysisProvider
from audio_server.processing.contracts import (
    AnalysisStatus,
    DailyRecordingDigest,
    MergedTranscriptSegment,
)
from audio_server.processing.daily_summary import LMStudioDailySummaryProvider
from audio_server.processing.errors import (
    PermanentProcessingError,
    ProcessingError,
    ProviderConfigurationError,
    RetryableProcessingError,
)
from audio_server.processing.strata import StrataClient, StrataSettings
from audio_server.processing.translation import LMStudioTranslationProvider
from audio_server.worker_runtime import (
    _build_analysis_provider,
    _build_daily_summary_provider,
    _build_translation_provider,
)

API_KEY = "synthetic-strata-key"
QUOTE = "一旦こちらで持ち帰ります。"


@dataclass
class Reply:
    status: int = 200
    content_type: str = "application/json"
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Seen:
    method: str
    path: str
    authorization: str | None
    body: Any


class FakeStrata:
    """Records every request and answers the way Strata's server does."""

    def __init__(self) -> None:
        self.base_url = ""
        self.models = ["loaded-model"]
        self.chat: list[Reply] = []
        self.routes: dict[str, Reply] = {}
        self.seen: list[Seen] = []

    def reply_for(self, method: str, path: str) -> Reply:
        if path in self.routes:
            return self.routes[path]
        if (method, path) == ("GET", "/v1/models"):
            data = [{"id": name, "object": "model", "owned_by": "strata"} for name in self.models]
            return Reply(body=json.dumps({"object": "list", "data": data}).encode())
        if (method, path) == ("POST", "/v1/chat/completions") and self.chat:
            return self.chat.pop(0) if len(self.chat) > 1 else self.chat[0]
        return Reply(status=404, body=b'{"error":"not found"}')


def _handler(strata: FakeStrata) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._answer()

        def do_POST(self) -> None:
            self._answer()

        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            strata.seen.append(
                Seen(
                    method=self.command,
                    path=self.path,
                    authorization=self.headers.get("Authorization"),
                    body=json.loads(raw) if raw else None,
                )
            )
            reply = strata.reply_for(self.command, self.path)
            self.send_response(reply.status)
            self.send_header("Content-Type", reply.content_type)
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(reply.body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler


@pytest.fixture
def strata() -> Iterator[FakeStrata]:
    fake = FakeStrata()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    # A short poll interval keeps shutdown from waiting half a second per test.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    fake.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _settings(strata: FakeStrata, **overrides: Any) -> StrataSettings:
    return StrataSettings(
        base_url=strata.base_url, api_key=API_KEY, timeout_seconds=5, **overrides
    )


def _stream(
    content: str, *, finish: str | None = "stop", reasoning: str = "", done: bool = True
) -> Reply:
    """A reply as Strata streams it: keep-alives first, the answer in pieces."""

    half = len(content) // 2
    first: dict[str, str] = {"content": content[:half]}
    if reasoning:
        first["reasoning_content"] = reasoning
    deltas: list[tuple[dict[str, str], str | None]] = [
        ({"role": "assistant", "content": ""}, None),
        (first, None),
        ({"content": content[half:]}, None),
    ]
    if finish is not None:
        deltas.append(({}, finish))
    lines = [": keep-alive", "", ": keep-alive", ""]
    for delta, reason in deltas:
        chunk = {
            "object": "chat.completion.chunk",
            "model": "loaded-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        }
        lines += [f"data: {json.dumps(chunk, ensure_ascii=False)}", ""]
    if done:
        lines += ["data: [DONE]", ""]
    return Reply(content_type="text/event-stream", body=("\n".join(lines) + "\n").encode())


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zh_hk: str = Field(min_length=1)


def _segment() -> MergedTranscriptSegment:
    return MergedTranscriptSegment(
        sequence=3, speaker_label="SPEAKER_00", start=1.0, end=3.5, text=QUOTE, language="ja"
    )


def _draft() -> dict[str, object]:
    return {
        "description": {"ja": "打ち合わせです。", "zh_hk": "呢段係會議內容。"},
        "summary": {
            "ja": "来週の対応を確認しました。担当者は持ち帰って検討します。",
            "zh_hk": "確認咗下星期點處理。負責人會帶返去研究。",
        },
        "tags": [{"ja": "検討", "zh_hk": "研究"}],
        "natural_expressions": [
            {
                "segment_sequence": 3,
                "original_ja": QUOTE,
                "translation_zh_hk": "我哋暫時拎返去研究。",
                "usage_ja": "保留するときの表現です。",
                "usage_zh_hk": "表示要內部研究時用。",
            }
        ],
        "highlights": [],
    }


def test_strata_lists_its_loaded_models_with_the_configured_key(strata: FakeStrata) -> None:
    strata.models = ["iq3_s"]

    models = StrataClient(_settings(strata)).list_loaded_models("llm")

    assert [model.identifier for model in models] == ["iq3_s"]
    assert [(seen.method, seen.path) for seen in strata.seen] == [("GET", "/v1/models")]
    assert strata.seen[0].authorization == f"Bearer {API_KEY}"


def test_strata_streams_a_structured_answer_and_leaves_out_the_reasoning(
    strata: FakeStrata,
) -> None:
    strata.chat = [_stream('{"zh_hk":"聽日見。"}', reasoning="Thinking it over first.")]
    [model] = StrataClient(_settings(strata)).list_loaded_models()

    reply = model.respond(
        "Sentence: 明日会いましょう。",
        response_format=Answer,
        config={"temperature": 0.2, "maxTokens": 512},
    )

    assert reply.content == '{"zh_hk":"聽日見。"}'
    assert analysis.structured_payload(reply, Answer).zh_hk == "聽日見。"
    request = strata.seen[-1]
    assert (request.method, request.path) == ("POST", "/v1/chat/completions")
    assert request.authorization == f"Bearer {API_KEY}"
    assert request.body == {
        # The identifier Strata reported, never a configured model name.
        "model": "loaded-model",
        "messages": [{"role": "user", "content": "Sentence: 明日会いましょう。"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "Answer", "strict": True, "schema": Answer.model_json_schema()},
        },
        "max_tokens": 512,
        "stream": True,
        "temperature": 0.2,
    }


def test_strata_reads_an_answer_that_was_sent_whole(strata: FakeStrata) -> None:
    completion = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"zh_hk":"好。"}'},
                "finish_reason": "stop",
            }
        ]
    }
    strata.chat = [Reply(body=json.dumps(completion, ensure_ascii=False).encode())]
    [model] = StrataClient(_settings(strata)).list_loaded_models()

    assert model.respond("prompt", response_format=Answer).content == '{"zh_hk":"好。"}'


def test_a_reply_cut_off_at_the_token_limit_is_not_read_as_an_answer(
    strata: FakeStrata,
) -> None:
    strata.chat = [_stream('{"zh_hk":"聽日', finish="length")]
    [model] = StrataClient(_settings(strata)).list_loaded_models()

    with pytest.raises(PermanentProcessingError) as caught:
        model.respond("prompt", response_format=Answer)

    assert caught.value.code == "strata_output_truncated"
    assert "STRATA_MAX_TOKENS" in caught.value.safe_message


def test_a_stream_that_ends_mid_reply_is_retryable(strata: FakeStrata) -> None:
    strata.chat = [_stream('{"zh_hk":"聽日', finish=None, done=False)]
    [model] = StrataClient(_settings(strata)).list_loaded_models()

    with pytest.raises(RetryableProcessingError) as caught:
        model.respond("prompt", response_format=Answer)

    assert caught.value.code == "strata_unavailable"


def test_an_error_event_in_the_stream_rejects_the_request(strata: FakeStrata) -> None:
    strata.chat = [
        Reply(
            content_type="text/event-stream",
            body=b': keep-alive\n\ndata: {"error": {"message": "context exceeded"}}\n\n',
        )
    ]
    [model] = StrataClient(_settings(strata)).list_loaded_models()

    with pytest.raises(PermanentProcessingError) as caught:
        model.respond("prompt", response_format=Answer)

    assert caught.value.code == "strata_request_rejected"


@pytest.mark.parametrize(
    ("status", "error_type", "code"),
    [
        (401, ProviderConfigurationError, "strata_authentication_failed"),
        (403, ProviderConfigurationError, "strata_authentication_failed"),
        (429, RetryableProcessingError, "strata_unavailable"),
        (503, RetryableProcessingError, "strata_unavailable"),
        (400, PermanentProcessingError, "strata_request_rejected"),
        (404, PermanentProcessingError, "strata_request_rejected"),
    ],
)
def test_strata_error_statuses_follow_the_retry_policy(
    strata: FakeStrata,
    caplog: pytest.LogCaptureFixture,
    status: int,
    error_type: type[ProcessingError],
    code: str,
) -> None:
    strata.routes["/v1/models"] = Reply(status=status, body=b'{"error":"Invalid API key"}')

    with (
        caplog.at_level(logging.DEBUG, logger="audio_server.processing.strata"),
        pytest.raises(ProcessingError) as caught,
    ):
        StrataClient(_settings(strata)).list_loaded_models()

    assert type(caught.value) is error_type
    assert caught.value.code == code
    assert API_KEY not in caught.value.safe_message
    # Only the status reaches the log; the body may echo what was sent.
    assert all(
        API_KEY not in str(vars(record)) and "Invalid API key" not in str(vars(record))
        for record in caplog.records
    )


def test_strata_never_follows_a_redirect_with_the_key(strata: FakeStrata) -> None:
    strata.routes["/v1/models"] = Reply(status=302, headers={"Location": "/elsewhere"})

    with pytest.raises(PermanentProcessingError) as caught:
        StrataClient(_settings(strata)).list_loaded_models()

    assert caught.value.code == "strata_request_rejected"
    assert [seen.path for seen in strata.seen] == ["/v1/models"]


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"object": "list"}', b'{"object": "list", "data": [{"id": ""}]}'],
)
def test_an_unreadable_model_list_fails_safely(strata: FakeStrata, body: bytes) -> None:
    strata.routes["/v1/models"] = Reply(body=body)

    with pytest.raises(PermanentProcessingError) as caught:
        StrataClient(_settings(strata)).list_loaded_models()

    assert caught.value.code == "strata_response_invalid"


def test_an_unreachable_strata_is_retryable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = StrataSettings(base_url=f"http://127.0.0.1:{port}", timeout_seconds=5)

    with pytest.raises(RetryableProcessingError) as caught:
        StrataClient(settings).list_loaded_models()

    assert caught.value.code == "strata_unavailable"


def test_a_strata_that_stops_answering_is_retryable() -> None:
    class TimingOut(OpenerDirector):
        def open(self, *args: object, **kwargs: object) -> Any:
            del args, kwargs
            raise TimeoutError("timed out")

    client = StrataClient(StrataSettings(base_url="http://strata.test:47831"), opener=TimingOut())

    with pytest.raises(RetryableProcessingError) as caught:
        client.list_loaded_models()

    assert caught.value.code == "strata_unavailable"


def test_analysis_through_strata_is_stored_under_strata(strata: FakeStrata) -> None:
    strata.models = ["iq3_s"]
    strata.chat = [_stream(json.dumps(_draft(), ensure_ascii=False))]

    result = LMStudioAnalysisProvider(_settings(strata)).analyze("recording-id", [_segment()])

    assert result.status is AnalysisStatus.COMPLETED
    assert (result.provider, result.model) == ("strata", "iq3_s")
    assert result.data is not None
    expression = result.data["natural_expressions"][0]  # type: ignore[index]
    assert expression["original_ja"] == QUOTE  # type: ignore[index]
    assert expression["start_time"] == 1.0  # type: ignore[index]
    request = strata.seen[-1].body
    assert request["model"] == "iq3_s"
    assert QUOTE in request["messages"][0]["content"]
    assert (
        request["response_format"]["json_schema"]["schema"]
        == analysis._GenerationAnalysisDraft.model_json_schema()
    )


def test_an_answer_that_breaks_the_schema_is_named_after_strata(strata: FakeStrata) -> None:
    strata.chat = [_stream('{"unexpected": true}')]

    with pytest.raises(PermanentProcessingError) as caught:
        LMStudioAnalysisProvider(_settings(strata)).analyze("recording-id", [_segment()])

    assert caught.value.code == "strata_schema_invalid"
    assert caught.value.safe_message == "Strata returned an invalid structured analysis."


def test_translation_through_strata_is_stored_under_strata(strata: FakeStrata) -> None:
    strata.chat = [_stream('{"zh_hk":"我哋暫時拎返去研究。"}')]

    result = LMStudioTranslationProvider(_settings(strata)).translate(
        "recording-id", [_segment()]
    )

    assert (result.provider, result.model) == ("strata", "loaded-model")
    assert [item.text_zh_hk for item in result.translations] == ["我哋暫時拎返去研究。"]


def test_a_day_summary_through_strata_is_stored_under_strata(strata: FakeStrata) -> None:
    draft = {
        "overview": {"ja": "会議の日でした。", "zh_hk": "今日主要係開會。"},
        "key_points": [{"recording_index": 0, "ja": "持ち帰って検討。", "zh_hk": "帶返去研究。"}],
        "tags": [{"ja": "会議", "zh_hk": "會議"}],
    }
    strata.chat = [_stream(json.dumps(draft, ensure_ascii=False))]
    digest = DailyRecordingDigest(
        index=0,
        recording_id="recording-1",
        time_label="10:00",
        description_ja="会議です。",
        description_zh_hk="開會。",
    )

    result = LMStudioDailySummaryProvider(_settings(strata)).summarize("2026-11-06", [digest])

    assert (result.provider, result.model) == ("strata", "loaded-model")
    assert result.data is not None
    assert result.data["key_points"][0]["recording_id"] == "recording-1"  # type: ignore[index]


@pytest.mark.parametrize(
    "run",
    [
        lambda settings: LMStudioAnalysisProvider(settings).analyze("recording-id", [_segment()]),
        lambda settings: LMStudioTranslationProvider(settings).translate(
            "recording-id", [_segment()]
        ),
    ],
    ids=["analysis", "translation"],
)
@pytest.mark.parametrize(
    ("models", "code"),
    [([], "strata_model_not_loaded"), (["first", "second"], "strata_multiple_models")],
)
def test_strata_needs_exactly_one_loaded_model(
    strata: FakeStrata,
    run: Callable[[StrataSettings], object],
    models: list[str],
    code: str,
) -> None:
    strata.models = models

    with pytest.raises(ProviderConfigurationError) as caught:
        run(_settings(strata))

    assert caught.value.code == code
    assert "Strata" in caught.value.safe_message
    # Nothing was asked of a model the worker would have had to guess at.
    assert [seen.path for seen in strata.seen] == ["/v1/models"]


def test_strata_failures_never_log_the_key_or_the_transcript(
    strata: FakeStrata, caplog: pytest.LogCaptureFixture
) -> None:
    strata.chat = [Reply(status=500, body=QUOTE.encode())]

    with caplog.at_level(logging.DEBUG), pytest.raises(RetryableProcessingError):
        LMStudioAnalysisProvider(_settings(strata)).analyze("recording-id", [_segment()])

    logged = " ".join(str(vars(record)) for record in caplog.records)
    assert API_KEY not in logged
    assert "持ち帰ります" not in logged


def test_strata_is_a_supported_llm_provider() -> None:
    settings = Settings(_env_file=None, llm_enabled=True, llm_provider=" Strata ")  # type: ignore[call-arg]

    assert settings.llm_provider == "strata"


def test_an_unknown_llm_provider_is_rejected() -> None:
    with pytest.raises(ValidationError, match="lmstudio or strata"):
        Settings(_env_file=None, llm_enabled=True, llm_provider="openai")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "base_url",
    [
        "127.0.0.1:47831",
        "ftp://127.0.0.1:47831",
        "http://admin:synthetic-url-password@127.0.0.1:47831",
        "http://127.0.0.1:47831/?key=synthetic-url-password",
        "http://127.0.0.1:not-a-port",
    ],
)
def test_a_strata_base_url_must_be_a_plain_http_root(base_url: str) -> None:
    with pytest.raises(ValidationError, match="STRATA_BASE_URL") as caught:
        Settings(  # type: ignore[call-arg]
            _env_file=None, llm_enabled=True, llm_provider="strata", strata_base_url=base_url
        )

    assert "synthetic-url-password" not in str(caught.value)
    # The value only matters once Strata is the selected server.
    Settings(  # type: ignore[call-arg]
        _env_file=None, llm_enabled=True, llm_provider="lmstudio", strata_base_url=base_url
    )


def test_the_worker_sends_strata_requests_with_the_configured_key(strata: FakeStrata) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_enabled=True,
        llm_provider="strata",
        strata_base_url=strata.base_url,
        strata_api_key=API_KEY,
        strata_max_tokens=2048,
    )
    strata.chat = [_stream('{"zh_hk":"我哋暫時拎返去研究。"}')]

    result = _build_translation_provider(settings).translate("recording-id", [_segment()])

    assert result.provider == "strata"
    assert {seen.authorization for seen in strata.seen} == {f"Bearer {API_KEY}"}
    assert strata.seen[-1].body["max_tokens"] == 2048


@pytest.mark.parametrize("provider", ["lmstudio", "strata"])
def test_the_worker_builds_every_llm_step_for_the_selected_server(provider: str) -> None:
    settings = Settings(_env_file=None, llm_enabled=True, llm_provider=provider)  # type: ignore[call-arg]

    names = {
        _build_analysis_provider(settings).name,
        _build_translation_provider(settings).name,
        _build_daily_summary_provider(settings).name,
    }

    assert names == {provider}
