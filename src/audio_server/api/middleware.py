from __future__ import annotations

import logging
from http.cookies import CookieError, SimpleCookie

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from audio_server.core.security import TokenAuthenticator
from audio_server.web_auth.router import PUBLIC_WEB_AUTH_PATHS
from audio_server.web_auth.service import WebAuthError, WebAuthService

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
        web_auth_service: WebAuthService | None = None,
        max_upload_request_bytes: int,
    ) -> None:
        if max_upload_request_bytes <= 0:
            raise ValueError("max_upload_request_bytes must be positive")
        self._app = app
        self._authenticator = authenticator
        self._web_auth_service = web_auth_service
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
                headers = list(message.get("headers", []))
                if not any(name.lower() == b"cache-control" for name, _value in headers):
                    headers.append((b"cache-control", b"no-store"))
                    message = {**message, "headers": headers}
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
        if _is_web_recording_upload(scope):
            auth_error = self._browser_mutation_auth_error(scope)
            if auth_error is not None:
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=auth_error.status_code,
                    code=auth_error.code,
                    message=auth_error.safe_message,
                )
                return
            await self._handle_limited_upload(scope, receive, send)
            return
        if (
            _is_web_auth_path(scope)
            or _is_browser_read(scope)
            or _is_browser_recording_mutation(scope)
        ):
            await self._app(scope, receive, send)
            return

        if not self._is_bearer_authenticated(scope):
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

        await self._handle_limited_upload(scope, receive, send)

    async def _handle_limited_upload(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
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

    def _browser_mutation_auth_error(self, scope: Scope) -> WebAuthError | None:
        if self._web_auth_service is None:
            return WebAuthError(
                status_code=401,
                code="authentication_required",
                safe_message="A valid browser session is required.",
            )
        origins = _header_values(scope, b"origin")
        if len(origins) != 1 or origins[0] != self._web_auth_service.allowed_origin:
            return WebAuthError(
                status_code=403,
                code="origin_not_allowed",
                safe_message="Request origin is not allowed.",
            )
        csrf_values = _header_values(scope, b"x-csrf-token")
        csrf_token = csrf_values[0] if len(csrf_values) == 1 else ""
        cookies = SimpleCookie()
        cookie_values = _header_values(scope, b"cookie")
        if len(cookie_values) > 1:
            return WebAuthError(
                status_code=401,
                code="authentication_required",
                safe_message="A valid browser session is required.",
            )
        if cookie_values:
            try:
                cookies.load(cookie_values[0])
            except CookieError:
                cookies = SimpleCookie()
        session_cookie = cookies.get(self._web_auth_service.cookie_name)
        try:
            self._web_auth_service.require_mutation_session(
                session_token=session_cookie.value if session_cookie is not None else "",
                csrf_token=csrf_token,
            )
        except WebAuthError as exc:
            return exc
        return None

    def _is_bearer_authenticated(self, scope: Scope) -> bool:
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


def _is_web_recording_upload(scope: Scope) -> bool:
    path = str(scope.get("path", "")).rstrip("/")
    return scope.get("method") == "POST" and path == "/api/v1/web/recordings"


def _is_web_auth_path(scope: Scope) -> bool:
    return str(scope.get("path", "")) in PUBLIC_WEB_AUTH_PATHS | {
        "/api/v1/auth/me",
        "/api/v1/auth/logout",
    }


def _is_browser_read(scope: Scope) -> bool:
    path = str(scope.get("path", "")).rstrip("/")
    return scope.get("method") in {"GET", "HEAD"} and (
        path == "/api/v1/recordings" or path.startswith("/api/v1/recordings/")
    )


def _is_browser_recording_mutation(scope: Scope) -> bool:
    path = str(scope.get("path", "")).rstrip("/")
    parts = path.split("/")
    if parts[:4] != ["", "api", "v1", "recordings"]:
        return False
    if scope.get("method") == "DELETE" and len(parts) == 5:
        return True
    if scope.get("method") == "PUT" and len(parts) == 6 and parts[-1] in {
        "transcript",
        "analysis",
    }:
        return True
    if scope.get("method") == "POST" and len(parts) == 6 and parts[-1] == "reprocess":
        return True
    return (
        scope.get("method") == "POST"
        and len(parts) == 7
        and parts[-2:] == ["analysis", "reprocess"]
    )


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


def _header_values(scope: Scope, header_name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for name, value in scope.get("headers", ())
        if name.lower() == header_name
    ]


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
