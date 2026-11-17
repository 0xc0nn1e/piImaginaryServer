from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from audio_server.core.config import Settings
from audio_server.web_auth.models import OIDCLoginFlow, User, WebSession
from audio_server.web_auth.oidc import FLOW_COOKIE, IgniteClient, b64, flow_secrets
from audio_server.web_auth.service import create_web_auth_service, token_digest

ISSUER = "https://ignite.example.test"
SUBJECT = "d9bf2a15-ef56-4012-83ed-15740bd8ad43"
ORIGIN = "http://testserver"


@pytest.fixture
def ignite(app_client: TestClient, session_factory: sessionmaker[Session]):  # type: ignore[no-untyped-def]
    settings = Settings(
        _env_file=None,
        web_auth_mode="ignite",
        web_allowed_origin=ORIGIN,
        ignite_issuer=ISSUER,
        ignite_client_id="wave-test",
        ignite_client_secret="test-secret",
        ignite_admin_sub=SUBJECT,
    )
    service = create_web_auth_service(settings, session_factory)
    assert service.identity_digest is not None
    client = IgniteClient(settings, session_factory, service.identity_digest)
    app_client.app.state.web_auth_service = service
    app_client.app.state.ignite_client = client
    # The app's request guard captures the service at construction. Rebuild it
    # with the new service, just as a real restart after a config change does.
    for middleware in app_client.app.user_middleware:
        if "web_auth_service" in middleware.kwargs:
            middleware.kwargs["web_auth_service"] = service
    app_client.app.middleware_stack = app_client.app.build_middleware_stack()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="test-key", alg="RS256", use="sig")
    control: dict[str, object] = {"claims": {}, "token_calls": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == "WaveArchive/0.1 OIDC"
        assert request.headers["accept"] == "application/json"
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": control.get("issuer", ISSUER),
                    "authorization_endpoint": ISSUER + "/oauth2/authorize",
                    "token_endpoint": control.get("token_endpoint", ISSUER + "/oauth2/token"),
                    "jwks_uri": ISSUER + "/oauth2/jwks",
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if request.url.path == "/oauth2/jwks":
            return httpx.Response(200, json={"keys": [public]})
        assert request.url.path == "/oauth2/token"
        assert request.headers["authorization"].startswith("Basic ")
        form = parse_qs(request.content.decode())
        assert form["redirect_uri"] == [ORIGIN + "/api/v1/auth/oidc/callback"]
        assert form["code_verifier"] == [control["verifier"]]
        control["token_calls"] = int(control["token_calls"]) + 1  # type: ignore[call-overload]
        claims = {
            "iss": ISSUER,
            "sub": SUBJECT,
            "aud": "wave-test",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
            "nonce": control["nonce"],
        }
        claims.update(control["claims"])  # type: ignore[call-overload]
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})
        if control.get("bad_signature"):
            other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            token = jwt.encode(claims, other, algorithm="RS256", headers={"kid": "test-key"})
        return httpx.Response(200, json={"id_token": token, "access_token": "not-stored"})

    client.transport = httpx.MockTransport(transport)
    return app_client, client, control


def start(ignite):  # type: ignore[no-untyped-def]
    browser, _, control = ignite
    response = browser.post("/api/v1/auth/oidc/login", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    state = query["state"][0]
    cookie = browser.cookies[FLOW_COOKIE]
    verifier, nonce = flow_secrets(cookie, state)
    control.update(verifier=verifier, nonce=nonce)
    assert query["scope"] == ["openid"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [b64(hashlib.sha256(verifier.encode()).digest())]
    assert query["nonce"] == [nonce]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    return state


def finish(browser: TestClient, state: str) -> httpx.Response:
    return browser.get(
        "/api/v1/auth/oidc/callback",
        params={"state": state, "code": "test-code"},
        follow_redirects=False,
    )


def test_ignite_login_creates_private_session_and_logout(ignite, session_factory):  # type: ignore[no-untyped-def]
    browser, client, _ = ignite
    status = browser.get("/api/v1/auth/setup-status").json()
    assert status == {"auth_mode": "ignite", "setup_required": False, "setup_enabled": False}
    state = start(ignite)
    cookie = browser.cookies[FLOW_COOKIE]
    with session_factory() as session:
        flow = session.get(OIDCLoginFlow, token_digest(state))
        assert flow.browser_digest == token_digest(cookie)
        assert flow.browser_digest != cookie
    response = finish(browser, state)
    assert response.headers["location"] == "/recordings"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert FLOW_COOKIE not in browser.cookies
    assert browser.get("/api/v1/auth/me").status_code == 200
    assert browser.get("/api/v1/recordings").status_code == 200
    with session_factory() as session:
        stored = session.scalar(select(WebSession))
        assert stored.identity_digest == client.identity_digest
        assert stored.expires_at - stored.created_at == timedelta(hours=2)
    assert browser.post("/api/v1/auth/logout", headers={"Origin": ORIGIN}).status_code == 401
    assert (
        browser.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": browser.cookies["audio_server_csrf"],
            },
        ).status_code
        == 200
    )
    assert browser.get("/api/v1/auth/me").status_code == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://wrong.example.test"},
        {"aud": "another-client"},
        {"sub": "another-user"},
        {"nonce": "wrong"},
        {"exp": 1},
        {"iat": 9999999999},
        {"azp": "another-client"},
        {"exp": "9999999999"},
        {"aud": ["wave-test", "another-client"]},
    ],
)
def test_rejects_untrusted_identity(ignite, claims):  # type: ignore[no-untyped-def]
    browser, _, control = ignite
    state = start(ignite)
    control["claims"] = claims
    assert finish(browser, state).headers["location"] == "/login?ignite_error=1"
    assert browser.get("/api/v1/auth/me").status_code == 401


