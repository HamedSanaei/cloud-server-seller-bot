"""Tests for customer API tokens (M14-002).

Acceptance: revocable hashed tokens and scopes.

- plaintext exists ONLY at creation; persistence/audit hold hashes and
  metadata, never material;
- tokens are revocable and revocation is immediate + idempotent;
- scopes gate capabilities (least privilege);
- management is ownership-scoped in the application layer.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.tokens.domain import (
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
from cloud_platform.modules.tokens.service import TokenService

USER_A = uuid4()
USER_B = uuid4()


class LocalTokenRepo:
    def __init__(self) -> None:
        self.by_id: dict[Any, ApiToken] = {}
        self.by_hash: dict[str, ApiToken] = {}

    async def add(self, token: ApiToken) -> ApiToken:
        assert token.id is not None
        self.by_id[token.id] = token
        self.by_hash[token.token_hash] = token
        return token

    async def get(self, token_id: Any) -> ApiToken | None:
        return self.by_id.get(token_id)

    async def get_by_hash(self, token_hash: str) -> ApiToken | None:
        return self.by_hash.get(token_hash)

    async def list_for_user(self, user_id: Any) -> list[ApiToken]:
        return [t for t in self.by_id.values() if t.user_id == user_id]

    async def save(self, token: ApiToken) -> ApiToken:
        assert token.id is not None
        self.by_id[token.id] = token
        self.by_hash[token.token_hash] = token
        return token


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


def make_service(
    max_tokens: int = 10,
) -> tuple[TokenService, LocalTokenRepo, RecordingAudit]:
    repo = LocalTokenRepo()
    audit = RecordingAudit()
    service = TokenService(repo, AuditTrail(audit), max_tokens_per_user=max_tokens)
    return service, repo, audit


class TestHashedTokens:
    async def test_plaintext_shown_once_never_stored(self) -> None:
        service, repo, _audit = make_service()
        _token, plaintext = await service.create(
            actor_user_id=USER_A,
            owner_user_id=USER_A,
            name="ci",
            scopes=frozenset({TokenScope.SERVERS_READ}),
        )
        assert plaintext.startswith("cpt_")
        # the repo stores only the HASH of the plaintext
        stored_hashes = {t.token_hash for t in repo.by_id.values()}
        assert hash_token(plaintext) in stored_hashes
        assert all(plaintext != h for h in stored_hashes)

    async def test_audit_never_carries_material(self) -> None:
        service, _repo, audit = make_service()
        _token, plaintext = await service.create(
            actor_user_id=USER_A, owner_user_id=USER_A, name="ci", scopes=frozenset()
        )
        dumped = repr(audit.events)
        assert plaintext not in dumped

    async def test_authenticate_round_trip_and_last_used(self) -> None:
        service, repo, _audit = make_service()
        token, raw = await service.create(
            actor_user_id=USER_A,
            owner_user_id=USER_A,
            name="ci",
            scopes=frozenset({TokenScope.CATALOG_READ}),
        )
        auth = await service.authenticate(raw)
        assert auth.user_id == USER_A
        assert auth.scopes == {TokenScope.CATALOG_READ}
        assert auth.token_id == token.id
        stored = repo.by_id[token.id]
        assert stored.last_used_at is not None

    async def test_unknown_or_malformed_rejected_identically(self) -> None:
        service, _repo, _audit = make_service()
        for bad in ("", "not-a-token", "cpt_", "cpt_totally-unknown"):
            with pytest.raises(InvalidTokenError):
                await service.authenticate(bad)

    async def test_revocation_is_immediate_and_final(self) -> None:
        service, _repo, _audit = make_service()
        token, raw = await service.create(
            actor_user_id=USER_A, owner_user_id=USER_A, name="tmp", scopes=frozenset()
        )
        await service.authenticate(raw)  # works before revoke
        revoked = await service.revoke(actor_user_id=USER_A, token_id=token.id)  # type: ignore[arg-type]
        assert revoked.revoked_at is not None
        with pytest.raises(InvalidTokenError):
            await service.authenticate(raw)  # immediately dead

    async def test_revoke_is_idempotent_for_owner(self) -> None:
        service, _repo, _audit = make_service()
        token, _raw = await service.create(
            actor_user_id=USER_A, owner_user_id=USER_A, name="tmp", scopes=frozenset()
        )
        first = await service.revoke(actor_user_id=USER_A, token_id=token.id)  # type: ignore[arg-type]
        second = await service.revoke(actor_user_id=USER_A, token_id=token.id)  # type: ignore[arg-type]
        assert first.revoked_at is not None
        assert second.revoked_at == first.revoked_at

    async def test_scopes_subset_validation(self) -> None:
        service, _repo, _audit = make_service()
        with pytest.raises(ValueError):
            await service.create(
                actor_user_id=USER_A,
                owner_user_id=USER_A,
                name="bad",
                scopes=frozenset({"not:a:scope"}),  # type: ignore[set-item]
            )


class TestOwnershipAndQuota:
    async def test_cannot_create_for_another_user(self) -> None:
        service, repo, _audit = make_service()
        with pytest.raises(TokenNotFoundError):
            await service.create(
                actor_user_id=USER_A, owner_user_id=USER_B, name="x", scopes=frozenset()
            )
        assert repo.by_id == {}

    async def test_duplicate_name_per_user_rejected(self) -> None:
        service, _repo, _audit = make_service()
        await service.create(
            actor_user_id=USER_A, owner_user_id=USER_A, name="dup", scopes=frozenset()
        )
        with pytest.raises(DuplicateTokenNameError):
            await service.create(
                actor_user_id=USER_A, owner_user_id=USER_A, name="dup", scopes=frozenset()
            )

    async def test_quota_enforced(self) -> None:
        service, _repo, _audit = make_service(max_tokens=1)
        await service.create(
            actor_user_id=USER_A, owner_user_id=USER_A, name="one", scopes=frozenset()
        )
        with pytest.raises(TokenLimitError):
            await service.create(
                actor_user_id=USER_A, owner_user_id=USER_A, name="two", scopes=frozenset()
            )

    async def test_foreign_list_and_revoke_are_not_found(self) -> None:
        service, repo, _audit = make_service()
        token, _raw = await service.create(
            actor_user_id=USER_A, owner_user_id=USER_A, name="mine", scopes=frozenset()
        )
        with pytest.raises(TokenNotFoundError):
            await service.list_tokens(actor_user_id=USER_B, owner_user_id=USER_A)
        with pytest.raises(TokenNotFoundError):
            await service.revoke(actor_user_id=USER_B, token_id=token.id)  # type: ignore[arg-type]
        # untouched by foreign attempts
        assert repo.by_id[token.id].revoked_at is None  # type: ignore[index]

    async def test_audit_events_use_user_actor(self) -> None:
        service, _repo, audit = make_service()
        token, _raw = await service.create(
            actor_user_id=USER_A, owner_user_id=USER_A, name="a", scopes=frozenset()
        )
        await service.revoke(actor_user_id=USER_A, token_id=token.id)  # type: ignore[arg-type]
        actions = [e.action for e in audit.events]
        assert actions == ["token.created", "token.revoked"]
        assert all(e.actor_type is ActorType.USER for e in audit.events)


class TestScopeSemantics:
    def test_authentication_has_is_conjunction(self) -> None:
        auth = TokenAuthentication(
            user_id=USER_A,
            scopes=frozenset({TokenScope.SERVERS_READ, TokenScope.SERVERS_WRITE}),
            token_id=uuid4(),
        )
        assert auth.has(TokenScope.SERVERS_READ, TokenScope.SERVERS_WRITE)
        assert not auth.has(TokenScope.WALLET_READ)

    def test_scope_enum_values_are_stable_strings(self) -> None:
        assert TokenScope.CATALOG_READ.value == "catalog:read"
        assert TokenScope.TOKENS_MANAGE.value == "tokens:manage"

    def test_generate_produces_prefixed_unique_tokens(self) -> None:
        tokens = {generate_raw_token() for _ in range(50)}
        assert len(tokens) == 50
        assert all(t.startswith("cpt_") for t in tokens)
