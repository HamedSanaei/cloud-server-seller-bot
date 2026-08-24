"""Tests for volumes (M13-009).

Acceptance: **attach/detach/delete reconciliation.**

- ATTACH is idempotent (same-server re-attach makes no provider call).
- DETACH is idempotent (detaching an unattached volume is a no-op).
- DELETE always drops the local row and is 404-idempotent at the
  provider.
- ``reconcile`` repairs drift: remote-vanished volumes are purged,
  remote-detached bindings cleared, and the repair itself is audited.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.networking.volume_service import VolumeService, volume_port_of
from cloud_platform.modules.networking.volumes import (
    Volume,
    VolumeLimitError,
    VolumeNotFoundError,
    VolumeSizeError,
)
from cloud_platform.modules.pricing.volumes import (
    VolumePricingError,
    VolumeRateCard,
    volume_hourly_quantum_minor,
    volume_monthly_minor,
)

USER = uuid4()
OTHER = uuid4()
ACCOUNT = uuid4()
SERVER_ID = uuid4()
NOW = datetime.now(UTC)


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


class LocalVolumeRepo:
    def __init__(self) -> None:
        self.rows: dict[UUID, Volume] = {}

    async def add(self, volume: Volume) -> Volume:
        stored = replace(volume, id=volume.id or uuid4(), created_at=volume.created_at or NOW)
        self.rows[stored.id] = stored  # type: ignore[index]
        return stored

    async def get(self, volume_id: UUID) -> Volume | None:
        return self.rows.get(volume_id)

    async def list_for_user(self, user_id: UUID) -> list[Volume]:
        return [v for v in self.rows.values() if v.user_id == user_id]

    async def list_all(self) -> list[Volume]:
        return list(self.rows.values())

    async def save(self, volume: Volume) -> Volume:
        assert volume.id is not None
        self.rows[volume.id] = volume
        return volume

    async def delete(self, volume_id: UUID) -> None:
        self.rows.pop(volume_id, None)


class FakeServerRepo:
    def __init__(self, server: CloudServer | None) -> None:
        self.server = server

    async def get(self, server_id: UUID) -> CloudServer | None:
        if self.server is not None and self.server.id == server_id:
            return self.server
        return None


class FakeVolumePort:
    """Provider-side lifecycle with scriptable remote state."""

    def __init__(self, *, ipv4_server: str | None = "42") -> None:
        self.next_id = 500
        self.attached: dict[str, str] = {}
        self.gone: set[str] = set()
        self.created: set[str] = set()
        self.calls: list[tuple[str, ...]] = []
        #: what GET /volumes would report right now: (id, attached_server|None)
        self.remote_state: dict[str, str | None] = {}

    async def create_volume(self, name: str, size_gb: int, location_id: str) -> tuple[str, str]:
        self.calls.append(("create", name))
        self.next_id += 1
        vid = f"vol-{self.next_id}"
        self.created.add(vid)
        # provider-side default state mirrors our own bookkeeping until a
        # test overrides it via drift()
        self.remote_state.setdefault(vid, None)
        return vid, name

    async def attach_volume(self, provider_volume_id: str, provider_server_id: str) -> None:
        self.calls.append(("attach", provider_volume_id, provider_server_id))
        self.attached[provider_volume_id] = provider_server_id
        self.remote_state[provider_volume_id] = provider_server_id

    async def detach_volume(self, provider_volume_id: str) -> None:
        self.calls.append(("detach", provider_volume_id))
        self.attached.pop(provider_volume_id, None)
        self.remote_state[provider_volume_id] = None

    async def delete_volume(self, provider_volume_id: str) -> None:
        self.calls.append(("delete", provider_volume_id))
        self.gone.add(provider_volume_id)
        self.remote_state.pop(provider_volume_id, None)

    async def list_volumes(self) -> list[tuple[str, str | None]]:
        self.calls.append(("list",))
        return [(vid, sid) for vid, sid in self.remote_state.items() if vid not in self.gone]

    def drift(self, vid: str, *, detached: bool = False, vanished: bool = False) -> None:
        """Simulate out-of-band console changes for reconciliation tests."""
        if vanished:
            self.remote_state.pop(vid, None)
        elif detached:
            self.remote_state[vid] = None


def make_server() -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=USER,
        provider_key="hetzner",
        provider_account_id=ACCOUNT,
        state=ServerLifecycleState.RUNNING,
        provider_server_id="42",
    )


def make_service(*, max_volumes: int = 3) -> tuple[VolumeService, LocalVolumeRepo, RecordingAudit]:
    repo = LocalVolumeRepo()
    audit = RecordingAudit()
    service = VolumeService(
        repo=repo,  # type: ignore[arg-type]
        audit_repo=audit,  # type: ignore[arg-type]
        server_repo=FakeServerRepo(make_server()),  # type: ignore[arg-type]
        max_volumes_per_user=max_volumes,
    )
    return service, repo, audit


async def create(service: VolumeService, port: FakeVolumePort, user: UUID = USER) -> Volume:
    return await service.create(
        actor_user_id=user,
        owner_user_id=user,
        provider_account_id=ACCOUNT,
        provider_key="hetzner",
        provider_port=port,  # type: ignore[arg-type]
        name=f"data-{uuid4().hex[:6]}",
        size_gb=50,
        location_id="fsn1",
    )


class TestDomainAndPricing:
    def test_size_bounds_enforced(self) -> None:
        with pytest.raises(VolumeSizeError):
            Volume(
                user_id=USER,
                provider_account_id=ACCOUNT,
                provider_key="hetzner",
                provider_volume_id="v1",
                name="x",
                size_gb=5,
            )
        with pytest.raises(VolumeSizeError):
            Volume(
                user_id=USER,
                provider_account_id=ACCOUNT,
                provider_key="hetzner",
                provider_volume_id="v1",
                name="x",
                size_gb=20_000,
            )

    def test_rate_card_math(self) -> None:
        card = VolumeRateCard(currency="EUR", per_gb_month_minor=44)  # 0.44 EUR/GB
        assert volume_monthly_minor(card, 100) == 4400
        # 4400/720 = 6.11 -> ROUND_HALF_UP -> 6 minor/hour
        assert volume_hourly_quantum_minor(card, 100) == 6
        with pytest.raises(VolumePricingError):
            VolumeRateCard("EURO", 10)

    async def test_create_requires_self_service(self) -> None:
        service, _repo, audit = make_service()
        with pytest.raises(VolumeNotFoundError):
            await service.create(
                actor_user_id=OTHER,
                owner_user_id=USER,
                provider_account_id=ACCOUNT,
                provider_key="hetzner",
                provider_port=FakeVolumePort(),  # type: ignore[arg-type]
                name="x",
                size_gb=50,
                location_id="fsn1",
            )
        assert audit.events == []


class TestLifecycleReconciliation:
    async def test_attach_is_idempotent_same_server(self) -> None:
        service, _repo, _audit = make_service()
        port = FakeVolumePort()
        volume = await create(service, port)

        first = await service.attach(
            actor_user_id=USER,
            volume_id=volume.id,  # type: ignore[arg-type]
            server_id=SERVER_ID,
            provider_port=port,  # type: ignore[arg-type]
        )
        assert first.is_attached
        calls_after_first = len(port.calls)
        replay = await service.attach(
            actor_user_id=USER,
            volume_id=volume.id,  # type: ignore[arg-type]
            server_id=SERVER_ID,
            provider_port=port,  # type: ignore[arg-type]
        )
        assert replay.server_id == SERVER_ID
        assert len(port.calls) == calls_after_first  # NO second provider call

    async def test_detach_is_idempotent(self) -> None:
        service, _repo, _audit = make_service()
        port = FakeVolumePort()
        volume = await create(service, port)
        unattached = await service.detach(
            actor_user_id=USER,
            volume_id=volume.id,  # type: ignore[arg-type]
            provider_port=port,  # type: ignore[arg-type]
        )
        assert not unattached.is_attached
        assert not any(c[0] == "detach" for c in port.calls)  # no-op, no call

    async def test_delete_drops_local_row_and_is_provider_idempotent(self) -> None:
        service, repo, audit = make_service()
        port = FakeVolumePort()
        volume = await create(service, port)
        await service.delete(
            actor_user_id=USER,
            volume_id=volume.id,  # type: ignore[arg-type]
            provider_port=port,  # type: ignore[arg-type]
        )
        assert volume.id not in repo.rows
        assert ("delete", volume.provider_volume_id) in port.calls
        assert audit.events[-1].action == "volume.deleted"
        # deleting again reads as missing (row already gone)
        with pytest.raises(VolumeNotFoundError):
            await service.delete(
                actor_user_id=USER,
                volume_id=volume.id,  # type: ignore[arg-type]
                provider_port=port,  # type: ignore[arg-type]
            )

    async def test_quota_enforced(self) -> None:
        service, _repo, _audit = make_service(max_volumes=2)
        port = FakeVolumePort()
        await create(service, port)
        await create(service, port)
        with pytest.raises(VolumeLimitError):
            await create(service, port)


class TestDriftRepair:
    async def test_reconcile_purges_remote_vanished_volumes(self) -> None:
        service, repo, audit = make_service()
        port = FakeVolumePort()
        volume = await create(service, port)
        port.drift(volume.provider_volume_id, vanished=True)

        summary = await service.reconcile(port)  # type: ignore[arg-type]

        assert summary["purged"] == 1
        assert volume.id not in repo.rows
        event = audit.events[-1]
        assert event.action == "volume.reconciled"
        assert event.actor_type is ActorType.SYSTEM

    async def test_reconcile_clears_bindings_detached_remotely(self) -> None:
        service, repo, _audit = make_service()
        port = FakeVolumePort()
        volume = await create(service, port)
        await service.attach(
            actor_user_id=USER,
            volume_id=volume.id,  # type: ignore[arg-type]
            server_id=SERVER_ID,
            provider_port=port,  # type: ignore[arg-type]
        )
        # someone detached it at the provider console
        port.drift(volume.provider_volume_id, detached=True)

        summary = await service.reconcile(port)  # type: ignore[arg-type]

        assert summary["unbound"] == 1
        stored = await repo.get(volume.id)  # type: ignore[arg-type]
        assert stored is not None and not stored.is_attached

    async def test_reconcile_leaves_consistent_rows_alone(self) -> None:
        service, repo, audit = make_service()
        port = FakeVolumePort()
        bound = await create(service, port)
        await service.attach(
            actor_user_id=USER,
            volume_id=bound.id,  # type: ignore[arg-type]
            server_id=SERVER_ID,
            provider_port=port,  # type: ignore[arg-type]
        )
        free = await create(service, port)

        summary = await service.reconcile(port)  # type: ignore[arg-type]

        assert summary == {"purged": 0, "unbound": 0}
        assert all(e.action != "volume.reconciled" for e in audit.events)
        assert set(repo.rows) == {bound.id, free.id}


class TestCapabilityProbe:
    async def test_probe_requires_full_port(self) -> None:
        class HalfPort:
            async def create_volume(
                self, name: str, size_gb: int, location_id: str
            ) -> tuple[str, str]:
                return "1", name

        class Provider:
            def __init__(self, port: Any) -> None:
                self.volumes = port

        assert volume_port_of(Provider(FakeVolumePort())) is not None
        assert volume_port_of(Provider(HalfPort())) is None

        class NoVolumes:
            pass

        assert volume_port_of(NoVolumes()) is None


class TestHetznerAdapterMapping:
    async def test_hetzner_attach_maps_endpoint(self) -> None:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        calls: list[tuple[str, str]] = []

        class Transport:
            async def request(self, method: str, path: str, **kwargs: Any):
                calls.append((method, path))
                assert method == "POST"
                assert path == "/volumes/vol-9/actions/attach"

                from tests.unit.test_ssh_keys import _Resp

                return _Resp(201, {"action": {"status": "running"}})

        provider = HetznerCloudProvider(token="t")
        provider._client = Transport()  # type: ignore[assignment]
        await provider.volumes.attach_volume("vol-9", "42")
        assert calls == [("POST", "/volumes/vol-9/actions/attach")]
