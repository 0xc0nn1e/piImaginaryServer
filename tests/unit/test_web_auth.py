from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from audio_server.core.config import Settings
from audio_server.web_auth.models import User, WebSession
from audio_server.web_auth.router import register_web_auth_error_handler, router
from audio_server.web_auth.service import (
    Argon2idPasswordManager,
    LoginRateLimiter,
    WebAuthError,
    WebAuthService,
    token_digest,
)

SETUP_TOKEN = "synthetic-setup-token-that-is-at-least-32-characters"
TRUSTED_ORIGIN = "https://audio.test"
PASSWORD = "correct horse battery staple"


class FastPasswordManager:
    def hash(self, password: str) -> str:
        return f"$test-hash${token_digest(password)}"

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == self.hash(password)

    def needs_rehash(self, password_hash: str) -> bool:
        return False


@pytest.fixture
def auth_service(session_factory: sessionmaker[Session]) -> WebAuthService:
    return WebAuthService(
        session_factory,
        setup_token=SETUP_TOKEN,
        allowed_origin=TRUSTED_ORIGIN,
        password_manager=FastPasswordManager(),
    )


@pytest.fixture
def auth_client(auth_service: WebAuthService) -> TestClient:
    app = FastAPI()
    app.state.web_auth_service = auth_service
    register_web_auth_error_handler(app)
    app.include_router(router)
    return TestClient(app)


def _setup(client: TestClient, *, username: str = "Admin.User") -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/setup",
        headers={"Origin": TRUSTED_ORIGIN, "X-Setup-Token": SETUP_TOKEN},
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 201
    return response.json()


def _login(
    client: TestClient,
    *,
    username: str = "admin.user",
    password: str = PASSWORD,
):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/auth/login",
        headers={"Origin": TRUSTED_ORIGIN},
        json={"username": username, "password": password},
    )


def test_argon2_manager_produces_argon2id_hash() -> None:
    manager = Argon2idPasswordManager()

    password_hash = manager.hash(PASSWORD)

    assert password_hash.startswith("$argon2id$")
    assert manager.verify(password_hash, PASSWORD) is True
    assert manager.verify(password_hash, "wrong-password-value") is False


def test_setup_is_origin_and_token_guarded_and_permanently_single_admin(
    auth_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    status = auth_client.get("/api/v1/auth/setup-status")
    assert status.json() == {"setup_required": True, "setup_enabled": True}
    assert status.headers["cache-control"] == "no-store"

    missing_origin = auth_client.post(
        "/api/v1/auth/setup",
        headers={"X-Setup-Token": SETUP_TOKEN},
        json={"username": "admin", "password": PASSWORD},
    )
    assert missing_origin.status_code == 403
    assert missing_origin.json()["error"]["code"] == "origin_not_allowed"

    wrong_token = auth_client.post(
        "/api/v1/auth/setup",
        headers={"Origin": TRUSTED_ORIGIN, "X-Setup-Token": "x" * 32},
        json={"username": "admin", "password": PASSWORD},
    )
    assert wrong_token.status_code == 403
    assert wrong_token.json()["error"]["code"] == "setup_authorization_failed"

    body = _setup(auth_client)
    assert body["user"]["username"] == "admin.user"  # type: ignore[index]
    assert "password" not in str(body).lower()

    second = auth_client.post(
        "/api/v1/auth/setup",
        headers={"Origin": TRUSTED_ORIGIN, "X-Setup-Token": SETUP_TOKEN},
        json={"username": "other-admin", "password": PASSWORD},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "setup_already_completed"
    assert auth_client.get("/api/v1/auth/setup-status").json() == {
        "setup_required": False,
        "setup_enabled": False,
    }

    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        assert user.username == "admin.user"
        assert PASSWORD not in user.password_hash


def test_database_singleton_constraint_is_final_setup_race_guard(
    auth_service: WebAuthService,
    session_factory: sessionmaker[Session],
) -> None:
    auth_service.setup_admin(
        supplied_setup_token=SETUP_TOKEN,
        username="admin",
        password=PASSWORD,
    )
    with pytest.raises(IntegrityError), session_factory.begin() as session:
        session.add(
            User(
                singleton_key=1,
                username="racing-admin",
                password_hash="$test-hash$value",
            )
        )


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("missing-user", PASSWORD),
        ("INVALID USER", PASSWORD),
        ("admin.user", "wrong-password"),
        ("admin.user", "short"),
    ],
)
def test_login_uses_one_generic_error_for_invalid_credentials(
    auth_client: TestClient,
    username: str,
    password: str,
) -> None:
    _setup(auth_client)

    response = _login(auth_client, username=username, password=password)

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "invalid_credentials",
            "message": "Username or password is invalid.",
        }
    }


