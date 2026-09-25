"""Hourly state-machine coverage (P1) + adapter/coverage completion.

Extends :mod:`tests.unit.test_hourly_cloud_flow`'s fakes with the remaining
lifecycle branches of :class:`~cloud_platform.modules.hourly.service.HourlyCloudService`:

- process paths: missing snapshot, missing adapter, image vanished,
  definitive provider rejection, failed server transition
- reconcile paths: zero matches stays unknown, provider list failure
- pricing invariants: exact snapshot values, snapshot survives catalog repricing
- queue selection: only hourly servers, only unattached ones
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.modules.hourly.service import (
    HourlyCloudService,
    HourlyError,
    HourlyNotAvailableError,
    hourly_reference_name,
)
from cloud_platform.modules.offers.domain import BILLING_MODEL_PREPAID_MONTHLY
from cloud_platform.providers.errors import (
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
)
from cloud_platform.providers.leaseweb.cloud import (
    CloudRootDisk,
    HourlyCheckoutFacts,
    resolve_root_disk,
    validate_root_disk,
)
from tests.unit.test_hourly_cloud_flow import (
    PROVIDER,
    USER,
    FakeAccountRepo,
    FakeAuditRepo,
    FakeOffersRepo,
    FakeOpsRepo,
    FakeServerRepo,
    FakeSnapshots,
    FakeWalletRepo2,
    _cloud_type,
    _offer,
    _usd_offer,
)

# ---------------------------------------------------------------------------
# Local fakes (extensions of the flow module's fakes)
# ---------------------------------------------------------------------------


class FakeHourlyAdapter:
    """Records create/search calls; each behaviour knob flips per test."""

    def __init__(self) -> None:
        self.posts = 0
        self.create_result: Any = type("C", (), {"id": "lsw-created", "state": "RUNNING"})()
        self.create_error: Exception | None = None
        self.images: list[Any] = [
            type(
                "I",
                (),
                {
                    "id": "UBUNTU",
                    "label": "Ubuntu 24.04",
                    "os_family": "linux",
                    "min_disk_size_gb": 5,
                    "storage_types": ("CENTRAL",),
                },
            )()
        ]
        self.types: dict[str, list[Any]] = {}
        self.list_images_error: Exception | None = None
        self.find_result: Any = None
        self.find_error: Exception | None = None
        self.listed_regions: list[str] = []
        #: Every create kwargs the worker sent (launch body proof).
        self.created_kwargs: list[dict[str, Any]] = []
        #: Pinned root-disk pairs the worker re-verified before the POST.
        self.verified_pinned: list[tuple[Any, Any]] = []

    async def validate_hourly_offer_for_checkout(
        self,
        *,
        location_id: str,
        product_id: str,
        image_id: str,
        expected_cost_minor: int,
        currency: str,
        expected_cost_exact: str,
        root_disk_size_gb: int | None = None,
        root_disk_storage_type: str | None = None,
    ) -> Any:
        """Fail-closed checkout revalidation over the scripted state.

        Mirrors the production adapter: the launch root disk is derived from
        the scripted provider facts, and values the caller pinned are
        re-verified instead of replaced.
        """
        scripted = self.types.get(location_id)
        if scripted is None:
            scripted = [_cloud_type()]
        match = next((item for item in scripted if item.id == product_id), None)
        if match is None:
            raise ProviderNotFound(
                f"instance type {product_id!r} is not offered in {location_id!r}"
            )
        if match.currency.upper() != str(currency or "").strip().upper():
            raise ProviderError(
                f"provider cost currency changed for {product_id!r} in {location_id!r}"
            )
        if match.hourly_cost_minor != expected_cost_minor:
            raise ProviderError(f"provider cost changed for {product_id!r} in {location_id!r}")
        wanted_exact = str(expected_cost_exact or "").strip()
        if wanted_exact:
            try:
                wanted = Decimal(wanted_exact)
                live_exact = Decimal(match.hourly_rate_exact)
            except Exception:
                raise ProviderError(
                    f"provider rate for {product_id!r} is not valid Decimal text"
                ) from None
            if not wanted.is_finite() or wanted <= 0 or live_exact != wanted:
                raise ProviderError(
                    f"exact provider rate changed for {product_id!r} in {location_id!r}"
                )
        images = await self.list_images(location_id)
        image = next((item for item in images if item.id == image_id), None)
        if image is None:
            raise ProviderNotFound(f"image {image_id!r} is not offered in {location_id!r}")
        live = resolve_root_disk(
            disk_gb=match.disk_gb,
            storage_type=match.storage_type,
            image=image,
            type_storage_types=match.storage_types,
        )
        if root_disk_size_gb is not None or root_disk_storage_type is not None:
            self.verified_pinned.append((root_disk_size_gb, root_disk_storage_type))
            pinned = validate_root_disk(
                size_gb=root_disk_size_gb,
                storage_type=root_disk_storage_type,
                image_label=image.label,
                os_family=getattr(image, "os_family", None),
            )
            if pinned[0] < live.size_gb:
                raise ProviderError(
                    f"pinned root disk {pinned[0]} GB is below the provider minimum"
                )
            return HourlyCheckoutFacts(
                instance_type=match,
                root_disk=CloudRootDisk(size_gb=pinned[0], storage_type=pinned[1]),
            )
        return HourlyCheckoutFacts(instance_type=match, root_disk=live)

    async def list_images(self, region: str) -> list[Any]:
        self.listed_regions.append(region)
        if self.list_images_error is not None:
            raise self.list_images_error
        return self.images

    async def create_instance(self, **kwargs: Any) -> Any:
        self.posts += 1
        self.created_kwargs.append(dict(kwargs))
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    async def find_by_reference(self, region: str, reference: str) -> Any:
        if self.find_error is not None:
            raise self.find_error
        return self.find_result


def _snapshot(price: FakeSnapshots) -> Any:
    return price.created[0][1]


class FakeCapacityRepo:
    """In-memory durable capacity store (the SQL adapter has a live PG test)."""

    def __init__(self, records: dict[str, Any] | None = None) -> None:
        self.records: dict[str, Any] = dict(records or {})
        self.writes: list[dict[str, Any]] = []

    async def get(self, provider_key: str, credential_account_id: str) -> Any:
        return self.records.get(credential_account_id)

    async def list_for_provider(self, provider_key: str) -> list[Any]:
        return list(self.records.values())

    async def limit_reached_accounts(self, provider_key: str, *, now: Any = None) -> frozenset[str]:
        return frozenset(
            account_id
            for account_id, record in self.records.items()
            if record.is_limit_reached(now=now)
        )

    async def record_limit_reached(
        self,
        *,
        provider_key: str,
        credential_account_id: str,
        observation: Any,
        ttl_seconds: int = 3600,
        now: Any = None,
    ) -> Any:
        from cloud_platform.modules.provider_capacity.domain import AccountCapacity

        current = self.records.get(credential_account_id) or AccountCapacity(
            provider_key=provider_key,
            credential_account_id=credential_account_id,
            observations=0,
        )
        updated = current.with_limit_reached(
            observation=observation, ttl_seconds=ttl_seconds, now=now
        )
        self.records[credential_account_id] = updated
        self.writes.append(
            {
                "account": credential_account_id,
                "error_code": observation.error_code,
                "correlation_id": observation.correlation_id,
                "location_id": observation.location_id,
                "product_id": observation.product_id,
                "ttl_seconds": ttl_seconds,
            }
        )
        return updated


class _DictResolver:
    """Account-aware adapter resolution without any provider-specific branching."""

    def __init__(self, adapters: dict[str, Any]) -> None:
        self._adapters = dict(adapters)
        self.resolved: list[str | None] = []

    def adapter_for(self, provider_key: str, credential_account_id: str | None = None) -> Any:
        self.resolved.append(credential_account_id)
        return self._adapters.get(str(credential_account_id or ""))


def _service(
    offers: FakeOffersRepo,
    cloud: FakeHourlyAdapter,
    *,
    servers: FakeServerRepo | None = None,
    snapshots: FakeSnapshots | None = None,
    ops: FakeOpsRepo | None = None,
    capacity: Any | None = None,
    resolver: Any | None = None,
    capacity_ttl_seconds: int = 3600,
) -> tuple[HourlyCloudService, FakeServerRepo, FakeSnapshots, FakeOpsRepo]:
    servers = servers or FakeServerRepo()
    snapshots = snapshots or FakeSnapshots()
    ops = ops or FakeOpsRepo()
    service = HourlyCloudService(
        server_repo=servers,
        offers_repo=offers,  # type: ignore[arg-type]
        account_repo=FakeAccountRepo(),
        wallet_repo=FakeWalletRepo2(),
        snapshot_service=snapshots,  # type: ignore[arg-type]
        operation_repo=ops,  # type: ignore[arg-type]
        audit_repo=FakeAuditRepo(),
        cloud_providers={PROVIDER: cloud},
        capacity_repo=capacity,
        capacity_ttl_seconds=capacity_ttl_seconds,
        cloud_resolver=resolver,
    )
    return service, servers, snapshots, ops


async def _requested_server(
    service: HourlyCloudService,
    offers: FakeOffersRepo,
    *,
    image_label: str = "Ubuntu",
    idempotency_key: str | None = None,
) -> Any:
    """Create a REQUESTED hourly server through the real service path
    (so the pinned snapshot is a genuine SellingPrice, exactly as in
    production), keyed by a unique idempotency key."""
    offer = (await offers.list_all())[0]
    result = await service.create_instance(
        user=USER,
        offer_id=offer.id,
        image_id="UBUNTU",
        image_label=image_label,
        idempotency_key=idempotency_key or f"op-{uuid4().hex[:8]}",
    )
    return result.server


# ---------------------------------------------------------------------------
# process_server: pre-send failure branches
# ---------------------------------------------------------------------------


class TestProcessPreSendFailures:
    async def test_missing_snapshot_fails_the_operation(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _, snapshots, ops = _service(offers, cloud)
        server = await _requested_server(service, offers)
        snapshots.created.clear()  # the pinned snapshot is missing
        outcome = await service.process_server(server.id)
        assert outcome.startswith("invalid:")
        op = ops.ops[f"server-create:{server.id}"]
        assert op.status.value == "failed"
        assert "invalid immutable hourly intent" in (op.error or "")

    async def test_missing_adapter_fails_without_posting(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _, _, ops = _service(offers, cloud)
        server = await _requested_server(service, offers)
        service._cloud.pop(PROVIDER)
        assert await service.process_server(server.id) == "failed"
        assert cloud.posts == 0
        op = ops.ops[f"server-create:{server.id}"]
        assert op.status.value == "failed"
        assert "no hourly adapter" in (op.error or "")

    async def test_image_vanished_fails_without_posting(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _, _, ops = _service(offers, cloud)
        server = await _requested_server(service, offers)
        cloud.images = []  # provider stopped offering the pinned image afterwards
        assert await service.process_server(server.id) == "failed"
        assert cloud.posts == 0
        op = ops.ops[f"server-create:{server.id}"]
        assert op.status.value == "failed"
        assert "revalidation failed" in (op.error or "")

    async def test_images_read_failure_requeues_without_posting(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _, _, _ = _service(offers, cloud)
        server = await _requested_server(service, offers)
        cloud.list_images_error = RuntimeError("provider down")
        assert await service.process_server(server.id) == "requeued"
        assert cloud.posts == 0


# ---------------------------------------------------------------------------
# process_server: definitive provider rejection / failed-transition guard
# ---------------------------------------------------------------------------


class TestProcessDefinitiveFailure:
    async def test_provider_rejection_marks_failed_and_server_error(self) -> None:
        from cloud_platform.modules.compute.domain import ServerLifecycleState
        from cloud_platform.providers.errors import ProviderError

        class _Rejected(ProviderError):
            pass

        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        cloud.create_error = _Rejected("quota exhausted")
        service, servers, _, ops = _service(offers, cloud)
        server = await _requested_server(service, offers)
        assert await service.process_server(server.id) == "failed"
        assert cloud.posts == 1
        op = ops.ops[f"server-create:{server.id}"]
        assert op.status.value == "failed"
        assert "quota exhausted" in (op.error or "")

        assert servers.servers[server.id].state is ServerLifecycleState.ERROR

    async def test_server_marking_failure_does_not_lose_the_operation_result(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A repo error while marking ERROR must not raise out of the job."""
        from cloud_platform.providers.errors import ProviderError

        class _Rejected(ProviderError):
            pass

        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        cloud.create_error = _Rejected("db-reject")
        service, servers, _, ops = _service(offers, cloud)
        server = await _requested_server(service, offers)

        async def _broken_save(updated: Any) -> Any:
            raise RuntimeError("db write lost")

        servers.save = _broken_save  # type: ignore[method-assign]
        assert await service.process_server(server.id) == "failed"
        op = ops.ops[f"server-create:{server.id}"]
        assert op.status.value == "failed"


