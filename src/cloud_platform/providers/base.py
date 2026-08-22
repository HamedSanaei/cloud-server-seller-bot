from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from cloud_platform.core.idempotency import IdempotencyKey


class Capability(StrEnum):
    COMPUTE = "compute"
    POWER = "power"
    REBUILD = "rebuild"
    RESCUE = "rescue"
    SNAPSHOT = "snapshot"
    BACKUP = "backup"
    FIREWALL = "firewall"
    NETWORK = "network"
    VOLUME = "volume"
    FLOATING_IP = "floating_ip"
    PRIMARY_IP = "primary_ip"
    RDNS = "rdns"


@dataclass(frozen=True, slots=True)
class ProviderLocation:
    id: str
    name: str
    country_code: str
    city: str | None = None
    network_zone: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderPlan:
    id: str
    name: str
    architecture: str
    vcpu: int
    memory_mb: int
    disk_gb: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderImage:
    id: str
    name: str
    os_family: str
    architecture: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CreateServerRequest:
    name: str
    plan_id: str
    image_id: str
    location_id: str
    ssh_key_ids: tuple[str, ...] = ()
    user_data: str | None = None
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderServer:
    id: str
    name: str
    status: str
    ipv4: str | None = None
    ipv6: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class CloudProvider(Protocol):
    key: str
    capabilities: frozenset[Capability]

    async def list_locations(self) -> list[ProviderLocation]: ...
    async def list_plans(self) -> list[ProviderPlan]: ...
    async def list_images(self) -> list[ProviderImage]: ...
    async def get_server(self, provider_server_id: str) -> ProviderServer | None: ...
    async def create_server(
        self, request: CreateServerRequest, idempotency_key: IdempotencyKey
    ) -> ProviderServer: ...
    async def delete_server(
        self, provider_server_id: str, idempotency_key: IdempotencyKey
    ) -> None: ...
    async def power_on(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None: ...
    async def power_off(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None: ...
    async def reboot(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None: ...


def supports(provider: CloudProvider, capability: Capability) -> bool:
    """Return True if ``provider`` advertises support for ``capability``."""
    return capability in provider.capabilities


def supports_all(provider: CloudProvider, capabilities: Iterable[Capability]) -> bool:
    """Return True if ``provider`` advertises support for every capability in ``capabilities``."""
    return frozenset(capabilities).issubset(provider.capabilities)


def supports_any(provider: CloudProvider, capabilities: Iterable[Capability]) -> bool:
    """Return True if ``provider`` advertises support for any capability in ``capabilities``."""
    return not provider.capabilities.isdisjoint(capabilities)
