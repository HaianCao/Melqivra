"""Framework-neutral authentication principals and adapters."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from agent_music.domain import UserId, validate_identifier


class AuthenticationError(RuntimeError):
    """A request did not present valid credentials."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated service identity used by authorization boundaries."""

    user_id: UserId
    subject: str
    authentication_method: str


class Authenticator(Protocol):
    """Resolve one authenticated principal from transport headers."""

    def authenticate(self, headers: Mapping[str, str]) -> Principal: ...


class DevelopmentHeaderAuthenticator:
    """Explicit local-only identity adapter for the documented development API."""

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        raw_user_id = headers.get("x-user-id", "").strip()
        if not raw_user_id:
            raise AuthenticationError("Development user identity is required")
        try:
            validate_identifier(raw_user_id)
        except ValueError as error:
            raise AuthenticationError("Development user identity is invalid") from error
        user_id = UserId(raw_user_id)
        return Principal(user_id, raw_user_id, "development_header")


class StaticBearerAuthenticator:
    """Injected bearer-token adapter for deployments without framework coupling."""

    def __init__(self, tokens: Mapping[str, UserId]) -> None:
        if not tokens:
            raise ValueError("At least one bearer token is required")
        normalized: list[tuple[str, UserId]] = []
        for token, user_id in tokens.items():
            if len(token) < 16 or len(token) > 4096:
                raise ValueError("Bearer tokens must contain 16 to 4096 characters")
            validate_identifier(str(user_id))
            normalized.append((token, user_id))
        self._tokens = tuple(normalized)

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        authorization = headers.get("authorization", "")
        scheme, separator, credential = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not credential:
            raise AuthenticationError("Bearer authentication is required")
        matched: UserId | None = None
        for token, user_id in self._tokens:
            if hmac.compare_digest(credential, token):
                matched = user_id
        if matched is None:
            raise AuthenticationError("Bearer token is invalid")
        return Principal(matched, str(matched), "bearer")
