from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from audio_server.core.config import Settings
from audio_server.main import _validate_api_token, _validate_web_auth


def test_settings_errors_hide_invalid_database_url_credentials() -> None:
    sentinel = "SYNTHETIC_DATABASE_PASSWORD"

    with pytest.raises(ValidationError) as captured:
        Settings(database_url=f"postgresql://audio:{sentinel}@database/audio")

    assert sentinel not in str(captured.value)


def test_blank_optional_retention_values_are_unset() -> None:
    settings = Settings.model_validate(
        {"audio_retention_days": "", "transcript_retention_days": ""}
    )

    assert settings.audio_retention_days is None
    assert settings.transcript_retention_days is None


@pytest.mark.parametrize(
    ("environment", "token"),
    [("development", ""), ("production", "too-short")],
)
def test_api_runtime_rejects_missing_or_short_token(
    environment: Literal["development", "production"], token: str
) -> None:
    settings = Settings(app_env=environment, api_token=token)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="API_TOKEN"):
        _validate_api_token(settings)


def test_production_web_origin_requires_https() -> None:
    settings = Settings(app_env="production", web_allowed_origin="http://audio.example")
    with pytest.raises(ValueError, match="WEB_ALLOWED_ORIGIN must use HTTPS"):
        _validate_web_auth(settings)

    settings = Settings(app_env="production", web_allowed_origin="https://audio.example")
    _validate_web_auth(settings)
