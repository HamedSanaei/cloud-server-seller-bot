"""Volume service (M13-009).

Acceptance: attach/detach/delete reconciliation.

Reconciliation semantics, all proven by tests:

- ATTACH is idempotent: attaching to the server it is already attached
  to succeeds WITHOUT a provider call.
- DETACH is idempotent: detaching an unattached volume is a no-op.
- DELETE is 404-idempotent at the provider and always drops the local
  row (a volume the provider already forgot disappears for us too).
- ``reconcile`` repairs DRIFT between local rows and remote state:
  volumes that vanished remotely are purged locally; volumes detached
  remotely but still bound locally are unbound.

Every operation is ownership-scoped; another user's volumes read as
missing. The optional ``volumes`` port is capability-probed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.networking.volumes import (
    Volume,
    VolumeLimitError,
    VolumeNotFoundError,
)


class VolumeRepository(Protocol):
    async def add(self, volume: Volume) -> Volume: ...
    async def get(self, volume_id: UUID) -> Volume | None: ...
    async def list_for_user(self, user_id: UUID) -> list[Volume]: ...
    async def save(self, volume: Volume) -> Volume: ...
    async def delete(self, volume_id: UUID) -> None: ...
    async def list_all(self) -> list[Volume]: ...


class ProviderVolumePort(Protocol):
    """Provider adapter surface for volume lifecycle."""

    async def create_volume(self, name: str, size_gb: int, location_id: str) -> tuple[str, str]:
        """Return (provider_volume_id, provider_volume_name)."""
        ...

    async def attach_volume(self, provider_volume_id: str, provider_server_id: str) -> None: ...

    async def detach_volume(self, provider_volume_id: str) -> None: ...

    async def delete_volume(self, provider_volume_id: str) -> None:
        """Must be idempotent (404 treated as already gone)."""
        ...

    async def list_volumes(self) -> list[tuple[str, str | None]]:
        """Return (provider_volume_id, attached_provider_server_id | None)."""
        ...


class ServerLookupPort(Protocol):
    async def get(self, server_id: UUID) -> Any:
        ...


def volume_port_of(provider: object) -> ProviderVolumePort | None:
    """Capability probe for the optional volume port."""
    port = getattr(provider, "volumes", None)
    required = (
        "create_volume",
        "attach_volume",
        "detach_volume",
        "delete_volume",
        "list_volumes",
    )
    if port is not None and all(callable(getattr(port, name, None)) for name in required):
        return port  # type: ignore[no-any-return]
    return None


class VolumeService:
    """Ownership-scoped volume lifecycle with drift reconciliation."""

    resource_type = "volume"

    def __init__(
        self,
        *,
        repo: VolumeRepository,
        audit_repo: Any,
        server_repo: ServerLookupPort | None = None,
        max_volumes_per_user: int = 10,
    ) -> None:
        self._repo = repo
        self._audit = AuditTrail(audit_repo)
        self._servers = server_repo
        self._max = max_volumes_per_user

    async def create(
        self,
        *,
        actor_user_id: UUID,
        owner_user_id: UUID,
        provider_account_id: UUID,
        provider_key: str,
        provider_port: ProviderVolumePort,
        name: str,
        size_gb: int,
        location_id: str,
    ) -> Volume:
        if actor_user_id != owner_user_id:
            raise VolumeNotFoundError("manage your own volumes")
        owned = await self._repo.list_for_user(owner_user_id)
        if len(owned) >= self._max:
            raise VolumeLimitError(f"at most {self._max} volumes per user")
        provider_volume_id, _remote_name = await provider_port.create_volume(
            name, size_gb, location_id
        )
        saved = await self._repo.add(
            Volume(
                user_id=owner_user_id,
                provider_account_id=provider_account_id,
                provider_key=provider_key,
                provider_volume_id=provider_volume_id,
                name=name,
                size_gb=size_gb,
                location_id=location_id,
            )
        )
        await self._record(owner_user_id, "volume.created", saved, {"size_gb": str(size_gb)})
        return saved

    async def list_owned(self, *, actor_user_id: UUID) -> list[Volume]:
        return await self._repo.list_for_user(actor_user_id)

    async def get_owned(self, *, actor_user_id: UUID, volume_id: UUID) -> Volume:
        volume = await self._repo.get(volume_id)
        if volume is None or volume.user_id != actor_user_id:
            raise VolumeNotFoundError("volume not found")
        return volume

    async def attach(
        self,
        *,
        actor_user_id: UUID,
        volume_id: UUID,
        server_id: UUID,
        provider_port: ProviderVolumePort,
    ) -> Volume:
        volume = await self.get_owned(actor_user_id=actor_user_id, volume_id=volume_id)
        if volume.is_attached and volume.server_id == server_id:
            return volume  # idempotent replay - no provider call
        if self._servers is None:
            raise VolumeNotFoundError("server lookup is not configured")
        server = await self._servers.get(server_id)
        if server is None or server.user_id != actor_user_id:
            raise VolumeNotFoundError("server not found")
        await provider_port.attach_volume(volume.provider_volume_id, str(server.provider_server_id))
        saved = await self._repo.save(replace(volume, server_id=server_id))
        await self._record(actor_user_id, "volume.attached", saved, {"server_id": str(server_id)})
        return saved

    async def detach(
        self, *, actor_user_id: UUID, volume_id: UUID, provider_port: ProviderVolumePort
    ) -> Volume:
        volume = await self.get_owned(actor_user_id=actor_user_id, volume_id=volume_id)
        if not volume.is_attached:
            return volume  # already detached - no-op
        await provider_port.detach_volume(volume.provider_volume_id)
        saved = await self._repo.save(replace(volume, server_id=None))
        await self._record(actor_user_id, "volume.detached", saved, {})
        return saved

    async def delete(
        self, *, actor_user_id: UUID, volume_id: UUID, provider_port: ProviderVolumePort
    ) -> None:
        volume = await self.get_owned(actor_user_id=actor_user_id, volume_id=volume_id)
        # Idempotent at the provider (404 == already gone).
        await provider_port.delete_volume(volume.provider_volume_id)
        await self._repo.delete(volume_id)
        await self._record(actor_user_id, "volume.deleted", volume, {"name": volume.name})

    async def reconcile(self, provider_port: ProviderVolumePort) -> dict[str, int]:
        """Repair binding drift between local rows and the provider.

        - volumes the provider no longer knows are purged locally;
        - volumes detached remotely but still bound locally are unbound.

        Returns counters of applied actions for the drill evidence.
        """
        remote = {vid: attached for vid, attached in await provider_port.list_volumes()}
        purged = unbound = 0
        for volume in await self._repo.list_all():
            if volume.provider_volume_id not in remote:
                # The provider forgot this volume (deleted out-of-band):
                # our row must follow reality.
                await self._repo.delete(volume.id)  # type: ignore[arg-type]
                purged += 1
            elif remote[volume.provider_volume_id] is None and volume.is_attached:
                # Detached remotely but still bound locally: clear it.
                await self._repo.save(replace(volume, server_id=None))
                unbound += 1
        summary = {"purged": purged, "unbound": unbound}
        if sum(summary.values()):
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="volume.reconciled",
                resource_type=self.resource_type,
                resource_id="*",
                metadata={k: str(v) for k, v in summary.items()},
            )
        return summary

    async def _record(
        self, actor_user_id: UUID, action: str, volume: Volume, metadata: dict[str, str]
    ) -> None:
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            actor_id=actor_user_id,
            action=action,
            resource_type=self.resource_type,
            resource_id=str(volume.id),
            metadata={"provider_volume_id": volume.provider_volume_id, **metadata},
        )
