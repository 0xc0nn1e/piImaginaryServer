from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from audio_server.api.dependencies import AuthenticationError
from audio_server.services.bookmark_service import BookmarkServiceError
from audio_server.services.daily_service import DailyServiceError
from audio_server.services.recording_service import RecordingServiceError


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthenticationError)
    async def authentication_error_handler(
        _request: Request, _exc: AuthenticationError
    ) -> JSONResponse:
        return _error_response(
            401,
            "authentication_required",
            "A valid API credential or web session is required.",
        )

    @app.exception_handler(RecordingServiceError)
    async def recording_error_handler(
        _request: Request, exc: RecordingServiceError
    ) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.safe_message)

    @app.exception_handler(BookmarkServiceError)
    async def bookmark_error_handler(
        _request: Request, exc: BookmarkServiceError
    ) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.safe_message)

    @app.exception_handler(DailyServiceError)
    async def daily_error_handler(_request: Request, exc: DailyServiceError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.safe_message)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = sorted(
            {
                ".".join(str(part) for part in error["loc"] if part not in {"body", "header"})
                for error in exc.errors()
            }
        )
        suffix = f" Invalid fields: {', '.join(fields)}." if fields else ""
        return _error_response(
            422,
            "request_validation_failed",
            f"The request did not match the API contract.{suffix}",
        )
