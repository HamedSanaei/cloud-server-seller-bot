"""Firewall service (M13-006).

Acceptance: reusable rules and ownership.

Every operation is performed as an ACTING USER; another user's firewalls
look nonexistent. A firewall is a reusable rulebook: it is created once,
edited as a whole, and materialized at providers idempotently by name -
``ensure_remote_firewall`` reuses the provider-side firewall with the same
name (updating its rules) instead of ever duplicating it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.audit.service import AuditTrail

from .domain import (
    DuplicateFirewallError,
    Firewall,
    FirewallLimitError,
    FirewallNotFoundError,
    FirewallRule,
)


class FirewallRepository(Protocol):
    async def add(self, firewall: Firewall) -> Firewall: ...
    async def get(self, firewall_id: UUID) -> Firewall | None: ...
    async def list_for_user(self, user_id: UUID) -> list[Firewall]: ...
    async def save(self, firewall: Firewall) -> Firewall: ...
    async def delete(self, firewall_id: UUID) -> None: ...


class ProviderFirewallPort(Protocol):
    """What a provider adapter must offer for firewall sync."""

    async def list_firewalls(self) -> list[tuple[str, str]]:
        """Return (provider_id, name) pairs."""
        ...

    async def create_firewall(self, name: str, rules: list[dict[str, object]]) -> str: ...

    async def update_firewall_rules(
        self, provider_firewall_id: str, rules: list[dict[str, object]]
    ) -> None: ...


def firewall_port_of(provider: object) -> ProviderFirewallPort | None:
    """Capability probe (mirrors the ssh-keys pattern)."""
    port = getattr(provider, "firewalls", None)
    required = ("list_firewalls", "create_firewall", "update_firewall_rules")
    if port is not None and all(callable(getattr(port, name, None)) for name in required):
        return port  # type: ignore[no-any-return]
    return None


async def ensure_remote_firewall(
    port: ProviderFirewallPort,
    firewall: Firewall,
) -> str:
    """Materialize the rulebook at the provider; return the provider id.

    Idempotent BY NAME: an existing remote firewall with the same name is
    UPDATED to the current rules and reused - never duplicated. This is
    what makes the rulebook reusable across syncs and servers.
    """
    rules = firewall.to_dicts()
    for remote_id, remote_name in await port.list_firewalls():
        if remote_name == firewall.name:
            await port.update_firewall_rules(remote_id, rules)
            return remote_id
    return await port.create_firewall(firewall.name, rules)


class FirewallService:
    """Ownership-scoped CRUD for reusable firewall rulebooks."""

    resource_type = "firewall"

    def __init__(
        self,
        firewalls: FirewallRepository,
        audit: AuditTrail | None = None,
        *,
        max_firewalls_per_user: int = 10,
    ) -> None:
        self._repo = firewalls
        self._audit = audit
        self._max = max_firewalls_per_user

    async def create(
        self,
        *,
        actor_user_id: UUID,
        owner_user_id: UUID,
        name: str,
        rules: Sequence[FirewallRule],
    ) -> Firewall:
        if actor_user_id != owner_user_id:
            raise FirewallNotFoundError("manage your own firewalls")
        firewall = Firewall(user_id=owner_user_id, name=name, rules=tuple(rules))
        existing = await self._repo.list_for_user(owner_user_id)
        if len(existing) >= self._max:
            raise FirewallLimitError(f"at most {self._max} firewalls per user")
        if any(fw.name == firewall.name for fw in existing):
            raise DuplicateFirewallError(f"name {firewall.name!r} already registered")
        saved = await self._repo.add(firewall)
        await self._audit_mutation(
            actor_user_id=owner_user_id,
            action="firewall.created",
            firewall=saved,
            metadata={"name": saved.name, "rule_count": str(len(saved.rules))},
        )
        return saved

    async def get_owned(self, *, actor_user_id: UUID, firewall_id: UUID) -> Firewall:
        return await self._owned(actor_user_id, firewall_id)

    async def list_keys(self, *, actor_user_id: UUID, owner_user_id: UUID) -> list[Firewall]:
        if actor_user_id != owner_user_id:
            raise FirewallNotFoundError("list your own firewalls")
        return await self._repo.list_for_user(owner_user_id)

    async def replace_rules(
        self,
        *,
        actor_user_id: UUID,
        firewall_id: UUID,
        rules: Sequence[FirewallRule],
    ) -> Firewall:
        firewall = await self._owned(actor_user_id, firewall_id)
        updated = Firewall(
            id=firewall.id,
            user_id=firewall.user_id,
            name=firewall.name,
            rules=tuple(rules),
            provider_firewall_id=firewall.provider_firewall_id,
            created_at=firewall.created_at,
        )
        # editing rules invalidates the remote copy's content; keep the
        # provider linkage so the next sync UPDATES rather than duplicates.
        saved = await self._repo.save(updated)
        await self._audit_mutation(
            actor_user_id=actor_user_id,
            action="firewall.rules_replaced",
            firewall=saved,
            metadata={"name": saved.name, "rule_count": str(len(saved.rules))},
        )
        return saved

    async def delete(self, *, actor_user_id: UUID, firewall_id: UUID) -> None:
        firewall = await self._owned(actor_user_id, firewall_id)
        await self._repo.delete(firewall.id)  # type: ignore[arg-type]
        await self._audit_mutation(
            actor_user_id=actor_user_id,
            action="firewall.deleted",
            firewall=firewall,
            metadata={"name": firewall.name},
        )

    # -- internals ---------------------------------------------------------

    async def _owned(self, actor_user_id: UUID, firewall_id: UUID) -> Firewall:
        firewall = await self._repo.get(firewall_id)
        if firewall is None or firewall.user_id != actor_user_id:
            raise FirewallNotFoundError(f"firewall {firewall_id} not found")
        return firewall

    async def _audit_mutation(
        self,
        *,
        actor_user_id: UUID,
        action: str,
        firewall: Firewall,
        metadata: dict[str, str],
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            actor_id=actor_user_id,
            action=action,
            resource_type=self.resource_type,
            resource_id=str(firewall.id),
            metadata=metadata,
        )


def rules_from_payload(payload: Sequence[dict[str, Any]]) -> tuple[FirewallRule, ...]:
    """Validate untrusted rule payloads into domain values."""
    return tuple(FirewallRule.from_dict(dict(raw)) for raw in payload)
