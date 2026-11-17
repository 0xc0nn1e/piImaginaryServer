"""Browser authentication HTTP endpoints."""

from __future__ import annotations

import secrets
from typing import Annotated, TypeVar, cast

import httpx
import jwt
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from audio_server.core.client_address import resolve_client_ip
from audio_server.web_auth.dependencies import (
    get_web_auth_service,
    require_web_session,
)
from audio_server.web_auth.oidc import (
    CALLBACK_PATH,
    FLOW_COOKIE,
    FLOW_SECONDS,
    IgniteClient,
    oidc_error,
)
from audio_server.web_auth.schemas import (
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    MeResponse,
    SetupRequest,
    SetupResponse,
    SetupStatusResponse,
    UserResponse,
)
from audio_server.web_auth.service import (
    AuthenticatedWebSession,
    LoginResult,
    WebAuthError,
    WebAuthService,
    rate_limit_key,
)

PUBLIC_WEB_AUTH_PATHS = frozenset(
    {
        "/api/v1/auth/setup-status",
        "/api/v1/auth/setup",
        "/api/v1/auth/login",
        "/api/v1/auth/oidc/login",
        CALLBACK_PATH,
    }
)

router = APIRouter(prefix="/api/v1/auth", tags=["web-auth"])
RequestModel = TypeVar("RequestModel", bound=BaseModel)


@router.get("/setup-status", response_model=SetupStatusResponse)
async def setup_status(
    response: Response,
    service: Annotated[WebAuthService, Depends(get_web_auth_service)],
) -> SetupStatusResponse:
    status = await run_in_threadpool(service.get_setup_status)
    _set_no_store(response)
    return SetupStatusResponse(
        auth_mode="ignite" if service.identity_digest is not None else "local",
        setup_required=status.setup_required,
        setup_enabled=status.setup_enabled,
    )


@router.post("/setup", response_model=SetupResponse, status_code=201)
async def setup_admin(
    request: Request,
    response: Response,
    service: Annotated[WebAuthService, Depends(get_web_auth_service)],
) -> SetupResponse:
    _require_exact_origin(request, service)
    payload = await _read_json_request(request, SetupRequest, service.max_request_bytes)
    setup_token = _single_header(request, "x-setup-token")
    user = await run_in_threadpool(
        service.setup_admin,
        supplied_setup_token=setup_token,
        username=payload.username,
        password=payload.password,
    )
    _set_no_store(response)
    return SetupResponse(user=_user_response(user))


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    response: Response,
    service: Annotated[WebAuthService, Depends(get_web_auth_service)],
) -> LoginResponse:
    _require_exact_origin(request, service)
    payload = await _read_json_request(
        request,
        LoginRequest,
        service.max_request_bytes,
        generic_credentials_error=True,
    )
    client_ip = resolve_client_ip(
        peer_host=request.client.host if request.client is not None else None,
        forwarded_for=request.headers.getlist("x-forwarded-for"),
        trusted_networks=service.trusted_proxy_networks,
    )
    result = await run_in_threadpool(
        service.login,
        username=payload.username,
        password=payload.password,
        rate_limit_key=rate_limit_key(client_ip),
    )
    _set_session_cookies(response, service, result)
    _set_no_store(response)
    return LoginResponse(
        user=_user_response(result.principal.user),
        expires_at=result.principal.expires_at,
    )


