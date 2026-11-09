"""Strata's OpenAI-compatible server, behind the client shape LM Studio's SDK has.

The analysis, translation and day-summary steps were written against the LM
Studio SDK: list the loaded models, then ask the one that is loaded to respond
in a Pydantic response format. This module gives Strata that same shape, so
each step keeps its chunking, validation and grounding unchanged whichever
server ``LLM_PROVIDER`` selects.

Only prompt text built from transcripts and analyses leaves this process; audio
never does. Nothing here names, loads, or downloads a model: each request
carries back the identifier Strata itself reported as loaded.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException, HTTPMessage
from typing import IO, Any, ClassVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from pydantic import BaseModel, ConfigDict, Field

from audio_server.processing.errors import (
    PermanentProcessingError,
    ProcessingError,
    ProviderConfigurationError,
    RetryableProcessingError,
)

logger = logging.getLogger(__name__)

# A reply is bounded by max_tokens (at most 32768) of answer and reasoning
# together, so a legitimate one stays far below these. They only stop a
# misbehaving server from filling the worker's memory.
_MAX_LINE_BYTES = 4 * 1024 * 1024
_MAX_REPLY_BYTES = 16 * 1024 * 1024

# Statuses that describe a busy or restarting server rather than a bad request.
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def is_strata_base_url(value: str) -> bool:
    """Whether ``value`` is an http(s) server root that carries no credentials."""

    try:
        parsed = urlsplit(value.strip())
        port_is_valid = parsed.port is None or parsed.port > 0
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and port_is_valid
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


@dataclass(frozen=True, slots=True)
class StrataSettings:
    # Stored with each result and used as the prefix of its error codes.
    provider_name: ClassVar[str] = "strata"
    provider_label: ClassVar[str] = "Strata"

    base_url: str
    api_key: str = ""
    timeout_seconds: int = 600
    chunk_chars: int = 12_000
    max_tokens: int = 4096

    def __post_init__(self) -> None:
        if not is_strata_base_url(self.base_url):
            raise ValueError("Strata base URL must be an http(s) URL without credentials")
        if self.timeout_seconds <= 0:
            raise ValueError("Strata timeout must be positive")
        if not 1000 <= self.chunk_chars <= 100_000:
            raise ValueError("Strata chunk size is out of range")
        if not 256 <= self.max_tokens <= 32_768:
            raise ValueError("Strata max tokens is out of range")


@dataclass(frozen=True, slots=True)
class StrataReply:
    """A finished reply, in the shape ``structured_payload`` reads.

    Strata returns a reasoning model's thinking apart from its answer, so
    ``content`` holds the answer alone; the reasoning is never kept.
    """

    content: str


class _ListedModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)


class _ModelList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: list[_ListedModel]


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str | None = None


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = 0
    delta: _Message | None = None
    message: _Message | None = None
    finish_reason: str | None = None


class _Completion(BaseModel):
    """A whole completion or one streamed chunk of it; both carry choices."""

    model_config = ConfigDict(extra="ignore")

    choices: list[_Choice] = Field(default_factory=list)


class _NoRedirect(HTTPRedirectHandler):
    """Refuse redirects so the API key is never sent to another address."""

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        del req, fp, code, msg, headers, newurl
        return None


def _direct_opener() -> OpenerDirector:
    # An empty ProxyHandler ignores HTTP(S)_PROXY, so the key goes straight to
    # the configured server and never through a proxy.
    return build_opener(ProxyHandler({}), _NoRedirect())


class StrataModel:
    """The model Strata reported loaded, answering as the SDK's handle does."""

    def __init__(self, client: StrataClient, identifier: str) -> None:
        self._client = client
        self.identifier = identifier

    def respond(self, prompt: str, **kwargs: object) -> StrataReply:
        """Answer ``prompt`` in the given Pydantic ``response_format``.

        The keywords are the SDK's: ``response_format`` is the schema class and
        ``config`` may carry ``temperature`` and ``maxTokens``.
        """

        schema = kwargs.get("response_format")
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise TypeError("A Strata request needs a Pydantic response format")
        config = kwargs.get("config")
        options = config if isinstance(config, Mapping) else {}
        temperature = options.get("temperature")
        max_tokens = options.get("maxTokens")
        return StrataReply(
            content=self._client.complete(
                self.identifier,
                prompt,
                schema,
                temperature=(
                    float(temperature)
                    if isinstance(temperature, int | float) and not isinstance(temperature, bool)
                    else None
                ),
                max_tokens=(
                    max_tokens
                    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool)
                    else None
                ),
            )
        )


