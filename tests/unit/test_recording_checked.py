"""The administrator's own review mark, and the list filters that read it.

A recording carries no human state otherwise, so this flag is the only column a
browser session may write directly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from audio_server.db.models import Recording, RecordingStatus
from tests.conftest import TEST_API_TOKEN, TEST_WEB_SETUP_TOKEN

ORIGIN = "http://testserver"
PASSWORD = "a synthetic admin password"
BEARER = {"Authorization": f"Bearer {TEST_API_TOKEN}"}


def _seed(
    session_factory: sessionmaker[Session],
    *,
    device_id: str = "pi-recorder-01",
    status: RecordingStatus = RecordingStatus.COMPLETED,
) -> uuid.UUID:
    recording_id = uuid.uuid4()
    started = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        session.add(
            Recording(
                id=recording_id,
                device_id=device_id,
                original_filename="meeting.wav",
                storage_key=f"recordings/{recording_id}/original.wav",
                mime_type="audio/wav",
                audio_format="wav",
                file_size=1024,
                sha256=recording_id.hex * 2,
                started_at=started,
                ended_at=started + timedelta(seconds=1),
                duration_seconds=1.0,
                sample_rate=16_000,
                channels=1,
                processing_status=status,
            )
        )
    return recording_id


def _login(client: TestClient) -> str:
    setup = client.post(
        "/api/v1/auth/setup",
        headers={"Origin": ORIGIN, "X-Setup-Token": TEST_WEB_SETUP_TOKEN},
        json={"username": "admin", "password": PASSWORD},
    )
    assert setup.status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": "admin", "password": PASSWORD},
    )
    assert login.status_code == 200
    token = client.cookies.get("audio_server_csrf")
    assert token
    return token


def test_a_recording_starts_unchecked(
    app_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _seed(session_factory)

    body = app_client.get("/api/v1/recordings", headers=BEARER).json()

    assert body["items"][0]["checked"] is False


def test_checking_a_recording_persists_and_can_be_undone(
    app_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    recording_id = _seed(session_factory)
    csrf = _login(app_client)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}

    checked = app_client.put(
        f"/api/v1/recordings/{recording_id}/checked", headers=headers, json={"checked": True}
    )
    assert checked.status_code == 200
    assert checked.json()["checked"] is True

    cleared = app_client.put(
        f"/api/v1/recordings/{recording_id}/checked", headers=headers, json={"checked": False}
    )
    assert cleared.status_code == 200
    assert cleared.json()["checked"] is False


def test_the_list_filters_by_the_review_mark(
    app_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    reviewed = _seed(session_factory)
    pending = _seed(session_factory)
    csrf = _login(app_client)
    app_client.put(
        f"/api/v1/recordings/{reviewed}/checked",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={"checked": True},
    )

    only_checked = app_client.get("/api/v1/recordings?checked=true", headers=BEARER).json()
    only_pending = app_client.get("/api/v1/recordings?checked=false", headers=BEARER).json()
    everything = app_client.get("/api/v1/recordings", headers=BEARER).json()

    assert [uuid.UUID(item["id"]) for item in only_checked["items"]] == [reviewed]
    assert [uuid.UUID(item["id"]) for item in only_pending["items"]] == [pending]
    assert len(everything["items"]) == 2


@pytest.mark.parametrize(
    ("query", "expected_device"),
    [("device_id=pi-recorder-01", "pi-recorder-01"), ("device_id=web-upload", "web-upload")],
)
def test_the_list_filters_by_device(
    app_client: TestClient,
    session_factory: sessionmaker[Session],
    query: str,
    expected_device: str,
) -> None:
    _seed(session_factory, device_id="pi-recorder-01")
    _seed(session_factory, device_id="web-upload")

    body = app_client.get(f"/api/v1/recordings?{query}", headers=BEARER).json()

    assert [item["device_id"] for item in body["items"]] == [expected_device]


def test_the_list_filters_by_processing_status(
    app_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _seed(session_factory, status=RecordingStatus.COMPLETED)
    failed = _seed(session_factory, status=RecordingStatus.FAILED)

    body = app_client.get("/api/v1/recordings?status=failed", headers=BEARER).json()

    assert [uuid.UUID(item["id"]) for item in body["items"]] == [failed]


def test_checking_requires_the_exact_origin_and_csrf_token(
    app_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    recording_id = _seed(session_factory)
    csrf = _login(app_client)
    payload = {"checked": True}

    wrong_origin = app_client.put(
        f"/api/v1/recordings/{recording_id}/checked",
        headers={"Origin": "http://evil.example", "X-CSRF-Token": csrf},
        json=payload,
    )
    wrong_csrf = app_client.put(
        f"/api/v1/recordings/{recording_id}/checked",
        headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong-token"},
        json=payload,
    )
    no_csrf = app_client.put(
        f"/api/v1/recordings/{recording_id}/checked",
        headers={"Origin": ORIGIN},
        json=payload,
    )

    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["error"]["code"] == "origin_not_allowed"
    assert wrong_csrf.status_code == 403
    assert wrong_csrf.json()["error"]["code"] == "csrf_validation_failed"
    # No CSRF header at all is not a browser mutation, so it falls through to
    # the machine credential check and is refused there.
    assert no_csrf.status_code == 401
    with session_factory() as session:
        assert session.get(Recording, recording_id).checked is False


def test_checking_a_missing_recording_is_not_found(app_client: TestClient) -> None:
    csrf = _login(app_client)

    response = app_client.put(
        f"/api/v1/recordings/{uuid.uuid4()}/checked",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={"checked": True},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "recording_not_found"
