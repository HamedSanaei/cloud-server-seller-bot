"""API token domain (M14-002).

Acceptance: revocable hashed tokens and scopes.

A customer API token authenticates REST v1 requests. Invariants:

- the PLAINTEXT token exists only at creation time (shown once) - storage
  holds a SHA-256 hash only;
- tokens are REVOCABLE: ``revoked_at`` permanently invalidates one;
- SCOPES bound what a token may do, least privilege by construction.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class TokenError(Exception):
    """Base class for token errors."""


class InvalidTokenError(TokenError):
    """The presented token is unknown, malformed or revoked."""


class DuplicateTokenNameError(TokenError):
    """The user already owns a token with this name."""


class TokenLimitError(TokenError):
    """The per-user token quota is exhausted."""


class TokenNotFoundError(TokenError):
    """No such token FOR THIS USER."""


class TokenScope(StrEnum):
    """Stable v1 capability scopes."""

    CATALOG_READ = "catalog:read"
    WALLET_READ = "wallet:read"
    SERVERS_READ = "servers:read"
    SERVERS_WRITE = "servers:write"
    SSH_KEYS_READ = "ssh_keys:read"
    SSH_KEYS_WRITE = "ssh_keys:write"
    TOKENS_MANAGE = "tokens:manage"


ALL_SCOPES: frozenset[TokenScope] = frozenset(TokenScope)

TOKEN_PREFIX = "cpt_"
MAX_TOKENS_PER_USER = 10
MAX_NAME_LENGTH = 64


def generate_raw_token() -> str:
    """A fresh plaintext bearer token (``cpt_...``)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(24)


def hash_token(raw: str) -> str:
    """SHA-256 hex digest - the ONLY form ever persisted."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApiToken:
    """Metadata record; never carries plaintext material."""

    user_id: UUID
    name: str
    token_hash: str
    scopes: frozenset[TokenScope] = field(default_factory=frozenset)
    id: UUID | None = None
    prefix: str = ""  # short display hint (first chars of the raw token)
    created_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    def __post_init__(self) -> None:
        name = (self.name or "").strip()
        object.__setattr__(self, "name", name)
        if not name:
            raise ValueError("token name must not be empty")
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"token name longer than {MAX_NAME_LENGTH} characters")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @staticmethod
    def parse_scopes(raw: Any) -> frozenset[TokenScope]:
        """Validate untrusted scope lists into the stable enum."""
        values = tuple(TokenScope(s) for s in (raw or []))
        return frozenset(values)


@dataclass(frozen=True, slots=True)
class TokenAuthentication:
    """What a valid token proves about a request."""

    user_id: UUID
    scopes: frozenset[TokenScope]
    token_id: UUID

    def has(self, *scopes: TokenScope) -> bool:
        return all(scope in self.scopes for scope in scopes)
