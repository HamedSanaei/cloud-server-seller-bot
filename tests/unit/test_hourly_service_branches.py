"""Reconcile / claim-race / quarantine branches of HourlyCloudService.

Self-contained minimal fakes (copied shape, no cross-test imports):
servers keyed by id, snapshots pinned per server, operations keyed by
operation key, and a scripted hourly adapter. Servers are built through
the real ``create_instance`` path so snapshots carry genuine fingerprints.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.fx.domain import FxReferenceQuote
from cloud_platform.modules.fx.service import ReferenceRateResolution
from cloud_platform.modules.hourly import service as hourly_service
from cloud_platform.modules.hourly.service import (
    HourlyCloudService,
    HourlyError,
    hourly_reference_name,
)
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    PricingPolicy,
    SellableOffer,
)
from cloud_platform.modules.offers.pricing import CatalogOfferPricer
from cloud_platform.modules.operations.domain import Operation, OperationStatus
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.providers.errors import ProviderOutcomeUnknown
from cloud_platform.providers.leaseweb.cloud import (
    CloudInstanceType,
    CloudRootDisk,
    HourlyCheckoutFacts,
)

PROVIDER = "leaseweb"

USER = User(
    id=uuid4(),
    username="cust",
    email="cust@example.test",
    status=UserStatus.ACTIVE,
    role=Role.USER,
    telegram_user_id=12345,
)


class _Rates:
    """Deterministic EUR->USD reference rates (no network, no float)."""

    def __init__(self, rate: Decimal) -> None:
        self.rate = rate

    async def get_rate(
        self, base: str, quote: str, *, allow_catalog_stale: bool = False
    ) -> ReferenceRateResolution:
        now = datetime.now(UTC)
        return ReferenceRateResolution(
            FxReferenceQuote(
                base_currency=base,
                quote_currency=quote,
                rate=self.rate,
                source="frankfurter",
                source_market=f"{base}/{quote}",
                provider_date=now.date(),
                observed_at=now,
                expires_at=now + timedelta(hours=1),
            ),
            stale=False,
        )


def _base_offer() -> SellableOffer:
    return SellableOffer(
        id=uuid4(),
        provider_key=PROVIDER,
        product_id="lsw.mini",
        location_id="eu-west-3",
        name="Mini",
        vcpu=1,
        ram_gb=1,
        disk_gb=25,
        traffic=None,
        provider_cost_minor=2,
        provider_cost_currency="EUR",
        selling_price_minor=0,
        selling_currency="EUR",
        billing_parameters={"provider_hourly_rate": "0.02"},
        billing_model=BILLING_MODEL_HOURLY,
        # The catalog sync writes the provider's storage facts onto the offer;
        # they are what the launch root disk is pinned from.
        technical_metadata={"storage_type": "CENTRAL", "storage_types": ["CENTRAL"]},
        provider_available=True,
        enabled=True,
        created_at=datetime.now(UTC),
    )


async def _usd_offer() -> SellableOffer:
    base = _base_offer()
    priced = await CatalogOfferPricer(_Rates(Decimal("1.17")), "USD").price_auto(
        base, PricingPolicy(mode="markup", markup_percent=25)
    )
    return replace(
        base,
        selling_price_minor=priced.selling_price_minor,
        selling_currency=priced.selling_currency,
        pricing_metadata=dict(priced.pricing_metadata),
    )


class FakeOffersRepo:
    def __init__(self, offers: list[SellableOffer]) -> None:
        self._offers = list(offers)

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return next((o for o in self._offers if o.id == offer_id), None)

    async def list_all(self) -> list[SellableOffer]:
        return list(self._offers)


class FakeServerRepo:
    def __init__(self) -> None:
        self.servers: dict[UUID, Any] = {}
        self.by_key: dict[str, Any] = {}

    async def get(self, server_id: UUID) -> Any:
        return self.servers.get(server_id)

    async def get_by_idempotency_key(self, key: str) -> Any:
        return self.by_key.get(key)

    async def create(self, server: Any, intent: Any) -> Any:
        self.servers[server.id] = server
        self.by_key[intent.idempotency_key] = server
        return server

    async def save(self, server: Any) -> Any:
        self.servers[server.id] = server
        return server

    async def list_requested(self) -> list[Any]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.REQUESTED]

    async def list_provisioning(self) -> list[Any]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.PROVISIONING]


class FakeAccountRepo:
    async def get_or_create_active(self, user_id: UUID, provider_key: str) -> Any:
        return SimpleNamespace(id=uuid4())


class FakeWalletRepo:
    async def get(self, user_id: UUID) -> Any:
        return SimpleNamespace(id=uuid4(), balance=50_000, currency="USD")


class FakeSnapshots:
    def __init__(self) -> None:
        self.created: list[Any] = []

    async def create_snapshot(self, *, server_id: UUID, price: Any, actor: Any, reason: str) -> Any:
        self.created.append((server_id, price))
        return price

    async def require_snapshot(self, server_id: UUID) -> Any:
        for sid, price in self.created:
            if sid == server_id:
                return price
        raise LookupError("no snapshot")

    async def get_snapshot(self, server_id: UUID) -> Any:
        for sid, price in self.created:
            if sid == server_id:
                return price
        return None


class FakeOpsRepo:
    def __init__(self) -> None:
        self.ops: dict[str, Any] = {}

    async def get_or_create(self, **kwargs: Any) -> Any:
        key = kwargs["operation_key"]
        if key in self.ops:
            return self.ops[key]
        op = Operation(
            id=uuid4(),
            operation_key=key,
            operation_type=kwargs["operation_type"],
            resource_type=kwargs["resource_type"],
            resource_id=kwargs["resource_id"],
            provider_key=kwargs["provider_key"],
            status=OperationStatus.PENDING,
        )
        self.ops[key] = op
        return op

    async def get_by_key(self, key: str) -> Any:
        return self.ops.get(key)

    async def claim(self, operation_id: UUID) -> Any:
        for op in self.ops.values():
            if op.id == operation_id and op.status is OperationStatus.PENDING:
                op.mark_in_flight()
                return op
        return None

    async def save(self, operation: Any) -> Any:
        self.ops[operation.operation_key] = operation
        return operation


class FakeAuditRepo:
    async def append(self, event: Any) -> Any:
        return event


class FakeCloud:
    """Scripted hourly adapter echoing the stable creation identity."""

    def __init__(self) -> None:
        self.posts = 0
        self.create_error: Exception | None = None
        self.find_result: Any = None

    async def validate_hourly_offer_for_checkout(self, **kwargs: Any) -> Any:
        """Return the pinned launch facts (the real adapter derives them)."""
        return HourlyCheckoutFacts(
            instance_type=CloudInstanceType(
                id="lsw.mini",
                name="Mini",
                region="eu-west-3",
                family_key="general",
                family_name="General Purpose",
                vcpu=1,
                ram_gb=1,
                disk_gb=25,
                traffic=None,
                hourly_cost_minor=2,
                currency="EUR",
                storage_type="CENTRAL",
                storage_types=("CENTRAL",),
                hourly_rate_exact="0.02",
            ),
            root_disk=CloudRootDisk(size_gb=25, storage_type="CENTRAL"),
        )

    async def list_images(self, region: str) -> list[Any]:
        return [
            SimpleNamespace(
                id="UBUNTU",
                label="Ubuntu 24.04",
                os_family="linux",
                min_disk_size_gb=5,
                storage_types=("CENTRAL",),
            )
        ]

    async def create_instance(self, **kwargs: Any) -> Any:
        self.posts += 1
        if self.create_error is not None:
            raise self.create_error
        return SimpleNamespace(
            id="i-1",
            state="RUNNING",
            region=kwargs["region"],
            reference=kwargs["reference"],
            instance_type=kwargs["instance_type"],
            image_id=kwargs["image_id"],
        )

    async def find_by_reference(self, region: str, reference: str) -> Any:
        return self.find_result


def _service(
    offers: FakeOffersRepo,
    cloud: FakeCloud,
    *,
    servers: FakeServerRepo | None = None,
    snapshots: FakeSnapshots | None = None,
    ops: FakeOpsRepo | None = None,
) -> tuple[HourlyCloudService, FakeServerRepo, FakeSnapshots, FakeOpsRepo]:
    servers = servers or FakeServerRepo()
    snapshots = snapshots or FakeSnapshots()
    ops = ops or FakeOpsRepo()
    service = HourlyCloudService(
        server_repo=servers,
        offers_repo=offers,  # type: ignore[arg-type]
        account_repo=FakeAccountRepo(),
        wallet_repo=FakeWalletRepo(),  # type: ignore[arg-type]
        snapshot_service=snapshots,  # type: ignore[arg-type]
        operation_repo=ops,  # type: ignore[arg-type]
        audit_repo=FakeAuditRepo(),  # type: ignore[arg-type]
        cloud_providers={PROVIDER: cloud},
    )
    return service, servers, snapshots, ops


def _match(server_id: UUID, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": "i-9",
        "state": "RUNNING",
        "region": "eu-west-3",
        "reference": hourly_reference_name(server_id),
        "instance_type": "lsw.mini",
        "image_id": "UBUNTU",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _requested(service: HourlyCloudService, offers: FakeOffersRepo, key: str) -> CloudServer:
    offer = (await offers.list_all())[0]
    result = await service.create_instance(
        user=USER,
        offer_id=offer.id,
        image_id="UBUNTU",
        image_label="Ubuntu",
        idempotency_key=key,
    )
    return result.server


async def _ambiguous(
    service: HourlyCloudService, offers: FakeOffersRepo, cloud: FakeCloud, key: str
) -> CloudServer:
    server = await _requested(service, offers, key)
    cloud.create_error = ProviderOutcomeUnknown("connection lost after POST")
    assert await service.process_server(server.id) == "outcome-unknown"
    cloud.create_error = None
    return server


class TestReconcileBranches:
    async def test_zero_matches_stays_unknown(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, _, _, ops = _service(offers, cloud)
        server = await _ambiguous(service, offers, cloud, "zero-match")
        cloud.find_result = None

        assert await service.reconcile_server(server.id) == "still-unknown"
        assert ops.ops[f"server-create:{server.id}"].status is OperationStatus.OUTCOME_UNKNOWN

    async def test_exact_match_attaches_and_completes(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, servers, _, ops = _service(offers, cloud)
        server = await _ambiguous(service, offers, cloud, "attach")
        cloud.find_result = _match(server.id)

        assert await service.reconcile_server(server.id) == "attached"
        stored = servers.servers[server.id]
        assert stored.provider_server_id == "i-9"
        assert stored.state is ServerLifecycleState.PROVISIONING
        op = ops.ops[f"server-create:{server.id}"]
        assert op.status is OperationStatus.COMPLETED
        assert op.provider_response["provider_server_id"] == "i-9"

    async def test_identity_mismatch_stays_unknown_without_transition(self) -> None:
        # The operation is ALREADY outcome-unknown: re-marking it would raise
        # InvalidOperationTransition, so reconcile persists and stays unknown.
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, _, _, ops = _service(offers, cloud)
        server = await _ambiguous(service, offers, cloud, "mismatch")
        cloud.find_result = _match(server.id, instance_type="lsw.other")

        assert await service.reconcile_server(server.id) == "still-unknown"
        assert ops.ops[f"server-create:{server.id}"].status is OperationStatus.OUTCOME_UNKNOWN

    async def test_rejected_provider_state_stays_unknown(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, _, _, ops = _service(offers, cloud)
        server = await _ambiguous(service, offers, cloud, "rejected")
        cloud.find_result = _match(server.id, state="failed")

        assert await service.reconcile_server(server.id) == "still-unknown"
        assert ops.ops[f"server-create:{server.id}"].status is OperationStatus.OUTCOME_UNKNOWN

    async def test_already_attached_server_completes_operation(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, servers, _, ops = _service(offers, cloud)
        server = await _ambiguous(service, offers, cloud, "pre-attached")
        servers.servers[server.id].provider_server_id = "i-7"

        assert await service.reconcile_server(server.id) == "recovered"
        op = ops.ops[f"server-create:{server.id}"]
        assert op.status is OperationStatus.COMPLETED

    async def test_missing_snapshot_skips(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, _, snapshots, _ = _service(offers, cloud)
        server = await _ambiguous(service, offers, cloud, "no-snapshot")
        snapshots.created.clear()

        assert await service.reconcile_server(server.id) == "skipped"

    async def test_non_unknown_operation_skips(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, _, _, ops = _service(offers, cloud)
        server = await _requested(service, offers, "pending-op")

        assert await service.reconcile_server(server.id) == "skipped"
        assert ops.ops[f"server-create:{server.id}"].status is OperationStatus.PENDING

    async def test_no_adapter_skips(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, _, _, _ = _service(offers, cloud)
        server = await _ambiguous(service, offers, cloud, "no-adapter")
        service._cloud.clear()

        assert await service.reconcile_server(server.id) == "skipped"


class TestClaimRaceBranches:
    async def test_inflight_operation_recovers_via_read_only_find(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, servers, _, ops = _service(offers, cloud)
        server = await _requested(service, offers, "claim-race")
        ops.ops[f"server-create:{server.id}"].mark_in_flight()
        cloud.find_result = _match(server.id)

        assert await service.process_server(server.id) == "recovered"
        assert cloud.posts == 0  # never re-POSTs an in-flight operation
        assert servers.servers[server.id].state is ServerLifecycleState.PROVISIONING
        assert ops.ops[f"server-create:{server.id}"].status is OperationStatus.COMPLETED

    async def test_stale_inflight_lease_becomes_outcome_unknown(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, _, _, ops = _service(offers, cloud)
        server = await _requested(service, offers, "stale-lease")
        op = ops.ops[f"server-create:{server.id}"]
        op.mark_in_flight()
        op.updated_at = datetime.now(UTC) - timedelta(hours=1)
        cloud.find_result = None

        assert await service.process_server(server.id) == "outcome-unknown"
        assert op.status is OperationStatus.OUTCOME_UNKNOWN
        assert cloud.posts == 0


class TestQuarantineBranches:
    async def test_malformed_intent_with_inflight_op_is_reviewed(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, servers, snapshots, ops = _service(offers, cloud)
        server = await _requested(service, offers, "quarantine")
        snapshots.created.clear()  # pinned contract is unrecoverable
        ops.ops[f"server-create:{server.id}"].mark_in_flight()

        assert await service.process_server(server.id) == "review"
        assert servers.servers[server.id].state is ServerLifecycleState.ERROR
        assert cloud.posts == 0

    async def test_queue_selects_only_unattached_hourly_servers(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        servers = FakeServerRepo()
        service, _, _, _ = _service(offers, cloud, servers=servers)
        user_id = USER.id
        assert user_id is not None
        hourly = CloudServer(
            id=uuid4(),
            user_id=user_id,
            provider_key=PROVIDER,
            provider_account_id=uuid4(),
            state=ServerLifecycleState.REQUESTED,
        )
        prepaid = CloudServer(
            id=uuid4(),
            user_id=user_id,
            provider_key=PROVIDER,
            provider_account_id=uuid4(),
            state=ServerLifecycleState.REQUESTED,
            billing_model="prepaid_monthly_fixed",
        )
        attached = CloudServer(
            id=uuid4(),
            user_id=user_id,
            provider_key=PROVIDER,
            provider_account_id=uuid4(),
            state=ServerLifecycleState.PROVISIONING,
            provider_server_id="i-1",
        )
        free = CloudServer(
            id=uuid4(),
            user_id=user_id,
            provider_key=PROVIDER,
            provider_account_id=uuid4(),
            state=ServerLifecycleState.PROVISIONING,
        )
        for server in (hourly, prepaid, attached, free):
            servers.servers[server.id] = server

        queued = await service.servers_for_reconcile()

        assert {s.id for s in queued} == {hourly.id, free.id}


_UNSET: Any = object()


class _FastLoop:
    """Loop clock that jumps straight past the replay settlement deadline."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        self.now += 10.0
        return self.now


