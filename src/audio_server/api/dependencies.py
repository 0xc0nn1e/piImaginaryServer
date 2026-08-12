from __future__ import annotations

import secrets
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from audio_server.core.security import Principal
from audio_server.services.recording_service import RecordingService
from audio_server.web_auth.dependencies import resolve_request_web_session
from audio_server.web_auth.service import WebAuthError, WebAuthService

bearer_scheme = HTTPBearer(auto_error=False)


class AuthenticationError(Exception):
    pass


def require_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Principal:
    authorization_values = request.headers.getlist("authorization")
    if credentials is not None:
        if len(authorization_values) != 1 or credentials.scheme.lower() != "bearer":
            raise AuthenticationError
        principal = request.app.state.authenticator.authenticate(credentials.credentials)
        if principal is None:
            raise AuthenticationError
        return cast(Principal, principal)

    # Never let a malformed, unsupported, or duplicate Authorization header
    # silently fall back to an otherwise valid browser cookie.
    if authorization_values:
        raise AuthenticationError
    web_session = resolve_request_web_session(request)
    if web_session is None:
        raise AuthenticationError
    return Principal(
        subject=str(web_session.user.id),
        auth_method="web_session",
    )


def require_machine_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Principal:
    """Require the machine Bearer credential for mutating recording APIs."""

    if (
        len(request.headers.getlist("authorization")) != 1
        or credentials is None
        or credentials.scheme.lower() != "bearer"
    ):
        raise AuthenticationError
    principal = request.app.state.authenticator.authenticate(credentials.credentials)
    if principal is None:
        raise AuthenticationError
    return cast(Principal, principal)


def require_mutation_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Principal:
    """Allow machine Bearer auth or an exact-origin, CSRF-protected web session."""

    authorization_values = request.headers.getlist("authorization")
    if credentials is not None or authorization_values:
        if (
            len(authorization_values) != 1
            or credentials is None
            or credentials.scheme.lower() != "bearer"
        ):
            raise AuthenticationError
        principal = request.app.state.authenticator.authenticate(credentials.credentials)
        if principal is None:
            raise AuthenticationError
        return cast(Principal, principal)

    service = cast(WebAuthService, request.app.state.web_auth_service)
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or not secrets.compare_digest(origins[0], service.allowed_origin):
        raise WebAuthError(
            status_code=403,
            code="origin_not_allowed",
            safe_message="Request origin is not allowed.",
        )
    csrf_values = request.headers.getlist("x-csrf-token")
    csrf_token = csrf_values[0] if len(csrf_values) == 1 else ""
    web_session = service.require_mutation_session(
        session_token=request.cookies.get(service.cookie_name, ""),
        csrf_token=csrf_token,
    )
    return Principal(subject=str(web_session.user.id), auth_method="web_session")


def get_recording_service(request: Request) -> RecordingService:
    return cast(RecordingService, request.app.state.recording_service)
