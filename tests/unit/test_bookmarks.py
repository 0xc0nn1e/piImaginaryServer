from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from audio_server.db.models import Bookmark, JobStatus, ProcessingJob, Recording, RecordingStatus
from tests.conftest import TEST_API_TOKEN, TEST_WEB_SETUP_TOKEN, make_upload

ORIGIN = "http://testserver"
PASSWORD = "correct horse battery staple"


def _sign_in(client: TestClient) -> dict[str, str]:
    """Create the administrator and return browser mutation headers."""

    setup = client.post(
        "/api/v1/auth/setup",
        headers={"Origin": ORIGIN, "X-Setup-Token": TEST_WEB_SETUP_TOKEN},
        json={"username": "admin.user", "password": PASSWORD},
    )
    assert setup.status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": "admin.user", "password": PASSWORD},
    )
    assert login.status_code == 200
    csrf = client.cookies.get("audio_server_csrf")
    assert csrf is not None
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "expression",
        "original_ja": "一旦こちらで持ち帰ります。",
        "translation_zh_hk": "我哋先拎返去研究下。",
        "note_ja": "会議で結論を保留するときの定番表現。",
        "note_zh_hk": "開會時想暫時唔落決定嘅慣用講法。",
        "speaker_label": "SPEAKER_00",
        "start_time": 12.5,
        "end_time": 15.0,
    }
    payload.update(overrides)
    return payload


def _completed_recording(client: TestClient, wav_bytes: bytes) -> uuid.UUID:
    files, headers, metadata = make_upload(wav_bytes)
    assert client.post("/api/v1/recordings", files=files, headers=headers).status_code == 201
    recording_id = uuid.UUID(metadata["id"])
    with client.app.state.test_session_factory.begin() as session:
        recording = session.get(Recording, recording_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
        )
        assert recording is not None and job is not None
        recording.processing_status = RecordingStatus.COMPLETED
        job.status = JobStatus.COMPLETED
    return recording_id


def test_bookmarks_require_a_browser_session_not_a_bearer_token(
    app_client: TestClient,
) -> None:
    """The machine credential identifies a device, so it cannot own bookmarks."""

    anonymous = app_client.get("/api/v1/bookmarks")
    assert anonymous.status_code == 401

    bearer = app_client.get(
        "/api/v1/bookmarks", headers={"Authorization": f"Bearer {TEST_API_TOKEN}"}
    )
    assert bearer.status_code == 401


