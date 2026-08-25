"""Golden metadata payloads from the two recorder clients.

The Pi recorder (`piImaginary`, `RecordingMetadata.as_upload_dict`) and the iOS
recorder (`piImaginary-ios`, `Recording.uploadMetadata()`) build the `metadata`
part by hand, and neither repository can import this schema. These payloads are
copied from those two functions so that a field added on either side without a
matching change here fails loudly, rather than at 422 on a real device.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from audio_server.api.schemas import ClientRecordingMetadata
from audio_server.db.models import Recording
from tests.conftest import make_upload

# piImaginary — src/pi_recorder/models.py, as_upload_dict()
PI_METADATA: dict[str, Any] = {
    "id": "d7fd10c1-e9c8-4ec0-a1ea-1917fa95832a",
    "recording_start_time": "2026-08-10T09:00:00+00:00",
    "recording_end_time": "2026-08-10T09:10:00+00:00",
    "duration_seconds": 600.0,
    "filename": "2026-08-10/20260810T090000Z_d7fd10c1.wav",
    "file_size": 19_200_044,
    "checksum_sha256": "0" * 64,
    "upload_status": "pending",
    "retry_count": 0,
    "created_at": "2026-08-10T09:10:02+00:00",
    "device_id": "pi-recorder-01",
    "audio_format": "wav",
    "sample_rate": 16_000,
    "channels": 1,
    "extra": {
        "boot_id": "f0e1d2c3-0000-1111-2222-333344445555",
        "clock_synchronized": False,
        "monotonic_start_seconds": 31.5,
    },
}

# piImaginary-ios — piImaginaryIOS/Models/Recording.swift, uploadMetadata()
IOS_METADATA: dict[str, Any] = {
    "id": "6f1c3d2e-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
    "recording_start_time": "2026-08-24T09:00:00.000000+00:00",
    "recording_end_time": "2026-08-24T09:15:00.000000+00:00",
    "duration_seconds": 900.0,
    "filename": "2026-08-24/20260824T090000Z_6f1c3d2e.m4a",
    "file_size": 3_612_044,
    "checksum_sha256": "a" * 64,
    "upload_status": "pending",
    "retry_count": 0,
    "created_at": "2026-08-24T09:15:00.100000+00:00",
    "device_id": "iphone-0123456789ab",
    "audio_format": "m4a",
    "sample_rate": 16_000,
    "channels": 1,
    "extra": {
        "boot_id": "11111111-2222-3333-4444-555555555555",
        "clock_synchronized": True,
        "monotonic_start_seconds": 31.5,
    },
}

CLIENT_METADATA = {"pi": PI_METADATA, "ios": IOS_METADATA}


@pytest.mark.parametrize("client", sorted(CLIENT_METADATA))
def test_client_metadata_is_accepted(client: str) -> None:
    payload = CLIENT_METADATA[client]
    parsed = ClientRecordingMetadata.model_validate_json(json.dumps(payload))
    assert parsed.audio_format == payload["audio_format"]
    # Clock provenance is retained verbatim; the pipeline uses it to order chunks
    # from one boot when the wall clock was still wrong.
    assert parsed.extra == payload["extra"]


@pytest.mark.parametrize("client", sorted(CLIENT_METADATA))
def test_clock_provenance_at_top_level_is_rejected(client: str) -> None:
    """The shape both clients sent before 2026-08-25, kept as a regression guard."""
    payload = dict(CLIENT_METADATA[client])
    payload.update(payload.pop("extra"))

    with pytest.raises(ValidationError) as excinfo:
        ClientRecordingMetadata.model_validate_json(json.dumps(payload))

    rejected = {error["loc"][0] for error in excinfo.value.errors()}
    assert rejected == {"boot_id", "clock_synchronized", "monotonic_start_seconds"}


def test_clock_provenance_survives_ingestion(app_client: TestClient, wav_bytes: bytes) -> None:
    """`extra` is what the pipeline later reads to order chunks from one boot."""
    extra = PI_METADATA["extra"]
    files, headers, metadata = make_upload(wav_bytes, metadata_overrides={"extra": extra})

    response = app_client.post("/api/v1/recordings", files=files, headers=headers)
    assert response.status_code == 201

    session_factory = app_client.app.state.test_session_factory
    with session_factory() as session:
        recording = session.get(Recording, uuid.UUID(metadata["id"]))
        assert recording is not None
        assert recording.client_metadata["extra"] == extra
