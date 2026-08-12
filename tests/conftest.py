from __future__ import annotations

import hashlib
import io
import json
import uuid
import wave
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from audio_server.core.config import Settings
from audio_server.core.database import Database
from audio_server.db.models import Base
from audio_server.main import create_app
from audio_server.processing.contracts import AudioProbe
from audio_server.services.storage import LocalStorageBackend

TEST_API_TOKEN = "test-token-that-is-long-enough-for-all-tests"
TEST_WEB_SETUP_TOKEN = "test-web-setup-token-that-is-long-enough-for-tests"


class FakeAudioProcessor:
    def probe(self, source: Path) -> AudioProbe:
        if not source.is_file() or source.stat().st_size == 0:
            raise ValueError("invalid fixture")
        return AudioProbe(
            duration_seconds=1.0,
            codec_name="pcm_s16le",
            format_name="wav",
            sample_rate=16_000,
            channels=1,
            mime_type="audio/wav",
            preferred_extension=".wav",
        )

    def normalize(self, source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def app_client(tmp_path: Path, session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        storage_path=tmp_path / "data",
        api_token=TEST_API_TOKEN,
        web_setup_token=TEST_WEB_SETUP_TOKEN,
        web_allowed_origin="http://testserver",
        web_cookie_secure=False,
        diarization_enabled=False,
        docs_enabled=False,
    )
    engine = session_factory.kw["bind"]
    database = Database(engine=engine, session_factory=session_factory)
    app = create_app(
        settings=settings,
        database=database,
        storage=LocalStorageBackend(settings.storage_path),
        audio_preprocessor=FakeAudioProcessor(),
    )
    with TestClient(app) as client:
        client.app.state.test_session_factory = session_factory
        yield client


@pytest.fixture
def wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * 16_000)
    return buffer.getvalue()


def make_upload(
    audio: bytes,
    *,
    recording_id: uuid.UUID | None = None,
    device_id: str = "pi-recorder-01",
    checksum: str | None = None,
    metadata_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, tuple[str | None, bytes | str, str]], dict[str, str], dict[str, Any]]:
    identifier = recording_id or uuid.uuid4()
    digest = checksum or hashlib.sha256(audio).hexdigest()
    started_at = datetime(2026, 8, 10, tzinfo=UTC)
    metadata: dict[str, Any] = {
        "id": str(identifier),
        "recording_start_time": started_at.isoformat(),
        "recording_end_time": (started_at + timedelta(seconds=1)).isoformat(),
        "duration_seconds": 1.0,
        "filename": f"2026-08-10/{identifier}.wav",
        "file_size": len(audio),
        "checksum_sha256": digest,
        "upload_status": "uploading",
        "retry_count": 0,
        "created_at": started_at.isoformat(),
        "device_id": device_id,
        "audio_format": "wav",
        "sample_rate": 16_000,
        "channels": 1,
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)
    files = {
        "audio": ("chunk.wav", audio, "audio/wav"),
        "metadata": (None, json.dumps(metadata), "application/json"),
        "checksum": (None, digest, "text/plain"),
    }
    headers = {
        "Authorization": f"Bearer {TEST_API_TOKEN}",
        "Idempotency-Key": str(identifier),
        "X-Device-ID": device_id,
        "X-Content-SHA256": digest,
    }
    return files, headers, metadata
