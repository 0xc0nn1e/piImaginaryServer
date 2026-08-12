"""Request and response contracts for browser authentication."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Syntax and password limits are deliberately checked in the service so
    # malformed credentials receive the same response as unknown credentials.
    username: str
    password: str


class UserResponse(BaseModel):
    id: uuid.UUID
    username: str
    created_at: datetime
    last_login_at: datetime | None


class SetupStatusResponse(BaseModel):
    setup_required: bool
    setup_enabled: bool


class SetupResponse(BaseModel):
    user: UserResponse


class LoginResponse(BaseModel):
    user: UserResponse
    expires_at: datetime


class MeResponse(BaseModel):
    user: UserResponse
    expires_at: datetime


class LogoutResponse(BaseModel):
    status: Literal["logged_out"] = "logged_out"


class WebAuthErrorDetail(BaseModel):
    code: str
    message: str


class WebAuthErrorResponse(BaseModel):
    error: WebAuthErrorDetail
