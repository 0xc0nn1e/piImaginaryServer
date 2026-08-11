from __future__ import annotations

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from audio_server.core.security import TokenAuthenticator

MULTIPART_OVERHEAD_BYTES = 1024 * 1024
logger = logging.getLogger(__name__)


class _RequestBodyTooLarge(Exception):
    pass


class ApiRequestGuardMiddleware:
    """Authenticate private APIs and cap uploads before multipart parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        authenticator: TokenAuthenticator,
        max_upload_request_bytes: int,
    ) -> None:
        if max_upload_request_bytes <= 0:
            raise ValueError("max_upload_request_bytes must be positive")
        self._app = app
        self._authenticator = authenticator
        self._max_upload_request_bytes = max_upload_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_private_api(scope):
            await self._app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._handle_private_request(scope, receive, tracking_send)
        except _RequestBodyTooLarge:
            if not response_started:
                await self._send_too_large(scope, receive, send)
        except Exception as exc:
            # This middleware is inside Starlette's traceback-emitting server
            # error layer. Sanitizing here prevents SQL parameters, metadata,
            # provider output, or paths from reaching ordinary Uvicorn logs.
            logger.error(
                "unhandled API error",
                extra={"error_type": type(exc).__name__},
            )
            if not response_started:
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=500,
                    code="internal_server_error",
                    message="The server could not complete the request.",
                )

    async def _handle_private_request(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_authenticated(scope):
            await _send_error(
                scope,
                receive,
                send,
                status_code=401,
                code="authentication_required",
                message="A valid bearer token is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
            return

        if not _is_recording_upload(scope):
            await self._app(scope, receive, send)
            return

        declared_length = _content_length(scope)
        if declared_length is not None and declared_length > self._max_upload_request_bytes:
            await self._send_too_large(scope, receive, send)
            return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._max_upload_request_bytes:
                    raise _RequestBodyTooLarge
            return message

        await self._app(scope, limited_receive, send)

    def _is_authenticated(self, scope: Scope) -> bool:
        values = [
            value.decode("latin-1")
            for name, value in scope.get("headers", ())
            if name.lower() == b"authorization"
        ]
        if len(values) != 1:
            return False
        parts = values[0].split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        return self._authenticator.authenticate(parts[1]) is not None

    async def _send_too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        await _send_error(
            scope,
            receive,
            send,
            status_code=413,
            code="upload_request_too_large",
            message="Upload request exceeds the configured size limit.",
        )


def _is_private_api(scope: Scope) -> bool:
    path = str(scope.get("path", ""))
    return path == "/api/v1" or path.startswith("/api/v1/")


def _is_recording_upload(scope: Scope) -> bool:
    path = str(scope.get("path", "")).rstrip("/")
    return scope.get("method") == "POST" and path == "/api/v1/recordings"


def _content_length(scope: Scope) -> int | None:
    values = [
        value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"
    ]
    if len(values) != 1:
        return None
    try:
        length = int(values[0])
    except ValueError:
        return None
    return length if length >= 0 else None


async def _send_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> None:
    response = JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )
    await response(scope, receive, send)
