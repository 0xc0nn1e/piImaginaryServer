from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from audio_server.core.security import Principal
from audio_server.services.recording_service import RecordingService

bearer_scheme = HTTPBearer(auto_error=False)


class AuthenticationError(Exception):
    pass


def require_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError
    principal = request.app.state.authenticator.authenticate(credentials.credentials)
    if principal is None:
        raise AuthenticationError
    return cast(Principal, principal)


def get_recording_service(request: Request) -> RecordingService:
    return cast(RecordingService, request.app.state.recording_service)
