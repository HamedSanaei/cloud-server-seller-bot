"""Tests for floating/primary IP resources (M13-008).

Acceptance: **independent resource billing supported.**

- IPs are first-class owned resources with a quota; foreign IPs read as
  missing.
- Cost accrues per IP REGARDLESS of binding (the core of "independent
  resource billing"): rate-card math is exact and the service exposes
  the per-quantum accrual over all of a user's IPs.
- Provider lifecycle goes through the optional ``floating_ips`` port
  (probe); release is refused while bound, idempotent at the provider.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.networking.ip_service import IpService, floating_port_of
from cloud_platform.modules.networking.ips import (
    IpAddress,
    IpBoundError,
    IpLimitError,
    IpNotFoundError,
)
from cloud_platform.modules.pricing.floating import (
    FloatingIpPricingError,
    FloatingIpRateCard,
    floating_hourly_quantum_minor,
    floating_monthly_minor,
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


class LocalIpRepo:
    def __init__(self) -> None:
        self.rows: dict[UUID, IpAddress] = {}

    async def add(self, record: IpAddress) -> IpAddress:
        stored = replace(record, id=record.id or uuid4(), created_at=record.created_at or NOW)
        self.rows[stored.id] = stored  # type: ignore[index]
        return stored

    async def get(self, ip_id: UUID) -> IpAddress | None:
        return self.rows.get(ip_id)

    async def list_for_user(self, user_id: UUID) -> list[IpAddress]:
        return [r for r in self.rows.values() if r.user_id == user_id]

    async def save(self, record: IpAddress) -> IpAddress:
        assert record.id is not None
        self.rows[record.id] = record
        return record

    async def delete(self, ip_id: UUID) -> None:
        self.rows.pop(ip_id, None)


class FakeServerRepo:
    """Resolves ONLY its own server id - anything else is missing."""

    def __init__(self, server: CloudServer | None) -> None:
        self.server = server

    async def get(self, server_id: UUID) -> CloudServer | None:
        if self.server is not None and self.server.id == server_id:
            return self.server
        return None


class FakeFloatingPort:
    """The provider-side lifecycle; records every call."""

    def __init__(self) -> None:
        self.next_id = 100
        self.assigned: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []

    async def create_floating_ip(self, location_id: str) -> tuple[str, str]:
        self.calls.append(("create", location_id))
        self.next_id += 1
        return str(self.next_id), f"203.0.113.{self.next_id % 250}"

    async def assign_floating_ip(self, provider_ip_id: str, provider_server_id: str) -> None:
        self.calls.append(("assign", provider_ip_id, provider_server_id))
        self.assigned[provider_ip_id] = provider_server_id

    async def unassign_floating_ip(self, provider_ip_id: str) -> None:
        self.calls.append(("unassign", provider_ip_id))
        self.assigned.pop(provider_ip_id, None)

    async def delete_floating_ip(self, provider_ip_id: str) -> None:
        self.calls.append(("delete", provider_ip_id))


class MissingFloatingProvider:
    """No ``floating_ips`` attribute at all."""

    key = "plain"


def make_server() -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=USER,
        provider_key="hetzner",
        provider_account_id=ACCOUNT,
        state=ServerLifecycleState.RUNNING,
        provider_server_id="42",
    )


def make_service(
    *, max_ips: int = 5, server: CloudServer | None = None
) -> tuple[IpService, LocalIpRepo, RecordingAudit]:
    repo = LocalIpRepo()
    audit = RecordingAudit()
    service = IpService(
        repo=repo,  # type: ignore[arg-type]
        audit_repo=audit,  # type: ignore[arg-type]
        server_repo=FakeServerRepo(server or make_server()),  # type: ignore[arg-type]
        max_ips_per_user=max_ips,
    )
    return service, repo, audit


def allocate(
    service: IpService, port: FakeFloatingPort, user: UUID = USER, *, owner: UUID | None = None
):
    return service.allocate(
        actor_user_id=user,
        owner_user_id=owner or user,
        provider_account_id=ACCOUNT,
        provider_key="hetzner",
        provider_port=port,  # type: ignore[arg-type]
        location_id="fsn1",
    )


CARD = FloatingIpRateCard(currency="EUR", per_ip_month_minor=1080)  # 10.80 EUR/IP


class TestRateCard:
    def test_monthly_scales_by_count(self) -> None:
        assert floating_monthly_minor(CARD, 1) == 1080
        assert floating_monthly_minor(CARD, 3) == 3240

    def test_hourly_quantum_matches_hand_computation(self) -> None:
        # 1080 / 720 = 1.5 -> ROUND_HALF_UP -> 2 minor/hour per IP
        assert floating_hourly_quantum_minor(CARD, 1) == 2
        assert floating_hourly_quantum_minor(CARD, 4) == 6

    def test_card_validation(self) -> None:
        with pytest.raises(FloatingIpPricingError):
            FloatingIpRateCard("EURO", 100)
        with pytest.raises(FloatingIpPricingError):
            FloatingIpRateCard("EUR", -1)


class TestLifecycle:
    async def test_allocate_creates_independent_resource(self) -> None:
        service, _repo, audit = make_service()
        port = FakeFloatingPort()
        record = await allocate(service, port)
        assert record.kind.value == "floating"
        assert record.server_id is None  # bound to nothing - still exists
        assert record.created_at is not None
        event = audit.events[-1]
        assert event.action == "ip.allocated"
        assert event.actor_type is ActorType.USER

    async def test_foreign_actor_reads_as_missing(self) -> None:
        service, _repo, audit = make_service()
        with pytest.raises(IpNotFoundError):
            await allocate(service, FakeFloatingPort(), user=OTHER, owner=USER)
        assert audit.events == []

    async def test_quota_enforced(self) -> None:
        service, _repo, _audit = make_service(max_ips=2)
        port = FakeFloatingPort()
        await allocate(service, port)
        await allocate(service, port)
        with pytest.raises(IpLimitError):
            await allocate(service, port)

    async def test_assign_requires_owning_the_server(self) -> None:
        service, _repo, audit = make_service()
        port = FakeFloatingPort()
        record = await allocate(service, port)
        stranger_server = uuid4()

        with pytest.raises(IpNotFoundError):
            await service.assign(
                actor_user_id=USER,
                ip_id=record.id,  # type: ignore[arg-type]
                server_id=stranger_server,  # someone else's server
                provider_port=port,  # type: ignore[arg-type]
            )
        assert audit.events[-1].action != "ip.assigned"

    async def test_bind_unbind_release_roundtrip(self) -> None:
        service, repo, _audit = make_service()
        port = FakeFloatingPort()
        record = await allocate(service, port)

        bound = await service.assign(
            actor_user_id=USER,
            ip_id=record.id,  # type: ignore[arg-type]
            server_id=SERVER_ID,
            provider_port=port,  # type: ignore[arg-type]
        )
        assert bound.server_id == SERVER_ID
        assert port.assigned[record.provider_ip_id] == "42"

        unbound = await service.unbind(
            actor_user_id=USER,
            ip_id=record.id,  # type: ignore[arg-type]
            provider_port=port,  # type: ignore[arg-type]
        )
        assert unbound.server_id is None
        assert record.provider_ip_id not in port.assigned

        released = record.id
        await service.release(
            actor_user_id=USER,
            ip_id=released,  # type: ignore[arg-type]
            provider_port=port,  # type: ignore[arg-type]
        )
        assert released not in repo.rows
        assert ("delete", record.provider_ip_id) in port.calls

    async def test_cannot_release_while_bound(self) -> None:
        service, _repo, _audit = make_service()
        port = FakeFloatingPort()
        record = await allocate(service, port)
        await service.assign(
            actor_user_id=USER,
            ip_id=record.id,  # type: ignore[arg-type]
            server_id=SERVER_ID,
            provider_port=port,  # type: ignore[arg-type]
        )
        with pytest.raises(IpBoundError):
            await service.release(
                actor_user_id=USER,
                ip_id=record.id,  # type: ignore[arg-type]
                provider_port=port,  # type: ignore[arg-type]
            )

    async def test_probe_requires_full_port(self) -> None:
        class HalfPort:
            async def create_floating_ip(self, location_id: str) -> tuple[str, str]:
                return "1", "203.0.113.1"

        class Provider:
            def __init__(self, port: Any) -> None:
                self.floating_ips = port

        assert floating_port_of(Provider(FakeFloatingPort())) is not None
        assert floating_port_of(Provider(HalfPort())) is None
        assert floating_port_of(MissingFloatingProvider()) is None


class TestIndependentBilling:
    async def test_accrual_counts_ips_not_bindings(self) -> None:
        """THE acceptance: cost follows EXISTENCE, not server attachment."""
        service, _repo, _audit = make_service()
        port = FakeFloatingPort()
        first = await allocate(service, port)
        second = await allocate(service, port)
        _ = second

        unbound_cost = await service.accrual_quantum_minor(USER, CARD)

        await service.assign(
            actor_user_id=USER,
            ip_id=first.id,  # type: ignore[arg-type]
            server_id=SERVER_ID,
            provider_port=port,  # type: ignore[arg-type]
        )
        bound_cost = await service.accrual_quantum_minor(USER, CARD)

        # exact aggregate: 2 x 1080 / 720 = 3 minor per hour - binding changes nothing
        assert unbound_cost == bound_cost == 3
        # releasing one drops the bill immediately (1 IP -> 1080/720 = 1.5 -> 2 half-up)
        await service.unbind(
            actor_user_id=USER,
            ip_id=first.id,
            provider_port=port,  # type: ignore[arg-type]
        )
        await service.release(
            actor_user_id=USER,
            ip_id=first.id,  # type: ignore[arg-type]
            provider_port=port,  # type: ignore[arg-type]
        )
        assert await service.accrual_quantum_minor(USER, CARD) == 2

    async def test_billing_is_per_user_scoped(self) -> None:
        service, _repo, _audit = make_service()
        await allocate(service, FakeFloatingPort())
        assert await service.accrual_quantum_minor(OTHER, CARD) == 0
