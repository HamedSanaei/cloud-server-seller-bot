"""Floating/primary IP service (M13-008).

Acceptance: independent resource billing supported.

IPs are FIRST-CLASS RESOURCES: a user allocates a floating IP (quota-
limited), binds it to at most one of their servers, and releases it -
and while it exists it accrues cost through the operator's floating-IP
rate card whether bound or not (see modules/pricing/floating.py; the
service exposes ``accrual_quantum_minor`` so the billing engine can
charge IPs independently of any server).

Every operation is performed as an ACTING USER; another user's IPs look
nonexistent. Provider calls go through the OPTIONAL ``floating_ips``
port (capability probe ``floating_port_of``); release is 404-idempotent.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.networking.ips import (
    IpAddress,
    IpAddressError,
    IpBoundError,
    IpKind,
    IpLimitError,
    IpNotFoundError,
)
from cloud_platform.modules.pricing.floating import floating_hourly_quantum_minor


class IpRepository(Protocol):
    async def add(self, record: IpAddress) -> IpAddress: ...
    async def get(self, ip_id: UUID) -> IpAddress | None: ...
    async def list_for_user(self, user_id: UUID) -> list[IpAddress]: ...
    async def save(self, record: IpAddress) -> IpAddress: ...
    async def delete(self, ip_id: UUID) -> None: ...


class FloatingIpPort(Protocol):
    """Provider adapter surface for floating IP lifecycle."""

    async def create_floating_ip(self, location_id: str) -> tuple[str, str]:
        """Return (provider_ip_id, ip)."""
        ...

    async def assign_floating_ip(self, provider_ip_id: str, provider_server_id: str) -> None: ...

    async def unassign_floating_ip(self, provider_ip_id: str) -> None: ...

    async def delete_floating_ip(self, provider_ip_id: str) -> None:
        """Must be idempotent (404 treated as already gone)."""
        ...


class ServerLookupPort(Protocol):
    async def get(self, server_id: UUID) -> Any: ...


def floating_port_of(provider: object) -> FloatingIpPort | None:
    """Capability probe for the optional floating-IP port."""
    port = getattr(provider, "floating_ips", None)
    required = (
        "create_floating_ip",
        "assign_floating_ip",
        "unassign_floating_ip",
        "delete_floating_ip",
    )
    if port is not None and all(callable(getattr(port, name, None)) for name in required):
        return port  # type: ignore[no-any-return]
    return None


class IpService:
    """Ownership-scoped floating-IP lifecycle with independent accrual."""

    resource_type = "floating_ip"

    def __init__(
        self,
        *,
        repo: IpRepository,
        audit_repo: Any,
        server_repo: ServerLookupPort | None = None,
        max_ips_per_user: int = 5,
    ) -> None:
        self._repo = repo
        self._audit = AuditTrail(audit_repo)
        self._servers = server_repo
        self._max = max_ips_per_user

    async def allocate(
        self,
        *,
        actor_user_id: UUID,
        owner_user_id: UUID,
        provider_account_id: UUID,
        provider_key: str,
        provider_port: FloatingIpPort,
        location_id: str,
    ) -> IpAddress:
        if actor_user_id != owner_user_id:
            raise IpNotFoundError("manage your own IPs")
        owned = await self._repo.list_for_user(owner_user_id)
        if len([r for r in owned if r.kind is IpKind.FLOATING]) >= self._max:
            raise IpLimitError(f"at most {self._max} floating IPs per user")
        provider_ip_id, ip = await provider_port.create_floating_ip(location_id)
        saved = await self._repo.add(
            IpAddress(
                user_id=owner_user_id,
                provider_account_id=provider_account_id,
                provider_key=provider_key,
                provider_ip_id=provider_ip_id,
                ip=ip,
                kind=IpKind.FLOATING,
                location_id=location_id,
            )
        )
        await self._record(owner_user_id, "ip.allocated", saved, {"ip": saved.ip})
        return saved

    async def list_owned(self, *, actor_user_id: UUID) -> list[IpAddress]:
        return await self._repo.list_for_user(actor_user_id)

    async def get_owned(self, *, actor_user_id: UUID, ip_id: UUID) -> IpAddress:
        record = await self._repo.get(ip_id)
        if record is None or record.user_id != actor_user_id:
            raise IpNotFoundError("ip not found")
        return record

    async def assign(
        self, *, actor_user_id: UUID, ip_id: UUID, server_id: UUID, provider_port: FloatingIpPort
    ) -> IpAddress:
        record = await self.get_owned(actor_user_id=actor_user_id, ip_id=ip_id)
        if self._servers is None:
            raise IpAddressError("server lookup is not configured")
        server = await self._servers.get(server_id)
        if server is None or server.user_id != actor_user_id:
            raise IpNotFoundError("server not found")
        await provider_port.assign_floating_ip(
            record.provider_ip_id, str(server.provider_server_id)
        )
        saved = await self._repo.save(replace(record, server_id=server_id))
        await self._record(actor_user_id, "ip.assigned", saved, {"server_id": str(server_id)})
        return saved

    async def unbind(
        self, *, actor_user_id: UUID, ip_id: UUID, provider_port: FloatingIpPort
    ) -> IpAddress:
        record = await self.get_owned(actor_user_id=actor_user_id, ip_id=ip_id)
        if record.is_bound:
            await provider_port.unassign_floating_ip(record.provider_ip_id)
            saved = await self._repo.save(replace(record, server_id=None))
            await self._record(actor_user_id, "ip.unassigned", saved, {})
            return saved
        return record

    async def release(
        self, *, actor_user_id: UUID, ip_id: UUID, provider_port: FloatingIpPort
    ) -> None:
        record = await self.get_owned(actor_user_id=actor_user_id, ip_id=ip_id)
        if record.is_bound:
            raise IpBoundError("unbind the IP from its server before releasing it")
        # Idempotent at the provider (404 == already gone).
        await provider_port.delete_floating_ip(record.provider_ip_id)
        await self._repo.delete(ip_id)
        await self._record(actor_user_id, "ip.released", record, {"ip": record.ip})

    async def accrual_quantum_minor(
        self,
        actor_user_id: UUID,
        card: Any,
        quantum_seconds: int = 3600,
    ) -> int:
        """Per-quantum cost of ALL the user's floating IPs - independent of binding."""
        owned = await self._repo.list_for_user(actor_user_id)
        return floating_hourly_quantum_minor(card, len(owned), quantum_seconds)

    async def _record(
        self, actor_user_id: UUID, action: str, record: IpAddress, metadata: dict[str, str]
    ) -> None:
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            actor_id=actor_user_id,
            action=action,
            resource_type=self.resource_type,
            resource_id=str(record.id),
            metadata={"provider_ip_id": record.provider_ip_id, **metadata},
        )
