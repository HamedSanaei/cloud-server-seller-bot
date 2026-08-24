"""API token service (M14-002).

Acceptance: revocable hashed tokens and scopes.

- ``create`` returns the PLAINTEXT token exactly once; only the SHA-256
  hash is stored or audited.
- ``authenticate`` resolves a presented bearer token to a
  :class:`TokenAuthentication` (user + scopes) and stamps ``last_used_at``.
  Unknown, malformed and REVOKED tokens all fail identically.
- Every management operation is ownership-scoped in the application layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.audit.service import AuditTrail

from .domain import (
    ALL_SCOPES,
    MAX_TOKENS_PER_USER,
    ApiToken,
    DuplicateTokenNameError,
    InvalidTokenError,
    TokenAuthentication,
    TokenLimitError,
    TokenNotFoundError,
    TokenScope,
    generate_raw_token,
    hash_token,
)


class ApiTokenRepository(Protocol):
    async def add(self, token: ApiToken) -> ApiToken: ...
    async def get_by_hash(self, token_hash: str) -> ApiToken | None: ...
    async def get(self, token_id: UUID) -> ApiToken | None: ...
    async def list_for_user(self, user_id: UUID) -> list[ApiToken]: ...
    async def save(self, token: ApiToken) -> ApiToken: ...


class TokenService:
    """Create / list / revoke hashed API tokens; authenticate bearers."""

    resource_type = "api_token"

    def __init__(
        self,
        tokens: ApiTokenRepository,
        audit: AuditTrail | None = None,
        *,
        max_tokens_per_user: int = MAX_TOKENS_PER_USER,
        clock: type[datetime] = datetime,
    ) -> None:
        self._repo = tokens
        self._audit = audit
        self._max = max_tokens_per_user
        self._clock = clock

    async def create(
        self,
        *,
        actor_user_id: UUID,
        owner_user_id: UUID,
        name: str,
        scopes: frozenset[TokenScope],
    ) -> tuple[ApiToken, str]:
        """Register a new token; returns (metadata, plaintext-shown-once)."""
        if actor_user_id != owner_user_id:
            raise TokenNotFoundError("manage your own tokens")
        unknown = scopes - ALL_SCOPES
        if unknown:
            raise ValueError(f"unknown scopes: {sorted(str(s) for s in unknown)}")
        existing = await self._repo.list_for_user(owner_user_id)
        if len(existing) >= self._max:
            raise TokenLimitError(f"at most {self._max} tokens per user")
        if any(t.name == (name or "").strip() for t in existing):
            raise DuplicateTokenNameError(f"name {name!r} already registered")

        raw = generate_raw_token()
        token = ApiToken(
            id=uuid4(),
            user_id=owner_user_id,
            name=name,
            token_hash=hash_token(raw),
            prefix=raw[:10],
            scopes=frozenset(scopes),
            created_at=self._now(),
        )
        saved = await self._repo.add(token)
        await self._audit_mutation(
            actor_user_id=owner_user_id,
            action="token.created",
            token=saved,
            metadata={
                "name": saved.name,
                "scopes": ",".join(sorted(s.value for s in saved.scopes)),
            },
        )
        return saved, raw

    async def authenticate(self, raw_token: str) -> TokenAuthentication:
        """Resolve a bearer token; revoked/unknown -> InvalidTokenError."""
        if not raw_token.startswith("cpt_"):
            raise InvalidTokenError("invalid or revoked token")
        record = await self._repo.get_by_hash(hash_token(raw_token))
        if record is None or record.revoked_at is not None:
            raise InvalidTokenError("invalid or revoked token")
        assert record.id is not None
        used = ApiToken(
            id=record.id,
            user_id=record.user_id,
            name=record.name,
            token_hash=record.token_hash,
            prefix=record.prefix,
            scopes=record.scopes,
            created_at=record.created_at,
            revoked_at=record.revoked_at,
            last_used_at=self._now(),
        )
        await self._repo.save(used)
        return TokenAuthentication(user_id=record.user_id, scopes=record.scopes, token_id=record.id)

    async def list_tokens(self, *, actor_user_id: UUID, owner_user_id: UUID) -> list[ApiToken]:
        """Metadata only - never any material derived from the plaintext."""
        if actor_user_id != owner_user_id:
            raise TokenNotFoundError("list your own tokens")
        return await self._repo.list_for_user(owner_user_id)

    async def revoke(self, *, actor_user_id: UUID, token_id: UUID) -> ApiToken:
        token = await self._repo.get(token_id)
        if token is None or token.user_id != actor_user_id:
            raise TokenNotFoundError(f"token {token_id} not found")
        if token.revoked_at is not None:
            return token  # idempotent revoke
        revoked = ApiToken(
            id=token.id,
            user_id=token.user_id,
            name=token.name,
            token_hash=token.token_hash,
            prefix=token.prefix,
            scopes=token.scopes,
            created_at=token.created_at,
            revoked_at=self._now(),
            last_used_at=token.last_used_at,
        )
        saved = await self._repo.save(revoked)
        await self._audit_mutation(
            actor_user_id=actor_user_id,
            action="token.revoked",
            token=saved,
            metadata={"name": saved.name},
        )
        return saved

    async def _owned(self, actor_user_id: UUID, token_id: UUID) -> ApiToken:
        token = await self._repo.get(token_id)
        if token is None or token.user_id != actor_user_id:
            raise TokenNotFoundError(f"token {token_id} not found")
        return token

    def _now(self) -> datetime:
        return self._clock.now(UTC)

    async def _audit_mutation(
        self,
        *,
        actor_user_id: UUID,
        action: str,
        token: ApiToken,
        metadata: dict[str, str],
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            actor_id=actor_user_id,
            action=action,
            resource_type=self.resource_type,
            resource_id=str(token.id),
            metadata=metadata,
        )
