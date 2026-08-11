from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

from starlette.types import Message, Receive, Scope, Send

from audio_server.api.middleware import ApiRequestGuardMiddleware
from audio_server.core.security import TokenAuthenticator

TOKEN = "request-guard-test-token"


def test_private_api_rejects_authentication_before_reading_body() -> None:
    downstream_called = False
    receive_called = False

    async def downstream(_scope: Scope, _receive: Receive, _send: Send) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def receive() -> Message:
        nonlocal receive_called
        receive_called = True
        raise AssertionError("unauthenticated body must not be read")

    middleware = ApiRequestGuardMiddleware(
        downstream,
        authenticator=TokenAuthenticator(TOKEN),
        max_upload_request_bytes=100,
    )
    messages = asyncio.run(_invoke(middleware, receive=receive, headers=[]))

    assert not downstream_called
    assert not receive_called
    assert _status(messages) == 401
    assert _error_code(messages) == "authentication_required"


def test_declared_oversized_upload_is_rejected_before_reading_body() -> None:
    downstream_called = False

    async def downstream(_scope: Scope, _receive: Receive, _send: Send) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def receive() -> Message:
        raise AssertionError("oversized declared body must not be read")

    middleware = ApiRequestGuardMiddleware(
        downstream,
        authenticator=TokenAuthenticator(TOKEN),
        max_upload_request_bytes=100,
    )
    messages = asyncio.run(
        _invoke(
            middleware,
            receive=receive,
            headers=_auth_headers() + [(b"content-length", b"101")],
        )
    )

    assert not downstream_called
    assert _status(messages) == 413
    assert _error_code(messages) == "upload_request_too_large"


def test_streamed_upload_is_stopped_when_actual_bytes_exceed_limit() -> None:
    chunks = iter(
        [
            {"type": "http.request", "body": b"a" * 60, "more_body": True},
            {"type": "http.request", "body": b"b" * 41, "more_body": False},
        ]
    )

    async def downstream(_scope: Scope, receive: Receive, _send: Send) -> None:
        while True:
            message = await receive()
            if not message.get("more_body", False):
                return

    async def receive() -> Message:
        return next(chunks)

    middleware = ApiRequestGuardMiddleware(
        downstream,
        authenticator=TokenAuthenticator(TOKEN),
        max_upload_request_bytes=100,
    )
    messages = asyncio.run(_invoke(middleware, receive=receive, headers=_auth_headers()))

    assert _status(messages) == 413
    assert _error_code(messages) == "upload_request_too_large"


async def _invoke(
    app: ApiRequestGuardMiddleware,
    *,
    receive: Receive,
    headers: list[tuple[bytes, bytes]],
) -> list[Message]:
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/recordings",
        "raw_path": b"/api/v1/recordings",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
        "state": {},
    }
    await app(scope, receive, send)
    return messages


def _auth_headers() -> list[tuple[bytes, bytes]]:
    return [(b"authorization", f"Bearer {TOKEN}".encode())]


def _status(messages: Sequence[Message]) -> int:
    start = next(message for message in messages if message["type"] == "http.response.start")
    return int(start["status"])


def _error_code(messages: Sequence[Message]) -> str:
    body = next(message for message in messages if message["type"] == "http.response.body")
    payload = json.loads(body["body"])
    return str(payload["error"]["code"])
