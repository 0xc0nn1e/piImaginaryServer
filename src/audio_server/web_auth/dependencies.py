"""FastAPI dependencies and request helpers for browser sessions."""

from __future__ import annotations

import secrets
from typing import Annotated, cast

from fastapi import Depends, Request

from audio_server.web_auth.service import (
    AuthenticatedWebSession,
    WebAuthError,
    WebAuthService,
)


def get_web_auth_service(request: Request) -> WebAuthService:
    return cast(WebAuthService, request.app.state.web_auth_service)


def resolve_web_session(
    request: Request,
    service: Annotated[WebAuthService, Depends(get_web_auth_service)],
) -> AuthenticatedWebSession | None:
    token = request.cookies.get(service.cookie_name, "")
    return service.resolve_session(token)


def require_web_session(
    principal: Annotated[
        AuthenticatedWebSession | None,
        Depends(resolve_web_session),
    ],
) -> AuthenticatedWebSession:
    if principal is None:
        raise WebAuthError(
            status_code=401,
            code="web_authentication_required",
            safe_message="A valid web session is required.",
        )
    return principal


def require_web_mutation_session(
    request: Request,
    service: Annotated[WebAuthService, Depends(get_web_auth_service)],
) -> AuthenticatedWebSession:
    """Require a browser session with an exact origin and CSRF token.

    Used by resources owned by an administrator account rather than by a
    machine client, where the Bearer credential carries no user identity.
    """

    origins = request.headers.getlist("origin")
    if len(origins) != 1 or not secrets.compare_digest(origins[0], service.allowed_origin):
        raise WebAuthError(
            status_code=403,
            code="origin_not_allowed",
            safe_message="Request origin is not allowed.",
        )
    csrf_values = request.headers.getlist("x-csrf-token")
    csrf_token = csrf_values[0] if len(csrf_values) == 1 else ""
    return service.require_mutation_session(
        session_token=request.cookies.get(service.cookie_name, ""),
        csrf_token=csrf_token,
    )


def resolve_request_web_session(request: Request) -> AuthenticatedWebSession | None:
    """Resolve a cookie outside dependency injection, for composite middleware."""

    service = get_web_auth_service(request)
    token = request.cookies.get(service.cookie_name, "")
    return service.resolve_session(token)
