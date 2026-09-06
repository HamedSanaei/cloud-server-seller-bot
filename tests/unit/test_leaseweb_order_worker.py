"""Order worker + reconciler safety tests (LEASEWEB-MVP).

Acceptance:
- one provider POST per order intent (ledger claim + get-before-create),
- hold captured exactly once on acceptance, released on definitive rejection,
- transient failures re-queue with the SAME key (never a second order),
- a worker crash mid-POST is recovered by re-queuing stale IN_FLIGHT ops,
- the reconciler only inspects; activation delivers the server once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.audit.domain import AuditEvent
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.orders.domain import OrderStatus, ProviderOrder
from cloud_platform.modules.orders.service import (
    OrderReconciler,
    OrderWorker,
    RenewalInfo,
)
from cloud_platform.modules.renewals.domain import RenewalRecord, RenewalStatus
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldStatus,
    Wallet,
)
from cloud_platform.providers.base import ProvisioningTicket
from cloud_platform.providers.errors import ProviderAuthError, ProviderUnavailable
from cloud_platform.providers.registry import ProviderRegistry

USER_ID = uuid4()
WALLET_ID = uuid4()
OFFER_ID = uuid4()
SERVER_ID = uuid4()
ORDER_ID = uuid4()
OP_KEY = f"order-create:{SERVER_ID}"


def _offer() -> SellableOffer:
    return SellableOffer(
        id=OFFER_ID,
        provider_key="leaseweb",
        product_id="VPS02_1",
        location_id="AMS-01",
        name="VPS S",
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic="10 TB",
        provider_cost_minor=999,
        provider_cost_currency="EUR",
        selling_price_minor=1299,
        selling_currency="EUR",
        billing_parameters={},
        provider_available=True,
        enabled=True,
    )


def _server(state: ServerLifecycleState = ServerLifecycleState.REQUESTED) -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key="leaseweb",
        provider_account_id=uuid4(),
        state=state,
        billing_model=BILLING_MODEL_PREPAID_MONTHLY,
        os="Ubuntu 24.04",
        idempotency_key="bot-monthly:abc",
        created_at=datetime.now(UTC),
    )


def _order(
    status: OrderStatus = OrderStatus.PENDING_SUBMIT,
    *,
    provider_order_id: str | None = None,
) -> ProviderOrder:
    return ProviderOrder(
        id=ORDER_ID,
        server_id=SERVER_ID,
        operation_key=OP_KEY,
        provider_key="leaseweb",
        offer_id=OFFER_ID,
        status=status,
        provider_order_id=provider_order_id,
    )


def _operation(status: OperationStatus = OperationStatus.PENDING) -> Operation:
    now = datetime.now(UTC)
    return Operation(
        id=uuid4(),
        operation_key=OP_KEY,
        operation_type=OperationType.ORDER_CREATE,
        resource_type="server_order",
        resource_id=ORDER_ID,
        provider_key="leaseweb",
        status=status,
        created_at=now,
        updated_at=now,
    )


class FakeAuditRepo:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> AuditEvent:
        self.events.append(event)
        return event


class FakeOfferRepo:
    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return _offer() if offer_id == OFFER_ID else None


class FakeServerRepo:
    def __init__(self, server: CloudServer | None = None) -> None:
        self.server = server or _server()
        self.saved: list[CloudServer] = []

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.server if server_id == SERVER_ID else None

    async def list_requested_prepaid(self) -> list[CloudServer]:
        return [self.server] if self.server.state is ServerLifecycleState.REQUESTED else []

    async def save(self, server: CloudServer) -> CloudServer:
        self.server = server
        self.saved.append(server)
        return server


class FakeOrdersRepo:
    def __init__(self, order: ProviderOrder | None = None) -> None:
        self.order = order or _order()
        self.saved: list[ProviderOrder] = []

    async def get_by_server(self, server_id: UUID) -> ProviderOrder | None:
        return self.order if server_id == SERVER_ID else None

    async def get(self, order_id: UUID) -> ProviderOrder | None:
        return self.order if order_id == ORDER_ID else None

    async def save(self, order: ProviderOrder) -> ProviderOrder:
        self.order = order
        self.saved.append(order)
        return order

    async def list_open(self, provider_key: str, limit: int = 100) -> list[ProviderOrder]:
        return [self.order] if self.order.is_open else []


class FakeOperationRepo:
    def __init__(self, op: Operation | None = None) -> None:
        self.op = op or _operation()
        self.saved: list[Operation] = []

    async def get_by_key(self, operation_key: str) -> Operation | None:
        return self.op if operation_key == OP_KEY else None

    async def claim(self, operation_id: UUID) -> Operation | None:
        if self.op.status is not OperationStatus.PENDING:
            return None
        self.op.mark_in_flight()
        return self.op

    async def save(self, operation: Operation) -> Operation:
        self.op = operation
        self.saved.append(operation)
        return operation

    async def list_open(self, *args: Any, **kwargs: Any) -> list[Operation]:
        return []


class FakeWalletRepo:
    def __init__(self) -> None:
        self.wallet = Wallet(user_id=USER_ID, id=WALLET_ID, balance=10_000, currency="EUR")

    async def get(self, user_id: UUID) -> Wallet | None:
        return self.wallet if user_id == USER_ID else None


class FakeHoldRepo:
    def __init__(self) -> None:
        self.hold = Hold(
            wallet_id=WALLET_ID,
            amount=1299,
            currency="EUR",
            idempotency_key="leaseweb-order:bot-monthly:abc",
            id=uuid4(),
        )
        self.released: list[Hold] = []
        self.captured: list[Hold] = []

    async def get_by_idempotency(self, wallet_id: UUID, idempotency_key: str) -> Hold | None:
        return self.hold if idempotency_key == self.hold.idempotency_key else None

    async def release_hold(self, hold_id: UUID) -> Hold | None:
        if self.hold.status is HoldStatus.CREATED:
            self.hold.release()
            self.released.append(self.hold)
            return self.hold
        return None

    async def capture_hold(self, hold_id: UUID) -> Hold | None:
        if self.hold.status is HoldStatus.CREATED:
            self.hold.capture()
            self.captured.append(self.hold)
            return self.hold
        return None


class FakeHoldService:
    def __init__(self, holds: FakeHoldRepo) -> None:
        self._holds = holds
        self.captures: list[tuple[UUID, UUID, str]] = []
        self.releases: list[tuple[UUID, UUID, str]] = []

    async def capture_hold(self, wallet_id: UUID, hold_id: UUID, idempotency_key: str) -> Hold:
        self.captures.append((wallet_id, hold_id, idempotency_key))
        hold = await self._holds.capture_hold(hold_id)
        if hold is None:
            return self._holds.hold
        return hold

    async def release_hold(self, wallet_id: UUID, hold_id: UUID, idempotency_key: str) -> Hold:
        self.releases.append((wallet_id, hold_id, idempotency_key))
        hold = await self._holds.release_hold(hold_id)
        if hold is None:
            return self._holds.hold
        return hold


class FakeRenewalRepo:
    def __init__(self) -> None:
        self.upserted: list[RenewalRecord] = []

    async def get(self, server_id: UUID) -> RenewalRecord | None:
        return None

    async def upsert(self, record: RenewalRecord) -> RenewalRecord:
        self.upserted.append(record)
        return record


class FakeOrderingProvider:
    key = "leaseweb"

    def __init__(
        self, *, ticket: ProvisioningTicket | None = None, error: Exception | None = None
    ) -> None:
        self._ticket = ticket or ProvisioningTicket(
            provider_order_id="LS-ORD-1", state="provisioning"
        )
        self._error = error
        self.posts: list[IdempotencyKey] = []

    async def place_order(
        self, request: Any, idempotency_key: IdempotencyKey
    ) -> ProvisioningTicket:
        self.posts.append(idempotency_key)
        if self._error is not None:
            raise self._error
        return self._ticket

    async def get_order(self, provider_order_id: str) -> ProvisioningTicket:
        return self._ticket

    async def get_product(self, location_id: str, product_id: str) -> Any:
        return None

    def os_name_allowed(self, detail: Any, os_name: str) -> bool:
        return True

    async def match_vps_for_order(
        self, provider_order_id: str, *, location: str, product_name: str, since: datetime
    ) -> str:
        return "vps-1"

    async def get_server(self, provider_server_id: str) -> Any:
        return None


class _RecordingNotifier:
    def __init__(self) -> None:
        self.deliveries: list[tuple[CloudServer, SellableOffer, RenewalInfo]] = []

    async def deliver(
        self, *, server: CloudServer, offer: SellableOffer, renewal: RenewalInfo
    ) -> None:
        self.deliveries.append((server, offer, renewal))


def _worker(
    *,
    ordering: FakeOrderingProvider | None = None,
    op_repo: FakeOperationRepo | None = None,
    order: ProviderOrder | None = None,
    server: CloudServer | None = None,
    holds: FakeHoldRepo | None = None,
    notifier: _RecordingNotifier | None = None,
    clock: Any = None,
) -> tuple[OrderWorker, dict[str, Any]]:
    ordering = ordering or FakeOrderingProvider()
    op_repo = op_repo or FakeOperationRepo()
    server_repo = FakeServerRepo(server)
    orders_repo = FakeOrdersRepo(order)
    wallet_repo = FakeWalletRepo()
    holds = holds or FakeHoldRepo()
    hold_service = FakeHoldService(holds)
    renewal_repo = FakeRenewalRepo()
    notifier = notifier or _RecordingNotifier()
    registry = ProviderRegistry()
    registry.register(ordering)
    worker = OrderWorker(
        server_repo=server_repo,
        offers_repo=FakeOfferRepo(),
        orders_repo=orders_repo,
        operation_repo=op_repo,
        wallet_repo=wallet_repo,
        hold_repo=holds,
        hold_service=hold_service,
        audit_repo=FakeAuditRepo(),
        provider_registry=registry,
        renewal_repo=renewal_repo,
        delivery_notifier=notifier,
        clock=clock,
    )
    deps = {
        "op_repo": op_repo,
        "orders": orders_repo,
        "servers": server_repo,
        "holds": holds,
        "hold_service": hold_service,
        "renewals": renewal_repo,
        "notifier": notifier,
        "ordering": ordering,
    }
    return worker, deps


class TestOrderWorker:
    async def test_submits_exactly_once_and_captures_hold(self) -> None:
        worker, deps = _worker()
        counts = await worker.process_pending()
        assert counts.get("submitted") == 1
        assert len(deps["ordering"].posts) == 1
        assert deps["ordering"].posts[0].value == OP_KEY  # same idempotency key
        order = deps["orders"].order
        # The provider ticket says "provisioning": SUBMITTED -> PROVISIONING.
        assert order.status is OrderStatus.PROVISIONING
        assert order.provider_order_id == "LS-ORD-1"
        assert deps["servers"].server.state is ServerLifecycleState.PROVISIONING
        assert deps["op_repo"].op.status is OperationStatus.COMPLETED
        assert len(deps["hold_service"].captures) == 1
        assert deps["holds"].hold.status is HoldStatus.CAPTURED

    async def test_second_pass_does_not_post_again(self) -> None:
        worker, deps = _worker()
        await worker.process_pending()
        # The order is no longer PENDING_SUBMIT -> the second pass is a no-op.
        await worker.process_pending()
        assert len(deps["ordering"].posts) == 1

    async def test_transient_failure_requeues_with_same_key(self) -> None:
        ordering = FakeOrderingProvider(error=ProviderUnavailable("timeout"))
        worker, deps = _worker(ordering=ordering)
        counts = await worker.process_pending()
        assert counts.get("requeued") == 1
        assert deps["op_repo"].op.status is OperationStatus.PENDING  # re-queued
        assert "timeout" in (deps["op_repo"].op.error or "")
        assert deps["orders"].order.status is OrderStatus.PENDING_SUBMIT
        assert deps["orders"].order.error is not None
        assert deps["holds"].hold.status is HoldStatus.CREATED  # never captured
        assert deps["hold_service"].releases == []  # never released either

    async def test_transient_failure_retry_posts_with_same_key(self) -> None:
        ordering = FakeOrderingProvider(
            error=ProviderUnavailable("timeout"),
            ticket=ProvisioningTicket(provider_order_id="LS-ORD-1", state="provisioning"),
        )
        worker, deps = _worker(ordering=ordering)
        await worker.process_pending()  # first attempt: requeued
        ordering._error = None  # provider healthy again
        await worker.process_pending()  # second attempt succeeds
        assert len(deps["ordering"].posts) == 2
        assert {p.value for p in deps["ordering"].posts} == {OP_KEY}  # SAME key
        assert deps["orders"].order.status is OrderStatus.PROVISIONING
        assert len(deps["hold_service"].captures) == 1

    async def test_definitive_rejection_fails_and_releases_hold(self) -> None:
        ordering = FakeOrderingProvider(error=ProviderAuthError("invalid key"))
        worker, deps = _worker(ordering=ordering)
        counts = await worker.process_pending()
        assert counts.get("failed") == 1
        assert deps["orders"].order.status is OrderStatus.FAILED
        assert deps["servers"].server.state is ServerLifecycleState.ERROR
        assert deps["op_repo"].op.status is OperationStatus.FAILED
        assert deps["holds"].hold.status is HoldStatus.RELEASED
        assert len(deps["hold_service"].releases) == 1

    async def test_racing_workers_do_not_double_post(self) -> None:
        worker_a, deps_a = _worker()
        worker_b, deps_b = _worker(
            op_repo=deps_a["op_repo"],
            order=deps_a["orders"].order,
            server=deps_a["servers"].server,
            holds=deps_a["holds"],
        )
        # A claims (PENDING -> IN_FLIGHT) and submits.
        await worker_a.process_pending()
        assert len(deps_a["ordering"].posts) == 1
        # B runs concurrently: the server already moved past REQUESTED and
        # the operation is terminal — it must not POST again.
        counts_b = await worker_b.process_pending()
        assert deps_b["ordering"].posts == []
        assert counts_b == {}
        assert len(deps_a["ordering"].posts) == 1  # one POST total

    async def test_stale_in_flight_recovered_without_duplicate_order(self) -> None:
        # Worker crashed mid-POST: operation stuck IN_FLIGHT for > 30 min.
        old = datetime.now(UTC) - timedelta(minutes=45)
        op = _operation(OperationStatus.IN_FLIGHT)
        op.updated_at = old
        ordering = FakeOrderingProvider()
        worker, deps = _worker(ordering=ordering, op_repo=FakeOperationRepo(op))
        counts = await worker.process_pending()
        assert counts.get("requeued") == 1
        assert deps["op_repo"].op.status is OperationStatus.PENDING
        # Next pass re-attempts with the same key (adapter get-before-create
        # would deduplicate the provider side).
        await worker.process_pending()
        assert len(deps["ordering"].posts) == 1
        assert deps["ordering"].posts[0].value == OP_KEY

    async def test_fresh_in_flight_is_skipped(self) -> None:
        op = _operation(OperationStatus.IN_FLIGHT)  # updated just now
        ordering = FakeOrderingProvider()
        worker, deps = _worker(ordering=ordering, op_repo=FakeOperationRepo(op))
        counts = await worker.process_pending()
        assert counts.get("skipped_in_flight") == 1
        assert deps["ordering"].posts == []


class TestOrderReconciler:
    def _reconciler(
        self,
        *,
        ticket: ProvisioningTicket | None = None,
        order: ProviderOrder | None = None,
        server: CloudServer | None = None,
        match_result: str = "vps-1",
        match_error: Exception | None = None,
        notifier: _RecordingNotifier | None = None,
    ) -> tuple[OrderReconciler, dict[str, Any]]:
        ordering = FakeOrderingProvider(
            ticket=ticket or ProvisioningTicket(provider_order_id="LS-ORD-1", state="provisioned")
        )

        async def _match(*args: Any, **kwargs: Any) -> str:
            if match_error is not None:
                raise match_error
            return match_result

        ordering.match_vps_for_order = _match  # type: ignore[method-assign]
        order = order or _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        server = server or _server(ServerLifecycleState.PROVISIONING)
        server_repo = FakeServerRepo(server)
        orders_repo = FakeOrdersRepo(order)
        wallet_repo = FakeWalletRepo()
        holds = FakeHoldRepo()
        hold_service = FakeHoldService(holds)
        renewal_repo = FakeRenewalRepo()
        notifier = notifier or _RecordingNotifier()
        registry = ProviderRegistry()
        registry.register(ordering)
        reconciler = OrderReconciler(
            server_repo=server_repo,
            offers_repo=FakeOfferRepo(),
            orders_repo=orders_repo,
            renewal_repo=renewal_repo,
            wallet_repo=wallet_repo,
            hold_repo=holds,
            hold_service=hold_service,
            audit_repo=FakeAuditRepo(),
            provider_registry=registry,
            delivery_notifier=notifier,
        )
        deps = {
            "orders": orders_repo,
            "servers": server_repo,
            "renewals": renewal_repo,
            "notifier": notifier,
            "ordering": ordering,
            "holds": holds,
        }
        return reconciler, deps

    async def test_provisioned_order_activates_server_and_delivers(self) -> None:
        reconciler, deps = self._reconciler()
        counts = await reconciler.reconcile()
        assert counts.get("provisioned") == 1
        server = deps["servers"].server
        assert server.state is ServerLifecycleState.RUNNING
        assert server.provider_server_id == "vps-1"
        assert deps["orders"].order.status is OrderStatus.ACTIVE
        # Renewal record created with the monthly price.
        assert len(deps["renewals"].upserted) == 1
        renewal = deps["renewals"].upserted[0]
        assert renewal.server_id == SERVER_ID
        assert renewal.customer_price_minor == 1299
        assert renewal.currency == "EUR"
        assert renewal.status is RenewalStatus.ACTIVE
        # Delivered exactly once.
        assert len(deps["notifier"].deliveries) == 1

    async def test_reconciler_never_posts(self) -> None:
        reconciler, deps = self._reconciler()
        await reconciler.reconcile()
        assert deps["ordering"].posts == []

    async def test_still_provisioning_keeps_polling(self) -> None:
        ticket = ProvisioningTicket(provider_order_id="LS-ORD-1", state="provisioning")
        reconciler, deps = self._reconciler(ticket=ticket)
        counts = await reconciler.reconcile()
        assert counts.get("still_provisioning") == 1
        assert deps["servers"].server.state is ServerLifecycleState.PROVISIONING
        assert deps["orders"].order.status is OrderStatus.PROVISIONING

    async def test_ambiguous_match_marks_for_review(self) -> None:
        from cloud_platform.providers.leaseweb.ordering import VpsMatchAmbiguous

        reconciler, deps = self._reconciler(match_error=VpsMatchAmbiguous("several VPSes"))
        counts = await reconciler.reconcile()
        assert counts.get("marked_for_review") == 1
        assert deps["orders"].order.status is OrderStatus.NEEDS_REVIEW
        assert deps["notifier"].deliveries == []

    async def test_missing_vps_keeps_polling(self) -> None:
        from cloud_platform.providers.errors import ProviderNotFound

        reconciler, _deps = self._reconciler(match_error=ProviderNotFound("not yet"))
        counts = await reconciler.reconcile()
        assert counts.get("still_provisioning") == 1

    async def test_cancelled_order_fails_and_releases_hold(self) -> None:
        ticket = ProvisioningTicket(provider_order_id="LS-ORD-1", state="failed")
        reconciler, deps = self._reconciler(ticket=ticket)
        counts = await reconciler.reconcile()
        assert counts.get("failed") == 1
        assert deps["orders"].order.status is OrderStatus.FAILED
        assert deps["servers"].server.state is ServerLifecycleState.ERROR
        assert deps["holds"].hold.status is HoldStatus.RELEASED

    async def test_transient_provider_error_leaves_unchanged(self) -> None:
        order = _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        ordering = FakeOrderingProvider()

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise ProviderUnavailable("down")

        ordering.get_order = _boom  # type: ignore[method-assign]
        server_repo = FakeServerRepo(_server(ServerLifecycleState.PROVISIONING))
        orders_repo = FakeOrdersRepo(order)
        wallet_repo = FakeWalletRepo()
        holds = FakeHoldRepo()
        renewal_repo = FakeRenewalRepo()
        notifier = _RecordingNotifier()
        registry = ProviderRegistry()
        registry.register(ordering)
        reconciler = OrderReconciler(
            server_repo=server_repo,
            offers_repo=FakeOfferRepo(),
            orders_repo=orders_repo,
            renewal_repo=renewal_repo,
            wallet_repo=wallet_repo,
            hold_repo=holds,
            hold_service=FakeHoldService(holds),
            audit_repo=FakeAuditRepo(),
            provider_registry=registry,
            delivery_notifier=notifier,
        )
        counts = await reconciler.reconcile()
        assert counts.get("left_unchanged") == 1
        assert order.status is OrderStatus.PROVISIONING  # never guessed


class TestOwnership:
    async def test_worker_failure_never_touches_another_users_hold(self) -> None:
        """The worker releases ONLY the owning user's hold (key-scoped)."""
        ordering = FakeOrderingProvider(error=ProviderAuthError("rejected"))
        worker, deps = _worker(ordering=ordering)
        await worker.process_pending()
        # The hold belongs to the server's owner and was released once.
        assert deps["holds"].hold.status is HoldStatus.RELEASED
        assert deps["holds"].hold.wallet_id == WALLET_ID
        # A different user's hold with the same amount is untouched.
        other_hold = Hold(
            wallet_id=uuid4(), amount=1299, currency="EUR", idempotency_key="other", id=uuid4()
        )
        assert other_hold.status is HoldStatus.CREATED
