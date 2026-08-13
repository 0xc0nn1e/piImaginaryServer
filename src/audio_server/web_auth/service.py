"""Security-sensitive web authentication business logic."""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from argon2.low_level import Type
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from audio_server.core.client_address import TrustedNetworks
from audio_server.core.config import Settings
from audio_server.web_auth.models import User, WebSession

USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{3,64}$")
SESSION_COOKIE_NAME = "audio_server_session"
CSRF_COOKIE_NAME = "audio_server_csrf"
TOKEN_BYTES = 32
MAX_TOKEN_TEXT_LENGTH = 256
LAST_SEEN_WRITE_INTERVAL = timedelta(minutes=5)


class SessionFactory(Protocol):
    def __call__(self) -> Session: ...


class PasswordManager(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password_hash: str, password: str) -> bool: ...

    def needs_rehash(self, password_hash: str) -> bool: ...


class Argon2idPasswordManager:
    """Argon2id wrapper that never exposes verification-library errors."""

    def __init__(self) -> None:
        self._hasher = PasswordHasher(type=Type.ID)

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, ValueError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except (VerificationError, ValueError):
            return False


class WebAuthError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        safe_message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.status_code = status_code
        self.code = code
        self.safe_message = safe_message
        self.headers = headers


@dataclass(frozen=True, slots=True)
class SetupStatus:
    setup_required: bool
    setup_enabled: bool


@dataclass(frozen=True, slots=True)
class WebUser:
    id: uuid.UUID
    username: str
    created_at: datetime
    last_login_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuthenticatedWebSession:
    user: WebUser
    session_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LoginResult:
    principal: AuthenticatedWebSession
    session_token: str
    csrf_token: str


