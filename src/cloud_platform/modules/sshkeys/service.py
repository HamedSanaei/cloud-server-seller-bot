"""SSH key service (M13-001).

Acceptance: user keys are ownership-scoped.

Every method takes ``actor_user_id`` and enforces, IN THIS LAYER, that the
actor owns what they touch. Another user's keys are indistinguishable from
nonexistent keys (no existence leak). Registration validates the key,
fingerprint-dedupes it and caps the per-user quota; sync pushes a user's
keys to a provider idempotently (same deterministic name + same
fingerprint => reuse).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.audit.service import AuditTrail

from .domain import (
    DuplicateSshKeyError,
    SshKey,
    SshKeyLimitError,
    SshKeyNotFoundError,
)


class SshKeyRepository(Protocol):
    async def add(self, key: SshKey) -> SshKey: ...
    async def get(self, key_id: UUID) -> SshKey | None: ...
    async def list_for_user(self, user_id: UUID) -> list[SshKey]: ...
    async def delete(self, key_id: UUID) -> None: ...


class SshKeyService:
    """Ownership-scoped CRUD plus provider sync for user SSH keys."""

    resource_type = "ssh_key"

    def __init__(
        self,
        keys: SshKeyRepository,
        audit: AuditTrail | None = None,
        *,
        max_keys_per_user: int = 20,
    ) -> None:
        self._keys = keys
        self._audit = audit
        self._max_keys_per_user = max_keys_per_user

    # -- registration -----------------------------------------------------

    async def register(
        self, *, actor_user_id: UUID, owner_user_id: UUID, name: str, public_key: str
    ) -> SshKey:
        if actor_user_id != owner_user_id:
            # users may only register keys on their own account
            raise SshKeyNotFoundError("register keys on your own account")
        existing = await self._keys.list_for_user(owner_user_id)
        new_key = SshKey(user_id=owner_user_id, name=name, public_key=public_key)
        if len(existing) >= self._max_keys_per_user:
            raise SshKeyLimitError(f"at most {self._max_keys_per_user} keys per user")
        for key in existing:
            if key.name == new_key.name:
                raise DuplicateSshKeyError(f"name {new_key.name!r} already registered")
            if key.fingerprint == new_key.fingerprint:
                raise DuplicateSshKeyError("this exact key is already registered")
        saved = await self._keys.add(new_key)
        await self._audit_mutation(
            actor_user_id=owner_user_id,
            action="sshkey.registered",
            key=saved,
            metadata={"name": saved.name, "fingerprint": saved.fingerprint},
        )
        return saved

    # -- reads (ownership-scoped) ------------------------------------------

    async def get_owned(self, *, actor_user_id: UUID, key_id: UUID) -> SshKey:
        return await self._owned(actor_user_id, key_id)

    async def list_keys(self, *, actor_user_id: UUID, owner_user_id: UUID) -> list[SshKey]:
        if actor_user_id != owner_user_id:
            # scoped listing: another user's list is empty-by-denial
            raise SshKeyNotFoundError("list your own keys")
        return await self._keys.list_for_user(owner_user_id)

    # -- deletion (ownership-scoped) ----------------------------------------

    async def delete(self, *, actor_user_id: UUID, key_id: UUID) -> None:
        key = await self._owned(actor_user_id, key_id)
        await self._keys.delete(key.id)  # type: ignore[arg-type]
        await self._audit_mutation(
            actor_user_id=actor_user_id,
            action="sshkey.deleted",
            key=key,
            metadata={"name": key.name},
        )

    # -- provider sync -------------------------------------------------------

    async def ids_for_provider(self, owner_user_id: UUID) -> list[str]:
        """Stable identifiers a provider adapter can consume at create time."""
        keys = await self._keys.list_for_user(owner_user_id)
        return [str(key.id) for key in keys if key.id is not None]

    # -- internals -------------------------------------------------------------

    async def _owned(self, actor_user_id: UUID, key_id: UUID) -> SshKey:
        key = await self._keys.get(key_id)
        if key is None or key.user_id != actor_user_id:
            # no existence leak: foreign keys look like missing keys
            raise SshKeyNotFoundError(f"ssh key {key_id} not found")
        return key

    async def _audit_mutation(
        self,
        *,
        actor_user_id: UUID,
        action: str,
        key: SshKey,
        metadata: dict[str, str],
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            actor_id=actor_user_id,
            action=action,
            resource_type=self.resource_type,
            resource_id=str(key.id),
            metadata=metadata,
        )


def provider_key_name(key: SshKey) -> str:
    """Deterministic provider-side name so syncs are idempotent by name."""

    short_user = str(key.user_id).replace("-", "")[:8]
    safe_name = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in key.name.lower())
    return f"platform-{short_user}-{safe_name}"[:64]


class ProviderSshKeyPort(Protocol):
    """What a provider adapter must offer for SSH-key sync.

    Implemented by adapters that have a native SSH-key API (Hetzner);
    capability-probed via :func:`ssh_key_port_of` - adapters without one
    simply do not provide the attribute.
    """

    async def list_ssh_keys(self) -> list[tuple[str, str, str]]:
        """Return (provider_id, name, fingerprint) triples."""
        ...

    async def upload_ssh_key(self, name: str, public_key: str) -> str: ...


def ssh_key_port_of(provider: object) -> ProviderSshKeyPort | None:
    """Capability probe: return the provider's ssh-key port if it has one."""

    port = getattr(provider, "ssh_keys", None)
    required = ("list_ssh_keys", "upload_ssh_key")
    if port is not None and all(callable(getattr(port, name, None)) for name in required):
        return port  # type: ignore[no-any-return]
    return None


async def sync_keys_to_provider(
    port: ProviderSshKeyPort,
    keys: Sequence[SshKey],
) -> list[str]:
    """Ensure every key exists at the provider; return the provider ids.

    Idempotent by construction: an existing remote entry with the same
    deterministic name AND same fingerprint is reused, not re-uploaded; a
    missing one is uploaded once. A same-name/different-fingerprint remote
    raises ``DuplicateSshKeyError`` - the remote name belongs to foreign
    material and is never silently overwritten.
    """
    remote_by_name: dict[str, tuple[str, str]] = {}
    for remote_id, name, fingerprint in await port.list_ssh_keys():
        remote_by_name[name] = (remote_id, fingerprint)

    ids: list[str] = []
    for key in keys:
        name = provider_key_name(key)
        existing = remote_by_name.get(name)
        if existing is not None:
            remote_id, remote_fingerprint = existing
            if remote_fingerprint != key.fingerprint:
                raise DuplicateSshKeyError(
                    f"provider already has a key named {name!r} with different material"
                )
            ids.append(remote_id)
            continue
        ids.append(await port.upload_ssh_key(name, key.public_key))
    return ids