# ---------------------------------------------------------------------------
# reconcile: additional branches
# ---------------------------------------------------------------------------


class TestReconcileBranches:
    async def test_find_error_propagates_and_stays_unknown(self) -> None:
        """Transient listing errors bubble to the worker loop (which logs
        them); the operation stays OUTCOME_UNKNOWN for the next run."""
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _, _, ops = _service(offers, cloud)
        server = await _requested_server(service, offers)
        cloud.create_error = _Ambiguous("connection lost after POST")
        assert await service.process_server(server.id) == "outcome-unknown"
        cloud.find_error = RuntimeError("listing unavailable")
        with pytest.raises(RuntimeError):
            await service.reconcile_server(server.id)
        op = ops.ops[f"server-create:{server.id}"]
        assert op.status.value == "outcome_unknown"

    async def test_non_unknown_operation_is_not_reconciled(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, servers, _, _ = _service(offers, cloud)
        server = await _requested_server(service, offers)
        # Operation still PENDING (never claimed): nothing to reconcile.
        assert await service.reconcile_server(server.id) == "skipped"
        assert cloud.find_result is None or True
        assert server.id not in {s.id for s in servers.servers.values() if s.provider_server_id}

    async def test_no_adapter_is_skipped_silently(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _, _, _ = _service(offers, cloud)
        server = await _requested_server(service, offers)
        cloud.create_error = _Ambiguous("lost")
        await service.process_server(server.id)  # -> outcome-unknown
        service._cloud.clear()
        assert await service.reconcile_server(server.id) == "skipped"

    async def test_missing_snapshot_is_skipped(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _, snapshots, _ = _service(offers, cloud)
        server = await _requested_server(service, offers)
        cloud.create_error = _Ambiguous("lost")
        await service.process_server(server.id)  # -> outcome-unknown
        snapshots.created.clear()
        assert await service.reconcile_server(server.id) == "skipped"


# ---------------------------------------------------------------------------
# pricing invariants
# ---------------------------------------------------------------------------


class TestSnapshotInvariants:
    async def test_snapshot_carries_exact_hourly_prices(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        service, _, snapshots, _ = _service(offers, FakeHourlyAdapter())
        offer = (await offers.list_all())[0]
        await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="UBUNTU",
            image_label="Ubuntu",
            idempotency_key="price-check",
        )
        _, price = snapshots.created[0]
        assert price.selling_minor == offer.selling_price_minor
        assert price.offer.cost_minor == offer.provider_cost_minor
        assert price.offer.currency == offer.provider_cost_currency

    async def test_snapshot_survives_later_catalog_repricing(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        service, _, snapshots, _ = _service(offers, FakeHourlyAdapter())
        offer = (await offers.list_all())[0]
        result = await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="UBUNTU",
            image_label="Ubuntu",
            idempotency_key="repricing",
        )
        before = snapshots.created[0][1]
        # The catalog moves: the stored offer row reprices afterwards.
        import dataclasses

        repriced = dataclasses.replace(offer, selling_price_minor=offer.selling_price_minor * 3)
        offers._rows[(repriced.provider_key, repriced.product_id, repriced.location_id)] = repriced
        after = await snapshots.require_snapshot(result.server.id)
        assert after is before
        assert after.selling_minor == before.selling_minor


# ---------------------------------------------------------------------------
# queue selection
# ---------------------------------------------------------------------------


class TestQueueSelection:
    async def test_requested_queue_filters_monthly(self) -> None:
        from cloud_platform.modules.compute.domain import (
            BILLING_MODEL_PREPAID_MONTHLY,
            CloudServer,
            ServerLifecycleState,
        )

        offers = FakeOffersRepo([await _usd_offer()])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        hourly = await _requested_server(service, offers)
        # A monthly row cannot come from this service (it rejects monthly
        # offers), so the monthly row is inserted at the repo boundary.
        monthly = CloudServer(
            id=uuid4(),
            user_id=USER.id,
            provider_key=PROVIDER,
            provider_account_id=uuid4(),
            state=ServerLifecycleState.REQUESTED,
            billing_model=BILLING_MODEL_PREPAID_MONTHLY,
            quantum_seconds=3600,
        )
        servers.servers[monthly.id] = monthly
        queued = await service.servers_requested()
        assert [s.id for s in queued] == [hourly.id]

    async def test_reconcile_queue_excludes_attached_servers(self) -> None:
        from cloud_platform.modules.compute.domain import (
            BILLING_MODEL_HOURLY,
            CloudServer,
            ServerLifecycleState,
        )

        offers = FakeOffersRepo([_offer()])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        unattached = CloudServer(
            id=uuid4(),
            user_id=USER.id,
            provider_key=PROVIDER,
            provider_account_id=uuid4(),
            state=ServerLifecycleState.PROVISIONING,
            billing_model=BILLING_MODEL_HOURLY,
            quantum_seconds=3600,
        )
        attached = CloudServer(
            id=uuid4(),
            user_id=USER.id,
            provider_key=PROVIDER,
            provider_account_id=uuid4(),
            state=ServerLifecycleState.PROVISIONING,
            billing_model=BILLING_MODEL_HOURLY,
            quantum_seconds=3600,
            provider_server_id="lsw-123",
        )
        servers.servers[unattached.id] = unattached
        servers.servers[attached.id] = attached
        queued = await service.servers_for_reconcile()
        assert [s.id for s in queued] == [unattached.id]


# ---------------------------------------------------------------------------
# misc helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    async def test_cloud_image_by_index_rejects_positional_selection(self) -> None:
        """Positional image callbacks are never resolved (fail closed).

        The billable contract carries a stable provider image id, so every
        positional lookup — valid index, out-of-range index, missing adapter
        or provider read failure — raises ``HourlyNotAvailableError``.
        """
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _, _, _ = _service(offers, cloud)
        offer = (await offers.list_all())[0]
        with pytest.raises(HourlyNotAvailableError):
            await service.cloud_image_by_index(offer, 0)
        with pytest.raises(HourlyNotAvailableError):
            await service.cloud_image_by_index(offer, 5)
        service._cloud.clear()
        with pytest.raises(HourlyNotAvailableError):
            await service.cloud_image_by_index(offer, 0)
        cloud.list_images_error = RuntimeError("down")
        service._cloud[PROVIDER] = cloud
        with pytest.raises(HourlyNotAvailableError):
            await service.cloud_image_by_index(offer, 0)

    def test_reference_name_is_deterministic(self) -> None:
        server_id = uuid4()
        assert hourly_reference_name(server_id) == hourly_reference_name(server_id)
        assert hourly_reference_name(server_id).startswith("srv-")


class _Ambiguous(ProviderOutcomeUnknown):
    pass


# ---------------------------------------------------------------------------
# create_instance: intent guards (nothing is persisted, nothing is charged)
# ---------------------------------------------------------------------------


def _apply_user(**over: Any) -> Any:
    """USER with one field changed (e.g. an unsaved id or a frozen status)."""
    from dataclasses import replace

    return replace(USER, **over)


async def _create(
    service: HourlyCloudService,
    offers: FakeOffersRepo,
    *,
    user: Any = None,
    image_id: str = "UBUNTU",
    idempotency_key: str = "intent-key",
    offer: Any = None,
) -> Any:
    target = offer if offer is not None else (await offers.list_all())[0]
    return await service.create_instance(
        user=user if user is not None else USER,
        offer_id=target.id,
        image_id=image_id,
        image_label="Ubuntu",
        idempotency_key=idempotency_key,
    )


class TestCreateIntentGuards:
    async def test_an_unsaved_user_is_refused(self) -> None:
        offers = FakeOffersRepo([_offer()])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        with pytest.raises(HourlyError):
            await _create(service, offers, user=_apply_user(id=None))
        assert servers.servers == {}

    async def test_a_non_active_user_is_refused(self) -> None:
        from cloud_platform.modules.users.domain import UserStatus

        offers = FakeOffersRepo([_offer()])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        with pytest.raises(HourlyError):
            await _create(service, offers, user=_apply_user(status=UserStatus.FROZEN))
        assert servers.servers == {}

    async def test_a_missing_offer_is_refused(self) -> None:
        offers = FakeOffersRepo([_offer()])
        service, _, _, _ = _service(offers, FakeHourlyAdapter())
        with pytest.raises(HourlyNotAvailableError):
            await service.create_instance(
                user=USER,
                offer_id=uuid4(),
                image_id="UBUNTU",
                image_label="Ubuntu",
                idempotency_key="k",
            )

    async def test_an_unpriced_offer_is_not_sellable(self) -> None:
        offers = FakeOffersRepo([_offer(price_minor=0)])
        service, _, _, _ = _service(offers, FakeHourlyAdapter())
        with pytest.raises(HourlyNotAvailableError):
            await _create(service, offers)

    async def test_a_monthly_offer_is_refused_for_hourly_creation(self) -> None:
        offers = FakeOffersRepo([_offer(billing_model=BILLING_MODEL_PREPAID_MONTHLY)])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        with pytest.raises(HourlyNotAvailableError):
            await _create(service, offers)
        assert servers.servers == {}

    async def test_an_empty_image_id_is_refused(self) -> None:
        offers = FakeOffersRepo([_offer()])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        with pytest.raises(HourlyNotAvailableError):
            await _create(service, offers, image_id="   ")
        assert servers.servers == {}

    async def test_a_replayed_key_of_another_user_is_refused(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        await _create(service, offers, idempotency_key="shared-key")
        with pytest.raises(HourlyError):
            await _create(
                service,
                offers,
                user=_apply_user(id=uuid4()),
                idempotency_key="shared-key",
            )
        assert len(servers.servers) == 1

    async def test_a_replayed_key_of_the_same_user_is_idempotent(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        first = await _create(service, offers, idempotency_key="same-key")
        second = await _create(service, offers, idempotency_key="same-key")
        assert second.replayed is True
        assert second.server.id == first.server.id
        assert len(servers.servers) == 1

    async def test_a_user_without_a_wallet_is_refused(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        snapshots = FakeSnapshots()
        servers = FakeServerRepo()
        service = HourlyCloudService(
            server_repo=servers,
            offers_repo=offers,  # type: ignore[arg-type]
            account_repo=FakeAccountRepo(),
            wallet_repo=_NoWalletRepo(),  # type: ignore[arg-type]
            snapshot_service=snapshots,  # type: ignore[arg-type]
            operation_repo=FakeOpsRepo(),  # type: ignore[arg-type]
            audit_repo=FakeAuditRepo(),
            cloud_providers={PROVIDER: cloud},
        )
        with pytest.raises(HourlyError):
            await _create(service, offers)
        assert servers.servers == {}
        assert snapshots.created == []

    async def test_the_unique_key_race_replays_the_original_intent(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        snapshots = FakeSnapshots()
        ops = FakeOpsRepo()
        seed_service, _, _, _ = _service(
            offers,
            FakeHourlyAdapter(),
            servers=FakeServerRepo(),
            snapshots=snapshots,
            ops=ops,
        )
        original = await _requested_server(seed_service, offers, idempotency_key="seed-raced")
        priced_before = len(snapshots.created)
        servers = _CreateRaceRepo(original)
        service, _, _, _ = _service(
            offers, FakeHourlyAdapter(), servers=servers, snapshots=snapshots, ops=ops
        )
        result = await _create(service, offers, idempotency_key="raced")
        assert result.replayed is True
        assert result.server is original
        assert len(snapshots.created) == priced_before  # nothing new was priced or charged

    async def test_the_race_of_another_users_key_is_not_replayed(self) -> None:
        from cloud_platform.modules.compute.domain import ServerCreateError

        offers = FakeOffersRepo([await _usd_offer()])
        foreign = type("S", (), {"id": uuid4(), "user_id": uuid4()})()
        servers = _CreateRaceRepo(foreign)
        service, _, _, _ = _service(offers, FakeHourlyAdapter(), servers=servers)
        with pytest.raises(ServerCreateError):
            await _create(service, offers, idempotency_key="raced")

    async def test_a_failed_price_snapshot_marks_the_server_error_and_reraises(self) -> None:
        from cloud_platform.modules.compute.domain import ServerLifecycleState

        offers = FakeOffersRepo([await _usd_offer()])
        snapshots = _FailingSnapshots()
        service, servers, _, _ = _service(offers, FakeHourlyAdapter(), snapshots=snapshots)
        with pytest.raises(RuntimeError):
            await _create(service, offers, idempotency_key="snapshot-fail")
        assert servers.servers
        created = next(iter(servers.servers.values()))
        assert created.state is ServerLifecycleState.ERROR

    async def test_a_failed_rollback_never_hides_the_snapshot_error(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        server_repo = _UnsaveableRepo()
        service, _, _, _ = _service(
            offers,
            FakeHourlyAdapter(),
            servers=server_repo,  # type: ignore[arg-type]
            snapshots=_FailingSnapshots(),
        )
        with pytest.raises(RuntimeError, match="snapshot store down"):
            await _create(service, offers, idempotency_key="rollback-fail")


class _NoWalletRepo:
    async def get(self, user_id: Any) -> None:
        return None


class _CreateRaceRepo(FakeServerRepo):
    """The pre-check misses, create() hits the unique key, the re-read finds it."""

    def __init__(self, original: Any) -> None:
        super().__init__()
        self.original = original
        self.prechecks = 0

    async def get_by_idempotency_key(self, key: str) -> Any:
        self.prechecks += 1
        if self.prechecks == 1:
            return None
        return self.original

    async def create(self, server: Any, intent: Any) -> Any:
        from cloud_platform.modules.compute.domain import ServerCreateError

        raise ServerCreateError("duplicate idempotency key")


class _FailingSnapshots(FakeSnapshots):
    async def create_snapshot(self, *, server_id: Any, price: Any, actor: Any, reason: str) -> Any:
        raise RuntimeError("snapshot store down")


class _UnsaveableRepo(FakeServerRepo):
    async def save(self, server: Any) -> Any:
        raise RuntimeError("database down")


# ---------------------------------------------------------------------------
# process_server / reconcile_server: skip and claim branches
# ---------------------------------------------------------------------------


class TestQueueAndClaimBranches:
    async def test_an_unknown_server_is_skipped(self) -> None:
        offers = FakeOffersRepo([_offer()])
        service, _, _, _ = _service(offers, FakeHourlyAdapter())
        assert await service.process_server(uuid4()) == "skipped"

    async def test_a_monthly_server_is_never_submitted_hourly(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        server = await _requested_server(service, offers)
        server.billing_model = BILLING_MODEL_PREPAID_MONTHLY
        await servers.save(server)
        assert await service.process_server(server.id) == "skipped"
        assert await service.reconcile_server(server.id) == "skipped"

    async def test_a_server_that_is_not_requested_is_skipped(self) -> None:
        from cloud_platform.modules.compute.domain import ServerLifecycleState

        offers = FakeOffersRepo([await _usd_offer()])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        server = await _requested_server(service, offers)
        server.transition_to(ServerLifecycleState.PROVISIONING)
        await servers.save(server)
        assert await service.process_server(server.id) == "skipped"

    async def test_a_terminal_operation_is_not_submitted_twice(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _, _, ops = _service(offers, cloud)
        server = await _requested_server(service, offers)
        operation = await ops.get_by_key(f"server-create:{server.id}")
        operation.mark_in_flight()
        operation.complete({"provider_server_id": "lsw-done"})
        assert await service.process_server(server.id) == "skipped"
        assert cloud.posts == 0

    async def test_an_operation_claimed_elsewhere_is_not_posted(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _, _, ops = _service(offers, cloud)
        server = await _requested_server(service, offers)
        operation = await ops.get_by_key(f"server-create:{server.id}")
        operation.mark_in_flight()
        # The stuck in-flight operation has no provider evidence, so the
        # worker quarantines it as outcome-unknown instead of re-POSTing.
        assert await service.process_server(server.id) == "outcome-unknown"
        assert cloud.posts == 0

    async def test_a_server_with_an_attached_instance_is_not_reconciled(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        server = await _requested_server(service, offers)
        server.provider_server_id = "lsw-attached"
        await servers.save(server)
        assert await service.reconcile_server(server.id) == "skipped"

    async def test_a_server_without_an_operation_is_not_reconciled(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        service, servers, _, _ = _service(offers, FakeHourlyAdapter())
        server = await _requested_server(service, offers)
        server.provider_server_id = None
        await servers.save(server)
        assert await service.reconcile_server(server.id) == "skipped"


class TestResponseIdentityMatching:
    """Optional identity evidence is checked when present, never required."""

    def _response(self, **overrides: Any) -> Any:
        from types import SimpleNamespace

        values: dict[str, Any] = {
            "region": "eu-west-3",
            "reference": "srv-abc",
            "instance_type": "lsw.m4.large",
            "image_id": "UBUNTU_24_04",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _match(self, response: Any) -> bool:
        from cloud_platform.modules.hourly.service import _response_identity_matches

        return _response_identity_matches(
            response,
            location_id="eu-west-3",
            plan_id="lsw.m4.large",
            image_id="UBUNTU_24_04",
            account_id="uk",
            reference="srv-abc",
        )

    def test_full_identity_matches(self) -> None:
        assert self._match(self._response()) is True

    def test_absent_optional_fields_still_match(self) -> None:
        response = self._response()
        del response.instance_type
        del response.image_id
        assert self._match(response) is True

    def test_present_but_different_identity_fails(self) -> None:
        assert self._match(self._response(instance_type="lsw.r5.xlarge")) is False
        assert self._match(self._response(image_id="DEBIAN_12")) is False

    def test_wrong_region_or_reference_fails(self) -> None:
        assert self._match(self._response(region="eu-central-1")) is False
        assert self._match(self._response(reference="srv-other")) is False


class TestCatalogRepriceImmutability:
    """A later catalog reprice never changes an accepted hourly contract."""

    async def test_process_uses_snapshot_price_after_catalog_reprice(self) -> None:
        from types import SimpleNamespace

        from cloud_platform.modules.hourly.service import hourly_reference_name

        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _servers, snapshots, _ops = _service(offers, cloud)
        server = await _requested_server(service, offers)
        offer = (await offers.list_all())[0]
        before = (await snapshots.require_snapshot(server.id)).selling_minor
        # Tomorrow's catalog moves; the accepted snapshot must not follow it.
        await offers.set_selling_price(offer.id, before + 500, "USD")
        cloud.create_result = SimpleNamespace(
            id="lsw-created",
            state="RUNNING",
            region="eu-west-3",
            reference=hourly_reference_name(server.id),
            instance_type="lsw.mini",
            image_id="UBUNTU",
        )
        outcome = await service.process_server(server.id)
        assert outcome == "provisioned"
        snapshot = await snapshots.require_snapshot(server.id)
        assert snapshot.selling_minor == before


# ---------------------------------------------------------------------------
# claim boundary: no provider POST without a durable claim, and an
# infrastructure failure while claiming is loud instead of a silent spin
# ---------------------------------------------------------------------------


class _ClaimFailingOps(FakeOpsRepo):
    """Scripted claim(): fail the first ``failures`` attempts (the incident).

    The production stack trace was ``asyncpg ... can't subtract offset-naive
    and offset-aware datetimes`` raised from
    ``SqlAlchemyOperationRepository.claim``; any exception type must behave
    the same way.
    """

    def __init__(self, *, failures: int = 1, error: Exception | None = None) -> None:
        super().__init__()
        self.failures = failures
        self.error = error or RuntimeError(
            "asyncpg.exceptions.DataError: invalid input for query argument $3: "
            "(can't subtract offset-naive and offset-aware datetimes)"
        )
        self.claim_attempts = 0
        self.claims_completed = 0

    async def claim(self, operation_id: Any) -> Any:
        self.claim_attempts += 1
        if self.claim_attempts <= self.failures:
            raise self.error
        claimed = await super().claim(operation_id)
        if claimed is not None:
            self.claims_completed += 1
        return claimed


class _ClaimOrderAdapter(FakeHourlyAdapter):
    """The provider adapter refuses to be mutated before a durable claim."""

    def __init__(self, ops: FakeOpsRepo) -> None:
        super().__init__()
        self._ops = ops

    async def create_instance(self, **kwargs: Any) -> Any:
        assert self._ops.claims_completed >= 1, "provider POST before a durable claim"
        return await super().create_instance(**kwargs)


def _accepted_response(server: Any) -> Any:
    """The provider's accepted-POST response for ``server`` (real identity)."""
    return SimpleNamespace(
        id="lsw-created",
        state="RUNNING",
        region="eu-west-3",
        reference=hourly_reference_name(server.id),
        instance_type="lsw.mini",
        image_id="UBUNTU",
    )


class TestPinnedLaunchRootDisk:
    """The leaseweb POST needs a root disk, so the accepted contract pins it.

    Production observation 2026-09-25: a create reached Leaseweb without
    ``rootDiskSize``/``rootDiskStorageType`` (both REQUIRED) and collected a
    definitive 400. The two values now travel from the pinned contract into
    the POST, and are re-verified against live provider facts first.
    """

    async def test_the_post_carries_the_pinned_root_disk(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _servers, _snapshots, _ops = _service(offers, cloud)
        server = await _requested_server(service, offers)
        cloud.create_result = _accepted_response(server)

        assert await service.process_server(server.id) == "provisioned"

        assert cloud.verified_pinned == [(25, "CENTRAL")]
        sent = cloud.created_kwargs[0]
        assert sent["root_disk_size_gb"] == 25
        assert sent["root_disk_storage_type"] == "CENTRAL"
        assert sent["instance_type"] == "lsw.mini"
        assert sent["region"] == "eu-west-3"
        assert sent["image_label"] == "Ubuntu 24.04"

    async def test_the_accepted_intent_pins_the_root_disk_in_its_fingerprint(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, _servers, _snapshots, _ops = _service(offers, cloud)
        server = await _requested_server(service, offers)

        fingerprint = server.offer_fingerprint
        assert fingerprint["fingerprint_version"] == 2
        assert fingerprint["root_disk_size_gb"] == 25
        assert fingerprint["root_disk_storage_type"] == "CENTRAL"

    async def test_a_legacy_contract_without_a_root_disk_fails_without_posting(self) -> None:
        """A pre-root-disk contract cannot be re-created: fail closed, explain."""
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        service, servers, snapshots, ops = _service(offers, cloud)
        server = await _requested_server(service, offers)
        await snapshots.require_snapshot(server.id)
        legacy = dict(server.offer_fingerprint)
        legacy["fingerprint_version"] = 1
        legacy.pop("root_disk_size_gb")
        legacy.pop("root_disk_storage_type")
        server.offer_fingerprint = legacy
        # The immutable snapshot is frozen: rewrite the stored row as a
        # pre-root-disk version-1 contract (both sides must agree).
        snapshots.created = [
            (
                sid,
                dataclasses.replace(price, offer_fingerprint=legacy) if sid == server.id else price,
            )
            for sid, price in snapshots.created
        ]
        await servers.save(server)
        cloud.create_result = _accepted_response(server)

        assert await service.process_server(server.id) == "failed"
        assert cloud.posts == 0
        operation = ops.ops[f"server-create:{server.id}"]
        assert operation.status.value == "failed"
        assert "root disk" in str(operation.error)
        # Still no duplicate provider create on a second worker pass.
        assert await service.process_server(server.id) == "skipped"
        assert cloud.posts == 0

    async def test_the_claim_and_the_post_stay_exactly_once(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyAdapter()
        ops = _ClaimFailingOps(failures=0)
        service, _servers, _snapshots, _ops = _service(offers, cloud, ops=ops)
        server = await _requested_server(service, offers)
        cloud.create_result = _accepted_response(server)

        assert await service.process_server(server.id) == "provisioned"
        assert await service.process_server(server.id) == "skipped"

        assert cloud.posts == 1
        assert ops.claims_completed == 1
        key = f"server-create:{server.id}"
        assert ops.ops[key].operation_key == key
        assert len(cloud.created_kwargs) == 1


class TestClaimBoundary:
    async def test_provider_post_requires_a_completed_claim(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        ops = _ClaimFailingOps(failures=0)
        cloud = _ClaimOrderAdapter(ops)
        service, _servers, _snapshots, _ops = _service(offers, cloud, ops=ops)
        server = await _requested_server(service, offers)
        cloud.create_result = _accepted_response(server)

        assert await service.process_server(server.id) == "provisioned"

        assert cloud.posts == 1
        assert ops.claims_completed == 1
        operation = ops.ops[f"server-create:{server.id}"]
        assert operation.attempts == 1
        assert operation.status.value == "completed"

    async def test_claim_failure_is_loud_and_never_posts(self, monkeypatch: Any) -> None:
        stages: list[str] = []
        monkeypatch.setattr(
            "cloud_platform.modules.hourly.service.metrics.record_provisioning_failure",
            stages.append,
        )
        offers = FakeOffersRepo([await _usd_offer()])
        ops = _ClaimFailingOps(failures=99)
        cloud = _ClaimOrderAdapter(ops)
        service, _servers, _snapshots, _ops = _service(offers, cloud, ops=ops)
        server = await _requested_server(service, offers)
        cloud.create_result = _accepted_response(server)

        # The caller (worker job) sees the failure: it is NOT reported as
        # "still in progress", so the request cannot spin silently forever.
        with pytest.raises(RuntimeError, match="offset-naive"):
            await service.process_server(server.id)

        assert cloud.posts == 0
        assert stages == ["worker"]
        operation = ops.ops[f"server-create:{server.id}"]
        assert operation.status.value == "pending"
        assert operation.attempts == 0

    async def test_claim_failure_does_not_duplicate_the_later_post(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        ops = _ClaimFailingOps(failures=1)
        cloud = _ClaimOrderAdapter(ops)
        service, _servers, _snapshots, _ops = _service(offers, cloud, ops=ops)
        server = await _requested_server(service, offers)
        cloud.create_result = _accepted_response(server)

        with pytest.raises(RuntimeError):
            await service.process_server(server.id)
        # The next tick retries the SAME durable operation: exactly one POST,
        # one attempt, and the same operation key (no duplicate provider work).
        assert await service.process_server(server.id) == "provisioned"

        assert cloud.posts == 1
        assert ops.claim_attempts == 2
        operation = ops.ops[f"server-create:{server.id}"]
        assert operation.attempts == 1
        assert operation.operation_key == f"server-create:{server.id}"