class TestReplayAndRepairContracts:
    """Same-key replay: the frozen contract wins, and never a second POST."""

    @staticmethod
    async def _committed(key: str) -> SimpleNamespace:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeCloud()
        service, servers, snapshots, ops = _service(offers, cloud)
        server = await _requested(service, offers, key)
        offer = (await offers.list_all())[0]
        return SimpleNamespace(
            service=service,
            offers=offers,
            cloud=cloud,
            servers=servers,
            snapshots=snapshots,
            ops=ops,
            offer=offer,
            server=server,
        )

    @staticmethod
    def _frozen(blob: SimpleNamespace) -> Any:
        return next(price for sid, price in blob.snapshots.created if sid == blob.server.id)

    async def _replay(
        self,
        blob: SimpleNamespace,
        *,
        user: User = USER,
        offer_id: UUID | None = None,
        image_id: str = "UBUNTU",
        expected_minor: Any = _UNSET,
        expected_currency: Any = _UNSET,
    ) -> Any:
        frozen = self._frozen(blob)
        return await blob.service._replay_hourly_server(
            blob.server,
            user,
            offer_id=blob.offer.id if offer_id is None else offer_id,
            image_id=image_id,
            expected_selling_price_minor=(
                frozen.selling_minor if expected_minor is _UNSET else expected_minor
            ),
            expected_selling_currency=(
                frozen.selling_currency if expected_currency is _UNSET else expected_currency
            ),
        )

    async def test_same_key_replay_reuses_the_frozen_contract(self) -> None:
        blob = await self._committed("replay-happy")
        frozen = self._frozen(blob)

        result = await self._replay(blob)

        assert result.replayed is True
        assert result.server is blob.server
        assert frozen.selling_currency == "USD"
        assert blob.cloud.posts == 0

    async def test_replay_for_another_user_is_refused(self) -> None:
        blob = await self._committed("replay-other-user")
        other = User(
            id=uuid4(),
            username="intruder",
            email="intruder@example.test",
            status=UserStatus.ACTIVE,
            role=Role.USER,
            telegram_user_id=999,
        )

        with pytest.raises(HourlyError, match="another user"):
            await self._replay(blob, user=other)

    async def test_replay_after_a_terminal_state_is_refused(self) -> None:
        blob = await self._committed("replay-terminal")
        blob.server.state = ServerLifecycleState.ERROR

        with pytest.raises(HourlyError, match="a new order is required"):
            await self._replay(blob)

    async def test_replay_for_a_different_image_is_refused(self) -> None:
        blob = await self._committed("replay-image")

        with pytest.raises(HourlyError, match="different image"):
            await self._replay(blob, image_id="DEBIAN")

    async def test_replay_for_a_different_offer_is_refused(self) -> None:
        blob = await self._committed("replay-offer")

        with pytest.raises(HourlyError, match="different offer"):
            await self._replay(blob, offer_id=uuid4())

    async def test_replay_for_a_different_price_or_currency_is_refused(self) -> None:
        blob = await self._committed("replay-price")
        frozen = self._frozen(blob)

        with pytest.raises(HourlyError, match="different price"):
            await self._replay(blob, expected_minor=frozen.selling_minor + 1)

        with pytest.raises(HourlyError, match="different price"):
            await self._replay(blob, expected_currency="EUR")

    async def test_replay_refuses_an_operation_that_already_failed(self) -> None:
        blob = await self._committed("replay-failed-op")
        operation = blob.ops.ops[f"server-create:{blob.server.id}"]
        operation.status = OperationStatus.FAILED

        with pytest.raises(HourlyError, match="a new order is required"):
            await self._replay(blob)

    async def test_missing_snapshot_is_repaired_without_a_provider_call(self) -> None:
        blob = await self._committed("replay-repair")
        frozen = self._frozen(blob)
        blob.snapshots.created.clear()

        result = await blob.service._replay_hourly_server(
            blob.server,
            USER,
            offer_id=blob.offer.id,
            image_id="UBUNTU",
            expected_selling_price_minor=frozen.selling_minor,
            expected_selling_currency=frozen.selling_currency,
        )

        assert result.replayed is True
        assert blob.cloud.posts == 0
        repaired = self._frozen(blob)
        assert repaired.selling_minor == frozen.selling_minor
        assert repaired.selling_currency == frozen.selling_currency
        # The exact provider rate is preserved for margin/audit.
        assert repaired.offer.provider_rate_exact == frozen.offer.provider_rate_exact == "0.02"

    async def test_catalog_repricing_never_changes_an_accepted_contract(self) -> None:
        blob = await self._committed("replay-immutable")
        frozen = self._frozen(blob)
        repriced = replace(
            blob.offer,
            selling_price_minor=frozen.selling_minor + 500,
            pricing_metadata={**blob.offer.pricing_metadata, "fx_rate": "9.99"},
        )
        blob.offers._offers[0] = repriced

        result = await self._replay(blob)

        assert result.replayed is True
        assert blob.cloud.posts == 0
        accepted = self._frozen(blob)
        assert accepted.selling_minor == frozen.selling_minor
        assert accepted.pricing_metadata["fx_rate"] != "9.99"

    async def test_incomplete_intent_is_left_for_durable_reconciliation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blob = await self._committed("replay-incomplete")
        frozen = self._frozen(blob)
        blob.snapshots.created.clear()
        blob.offers._offers.clear()
        loop = _FastLoop()
        monkeypatch.setattr(
            hourly_service,
            "asyncio",
            SimpleNamespace(get_running_loop=lambda: loop, sleep=asyncio.sleep),
        )

        with pytest.raises(HourlyError, match="durable reconciliation is required"):
            await blob.service._replay_hourly_server(
                blob.server,
                USER,
                offer_id=blob.offer.id,
                image_id="UBUNTU",
                expected_selling_price_minor=frozen.selling_minor,
                expected_selling_currency=frozen.selling_currency,
            )

        # Wall-clock expiry is not corruption: the intent stays REQUESTED.
        assert blob.servers.servers[blob.server.id].state is ServerLifecycleState.REQUESTED
        assert blob.ops.ops[f"server-create:{blob.server.id}"].status is not OperationStatus.FAILED
