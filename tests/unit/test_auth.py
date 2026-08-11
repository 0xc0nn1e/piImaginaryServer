import logging

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_API_TOKEN


def test_private_endpoint_requires_bearer_token(app_client: TestClient) -> None:
    response = app_client.get("/api/v1/recordings")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["www-authenticate"] == "Bearer"


def test_private_endpoint_rejects_wrong_token(app_client: TestClient) -> None:
    response = app_client.get("/api/v1/recordings", headers={"Authorization": "Bearer incorrect"})
    assert response.status_code == 401


def test_private_endpoint_accepts_configured_token(app_client: TestClient) -> None:
    response = app_client.get(
        "/api/v1/recordings",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )
    assert response.status_code == 200


def test_health_endpoints_do_not_require_authentication(app_client: TestClient) -> None:
    assert app_client.get("/health/live").status_code == 200
    assert app_client.get("/health/ready").status_code == 200


def test_unexpected_api_error_redacts_exception_text(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "SYNTHETIC_PRIVATE_METADATA"

    def fail_list(**_kwargs: object) -> list[object]:
        raise RuntimeError(sentinel)

    monkeypatch.setattr(
        app_client.app.state.recording_service,
        "list_recordings",
        fail_list,
    )
    with caplog.at_level(logging.ERROR):
        response = app_client.get(
            "/api/v1/recordings",
            headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_server_error"
    assert sentinel not in response.text
    assert sentinel not in caplog.text