class StrataClient:
    """Calls one Strata server. urllib keeps no connection open between calls."""

    def __init__(self, settings: StrataSettings, *, opener: OpenerDirector | None = None) -> None:
        self._settings = settings
        self._base_url = settings.base_url.strip().rstrip("/")
        self._opener = opener or _direct_opener()

    def list_loaded_models(self, namespace: str | None = None) -> list[StrataModel]:
        """Return the models Strata has loaded; its model list names no others."""

        del namespace  # Strata's list holds chat models only, so there is nothing to filter.
        with _transport_failures(), self._open("/v1/models", None) as response:
            raw = response.read(_MAX_REPLY_BYTES + 1)
        try:
            if len(raw) > _MAX_REPLY_BYTES:
                raise ValueError("model list is too large")
            listing = _ModelList.model_validate_json(raw)
        except ValueError:
            raise _response_invalid() from None
        return [StrataModel(self, entry.id) for entry in listing.data]

    def complete(
        self,
        model: str,
        prompt: str,
        schema: type[BaseModel],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Return the answer text of one structured chat completion."""

        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__.lstrip("_") or "response",
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            },
            "max_tokens": max_tokens or self._settings.max_tokens,
            # Strata keeps sending keep-alive comments while it works, which is
            # what lets the socket timeout act as an inactivity timeout rather
            # than a limit on how long the whole reply may take.
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        with _transport_failures(), self._open("/v1/chat/completions", payload) as response:
            content_type = response.headers.get_content_type()
            if content_type == "text/event-stream":
                content, finish_reason = _read_events(response)
            else:
                content, finish_reason = _read_completion(response)
        if finish_reason == "length":
            logger.warning(
                "Strata stopped a reply at its token limit",
                extra={"max_tokens": payload["max_tokens"]},
            )
            raise PermanentProcessingError(
                code="strata_output_truncated",
                safe_message=(
                    "Strata stopped before finishing its reply; raise STRATA_MAX_TOKENS "
                    "or load a model with a longer context."
                ),
            )
        return content

    def _open(self, path: str, payload: Mapping[str, object] | None) -> Any:
        headers = {"Accept": "application/json, text/event-stream"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        request = Request(
            self._base_url + path,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        return self._opener.open(request, timeout=self._settings.timeout_seconds)


@contextmanager
def strata_client(settings: StrataSettings) -> Iterator[StrataClient]:
    yield StrataClient(settings)


@contextmanager
def _transport_failures() -> Iterator[None]:
    """Map what can go wrong on the wire onto the retry policy.

    Only the status code and the exception type are logged: a response body
    may echo the prompt, and an error's text may carry the address or more.
    """

    try:
        yield
    except ProcessingError:
        raise
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise _http_failure(status) from None
    except (URLError, HTTPException, OSError) as exc:
        # URLError covers refused connections and DNS failures, OSError a
        # socket timeout or reset mid-reply, HTTPException a truncated one.
        logger.warning("Strata request failed", extra={"error_type": type(exc).__name__})
        raise _unavailable() from exc


def _http_failure(status: int) -> ProcessingError:
    logger.warning("Strata answered with an error status", extra={"status": status})
    if status in {401, 403}:
        return ProviderConfigurationError(
            code="strata_authentication_failed",
            safe_message="Strata rejected the configured STRATA_API_KEY.",
        )
    if status in _RETRYABLE_STATUSES:
        return _unavailable()
    return _request_rejected()


def _request_rejected() -> PermanentProcessingError:
    return PermanentProcessingError(
        code="strata_request_rejected",
        safe_message=(
            "Strata rejected the request; check STRATA_BASE_URL and that the loaded model "
            "supports structured output and fits the prompt in its context."
        ),
    )


def _unavailable() -> RetryableProcessingError:
    return RetryableProcessingError(
        code="strata_unavailable",
        safe_message="Strata is temporarily unavailable.",
    )


def _response_invalid() -> PermanentProcessingError:
    return PermanentProcessingError(
        code="strata_response_invalid",
        safe_message="Strata returned a response this server cannot read.",
    )


def _read_completion(response: Any) -> tuple[str, str | None]:
    """Read a reply the server sent whole rather than streamed."""

    raw = response.read(_MAX_REPLY_BYTES + 1)
    try:
        if len(raw) > _MAX_REPLY_BYTES:
            raise ValueError("reply is too large")
        payload = json.loads(raw)
        _refuse_error_payload(payload)
        completion = _Completion.model_validate(payload)
    except ValueError:
        raise _response_invalid() from None
    for choice in completion.choices:
        if choice.index == 0 and choice.message is not None:
            return choice.message.content or "", choice.finish_reason
    raise _response_invalid()


def _read_events(response: Any) -> tuple[str, str | None]:
    """Collect a streamed reply from its server-sent events.

    Comment lines, which is how Strata sends its keep-alives, are skipped. The
    reply is complete at ``[DONE]``, or at the end of the stream once a finish
    reason has arrived; a stream that ends before either was cut off.
    """

    pieces: list[str] = []
    finish_reason: str | None = None
    data: list[str] = []
    received = 0
    while True:
        line: bytes = response.readline(_MAX_LINE_BYTES + 1)
        received += len(line)
        if len(line) > _MAX_LINE_BYTES or received > _MAX_REPLY_BYTES:
            raise _response_invalid()
        if line.strip(b"\r\n"):
            try:
                text = line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                raise _response_invalid() from None
            field, _separator, value = text.partition(":")
            if field == "data":
                data.append(value.removeprefix(" "))
            # Anything else -- a comment, or an event, id or retry field --
            # carries nothing this reply needs.
            continue
        if data:
            event = "\n".join(data)
            data = []
            if event == "[DONE]":
                return "".join(pieces), finish_reason
            piece, reason = _event_delta(event)
            pieces.append(piece)
            finish_reason = reason or finish_reason
        if not line:
            break
    if finish_reason is None:
        raise _unavailable()
    return "".join(pieces), finish_reason


def _event_delta(event: str) -> tuple[str, str | None]:
    try:
        payload = json.loads(event)
        _refuse_error_payload(payload)
        chunk = _Completion.model_validate(payload)
    except ValueError:
        raise _response_invalid() from None
    for choice in chunk.choices:
        if choice.index == 0:
            delta = choice.delta if choice.delta is not None else choice.message
            content = delta.content if delta is not None else None
            return content or "", choice.finish_reason
    return "", None


def _refuse_error_payload(payload: object) -> None:
    """Raise when the server answered with an error object instead of a reply."""

    if isinstance(payload, Mapping) and "error" in payload:
        logger.warning("Strata returned an error instead of a reply")
        raise _request_rejected()