def test_rejects_invalid_signature(ignite):  # type: ignore[no-untyped-def]
    browser, _, control = ignite
    state = start(ignite)
    control["bad_signature"] = True
    assert finish(browser, state).headers["location"] == "/login?ignite_error=1"


@pytest.mark.parametrize("kind", ["missing_cookie", "wrong_state", "expired", "duplicate"])
def test_rejects_invalid_callback_before_token_exchange(ignite, session_factory, kind):  # type: ignore[no-untyped-def]
    browser, _, control = ignite
    state = start(ignite)
    if kind == "missing_cookie":
        browser.cookies.clear()
    elif kind == "wrong_state":
        state = "wrong"
    elif kind == "expired":
        with session_factory() as session, session.begin():
            flow = session.get(OIDCLoginFlow, token_digest(state))
            flow.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if kind == "duplicate":
        response = browser.get(
            "/api/v1/auth/oidc/callback",
            params=[
                ("state", state),
                ("state", state),
                ("code", "test-code"),
            ],
            follow_redirects=False,
        )
    else:
        response = finish(browser, state)
    assert response.headers["location"] == "/login?ignite_error=1"
    assert control["token_calls"] == 0


def test_flow_is_single_use_even_with_original_cookie(ignite):  # type: ignore[no-untyped-def]
    browser, _, control = ignite
    state = start(ignite)
    cookie = browser.cookies[FLOW_COOKIE]
    assert finish(browser, state).headers["location"] == "/recordings"
    browser.cookies.set(FLOW_COOKIE, cookie)
    assert finish(browser, state).headers["location"] == "/login?ignite_error=1"
    assert control["token_calls"] == 1


def test_keeps_existing_user_and_rejects_old_local_sessions(ignite, session_factory):  # type: ignore[no-untyped-def]
    browser, client, _ = ignite
    local = create_web_auth_service(
        Settings(_env_file=None, web_setup_token="s" * 32), session_factory
    )
    user = local.setup_admin(supplied_setup_token="s" * 32, username="existing", password="p" * 20)
    old = local.login(username="existing", password="p" * 20, rate_limit_key="test")
    service = browser.app.state.web_auth_service
    assert service.resolve_session(old.session_token) is None
    assert finish(browser, start(ignite)).headers["location"] == "/recordings"
    me = browser.get("/api/v1/auth/me").json()
    assert me["user"]["id"] == str(user.id)
    assert me["user"]["username"] == "existing"
    service.identity_digest = "different-issuer-or-subject"
    assert service.resolve_session(browser.cookies["audio_server_session"]) is None
    with session_factory() as session:
        assert len(session.scalars(select(User)).all()) == 1


def test_local_auth_disabled_and_origin_and_rate_limits_preserved(ignite):  # type: ignore[no-untyped-def]
    browser, _, _ = ignite
    for path in ("setup", "login"):
        response = browser.post(
            "/api/v1/auth/" + path,
            headers={"Origin": ORIGIN},
            json={"username": "admin", "password": "p" * 20},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "local_auth_disabled"
    assert browser.post("/api/v1/auth/oidc/login").status_code == 403
    for _ in range(5):
        assert (
            browser.post("/api/v1/auth/oidc/login", headers={"Origin": ORIGIN}).status_code == 200
        )
    assert browser.post("/api/v1/auth/oidc/login", headers={"Origin": ORIGIN}).status_code == 429


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "https://other.example.test"},
        {"token_endpoint": "https://other.example.test/token"},
    ],
)
def test_rejects_discovery_redirecting_credentials(ignite, change):  # type: ignore[no-untyped-def]
    browser, _, control = ignite
    control.update(change)
    assert browser.post("/api/v1/auth/oidc/login", headers={"Origin": ORIGIN}).status_code == 401


def test_configuration_requires_explicit_credentials_and_subject() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, web_auth_mode="ignite")


def test_provider_failure_is_safe_and_flow_cannot_be_retried(ignite):  # type: ignore[no-untyped-def]
    browser, client, _ = ignite
    state = start(ignite)
    cookie = browser.cookies[FLOW_COOKIE]

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sensitive-provider-details", request=request)

    client.transport = httpx.MockTransport(unavailable)
    response = finish(browser, state)
    assert response.headers["location"] == "/login?ignite_error=1"
    assert "sensitive" not in response.text
    browser.cookies.set(FLOW_COOKIE, cookie)
    assert finish(browser, state).headers["location"] == "/login?ignite_error=1"
    assert browser.get("/health/live").status_code == 200


def test_disabled_existing_admin_cannot_sign_in(ignite, session_factory):  # type: ignore[no-untyped-def]
    browser, _, _ = ignite
    assert finish(browser, start(ignite)).headers["location"] == "/recordings"
    with session_factory() as session, session.begin():
        user = session.scalar(select(User))
        user.is_active = False
    assert browser.get("/api/v1/auth/me").status_code == 401
    assert finish(browser, start(ignite)).headers["location"] == "/login?ignite_error=1"