class LoginRateLimiter:
    """A bounded, process-local fixed-window failure limiter.

    It is intentionally simple for a single API instance. Multiple API
    processes do not share counters; a distributed deployment must replace it
    with a shared limiter at the ingress or database/cache layer.
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        window: timedelta,
        max_entries: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1 or window <= timedelta(0) or max_entries < 1:
            raise ValueError("rate limiter settings must be positive")
        self._max_attempts = max_attempts
        self._window_seconds = window.total_seconds()
        self._max_entries = max_entries
        self._monotonic = monotonic
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def ensure_allowed(self, key: str) -> None:
        now = self._monotonic()
        with self._lock:
            failures = self._active_failures(key, now)
            if len(failures) >= self._max_attempts:
                retry_after = max(1, round(self._window_seconds - (now - failures[0])))
                raise WebAuthError(
                    status_code=429,
                    code="login_rate_limited",
                    safe_message="Too many login attempts. Try again later.",
                    headers={"Retry-After": str(retry_after)},
                )

    def record_failure(self, key: str) -> None:
        now = self._monotonic()
        with self._lock:
            failures = self._active_failures(key, now)
            failures.append(now)
            self._failures[key] = failures
            self._failures.move_to_end(key)
            while len(self._failures) > self._max_entries:
                self._failures.popitem(last=False)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _active_failures(self, key: str, now: float) -> deque[float]:
        failures = self._failures.get(key, deque())
        cutoff = now - self._window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if failures:
            self._failures[key] = failures
            self._failures.move_to_end(key)
        else:
            self._failures.pop(key, None)
        return failures


class WebAuthService:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        setup_token: str,
        allowed_origin: str,
        session_lifetime: timedelta = timedelta(hours=12),
        cookie_secure: bool = False,
        max_request_bytes: int = 4096,
        trusted_proxy_networks: TrustedNetworks = (),
        password_manager: PasswordManager | None = None,
        login_rate_limiter: LoginRateLimiter | None = None,
    ) -> None:
        if setup_token and len(setup_token) < 32:
            raise ValueError("setup_token must be empty or contain at least 32 characters")
        if not allowed_origin:
            raise ValueError("allowed_origin cannot be empty")
        if session_lifetime <= timedelta(0):
            raise ValueError("session_lifetime must be positive")
        if max_request_bytes < 256:
            raise ValueError("max_request_bytes must be at least 256")
        self._session_factory = session_factory
        self._setup_token = setup_token
        self.allowed_origin = allowed_origin
        self.session_lifetime = session_lifetime
        self.cookie_secure = cookie_secure
        self.max_request_bytes = max_request_bytes
        self.trusted_proxy_networks = trusted_proxy_networks
        self.cookie_name = SESSION_COOKIE_NAME
        self.csrf_cookie_name = CSRF_COOKIE_NAME
        self._passwords = password_manager or Argon2idPasswordManager()
        self._dummy_password_hash = self._passwords.hash(secrets.token_urlsafe(TOKEN_BYTES))
        self._login_limiter = login_rate_limiter or LoginRateLimiter(
            max_attempts=5,
            window=timedelta(minutes=5),
            max_entries=2048,
        )

    def get_setup_status(self) -> SetupStatus:
        with self._session_factory() as session:
            setup_required = session.scalar(select(User.id).limit(1)) is None
        return SetupStatus(
            setup_required=setup_required,
            setup_enabled=setup_required and bool(self._setup_token),
        )

    def setup_admin(
        self,
        *,
        supplied_setup_token: str,
        username: str,
        password: str,
        now: datetime | None = None,
    ) -> WebUser:
        with self._session_factory() as session:
            if session.scalar(select(User.id).limit(1)) is not None:
                raise _setup_completed_error()
        if not self._setup_token:
            raise WebAuthError(
                status_code=503,
                code="setup_unavailable",
                safe_message="Administrator setup is not enabled.",
            )
        if len(supplied_setup_token) > MAX_TOKEN_TEXT_LENGTH or not secrets.compare_digest(
            supplied_setup_token.encode("utf-8"),
            self._setup_token.encode("utf-8"),
        ):
            raise WebAuthError(
                status_code=403,
                code="setup_authorization_failed",
                safe_message="Administrator setup authorization failed.",
            )

        normalized_username = normalize_username(username)
        validate_password(password)
        created_at = _as_utc(now or datetime.now(UTC))
        user = User(
            singleton_key=1,
            username=normalized_username,
            password_hash=self._passwords.hash(password),
            is_active=True,
            created_at=created_at,
            updated_at=created_at,
        )
        try:
            with self._session_factory() as session, session.begin():
                session.add(user)
                session.flush()
        except IntegrityError:
            raise _setup_completed_error() from None
        return _web_user(user)

    def login(
        self,
        *,
        username: str,
        password: str,
        rate_limit_key: str,
        now: datetime | None = None,
    ) -> LoginResult:
        self._login_limiter.ensure_allowed(rate_limit_key)
        normalized_username = _normalize_login_username(username)
        password_valid = 12 <= len(password) <= 128
        login_at = _as_utc(now or datetime.now(UTC))

        with self._session_factory() as session, session.begin():
            user = (
                session.scalar(
                    select(User).where(User.username == normalized_username).with_for_update()
                )
                if normalized_username is not None
                else None
            )
            password_hash = user.password_hash if user is not None else self._dummy_password_hash
            verified = self._passwords.verify(password_hash, password)
            if user is None or not user.is_active or not password_valid or not verified:
                self._login_limiter.record_failure(rate_limit_key)
                raise _invalid_credentials_error()

            if self._passwords.needs_rehash(user.password_hash):
                user.password_hash = self._passwords.hash(password)
            user.last_login_at = login_at
            user.updated_at = login_at
            session_token = secrets.token_urlsafe(TOKEN_BYTES)
            csrf_token = secrets.token_urlsafe(TOKEN_BYTES)
            expires_at = login_at + self.session_lifetime
            web_session = WebSession(
                user_id=user.id,
                session_token_digest=token_digest(session_token),
                csrf_token_digest=token_digest(csrf_token),
                created_at=login_at,
                expires_at=expires_at,
                last_seen_at=login_at,
            )
            session.add(web_session)
            session.flush()
            principal = AuthenticatedWebSession(
                user=_web_user(user),
                session_id=web_session.id,
                expires_at=expires_at,
            )

        self._login_limiter.record_success(rate_limit_key)
        return LoginResult(
            principal=principal,
            session_token=session_token,
            csrf_token=csrf_token,
        )

    def resolve_session(
        self,
        session_token: str,
        *,
        now: datetime | None = None,
        touch: bool = True,
    ) -> AuthenticatedWebSession | None:
        if not session_token or len(session_token) > MAX_TOKEN_TEXT_LENGTH:
            return None
        checked_at = _as_utc(now or datetime.now(UTC))
        digest = token_digest(session_token)
        with self._session_factory() as session, session.begin():
            web_session = session.scalar(
                select(WebSession)
                .join(WebSession.user)
                .where(
                    WebSession.session_token_digest == digest,
                    WebSession.revoked_at.is_(None),
                    WebSession.expires_at > checked_at,
                    User.is_active.is_(True),
                )
            )
            if web_session is None:
                return None
            if touch:
                session.execute(
                    update(WebSession)
                    .where(
                        WebSession.id == web_session.id,
                        WebSession.last_seen_at <= checked_at - LAST_SEEN_WRITE_INTERVAL,
                    )
                    .values(last_seen_at=checked_at)
                    .execution_options(synchronize_session=False)
                )
            return AuthenticatedWebSession(
                user=_web_user(web_session.user),
                session_id=web_session.id,
                expires_at=_as_utc(web_session.expires_at),
            )

    def logout(
        self,
        *,
        session_token: str,
        csrf_token: str,
        now: datetime | None = None,
    ) -> None:
        if (
            not session_token
            or len(session_token) > MAX_TOKEN_TEXT_LENGTH
            or not csrf_token
            or len(csrf_token) > MAX_TOKEN_TEXT_LENGTH
        ):
            raise _authentication_required_error()
        revoked_at = _as_utc(now or datetime.now(UTC))
        session_digest = token_digest(session_token)
        csrf_digest = token_digest(csrf_token)
        with self._session_factory() as session, session.begin():
            web_session = session.scalar(
                select(WebSession)
                .join(WebSession.user)
                .where(
                    WebSession.session_token_digest == session_digest,
                    WebSession.revoked_at.is_(None),
                    WebSession.expires_at > revoked_at,
                    User.is_active.is_(True),
                )
                .with_for_update()
            )
            if web_session is None:
                raise _authentication_required_error()
            if not secrets.compare_digest(web_session.csrf_token_digest, csrf_digest):
                raise WebAuthError(
                    status_code=403,
                    code="csrf_validation_failed",
                    safe_message="CSRF validation failed.",
                )
            web_session.revoked_at = revoked_at

    def require_mutation_session(
        self,
        *,
        session_token: str,
        csrf_token: str,
        now: datetime | None = None,
    ) -> AuthenticatedWebSession:
        """Resolve an active browser session and its double-submit CSRF token."""

        if (
            not session_token
            or len(session_token) > MAX_TOKEN_TEXT_LENGTH
            or not csrf_token
            or len(csrf_token) > MAX_TOKEN_TEXT_LENGTH
        ):
            raise _authentication_required_error()
        checked_at = _as_utc(now or datetime.now(UTC))
        session_digest = token_digest(session_token)
        csrf_digest = token_digest(csrf_token)
        with self._session_factory() as session, session.begin():
            web_session = session.scalar(
                select(WebSession)
                .join(WebSession.user)
                .where(
                    WebSession.session_token_digest == session_digest,
                    WebSession.revoked_at.is_(None),
                    WebSession.expires_at > checked_at,
                    User.is_active.is_(True),
                )
            )
            if web_session is None:
                raise _authentication_required_error()
            if not secrets.compare_digest(web_session.csrf_token_digest, csrf_digest):
                raise WebAuthError(
                    status_code=403,
                    code="csrf_validation_failed",
                    safe_message="CSRF validation failed.",
                )
            session.execute(
                update(WebSession)
                .where(
                    WebSession.id == web_session.id,
                    WebSession.last_seen_at <= checked_at - LAST_SEEN_WRITE_INTERVAL,
                )
                .values(last_seen_at=checked_at)
                .execution_options(synchronize_session=False)
            )
            return AuthenticatedWebSession(
                user=_web_user(web_session.user),
                session_id=web_session.id,
                expires_at=_as_utc(web_session.expires_at),
            )

    def reset_password(
        self,
        *,
        username: str,
        new_password: str,
        now: datetime | None = None,
    ) -> None:
        normalized_username = normalize_username(username)
        validate_password(new_password)
        changed_at = _as_utc(now or datetime.now(UTC))
        password_hash = self._passwords.hash(new_password)
        with self._session_factory() as session, session.begin():
            user = session.scalar(
                select(User).where(User.username == normalized_username).with_for_update()
            )
            if user is None:
                raise WebAuthError(
                    status_code=404,
                    code="user_not_found",
                    safe_message="Administrator account was not found.",
                )
            user.password_hash = password_hash
            user.updated_at = changed_at
            session.execute(
                update(WebSession)
                .where(WebSession.user_id == user.id, WebSession.revoked_at.is_(None))
                .values(revoked_at=changed_at)
            )


def create_web_auth_service(
    settings: Settings,
    session_factory: SessionFactory,
) -> WebAuthService:
    return WebAuthService(
        session_factory,
        setup_token=settings.web_setup_token.get_secret_value(),
        allowed_origin=settings.web_allowed_origin,
        session_lifetime=timedelta(hours=settings.web_session_hours),
        cookie_secure=settings.app_env == "production" or settings.web_cookie_secure,
        max_request_bytes=settings.web_auth_max_request_bytes,
        trusted_proxy_networks=settings.trusted_proxy_networks,
        login_rate_limiter=LoginRateLimiter(
            max_attempts=settings.web_login_max_attempts,
            window=timedelta(seconds=settings.web_login_window_seconds),
            max_entries=settings.web_login_rate_limit_entries,
        ),
    )


def normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise WebAuthError(
            status_code=422,
            code="username_invalid",
            safe_message=(
                "Username must use 3-64 lowercase letters, numbers, dots, underscores, or hyphens."
            ),
        )
    return normalized


def validate_password(password: str) -> None:
    if not 12 <= len(password) <= 128:
        raise WebAuthError(
            status_code=422,
            code="password_invalid",
            safe_message="Password must contain 12-128 characters.",
        )


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def rate_limit_key(remote_host: str | None) -> str:
    # Keep client addresses out of the in-memory limiter keys and diagnostics.
    return token_digest(remote_host or "unknown-client")


def _normalize_login_username(username: str) -> str | None:
    normalized = username.strip().lower()
    return normalized if USERNAME_PATTERN.fullmatch(normalized) else None


def _web_user(user: User) -> WebUser:
    return WebUser(
        id=user.id,
        username=user.username,
        created_at=_as_utc(user.created_at),
        last_login_at=(_as_utc(user.last_login_at) if user.last_login_at is not None else None),
    )


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive DateTime round-trips to the UTC API contract."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _setup_completed_error() -> WebAuthError:
    return WebAuthError(
        status_code=409,
        code="setup_already_completed",
        safe_message="Administrator setup has already been completed.",
    )


def _invalid_credentials_error() -> WebAuthError:
    return WebAuthError(
        status_code=401,
        code="invalid_credentials",
        safe_message="Username or password is invalid.",
    )


def _authentication_required_error() -> WebAuthError:
    return WebAuthError(
        status_code=401,
        code="web_authentication_required",
        safe_message="A valid web session is required.",
    )