def test_saving_listing_and_removing_a_bookmark(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    headers = _sign_in(app_client)
    recording_id = _completed_recording(app_client, wav_bytes)

    created = app_client.post(
        "/api/v1/bookmarks",
        headers=headers,
        json=_payload(recording_id=str(recording_id)),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["kind"] == "expression"
    assert body["recording_id"] == str(recording_id)
    assert body["source_deleted_at"] is None
    assert body["source_label"].endswith(".wav")

    listed = app_client.get("/api/v1/bookmarks")
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["original_ja"] == "一旦こちらで持ち帰ります。"

    removed = app_client.delete(f"/api/v1/bookmarks/{body['id']}", headers=headers)
    assert removed.status_code == 204
    assert app_client.get("/api/v1/bookmarks").json()["items"] == []


def test_saving_the_same_quote_twice_is_idempotent(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    headers = _sign_in(app_client)
    recording_id = _completed_recording(app_client, wav_bytes)
    payload = _payload(recording_id=str(recording_id))

    first = app_client.post("/api/v1/bookmarks", headers=headers, json=payload)
    second = app_client.post("/api/v1/bookmarks", headers=headers, json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(app_client.get("/api/v1/bookmarks").json()["items"]) == 1


def test_bookmark_list_carries_hiragana_readings(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    """Saved quotes ship reading runs so the page can set furigana."""

    headers = _sign_in(app_client)
    recording_id = _completed_recording(app_client, wav_bytes)
    created = app_client.post(
        "/api/v1/bookmarks", headers=headers, json=_payload(recording_id=str(recording_id))
    )
    assert created.status_code == 201

    body = app_client.get("/api/v1/bookmarks").json()

    quote = "一旦こちらで持ち帰ります。"
    assert quote in body["furigana"]
    runs = body["furigana"][quote]
    # The runs must rebuild the quote exactly, with kanji carrying readings.
    assert "".join(run["text"] for run in runs) == quote
    assert {run["text"]: run["reading"] for run in runs if run["reading"]} == {
        "一旦": "いったん",
        "持ち帰": "もちかえ",
    }


def test_concurrent_duplicate_save_recovers_instead_of_failing(
    app_client: TestClient, wav_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unique index, not the pre-insert lookup, is the real guard.

    When a competing save commits between this request's lookup and its insert,
    the insert must recover and return the saved quote. Recovering inside the
    failed transaction would instead abort on the next statement and surface a
    server error, so this drives the path the sequential test never reaches.
    """

    headers = _sign_in(app_client)
    recording_id = _completed_recording(app_client, wav_bytes)
    payload = _payload(recording_id=str(recording_id))
    first = app_client.post("/api/v1/bookmarks", headers=headers, json=payload)
    assert first.status_code == 201

    service = app_client.app.state.bookmark_service
    original_find = service._find
    calls = {"count": 0}

    def racing_find(**kwargs: Any) -> Any:
        calls["count"] += 1
        # Miss once, exactly as a lookup issued before the competing insert
        # committed would.
        return None if calls["count"] == 1 else original_find(**kwargs)

    monkeypatch.setattr(service, "_find", racing_find)

    second = app_client.post("/api/v1/bookmarks", headers=headers, json=payload)

    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert calls["count"] >= 2
    assert len(app_client.get("/api/v1/bookmarks").json()["items"]) == 1


def test_highlights_and_expressions_are_saved_and_filtered_separately(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    headers = _sign_in(app_client)
    recording_id = _completed_recording(app_client, wav_bytes)
    # Identical text in both roles stays two bookmarks, because kind is part of
    # the saved identity.
    for kind in ("expression", "highlight"):
        response = app_client.post(
            "/api/v1/bookmarks",
            headers=headers,
            json=_payload(kind=kind, recording_id=str(recording_id)),
        )
        assert response.status_code == 201

    assert len(app_client.get("/api/v1/bookmarks").json()["items"]) == 2
    expressions = app_client.get("/api/v1/bookmarks?kind=expression").json()["items"]
    highlights = app_client.get("/api/v1/bookmarks?kind=highlight").json()["items"]
    assert [item["kind"] for item in expressions] == ["expression"]
    assert [item["kind"] for item in highlights] == ["highlight"]


def test_bookmark_survives_deletion_of_its_recording(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    """A saved quote is a study note, so losing the audio must not erase it."""

    headers = _sign_in(app_client)
    recording_id = _completed_recording(app_client, wav_bytes)
    created = app_client.post(
        "/api/v1/bookmarks",
        headers=headers,
        json=_payload(recording_id=str(recording_id)),
    )
    assert created.status_code == 201
    source_label = created.json()["source_label"]

    deleted = app_client.delete(f"/api/v1/recordings/{recording_id}", headers=headers)
    assert deleted.status_code == 204

    items = app_client.get("/api/v1/bookmarks").json()["items"]
    assert len(items) == 1
    assert items[0]["recording_id"] is None
    assert items[0]["source_deleted_at"] is not None
    # Provenance is still readable even though the recording row is gone.
    assert items[0]["source_label"] == source_label
    assert items[0]["original_ja"] == "一旦こちらで持ち帰ります。"


def test_mutations_require_the_exact_origin_and_csrf_token(
    app_client: TestClient, wav_bytes: bytes
) -> None:
    headers = _sign_in(app_client)
    recording_id = _completed_recording(app_client, wav_bytes)
    payload = _payload(recording_id=str(recording_id))

    wrong_origin = app_client.post(
        "/api/v1/bookmarks",
        headers={"Origin": "http://evil.test", "X-CSRF-Token": headers["X-CSRF-Token"]},
        json=payload,
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["error"]["code"] == "origin_not_allowed"

    missing_csrf = app_client.post(
        "/api/v1/bookmarks", headers={"Origin": ORIGIN}, json=payload
    )
    assert missing_csrf.status_code == 401

    assert app_client.get("/api/v1/bookmarks").json()["items"] == []


def test_bookmark_for_an_unknown_recording_is_rejected(app_client: TestClient) -> None:
    headers = _sign_in(app_client)

    response = app_client.post(
        "/api/v1/bookmarks",
        headers=headers,
        json=_payload(recording_id=str(uuid.uuid4())),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "recording_not_found"


def test_deleting_another_accounts_bookmark_is_not_found(app_client: TestClient) -> None:
    headers = _sign_in(app_client)
    created = app_client.post("/api/v1/bookmarks", headers=headers, json=_payload())
    assert created.status_code == 201

    with app_client.app.state.test_session_factory.begin() as session:
        bookmark = session.scalar(select(Bookmark))
        assert bookmark is not None
        bookmark.user_id = uuid.uuid4()

    response = app_client.delete(f"/api/v1/bookmarks/{created.json()['id']}", headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "bookmark_not_found"
