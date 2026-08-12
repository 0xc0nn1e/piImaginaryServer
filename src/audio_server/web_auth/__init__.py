"""Single-admin browser authentication."""

from audio_server.web_auth.dependencies import (
    require_web_session,
    resolve_request_web_session,
    resolve_web_session,
)
from audio_server.web_auth.models import User, WebSession
from audio_server.web_auth.router import (
    PUBLIC_WEB_AUTH_PATHS,
    register_web_auth_error_handler,
    router,
)
from audio_server.web_auth.service import (
    AuthenticatedWebSession,
    WebAuthService,
    create_web_auth_service,
)

__all__ = [
    "PUBLIC_WEB_AUTH_PATHS",
    "AuthenticatedWebSession",
    "User",
    "WebAuthService",
    "WebSession",
    "create_web_auth_service",
    "register_web_auth_error_handler",
    "require_web_session",
    "resolve_request_web_session",
    "resolve_web_session",
    "router",
]