def test_login_sets_strict_session_and_csrf_cookies_and_stores_only_digests(
    auth_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    _setup(auth_client)

    response = _login(auth_client, username="ADMIN.USER")

    assert response.status_code == 200
    assert "csrf_token" not in response.json()
    set_cookie_headers = response.headers.get_list("set-cookie")
    session_cookie = next(
        header for header in set_cookie_headers if header.startswith("audio_server_session=")
    )
    csrf_cookie = next(
        header for header in set_cookie_headers if header.startswith("audio_server_csrf=")
    )
    assert "HttpOnly" in session_cookie
    assert "SameSite=strict" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "SameSite=strict" in csrf_cookie

    raw_session = auth_client.cookies.get("audio_server_session")
    raw_csrf = auth_client.cookies.get("audio_server_csrf")
    assert raw_session and raw_csrf
    with session_factory() as session:
        web_session = session.scalar(select(WebSession))
        assert web_session is not None
        assert web_session.session_token_digest == token_digest(raw_session)
        assert web_session.csrf_token_digest == token_digest(raw_csrf)
        assert raw_session not in repr(web_session.__dict__)
        assert raw_csrf not in repr(web_session.__dict__)

    me = auth_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "admin.user"
    assert me.json()["user"]["created_at"].endswith("Z")
    assert me.json()["user"]["last_login_at"].endswith("Z")
    assert me.json()["expires_at"].endswith("Z")
    assert me.headers["cache-control"] == "no-store"


def test_secure_cookie_is_forced_by_service_configuration(
    session_factory: sessionmaker[Session],
) -> None:
    service = WebAuthService(
        session_factory,
        setup_token=SETUP_TOKEN,
        allowed_origin=TRUSTED_ORIGIN,
        cookie_secure=True,
        password_manager=FastPasswordManager(),
    )
    app = FastAPI()
    app.state.web_auth_service = service
    register_web_auth_error_handler(app)
    app.include_router(router)
    client = TestClient(app, base_url=TRUSTED_ORIGIN)
    _setup(client)

    response = _login(client)

    assert response.status_code == 200
    assert all("Secure" in header for header in response.headers.get_list("set-cookie"))


def test_logout_requires_exact_origin_and_csrf_then_revokes_session(
    auth_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    _setup(auth_client)
    assert _login(auth_client).status_code == 200
    csrf_token = auth_client.cookies.get("audio_server_csrf")
    assert csrf_token

    bad_origin = auth_client.post(
        "/api/v1/auth/logout",
        headers={"Origin": f"{TRUSTED_ORIGIN}.attacker", "X-CSRF-Token": csrf_token},
    )
    assert bad_origin.status_code == 403

    bad_csrf = auth_client.post(
        "/api/v1/auth/logout",
        headers={"Origin": TRUSTED_ORIGIN, "X-CSRF-Token": "wrong-csrf-token"},
    )
    assert bad_csrf.status_code == 403
    assert bad_csrf.json()["error"]["code"] == "csrf_validation_failed"

    response = auth_client.post(
        "/api/v1/auth/logout",
        headers={"Origin": TRUSTED_ORIGIN, "X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "logged_out"}
    assert auth_client.get("/api/v1/auth/me").status_code == 401
    with session_factory() as session:
        web_session = session.scalar(select(WebSession))
        assert web_session is not None
        assert web_session.revoked_at is not None


def test_session_expiry_is_fixed_and_last_seen_writes_are_throttled(
    auth_service: WebAuthService,
    session_factory: sessionmaker[Session],
) -> None:
    start = datetime(2026, 8, 12, tzinfo=UTC)
    auth_service.setup_admin(
        supplied_setup_token=SETUP_TOKEN,
        username="admin",
        password=PASSWORD,
        now=start,
    )
    login = auth_service.login(
        username="admin",
        password=PASSWORD,
        rate_limit_key="test-client",
        now=start,
    )

    auth_service.resolve_session(login.session_token, now=start + timedelta(minutes=1))
    with session_factory() as session:
        web_session = session.scalar(select(WebSession))
        assert web_session is not None
        assert web_session.last_seen_at == start.replace(tzinfo=None)
        original_expiry = web_session.expires_at

    auth_service.resolve_session(login.session_token, now=start + timedelta(minutes=6))
    with session_factory() as session:
        web_session = session.scalar(select(WebSession))
        assert web_session is not None
        assert web_session.last_seen_at == (start + timedelta(minutes=6)).replace(tzinfo=None)
        assert web_session.expires_at == original_expiry

    assert (
        auth_service.resolve_session(
            login.session_token,
            now=start + timedelta(hours=12, seconds=1),
        )
        is None
    )


def test_service_normalizes_injected_non_utc_times(
    auth_service: WebAuthService,
) -> None:
    tokyo_time = datetime(2026, 8, 12, 21, 30, tzinfo=timezone(timedelta(hours=9)))
    user = auth_service.setup_admin(
        supplied_setup_token=SETUP_TOKEN,
        username="admin",
        password=PASSWORD,
        now=tokyo_time,
    )

    login = auth_service.login(
        username="admin",
        password=PASSWORD,
        rate_limit_key="test-client",
        now=tokyo_time,
    )

    assert user.created_at == datetime(2026, 8, 12, 12, 30, tzinfo=UTC)
    assert login.principal.expires_at == datetime(2026, 8, 13, 0, 30, tzinfo=UTC)
    resolved = auth_service.resolve_session(
        login.session_token,
        now=tokyo_time + timedelta(minutes=1),
    )
    assert resolved is not None
    assert resolved.user.created_at.tzinfo is UTC
    assert resolved.user.last_login_at is not None
    assert resolved.user.last_login_at.tzinfo is UTC
    assert resolved.expires_at.tzinfo is UTC


def test_password_reset_revokes_sessions_and_changes_credentials(
    auth_service: WebAuthService,
) -> None:
    auth_service.setup_admin(
        supplied_setup_token=SETUP_TOKEN,
        username="admin",
        password=PASSWORD,
    )
    login = auth_service.login(
        username="admin",
        password=PASSWORD,
        rate_limit_key="before-reset",
    )

    new_password = "a different secure password"
    auth_service.reset_password(username="ADMIN", new_password=new_password)

    assert auth_service.resolve_session(login.session_token) is None
    with pytest.raises(WebAuthError) as old_login:
        auth_service.login(
            username="admin",
            password=PASSWORD,
            rate_limit_key="old-password",
        )
    assert old_login.value.code == "invalid_credentials"
    assert (
        auth_service.login(
            username="admin",
            password=new_password,
            rate_limit_key="new-password",
        ).principal.user.username
        == "admin"
    )


def test_login_rate_limiter_is_bounded_and_returns_retry_after(
    session_factory: sessionmaker[Session],
) -> None:
    clock = iter([0.0, 0.0, 1.0, 1.0, 2.0])
    limiter = LoginRateLimiter(
        max_attempts=2,
        window=timedelta(seconds=60),
        max_entries=2,
        monotonic=lambda: next(clock),
    )
    service = WebAuthService(
        session_factory,
        setup_token=SETUP_TOKEN,
        allowed_origin=TRUSTED_ORIGIN,
        password_manager=FastPasswordManager(),
        login_rate_limiter=limiter,
    )
    service.setup_admin(
        supplied_setup_token=SETUP_TOKEN,
        username="admin",
        password=PASSWORD,
    )

    for _ in range(2):
        with pytest.raises(WebAuthError) as failed:
            service.login(
                username="admin",
                password="wrong-password",
                rate_limit_key="client-a",
            )
        assert failed.value.code == "invalid_credentials"
    with pytest.raises(WebAuthError) as limited:
        service.login(username="admin", password=PASSWORD, rate_limit_key="client-a")
    assert limited.value.status_code == 429
    assert limited.value.headers is not None
    assert "Retry-After" in limited.value.headers


def test_auth_request_body_is_capped_before_validation(
    session_factory: sessionmaker[Session],
) -> None:
    service = WebAuthService(
        session_factory,
        setup_token=SETUP_TOKEN,
        allowed_origin=TRUSTED_ORIGIN,
        max_request_bytes=256,
        password_manager=FastPasswordManager(),
    )
    app = FastAPI()
    app.state.web_auth_service = service
    register_web_auth_error_handler(app)
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/login",
        headers={"Origin": TRUSTED_ORIGIN},
        json={"username": "admin", "password": "x" * 300},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "auth_request_too_large"


def test_web_auth_settings_validate_setup_secret_and_exact_origin() -> None:
    sentinel = "SHORT_SETUP_SECRET"
    with pytest.raises(ValidationError) as invalid_token:
        Settings(web_setup_token=sentinel)
    assert sentinel not in str(invalid_token.value)

    settings = Settings(web_allowed_origin="HTTPS://Audio.Example/")
    assert settings.web_allowed_origin == "https://audio.example"

    with pytest.raises(ValidationError, match="exact HTTP"):
        Settings(web_allowed_origin="https://audio.example/app")
