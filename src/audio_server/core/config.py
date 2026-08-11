from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://audio:audio@localhost:5432/audio"
    storage_path: Path = Path("data")
    api_token: SecretStr = SecretStr("")

    max_upload_bytes: int = 512 * 1024 * 1024
    max_audio_duration_seconds: float = 6 * 60 * 60
    max_metadata_bytes: int = 16 * 1024

    log_level: str = "INFO"
    log_format: Literal["json", "plain"] = "json"
    docs_enabled: bool = True

    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    ffmpeg_timeout_seconds: int = 3600

    processing_workers: int = 1
    processing_max_attempts: int = 3
    job_poll_seconds: float = 1.0
    job_heartbeat_seconds: int = 30
    job_lease_seconds: int = 300
    job_recovery_seconds: int = 30
    retry_base_seconds: int = 30
    retry_max_seconds: int = 900

    whisper_model: str = "small"
    whisper_device: Literal["cpu", "cuda"] = "cpu"
    whisper_compute_type: str = "int8"
    whisper_language: str | None = None
    whisper_cpu_threads: int = 4
    whisper_cache_dir: Path | None = None

    diarization_enabled: bool = True
    diarization_model: str = "pyannote/speaker-diarization-community-1"
    diarization_device: Literal["cpu", "cuda"] = "cpu"
    huggingface_token: SecretStr = SecretStr("")

    llm_enabled: bool = False
    llm_provider: str = "disabled"
    llm_api_key: SecretStr = SecretStr("")

    audio_retention_days: int | None = None
    transcript_retention_days: int | None = None

    @field_validator("database_url")
    @classmethod
    def database_url_must_be_supported(cls, value: str) -> str:
        if not value.startswith(("postgresql+psycopg://", "sqlite+pysqlite://")):
            raise ValueError("DATABASE_URL must use postgresql+psycopg or sqlite+pysqlite")
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL is invalid")
        return normalized

    @field_validator(
        "max_upload_bytes",
        "max_metadata_bytes",
        "processing_workers",
        "processing_max_attempts",
        "job_heartbeat_seconds",
        "job_lease_seconds",
        "job_recovery_seconds",
        "retry_base_seconds",
        "retry_max_seconds",
        "ffmpeg_timeout_seconds",
        "whisper_cpu_threads",
    )
    @classmethod
    def positive_integer(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("value must be positive")
        return value

    @field_validator("max_audio_duration_seconds", "job_poll_seconds")
    @classmethod
    def positive_number(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("value must be positive")
        return value

    @field_validator("audio_retention_days", "transcript_retention_days", mode="before")
    @classmethod
    def empty_retention_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("audio_retention_days", "transcript_retention_days")
    @classmethod
    def optional_positive_days(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("retention days must be positive when set")
        return value

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> Settings:
        if self.job_lease_seconds <= self.job_heartbeat_seconds * 2:
            raise ValueError("JOB_LEASE_SECONDS must exceed two heartbeat intervals")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("RETRY_MAX_SECONDS must be at least RETRY_BASE_SECONDS")
        if self.llm_enabled and self.llm_provider == "disabled":
            raise ValueError("LLM_PROVIDER must be configured when LLM_ENABLED is true")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
