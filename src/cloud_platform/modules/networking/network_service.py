"""Private network service (M13-010).

Acceptance: network lifecycle and capabilities.

LIFECYCLE: create -> (attach servers -> detach servers)* -> delete.
- create validates the RFC-1918 range, enforces the per-user quota;
- attach requires owning BOTH the network and the server and is
  idempotent for an already-joined server (no provider call);
- detach of a non-member is a no-op;
- delete is 404-idempotent at the provider and always drops the row.

CAPABILITIES: the provider port is OPTIONAL - ``network_port_of``
probes for it; unsupported providers surface NetworkUnsupportedError
instead of branching per provider. Every mutation is audited.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.networking.networks import (
    Network,
    NetworkLimitError,
    NetworkNotFoundError,
)


class NetworkRepository(Protocol):
    async def add(self, network: Network) -> Network: ...
    async def get(self, network_id: UUID) -> Network | None: ...
    async def list_for_user(self, user_id: UUID) -> list[Network]: ...
    async def save(self, network: Network) -> Network: ...
    async def delete(self, network_id: UUID) -> None: ...


class ProviderNetworkPort(Protocol):
    """Provider adapter surface for private-network lifecycle."""

    async def create_network(self, name: str, ip_range: str) -> str:
        """Return the provider network id."""
        ...

    async def attach_server(self, provider_network_id: str, provider_server_id: str) -> None: ...

    async def detach_server(self, provider_network_id: str, provider_server_id: str) -> None: ...

    async def delete_network(self, provider_network_id: str) -> None:
        """Must be idempotent (404 treated as already gone)."""
        ...


class ServerLookupPort(Protocol):
    async def get(self, server_id: UUID) -> Any: ...


def network_port_of(provider: object) -> ProviderNetworkPort | None:
    """Capability probe for the optional network port."""
    port = getattr(provider, "networks", None)
    required = ("create_network", "attach_server", "detach_server", "delete_network")
    if port is not None and all(callable(getattr(port, name, None)) for name in required):
        return port  # type: ignore[no-any-return]
    return None


class NetworkService:
    """Ownership-scoped private-network lifecycle."""

    resource_type = "private_network"

    def __init__(
        self,
        *,
        repo: NetworkRepository,
        audit_repo: Any,
        server_repo: ServerLookupPort | None = None,
        max_networks_per_user: int = 5,
    ) -> None:
        self._repo = repo
        self._audit = AuditTrail(audit_repo)
        self._servers = server_repo
        self._max = max_networks_per_user

    async def create(
        self,
        *,
        actor_user_id: UUID,
        owner_user_id: UUID,
        provider_account_id: UUID,
        provider_key: str,
        provider_port: ProviderNetworkPort,
        name: str,
        ip_range: str,
        location_id: str | None = None,
    ) -> Network:
        if actor_user_id != owner_user_id:
            raise NetworkNotFoundError("manage your own networks")
        owned = await self._repo.list_for_user(owner_user_id)
        if len(owned) >= self._max:
            raise NetworkLimitError(f"at most {self._max} networks per user")
        provider_network_id = await provider_port.create_network(name, ip_range)
        saved = await self._repo.add(
            Network(
                user_id=owner_user_id,
                provider_account_id=provider_account_id,
                provider_key=provider_key,
                provider_network_id=provider_network_id,
                name=name,
                ip_range=ip_range,
                location_id=location_id,
            )
        )
        await self._record(owner_user_id, "network.created", saved, {"ip_range": saved.ip_range})
        return saved

    async def list_owned(self, *, actor_user_id: UUID) -> list[Network]:
        return await self._repo.list_for_user(actor_user_id)

    async def get_owned(self, *, actor_user_id: UUID, network_id: UUID) -> Network:
        network = await self._repo.get(network_id)
        if network is None or network.user_id != actor_user_id:
            raise NetworkNotFoundError("network not found")
        return network

    async def attach_server(
        self,
        *,
        actor_user_id: UUID,
        network_id: UUID,
        server_id: UUID,
        provider_port: ProviderNetworkPort,
    ) -> Network:
        network = await self.get_owned(actor_user_id=actor_user_id, network_id=network_id)
        if network.joined(server_id):
            return network  # idempotent replay - no provider call
        if self._servers is None:
            raise NetworkNotFoundError("server lookup is not configured")
        server = await self._servers.get(server_id)
        if server is None or server.user_id != actor_user_id:
            raise NetworkNotFoundError("server not found")
        await provider_port.attach_server(
            network.provider_network_id, str(server.provider_server_id)
        )
        saved = await self._repo.save(replace(network, server_ids=(*network.server_ids, server_id)))
        await self._record(
            actor_user_id, "network.server_attached", saved, {"server_id": str(server_id)}
        )
        return saved

    async def detach_server(
        self,
        *,
        actor_user_id: UUID,
        network_id: UUID,
        server_id: UUID,
        provider_port: ProviderNetworkPort,
    ) -> Network:
        network = await self.get_owned(actor_user_id=actor_user_id, network_id=network_id)
        if not network.joined(server_id):
            return network  # already out - no-op
        remaining = tuple(sid for sid in network.server_ids if sid != server_id)
        saved_local = replace(network, server_ids=remaining)
        # resolve the provider id BEFORE dropping membership from our state
        server = await self._servers.get(server_id) if self._servers is not None else None
        if server is not None:
            await provider_port.detach_server(
                network.provider_network_id, str(server.provider_server_id)
            )
        saved = await self._repo.save(saved_local)
        await self._record(
            actor_user_id, "network.server_detached", saved, {"server_id": str(server_id)}
        )
        return saved

    async def delete(
        self, *, actor_user_id: UUID, network_id: UUID, provider_port: ProviderNetworkPort
    ) -> None:
        network = await self.get_owned(actor_user_id=actor_user_id, network_id=network_id)
        # Idempotent at the provider (404 == already gone).
        await provider_port.delete_network(network.provider_network_id)
        await self._repo.delete(network_id)
        await self._record(actor_user_id, "network.deleted", network, {"name": network.name})

    async def _record(
        self, actor_user_id: UUID, action: str, network: Network, metadata: dict[str, str]
    ) -> None:
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            actor_id=actor_user_id,
            action=action,
            resource_type=self.resource_type,
            resource_id=str(network.id),
            metadata={"provider_network_id": network.provider_network_id, **metadata},
        )
