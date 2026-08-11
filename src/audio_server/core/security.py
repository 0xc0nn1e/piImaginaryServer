from __future__ import annotations

import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    subject: str
    auth_method: str = "api_token"


class TokenAuthenticator:
    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token

    def authenticate(self, supplied_token: str) -> Principal | None:
        if not supplied_token or not secrets.compare_digest(
            supplied_token.encode("utf-8"), self._expected_token.encode("utf-8")
        ):
            return None
        return Principal(subject="api-token-client")
