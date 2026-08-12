"""FastAPI dependencies and request helpers for browser sessions."""

from __future__ import annotations

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


def resolve_request_web_session(request: Request) -> AuthenticatedWebSession | None:
    """Resolve a cookie outside dependency injection, for composite middleware."""

    service = get_web_auth_service(request)
    token = request.cookies.get(service.cookie_name, "")
    return service.resolve_session(token)
