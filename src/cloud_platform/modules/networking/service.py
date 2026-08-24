"""Networking services (M13-007): reverse DNS management.

Acceptance: IP ownership and validation.

``RdnsService.set_ptr`` proves, in order:

1. OWNERSHIP - the caller owns the platform server (foreign = not found);
2. CAPABILITY - the provider adapter implements reverse DNS at all;
3. IP MEMBERSHIP - the provider reports the requested IP as one of that
   server's addresses (the user cannot PTR an arbitrary internet host);
4. VALIDATION - the PTR hostname passes RFC-1035 checks.

The provider call is naturally idempotent (re-setting an identical PTR
is a no-op) and every change is audited.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.compute.domain import CloudServer
from cloud_platform.modules.networking.domain import (
    RdnsIpNotAssignedError,
    RdnsNotOwnerError,
    RdnsUnsupportedError,
    ReverseDnsRecord,
)
from cloud_platform.providers.base import rdns_support_of


class ServerRepositoryPort(Protocol):
    async def get(self, server_id: UUID) -> CloudServer | None: ...


class ProviderRegistryPort(Protocol):
    def get(self, provider_key: str) -> Any:
        ...


class RdnsService:
    """Ownership-proven reverse DNS set/reset for one server."""

    resource_type = "server_rdns"

    def __init__(
        self,
        *,
        server_repo: ServerRepositoryPort,
        provider_registry: ProviderRegistryPort,
        audit_repo: Any,
        clock: Any = None,
    ) -> None:
        self._servers = server_repo
        self._registry = provider_registry
        self._audit = AuditTrail(audit_repo)

    async def set_ptr(
        self,
        user_id: UUID,
        server_id: UUID,
        record: ReverseDnsRecord,
    ) -> dict[str, Any]:
        """Point ``record.ip`` at ``record.ptr`` (None resets)."""
        server = await self._servers.get(server_id)
        if server is None or server.user_id != user_id:
            raise RdnsNotOwnerError("server not found")

        provider = self._registry.get(server.provider_key)
        if provider is None:
            raise RdnsUnsupportedError(f"provider {server.provider_key} is not registered")
        setter = rdns_support_of(provider)
        if setter is None:
            raise RdnsUnsupportedError(
                f"provider {server.provider_key} does not support reverse DNS"
            )

        # IP ownership proof: the address must be one of THIS server's.
        remote = await provider.get_server(str(server.provider_server_id or ""))
        if remote is None:
            raise RdnsNotOwnerError("server not found")
        assigned = {str(remote.ipv4 or "")} | {str(remote.ipv6 or "")}
        if record.ip not in {ip for ip in assigned if ip}:
            raise RdnsIpNotAssignedError(f"ip {record.ip} is not assigned to this server")

        await setter(str(server.provider_server_id), record.ip, record.ptr)
        action = "rdns.reset" if record.ptr is None else "rdns.set"
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            actor_id=user_id,
            action=action,
            resource_type=self.resource_type,
            resource_id=str(server_id),
            metadata={"ip": record.ip, "ptr": record.ptr},
        )
        return {"server_id": str(server_id), "ip": record.ip, "ptr": record.ptr}
