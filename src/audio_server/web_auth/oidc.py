"""Ignite authorization code client, with durable single-use browser binding."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete

from audio_server.core.config import Settings
from audio_server.web_auth.models import OIDCLoginFlow
from audio_server.web_auth.service import SessionFactory, WebAuthError, token_digest

FLOW_COOKIE = "audio_server_oidc"
FLOW_SECONDS = 300
CALLBACK_PATH = "/api/v1/auth/oidc/callback"


class Discovery(BaseModel):
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    code_challenge_methods_supported: list[str]
    token_endpoint_auth_methods_supported: list[str]
    id_token_signing_alg_values_supported: list[str]


class TokenResponse(BaseModel):
    id_token: str = Field(min_length=1, max_length=32768)


class IDClaims(BaseModel):
    model_config = ConfigDict(strict=True)

    iss: str
    sub: str = Field(min_length=1, max_length=255)
    aud: str
    exp: int
    iat: int
    nonce: str
    azp: str | None = None


class RSAKey(BaseModel):
    kty: Literal["RSA"]
    kid: str
    n: str
    e: str
    use: Literal["sig"] = "sig"
    alg: Literal["RS256"] = "RS256"


class KeySet(BaseModel):
    keys: list[RSAKey] = Field(min_length=1, max_length=10)


class IgniteClient:
    def __init__(self, settings: Settings, factory: SessionFactory, identity_digest: str) -> None:
        self.settings = settings
        self.factory = factory
        self.identity_digest = identity_digest
        self.redirect_uri = settings.web_allowed_origin + CALLBACK_PATH
        # Separate connections per operation avoid sharing mutable OAuth state.
        self.transport: httpx.BaseTransport | None = None

    def _request(self, method: str, url: str, *, data: dict[str, str] | None = None) -> object:
        auth = None
        if data is not None:
            # OAuth client_secret_basic form-encodes each credential first.
            auth = httpx.BasicAuth(
                quote(self.settings.ignite_client_id, safe=""),
                quote(self.settings.ignite_client_secret.get_secret_value(), safe=""),
            )
        with (
            httpx.Client(
                timeout=10,
                follow_redirects=False,
                transport=self.transport,
                headers={"User-Agent": "WaveArchive/0.1 OIDC", "Accept": "application/json"},
            ) as client,
            client.stream(method, url, data=data, auth=auth) as response,
        ):
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 262144:
                    raise oidc_error()
            # Pydantic validates each external document at its use site.
            return json.loads(body)

    def discovery(self) -> Discovery:
        issuer = self.settings.ignite_issuer
        metadata = Discovery.model_validate(
            self._request("GET", issuer + "/.well-known/openid-configuration")
        )
        if (
            metadata.issuer != issuer
            or "S256" not in metadata.code_challenge_methods_supported
            or "client_secret_basic" not in metadata.token_endpoint_auth_methods_supported
            or "RS256" not in metadata.id_token_signing_alg_values_supported
        ):
            raise oidc_error()
        origin = urlsplit(issuer)
        for endpoint in (
            metadata.authorization_endpoint,
            metadata.token_endpoint,
            metadata.jwks_uri,
        ):
            parsed = urlsplit(endpoint)
            if (
                (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise oidc_error()
        return metadata

    def start(self) -> tuple[str, str]:
        metadata = self.discovery()
        state = secrets.token_urlsafe(32)
        browser = secrets.token_urlsafe(32)
        verifier, nonce = flow_secrets(browser, state)
        now = datetime.now(UTC)
        with self.factory() as session, session.begin():
            session.execute(delete(OIDCLoginFlow).where(OIDCLoginFlow.expires_at <= now))
            session.add(
                OIDCLoginFlow(
                    state_digest=token_digest(state),
                    browser_digest=token_digest(browser),
                    identity_digest=self.identity_digest,
                    expires_at=now + timedelta(seconds=FLOW_SECONDS),
                )
            )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.ignite_client_id,
                "redirect_uri": self.redirect_uri,
                "scope": "openid",
                "state": state,
                "nonce": nonce,
                "code_challenge": b64(hashlib.sha256(verifier.encode()).digest()),
                "code_challenge_method": "S256",
            }
        )
        return metadata.authorization_endpoint + "?" + query, browser

    def finish(self, *, state: str, browser: str, code: str) -> None:
        if not all(1 <= len(value) <= 256 for value in (state, browser, code)):
            raise oidc_error()
        # DELETE RETURNING atomically consumes the flow before any network I/O.
        with self.factory() as session, session.begin():
            consumed = session.scalar(
                delete(OIDCLoginFlow)
                .where(
                    OIDCLoginFlow.state_digest == token_digest(state),
                    OIDCLoginFlow.browser_digest == token_digest(browser),
                    OIDCLoginFlow.identity_digest == self.identity_digest,
                    OIDCLoginFlow.expires_at > datetime.now(UTC),
                )
                .returning(OIDCLoginFlow.state_digest)
            )
        if consumed is None:
            raise oidc_error()
        verifier, nonce = flow_secrets(browser, state)
        metadata = self.discovery()
        tokens = TokenResponse.model_validate(
            self._request(
                "POST",
                metadata.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "code_verifier": verifier,
                },
            )
        )
        keys = KeySet.model_validate(self._request("GET", metadata.jwks_uri))
        header = jwt.get_unverified_header(tokens.id_token)
        matching = [key for key in keys.keys if key.kid == header.get("kid")]
        if header.get("alg") != "RS256" or len(matching) != 1:
            raise oidc_error()
        key = jwt.PyJWK.from_dict(matching[0].model_dump(), algorithm="RS256")
        claims = IDClaims.model_validate(
            jwt.decode(
                tokens.id_token,
                key.key,
                algorithms=["RS256"],
                audience=self.settings.ignite_client_id,
                issuer=self.settings.ignite_issuer,
                options={
                    "require": ["iss", "sub", "aud", "exp", "iat", "nonce"],
                    "strict_aud": True,
                },
            )
        )
        if (
            not secrets.compare_digest(claims.nonce.encode(), nonce.encode())
            or claims.sub != self.settings.ignite_admin_sub
            or (claims.azp is not None and claims.azp != self.settings.ignite_client_id)
            or claims.exp <= claims.iat
        ):
            raise oidc_error()


def flow_secrets(browser: str, state: str) -> tuple[str, str]:
    # Derive independent values from an HttpOnly cookie; store only digests.
    def derive(label: str) -> str:
        return b64(hmac.digest(browser.encode(), (label + state).encode(), "sha256"))

    return derive("pkce:"), derive("nonce:")


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def oidc_error() -> WebAuthError:
    return WebAuthError(
        status_code=401,
        code="ignite_login_failed",
        safe_message="Ignite sign-in failed. Please try again.",
    )