def _set_session_cookies(response: Response, service: WebAuthService, result: LoginResult) -> None:
    response.set_cookie(
        key=service.cookie_name,
        value=result.session_token,
        max_age=int(service.session_lifetime.total_seconds()),
        expires=result.principal.expires_at,
        path="/",
        secure=service.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    # The SPA reads this cookie and echoes it in X-CSRF-Token. The raw token
    # never belongs in localStorage/sessionStorage, while the database retains
    # only its digest.
    response.set_cookie(
        key=service.csrf_cookie_name,
        value=result.csrf_token,
        max_age=int(service.session_lifetime.total_seconds()),
        expires=result.principal.expires_at,
        path="/",
        secure=service.cookie_secure,
        httponly=False,
        samesite="strict",
    )


class OIDCStartResponse(BaseModel):
    authorization_url: str


def _ignite_client(request: Request) -> IgniteClient:
    client = getattr(request.app.state, "ignite_client", None)
    if client is None:
        raise oidc_error()
    return cast(IgniteClient, client)


@router.post("/oidc/login", response_model=OIDCStartResponse)
def oidc_login(
    request: Request,
    response: Response,
    service: Annotated[WebAuthService, Depends(get_web_auth_service)],
) -> OIDCStartResponse:
    _require_exact_origin(request, service)
    client = _ignite_client(request)
    ip = resolve_client_ip(
        peer_host=request.client.host if request.client else None,
        forwarded_for=request.headers.getlist("x-forwarded-for"),
        trusted_networks=service.trusted_proxy_networks,
    )
    service.throttle_oidc(rate_limit_key(ip))
    try:
        url, browser = client.start()
    except (httpx.HTTPError, ValueError, WebAuthError):
        raise oidc_error() from None
    response.set_cookie(
        FLOW_COOKIE,
        browser,
        max_age=FLOW_SECONDS,
        path="/",
        secure=service.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    _set_no_store(response)
    return OIDCStartResponse(authorization_url=url)


@router.get("/oidc/callback")
def oidc_callback(
    request: Request,
    service: Annotated[WebAuthService, Depends(get_web_auth_service)],
) -> Response:
    response = RedirectResponse("/login?ignite_error=1", status_code=303)
    try:
        client = _ignite_client(request)
        params = request.query_params
        if (
            len(str(params)) > 2048
            or "error" in params
            or len(params.getlist("state")) != 1
            or len(params.getlist("code")) != 1
        ):
            raise oidc_error()
        client.finish(
            state=params["state"], code=params["code"], browser=request.cookies.get(FLOW_COOKIE, "")
        )
        result = service.login_oidc()
        response = RedirectResponse("/recordings", status_code=303)
        _set_session_cookies(response, service, result)
    except (httpx.HTTPError, ValueError, jwt.PyJWTError, WebAuthError, IntegrityError):
        # Never reflect or log codes, tokens, provider errors or exceptions.
        pass
    response.delete_cookie(
        FLOW_COOKIE, path="/", secure=service.cookie_secure, httponly=True, samesite="lax"
    )
    _set_no_store(response)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/me", response_model=MeResponse)
async def me(
    response: Response,
    principal: Annotated[AuthenticatedWebSession, Depends(require_web_session)],
) -> MeResponse:
    _set_no_store(response)
    return MeResponse(
        user=_user_response(principal.user),
        expires_at=principal.expires_at,
    )


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    response: Response,
    service: Annotated[WebAuthService, Depends(get_web_auth_service)],
) -> LogoutResponse:
    _require_exact_origin(request, service)
    session_token = request.cookies.get(service.cookie_name, "")
    csrf_token = _single_header(request, "x-csrf-token")
    await run_in_threadpool(
        service.logout,
        session_token=session_token,
        csrf_token=csrf_token,
    )
    response.delete_cookie(
        key=service.cookie_name,
        path="/",
        secure=service.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        key=service.csrf_cookie_name,
        path="/",
        secure=service.cookie_secure,
        httponly=False,
        samesite="strict",
    )
    _set_no_store(response)
    return LogoutResponse()


def register_web_auth_error_handler(app: FastAPI) -> None:
    @app.exception_handler(WebAuthError)
    async def web_auth_error_handler(_request: Request, error: WebAuthError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.safe_message}},
            headers={"Cache-Control": "no-store", **(error.headers or {})},
        )


async def _read_json_request(
    request: Request,
    model: type[RequestModel],
    max_bytes: int,
    *,
    generic_credentials_error: bool = False,
) -> RequestModel:
    declared_lengths = request.headers.getlist("content-length")
    if len(declared_lengths) == 1:
        try:
            if int(declared_lengths[0]) > max_bytes:
                raise _request_too_large_error()
        except ValueError:
            raise _invalid_request_error() from None
        if int(declared_lengths[0]) < 0:
            raise _invalid_request_error()
    elif len(declared_lengths) > 1:
        raise _invalid_request_error()

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise _request_too_large_error()
    try:
        return model.model_validate_json(body)
    except ValidationError:
        if generic_credentials_error:
            raise WebAuthError(
                status_code=401,
                code="invalid_credentials",
                safe_message="Username or password is invalid.",
            ) from None
        raise _invalid_request_error() from None


def _require_exact_origin(request: Request, service: WebAuthService) -> None:
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or not secrets.compare_digest(origins[0], service.allowed_origin):
        raise WebAuthError(
            status_code=403,
            code="origin_not_allowed",
            safe_message="Request origin is not allowed.",
        )


def _single_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1 or len(values[0]) > 256:
        return ""
    return values[0]


def _user_response(user: object) -> UserResponse:
    return UserResponse.model_validate(user, from_attributes=True)


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _request_too_large_error() -> WebAuthError:
    return WebAuthError(
        status_code=413,
        code="auth_request_too_large",
        safe_message="Authentication request exceeds the configured size limit.",
    )


def _invalid_request_error() -> WebAuthError:
    return WebAuthError(
        status_code=422,
        code="auth_request_invalid",
        safe_message="Authentication request is invalid.",
    )
