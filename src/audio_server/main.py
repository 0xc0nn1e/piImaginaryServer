from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from audio_server.api.activity import router as activity_router
from audio_server.api.errors import register_error_handlers
from audio_server.api.health import router as health_router
from audio_server.api.middleware import MULTIPART_OVERHEAD_BYTES, ApiRequestGuardMiddleware
from audio_server.api.recordings import router as recordings_router
from audio_server.core.config import Settings, get_settings
from audio_server.core.database import Database, create_database
from audio_server.core.logging import configure_logging
from audio_server.core.security import TokenAuthenticator
from audio_server.processing.audio import AudioProcessor, FFmpegSettings
from audio_server.processing.contracts import AudioPreprocessor
from audio_server.services.recording_service import RecordingService
from audio_server.services.storage import LocalStorageBackend, StorageBackend
from audio_server.web_auth import (
    create_web_auth_service,
    register_web_auth_error_handler,
)
from audio_server.web_auth import router as web_auth_router


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    storage: StorageBackend | None = None,
    audio_preprocessor: AudioPreprocessor | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    configure_logging(active_settings.log_level, active_settings.log_format)
    active_database = database or create_database(active_settings.database_url)
    active_storage = storage or LocalStorageBackend(active_settings.storage_path)
    active_audio = audio_preprocessor or AudioProcessor(
        FFmpegSettings(
            ffmpeg_binary=active_settings.ffmpeg_binary,
            ffprobe_binary=active_settings.ffprobe_binary,
            conversion_timeout_seconds=active_settings.ffmpeg_timeout_seconds,
        )
    )
    api_token = active_settings.api_token.get_secret_value()
    authenticator = TokenAuthenticator(api_token)
    web_auth_service = create_web_auth_service(
        active_settings,
        active_database.session_factory,
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        _validate_api_token(active_settings)
        _validate_web_auth(active_settings)
        yield

    docs_url = "/docs" if active_settings.docs_enabled else None
    openapi_url = "/openapi.json" if active_settings.docs_enabled else None
    application = FastAPI(
        title="Wave Archive API",
        version="0.1.0",
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )
    application.state.settings = active_settings
    application.state.database = active_database
    application.state.storage = active_storage
    application.state.authenticator = authenticator
    application.state.web_auth_service = web_auth_service
    recording_service = RecordingService(
        session_factory=active_database.session_factory,
        storage=active_storage,
        audio_preprocessor=active_audio,
        max_upload_bytes=active_settings.max_upload_bytes,
        max_metadata_bytes=active_settings.max_metadata_bytes,
        max_audio_duration_seconds=active_settings.max_audio_duration_seconds,
        max_attempts=active_settings.processing_max_attempts,
        audio_retention_days=active_settings.audio_retention_days,
        transcript_retention_days=active_settings.transcript_retention_days,
    )
    application.state.recording_service = recording_service

    application.add_middleware(
        ApiRequestGuardMiddleware,
        authenticator=authenticator,
        max_upload_request_bytes=(
            active_settings.max_upload_bytes
            + active_settings.max_metadata_bytes
            + MULTIPART_OVERHEAD_BYTES
        ),
    )
    register_error_handlers(application)
    register_web_auth_error_handler(application)
    application.include_router(health_router)
    application.include_router(web_auth_router)
    application.include_router(recordings_router)
    application.include_router(activity_router)
    return application


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("audio_server.main:app", host="0.0.0.0", port=8000)


def _validate_api_token(settings: Settings) -> None:
    token = settings.api_token.get_secret_value()
    if not token:
        raise ValueError("API_TOKEN must be configured for the API service")
    if settings.app_env == "production" and len(token) < 32:
        raise ValueError("API_TOKEN must contain at least 32 characters in production")


def _validate_web_auth(settings: Settings) -> None:
    if settings.app_env == "production" and not settings.web_allowed_origin.startswith("https://"):
        raise ValueError("WEB_ALLOWED_ORIGIN must use HTTPS in production")
