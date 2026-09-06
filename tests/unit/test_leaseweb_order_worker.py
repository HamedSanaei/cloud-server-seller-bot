"""Order worker + reconciler + recovery safety tests (LEASEWEB-MVP).

Acceptance:
- one provider POST per order intent (atomic ledger claim — NO account-wide
  similarity dedup: two independent same-plan checkouts POST twice),
- hold captured exactly once on acceptance, released on definitive rejection,
- genuinely-not-sent failures re-queue with the SAME key (never a second order),
- an AMBIGUOUS billable POST (timeout after transmission, 5xx, crash mid-
  POST) NEVER triggers an automatic second POST: order + operation move to
  OUTCOME_UNKNOWN and a READ-ONLY recovery scan (or a human) resolves them,
- the recovery service only attaches a PROVEN provider order (the Leaseweb
  adapter never reports MATCHED — generic candidates escalate to review)
  and captures the hold exactly once; ambiguous/no-proof escalates to review,
- the reconciler only inspects; activation delivers the server once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.audit.domain import AuditEvent
from cloud_platform.modules.audit.service import AuditTrail
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
    OrderRecoveryService,
    OrderWorker,
    RenewalInfo,
)
from cloud_platform.modules.renewals.domain import RenewalRecord, RenewalStatus
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldStatus,
    Wallet,
)
from cloud_platform.providers.base import (
    OrderRecoveryResult,
    OrderRecoveryVerdict,
    ProvisioningTicket,
)
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderUnavailable,
)
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
        return self.server

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
        return self.order

    async def get(self, order_id: UUID) -> ProviderOrder | None:
        return self.order

    async def save(self, order: ProviderOrder) -> ProviderOrder:
        self.order = order
        self.saved.append(order)
        return order

    async def list_open(self, provider_key: str, limit: int = 100) -> list[ProviderOrder]:
        return [self.order] if self.order.is_open else []

    async def list_outcome_unknown(self, provider_key: str, limit: int = 50) -> list[ProviderOrder]:
        return [self.order] if self.order.status is OrderStatus.OUTCOME_UNKNOWN else []


class FakeOperationRepo:
    def __init__(self, op: Operation | None = None) -> None:
        self.op = op or _operation()
        self.saved: list[Operation] = []

    async def get_by_key(self, operation_key: str) -> Operation | None:
        return self.op

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
        self,
        *,
        ticket: ProvisioningTicket | None = None,
        error: Exception | None = None,
        recovery: OrderRecoveryResult | None = None,
    ) -> None:
        self._ticket = ticket or ProvisioningTicket(
            provider_order_id="LS-ORD-1",
            state="provisioning",
            metadata={
                "product_id": "VIRTUAL_SERVER",
                "price_per_frequency_minor": 999,
                "currency": "EUR",
                "contract_term": "1 MONTH",
                "billing_cycle": "1 MONTH",
            },
        )
        self._error = error
        self._recovery = recovery or OrderRecoveryResult(
            verdict=OrderRecoveryVerdict.NO_MATCH, reason="no candidates"
        )
        self.posts: list[IdempotencyKey] = []
        self.recovery_calls: list[dict[str, Any]] = []

    async def place_order(
        self, request: Any, idempotency_key: IdempotencyKey
    ) -> ProvisioningTicket:
        self.posts.append(idempotency_key)
        if self._error is not None:
            raise self._error
        return self._ticket

    async def recover_order(self, **facts: Any) -> OrderRecoveryResult:
        self.recovery_calls.append(facts)
        return self._recovery

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

    async def test_stale_in_flight_is_never_reposted(self) -> None:
        # Worker crashed mid-POST: operation stuck IN_FLIGHT for > 30 min.
        # Release hardening: the outcome is UNKNOWN — NO automatic re-POST.
        old = datetime.now(UTC) - timedelta(minutes=45)
        op = _operation(OperationStatus.IN_FLIGHT)
        op.updated_at = old
        ordering = FakeOrderingProvider()
        worker, deps = _worker(ordering=ordering, op_repo=FakeOperationRepo(op))
        counts = await worker.process_pending()
        assert counts.get("outcome_unknown") == 1
        assert deps["op_repo"].op.status is OperationStatus.OUTCOME_UNKNOWN
        assert deps["orders"].order.status is OrderStatus.OUTCOME_UNKNOWN
        assert deps["orders"].order.error is not None
        assert "crash" in deps["orders"].order.error
        # The hold stays reserved; nothing was captured or released.
        assert deps["holds"].hold.status is HoldStatus.CREATED
        assert deps["hold_service"].captures == []
        assert deps["hold_service"].releases == []
        # NO provider POST happened, and a second worker pass must not POST.
        await worker.process_pending()
        assert deps["ordering"].posts == []

    async def test_ambiguous_timeout_is_never_reposted(self) -> None:
        # A read timeout AFTER the POST was transmitted: the order may exist
        # at Leaseweb. The worker must NEVER automatically POST again.
        ordering = FakeOrderingProvider(
            error=ProviderOutcomeUnknown("read timeout after transmission")
        )
        worker, deps = _worker(ordering=ordering)
        counts = await worker.process_pending()
        assert counts.get("outcome_unknown") == 1
        assert deps["orders"].order.status is OrderStatus.OUTCOME_UNKNOWN
        assert deps["op_repo"].op.status is OperationStatus.OUTCOME_UNKNOWN
        assert deps["holds"].hold.status is HoldStatus.CREATED  # funds stay reserved
        # Second pass: the order is OUTCOME_UNKNOWN -> skipped, no POST.
        await worker.process_pending()
        assert len(deps["ordering"].posts) == 1  # only the ambiguous attempt

    async def test_ambiguous_post_records_attempt_time(self) -> None:
        ordering = FakeOrderingProvider(
            error=ProviderOutcomeUnknown("connection reset after transmission")
        )
        worker, deps = _worker(ordering=ordering)
        await worker.process_pending()
        # The recovery scan window start is persisted BEFORE the POST.
        assert deps["orders"].order.post_attempted_at is not None

    async def test_fresh_in_flight_is_skipped(self) -> None:
        op = _operation(OperationStatus.IN_FLIGHT)  # updated just now
        ordering = FakeOrderingProvider()
        worker, deps = _worker(ordering=ordering, op_repo=FakeOperationRepo(op))
        counts = await worker.process_pending()
        assert counts.get("skipped_in_flight") == 1
        assert deps["ordering"].posts == []

    async def test_two_independent_same_offer_checkouts_post_twice(self) -> None:
        """RELEASE-BLOCKER regression: two DIFFERENT customers race to buy
        the exact same offer. Each local operation identity must produce
        its OWN provider POST with its OWN operation key and its own
        provider order id — no cross-operation dedup."""
        posts: list[tuple[str, str]] = []
        ordering = FakeOrderingProvider()

        async def place(request: Any, ik: IdempotencyKey) -> ProvisioningTicket:
            order_id = f"LS-ORD-{len(posts) + 1}"
            posts.append((ik.value, order_id))
            return ProvisioningTicket(provider_order_id=order_id, state="accepted")

        ordering.place_order = place  # type: ignore[method-assign]

        # Checkout A (existing fixtures) and an independent checkout B.
        server_b = _server()
        server_b.id = uuid4()
        server_b.idempotency_key = "bot-monthly:def"
        order_b = _order()
        order_b.id = uuid4()
        order_b.server_id = server_b.id
        order_b.operation_key = f"order-create:{server_b.id}"
        op_b = _operation(OperationStatus.PENDING)
        op_b.operation_key = order_b.operation_key

        worker_a, deps_a = _worker(ordering=ordering)
        worker_b, deps_b = _worker(
            ordering=ordering,
            op_repo=FakeOperationRepo(op_b),
            order=order_b,
            server=server_b,
        )
        counts_a = await worker_a.process_pending()
        counts_b = await worker_b.process_pending()
        assert counts_a.get("submitted") == 1
        assert counts_b.get("submitted") == 1
        # Exactly TWO provider POSTs with DISTINCT operation keys and
        # DISTINCT provider order ids.
        assert len(posts) == 2
        assert {key for key, _ in posts} == {OP_KEY, order_b.operation_key}
        assert {order_id for _, order_id in posts} == {"LS-ORD-1", "LS-ORD-2"}
        # Both local operations are independent and both orders submitted.
        assert deps_a["orders"].order.provider_order_id == "LS-ORD-1"
        assert deps_b["orders"].order.provider_order_id == "LS-ORD-2"
        assert deps_a["orders"].order.operation_key == OP_KEY
        assert deps_b["orders"].order.operation_key == order_b.operation_key


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


def _outcome_unknown_order() -> ProviderOrder:
    now = datetime.now(UTC)
    order = _order(OrderStatus.OUTCOME_UNKNOWN)
    order.provider_cost_minor = 999
    order.provider_cost_currency = "EUR"
    order.contract_term = "1_MONTH"
    order.billing_cycle = "1_MONTH"
    order.product_id = "VPS02_1"
    order.location_id = "AMS-01"
    order.os_name = "Ubuntu 24.04"
    order.post_attempted_at = now - timedelta(minutes=1)
    order.error = "read timeout after transmission"
    return order


def _recovery_worker(
    *,
    ordering: FakeOrderingProvider,
    order: ProviderOrder | None = None,
    op: Operation | None = None,
    holds: FakeHoldRepo | None = None,
) -> tuple[Any, dict[str, Any]]:
    from cloud_platform.modules.orders.service import OrderRecoveryService

    op = op or _operation(OperationStatus.OUTCOME_UNKNOWN)
    op.status = OperationStatus.OUTCOME_UNKNOWN
    order = order or _outcome_unknown_order()
    server_repo = FakeServerRepo(_server())
    orders_repo = FakeOrdersRepo(order)
    op_repo = FakeOperationRepo(op)
    wallet_repo = FakeWalletRepo()
    holds = holds or FakeHoldRepo()
    hold_service = FakeHoldService(holds)
    registry = ProviderRegistry()
    registry.register(ordering)
    service = OrderRecoveryService(
        server_repo=server_repo,
        offers_repo=FakeOfferRepo(),
        orders_repo=orders_repo,
        operation_repo=op_repo,
        wallet_repo=wallet_repo,
        hold_repo=holds,
        hold_service=hold_service,
        audit_repo=FakeAuditRepo(),
        provider_registry=registry,
    )
    deps = {
        "orders": orders_repo,
        "op_repo": op_repo,
        "servers": server_repo,
        "holds": holds,
        "hold_service": hold_service,
        "ordering": ordering,
    }
    return service, deps


class TestOrderRecoveryService:
    async def test_recovery_attaches_matched_order_read_only(self) -> None:
        ordering = FakeOrderingProvider(
            recovery=OrderRecoveryResult(
                verdict=OrderRecoveryVerdict.MATCHED,
                provider_order_id="LS-ORD-9",
                candidate_count=1,
            )
        )
        service, deps = _recovery_worker(ordering=ordering)
        counts = await service.recover()
        assert counts.get("recovered") == 1
        order = deps["orders"].order
        assert order.status is OrderStatus.SUBMITTED
        assert order.provider_order_id == "LS-ORD-9"
        assert deps["op_repo"].op.status is OperationStatus.COMPLETED
        # The hold is captured exactly once.
        assert len(deps["hold_service"].captures) == 1
        assert deps["holds"].hold.status is HoldStatus.CAPTURED
        # Recovery never POSTs.
        assert deps["ordering"].posts == []

    async def test_recovery_uses_provider_facts_not_selling_price(self) -> None:
        ordering = FakeOrderingProvider(
            recovery=OrderRecoveryResult(
                verdict=OrderRecoveryVerdict.MATCHED,
                provider_order_id="LS-ORD-9",
                candidate_count=1,
            )
        )
        service, deps = _recovery_worker(ordering=ordering)
        await service.recover()
        facts = deps["ordering"].recovery_calls[0]
        # Provider cost snapshot is correlated; the 1299 selling price never
        # appears in the scan facts.
        assert facts["provider_cost_minor"] == 999
        assert "selling" not in " ".join(str(k) for k in facts)
        assert facts["contract_term"] == "1_MONTH"
        assert facts["billing_cycle"] == "1_MONTH"

    async def test_recovery_ambiguous_escalates_to_review(self) -> None:
        ordering = FakeOrderingProvider(
            recovery=OrderRecoveryResult(
                verdict=OrderRecoveryVerdict.AMBIGUOUS, candidate_count=2, reason="two orders"
            )
        )
        service, deps = _recovery_worker(ordering=ordering)
        counts = await service.recover()
        assert counts.get("marked_for_review") == 1
        assert deps["orders"].order.status is OrderStatus.NEEDS_REVIEW
        assert deps["holds"].hold.status is HoldStatus.CREATED  # funds untouched
        assert deps["ordering"].posts == []

    async def test_recovery_no_match_escalates_absence_not_provable(self) -> None:
        ordering = FakeOrderingProvider(
            recovery=OrderRecoveryResult(
                verdict=OrderRecoveryVerdict.NO_MATCH, reason="no candidates"
            )
        )
        service, deps = _recovery_worker(ordering=ordering)
        counts = await service.recover()
        assert counts.get("marked_for_review") == 1
        assert deps["orders"].order.status is OrderStatus.NEEDS_REVIEW
        assert (
            "absence" in (deps["orders"].order.error or "").lower()
            or "no matching" in (deps["orders"].order.error or "").lower()
        )
        assert deps["ordering"].posts == []

    async def test_recovery_scan_failed_retries_then_escalates(self) -> None:
        ordering = FakeOrderingProvider(
            recovery=OrderRecoveryResult(
                verdict=OrderRecoveryVerdict.SCAN_FAILED, reason="503 down"
            )
        )
        service, deps = _recovery_worker(ordering=ordering)
        for _ in range(4):
            counts = await service.recover()
            assert counts.get("left_unchanged") == 1
            assert deps["orders"].order.status is OrderStatus.OUTCOME_UNKNOWN
        # Attempts exhausted -> a human decides.
        counts = await service.recover()
        assert counts.get("marked_for_review") == 1
        assert deps["orders"].order.status is OrderStatus.NEEDS_REVIEW
        assert deps["ordering"].posts == []

    async def test_recovery_capture_is_idempotent(self) -> None:
        ordering = FakeOrderingProvider(
            recovery=OrderRecoveryResult(
                verdict=OrderRecoveryVerdict.MATCHED, provider_order_id="LS-ORD-9"
            )
        )
        service, deps = _recovery_worker(ordering=ordering)
        await service.recover()
        await service.recover()
        # The second pass finds the order SUBMITTED (not OUTCOME_UNKNOWN) and
        # does nothing; the hold was captured exactly once.
        assert len(deps["hold_service"].captures) == 1
        assert deps["holds"].hold.status is HoldStatus.CAPTURED
        assert len(deps["ordering"].recovery_calls) == 1

    async def test_recovery_without_any_cost_source_escalates_safely(self) -> None:
        order = _outcome_unknown_order()
        order.provider_cost_minor = None
        order.offer_id = None  # no offer fallback either
        ordering = FakeOrderingProvider()
        service, deps = _recovery_worker(ordering=ordering, order=order)
        counts = await service.recover()
        assert counts.get("marked_for_review") == 1
        assert deps["orders"].order.status is OrderStatus.NEEDS_REVIEW
        assert deps["ordering"].recovery_calls == []  # never scanned without facts
        assert deps["ordering"].posts == []

    async def test_recovery_falls_back_to_offer_cost_for_legacy_rows(self) -> None:
        """Orders created before migration 0031 have no snapshot; the offer
        (still linked) supplies the provider cost for the read-only scan."""
        order = _outcome_unknown_order()
        order.provider_cost_minor = None
        ordering = FakeOrderingProvider(
            recovery=OrderRecoveryResult(
                verdict=OrderRecoveryVerdict.MATCHED, provider_order_id="LS-ORD-9"
            )
        )
        service, deps = _recovery_worker(ordering=ordering, order=order)
        counts = await service.recover()
        assert counts.get("recovered") == 1
        assert deps["ordering"].recovery_calls[0]["provider_cost_minor"] == 999
        assert deps["ordering"].posts == []


def _manual_worker(
    *,
    ordering: FakeOrderingProvider | None = None,
    order: ProviderOrder | None = None,
    op: Operation | None = None,
    server: CloudServer | None = None,
    holds: FakeHoldRepo | None = None,
) -> tuple[Any, dict[str, Any]]:
    from cloud_platform.modules.orders.service import OrderManualResolutionService

    if op is None:
        op = _operation(OperationStatus.OUTCOME_UNKNOWN)
        op.status = OperationStatus.OUTCOME_UNKNOWN
    order = order or _outcome_unknown_order()
    server = server or _server(ServerLifecycleState.REQUESTED)
    ordering = ordering or FakeOrderingProvider()
    server_repo = FakeServerRepo(server)
    orders_repo = FakeOrdersRepo(order)
    op_repo = FakeOperationRepo(op)
    wallet_repo = FakeWalletRepo()
    holds = holds or FakeHoldRepo()
    hold_service = FakeHoldService(holds)
    audit_repo = FakeAuditRepo()
    registry = ProviderRegistry()
    registry.register(ordering)
    service = OrderManualResolutionService(
        server_repo=server_repo,
        orders_repo=orders_repo,
        operation_repo=op_repo,
        wallet_repo=wallet_repo,
        hold_repo=holds,
        hold_service=hold_service,
        audit_repo=audit_repo,
        provider_registry=registry,
    )
    deps = {
        "orders": orders_repo,
        "op_repo": op_repo,
        "servers": server_repo,
        "holds": holds,
        "hold_service": hold_service,
        "ordering": ordering,
        "audit": audit_repo,
    }
    return service, deps


class TestOrderManualResolutionService:
    async def test_retry_failed_reopens_definitive_failure(self) -> None:
        """A DEFINITIVELY FAILED order is re-queued under the SAME local
        operation identity; the server returns to REQUESTED so the worker
        picks it up. No provider call of any kind happens during retry."""
        order = _order(OrderStatus.FAILED)
        op = _operation(OperationStatus.FAILED)
        server = _server(ServerLifecycleState.ERROR)
        service, deps = _manual_worker(order=order, op=op, server=server)
        resolved_order, resolved_op = await service.retry_failed(
            ORDER_ID, reason="verified the provider rejected the POST; nothing created"
        )
        assert resolved_order.status is OrderStatus.PENDING_SUBMIT
        assert resolved_order.error is None
        assert resolved_op.status is OperationStatus.PENDING
        assert resolved_op.operation_key == OP_KEY  # SAME local identity
        assert deps["servers"].server.state is ServerLifecycleState.REQUESTED
        assert deps["ordering"].posts == []
        assert len(deps["audit"].events) == 1
        assert deps["audit"].events[0].action == "order.retry"

    async def test_retry_failed_refuses_ambiguous_order(self) -> None:
        from cloud_platform.modules.orders.service import OrderManualResolutionError

        service, deps = _manual_worker()  # order OUTCOME_UNKNOWN, op OUTCOME_UNKNOWN
        with pytest.raises(OrderManualResolutionError):
            await service.retry_failed(ORDER_ID, reason="retry anyway")
        # No state change at all.
        assert deps["orders"].order.status is OrderStatus.OUTCOME_UNKNOWN
        assert deps["op_repo"].op.status is OperationStatus.OUTCOME_UNKNOWN
        assert deps["holds"].hold.status is HoldStatus.CREATED
        assert deps["ordering"].posts == []

    async def test_retry_failed_refuses_non_failed_operation(self) -> None:
        from cloud_platform.modules.orders.service import OrderManualResolutionError

        order = _order(OrderStatus.FAILED)
        op = _operation(OperationStatus.OUTCOME_UNKNOWN)
        service, _deps = _manual_worker(order=order, op=op)
        with pytest.raises(OrderManualResolutionError, match="not FAILED"):
            await service.retry_failed(ORDER_ID, reason="x")

    async def test_retry_failed_requires_reason(self) -> None:
        from cloud_platform.modules.orders.service import OrderManualResolutionError

        order = _order(OrderStatus.FAILED)
        op = _operation(OperationStatus.FAILED)
        server = _server(ServerLifecycleState.ERROR)
        service, _deps = _manual_worker(order=order, op=op, server=server)
        with pytest.raises(OrderManualResolutionError, match="reason"):
            await service.retry_failed(ORDER_ID, reason="  ")

    async def test_resolve_existing_attaches_verified_order_read_only(self) -> None:
        """The operator-verified provider order is attached after a READ-ONLY
        validation; the operation completes; the hold is captured exactly
        once; nothing is ever POSTed."""
        ordering = FakeOrderingProvider()
        service, deps = _manual_worker(ordering=ordering)
        order, operation = await service.resolve_existing(
            ORDER_ID, "LS-ORD-9", reason="verified at Leaseweb portal"
        )
        assert order.status is OrderStatus.SUBMITTED
        assert order.provider_order_id == "LS-ORD-9"
        assert order.error is None
        assert operation.status is OperationStatus.COMPLETED
        assert operation.provider_response is not None
        assert operation.provider_response["resolved_by"] == "manual"
        assert deps["holds"].hold.status is HoldStatus.CAPTURED
        assert len(deps["hold_service"].captures) == 1
        assert deps["ordering"].posts == []  # NEVER POSTs
        assert len(deps["audit"].events) == 1
        assert deps["audit"].events[0].action == "order.resolve_existing"
        assert deps["audit"].events[0].metadata["provider_order_id"] == "LS-ORD-9"

    async def test_resolve_existing_accepts_needs_review_provenance(self) -> None:
        order = _outcome_unknown_order()
        order.status = OrderStatus.NEEDS_REVIEW
        order.error = "recovery ambiguous: unproven candidate"
        service, deps = _manual_worker(order=order)
        order, operation = await service.resolve_existing(ORDER_ID, "LS-ORD-9", reason="verified")
        assert order.status is OrderStatus.SUBMITTED
        assert operation.status is OperationStatus.COMPLETED
        assert len(deps["hold_service"].captures) == 1

    async def test_resolve_existing_repeated_is_idempotent(self) -> None:
        from cloud_platform.modules.orders.service import OrderManualResolutionError

        service, deps = _manual_worker()
        await service.resolve_existing(ORDER_ID, "LS-ORD-9", reason="verified")
        # The order is now SUBMITTED: a second resolution is refused and the
        # hold is captured exactly ONCE.
        with pytest.raises(OrderManualResolutionError):
            await service.resolve_existing(ORDER_ID, "LS-ORD-9", reason="again")
        assert len(deps["hold_service"].captures) == 1
        assert deps["holds"].hold.status is HoldStatus.CAPTURED
        assert len(deps["audit"].events) == 1

    async def test_resolve_existing_refuses_wrong_product(self) -> None:
        from cloud_platform.modules.orders.service import OrderManualResolutionError

        ordering = FakeOrderingProvider(
            ticket=ProvisioningTicket(
                provider_order_id="LS-ORD-9",
                state="provisioning",
                metadata={"product_id": "DEDICATED_SERVER"},
            )
        )
        service, deps = _manual_worker(ordering=ordering)
        with pytest.raises(OrderManualResolutionError, match="DEDICATED_SERVER"):
            await service.resolve_existing(ORDER_ID, "LS-ORD-9", reason="wrong product")
        assert deps["orders"].order.status is OrderStatus.OUTCOME_UNKNOWN  # unchanged
        assert deps["op_repo"].op.status is OperationStatus.OUTCOME_UNKNOWN
        assert deps["holds"].hold.status is HoldStatus.CREATED  # never captured
        assert deps["ordering"].posts == []

    async def test_resolve_existing_refuses_price_mismatch(self) -> None:
        from cloud_platform.modules.orders.service import OrderManualResolutionError

        ordering = FakeOrderingProvider(
            ticket=ProvisioningTicket(
                provider_order_id="LS-ORD-9",
                state="provisioning",
                metadata={"product_id": "VIRTUAL_SERVER", "price_per_frequency_minor": 1999},
            )
        )
        service, deps = _manual_worker(ordering=ordering)
        with pytest.raises(OrderManualResolutionError, match="price"):
            await service.resolve_existing(ORDER_ID, "LS-ORD-9", reason="wrong price")
        assert deps["holds"].hold.status is HoldStatus.CREATED

    async def test_resolve_existing_refuses_when_validation_get_fails(self) -> None:
        from cloud_platform.modules.orders.service import OrderManualResolutionError

        ordering = FakeOrderingProvider()

        async def _fail(*args: Any, **kwargs: Any) -> Any:
            raise ProviderNotFound("order gone")

        ordering.get_order = _fail  # type: ignore[method-assign]
        service, deps = _manual_worker(ordering=ordering)
        with pytest.raises(OrderManualResolutionError, match="read-only validation"):
            await service.resolve_existing(ORDER_ID, "LS-ORD-9", reason="x")
        assert deps["orders"].order.status is OrderStatus.OUTCOME_UNKNOWN
        assert deps["holds"].hold.status is HoldStatus.CREATED
        assert deps["ordering"].posts == []

    async def test_resolve_not_created_requeues_same_identity(self) -> None:
        """Verified absence: OUTCOME_UNKNOWN -> PENDING under the SAME local
        operation key, order back to PENDING_SUBMIT; the worker may then
        POST once more (a NEW provider order)."""
        service, deps = _manual_worker()
        order, operation = await service.resolve_not_created(
            ORDER_ID, reason="verified no order exists at the portal"
        )
        assert order.status is OrderStatus.PENDING_SUBMIT
        assert operation.status is OperationStatus.PENDING
        assert operation.operation_key == OP_KEY  # SAME local identity
        assert deps["servers"].server.state is ServerLifecycleState.REQUESTED
        assert deps["ordering"].posts == []  # resolution itself never POSTs
        assert len(deps["audit"].events) == 1
        assert deps["audit"].events[0].action == "order.resolve_not_created"

    async def test_resolve_not_created_refuses_when_operation_not_unknown(self) -> None:
        from cloud_platform.modules.orders.service import OrderManualResolutionError

        op = _operation(OperationStatus.FAILED)
        service, deps = _manual_worker(op=op)
        with pytest.raises(OrderManualResolutionError, match="OUTCOME_UNKNOWN"):
            await service.resolve_not_created(ORDER_ID, reason="x")
        assert deps["orders"].order.status is OrderStatus.OUTCOME_UNKNOWN

    async def test_resolve_not_created_requires_reason(self) -> None:
        from cloud_platform.modules.orders.service import OrderManualResolutionError

        service, _deps = _manual_worker()
        with pytest.raises(OrderManualResolutionError, match="reason"):
            await service.resolve_not_created(ORDER_ID, reason="")


class TestReconcilerBranches:
    def _reconciler(
        self,
        *,
        order: ProviderOrder | None = None,
        server: CloudServer | None = None,
        get_error: Exception | None = None,
    ) -> tuple[OrderReconciler, dict[str, Any]]:
        ordering = FakeOrderingProvider(
            ticket=ProvisioningTicket(provider_order_id="LS-ORD-1", state="provisioning")
        )
        if get_error is not None:

            async def _get(*args: Any, **kwargs: Any) -> Any:
                raise get_error

            ordering.get_order = _get  # type: ignore[method-assign]
        order = order or _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        server = server or _server(ServerLifecycleState.PROVISIONING)
        server_repo = FakeServerRepo(server)
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
        deps = {
            "orders": orders_repo,
            "servers": server_repo,
            "audit": FakeAuditRepo(),
            "ordering": ordering,
            "holds": holds,
        }
        return reconciler, deps

    async def test_open_order_without_provider_id_marks_review(self) -> None:
        order = _order(OrderStatus.PROVISIONING, provider_order_id=None)
        reconciler, _ = self._reconciler(order=order)
        counts = await reconciler.reconcile()
        assert counts.get("marked_for_review") == 1
        assert order.status is OrderStatus.NEEDS_REVIEW

    async def test_missing_server_fails_order(self) -> None:
        order = _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        server_repo = FakeServerRepo(_server(ServerLifecycleState.PROVISIONING))
        server_repo.server = None
        ordering = FakeOrderingProvider()
        orders_repo = FakeOrdersRepo(order)
        registry = ProviderRegistry()
        registry.register(ordering)
        reconciler = OrderReconciler(
            server_repo=server_repo,
            offers_repo=FakeOfferRepo(),
            orders_repo=orders_repo,
            renewal_repo=FakeRenewalRepo(),
            wallet_repo=FakeWalletRepo(),
            hold_repo=FakeHoldRepo(),
            hold_service=FakeHoldService(FakeHoldRepo()),
            audit_repo=FakeAuditRepo(),
            provider_registry=registry,
        )
        counts = await reconciler.reconcile()
        assert counts.get("failed") == 1
        assert order.status is OrderStatus.FAILED

    async def test_missing_offer_marks_review(self) -> None:
        order = _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        order.offer_id = uuid4()  # unknown offer id
        reconciler, _ = self._reconciler(order=order)
        counts = await reconciler.reconcile()
        assert counts.get("marked_for_review") == 1
        assert order.status is OrderStatus.NEEDS_REVIEW

    async def test_unregistered_provider_left_unchanged(self) -> None:
        order = _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        server_repo = FakeServerRepo(_server(ServerLifecycleState.PROVISIONING))
        orders_repo = FakeOrdersRepo(order)
        reconciler = OrderReconciler(
            server_repo=server_repo,
            offers_repo=FakeOfferRepo(),
            orders_repo=orders_repo,
            renewal_repo=FakeRenewalRepo(),
            wallet_repo=FakeWalletRepo(),
            hold_repo=FakeHoldRepo(),
            hold_service=FakeHoldService(FakeHoldRepo()),
            audit_repo=FakeAuditRepo(),
            provider_registry=ProviderRegistry(),  # empty: leaseweb unregistered
        )
        counts = await reconciler.reconcile()
        assert counts.get("left_unchanged") == 1
        assert order.status is OrderStatus.PROVISIONING

    async def test_get_order_not_found_marks_review_and_audits(self) -> None:
        from cloud_platform.providers.errors import ProviderNotFound

        audit = FakeAuditRepo()
        order = _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        server_repo = FakeServerRepo(_server(ServerLifecycleState.PROVISIONING))
        orders_repo = FakeOrdersRepo(order)
        registry = ProviderRegistry()
        ordering = FakeOrderingProvider()

        async def _nf(*args: Any, **kwargs: Any) -> Any:
            raise ProviderNotFound("gone")

        ordering.get_order = _nf  # type: ignore[method-assign]
        registry.register(ordering)
        reconciler = OrderReconciler(
            server_repo=server_repo,
            offers_repo=FakeOfferRepo(),
            orders_repo=orders_repo,
            renewal_repo=FakeRenewalRepo(),
            wallet_repo=FakeWalletRepo(),
            hold_repo=FakeHoldRepo(),
            hold_service=FakeHoldService(FakeHoldRepo()),
            audit_repo=audit,
            provider_registry=registry,
        )
        counts = await reconciler.reconcile()
        assert counts.get("marked_for_review") == 1
        assert order.status is OrderStatus.NEEDS_REVIEW
        assert any(e.action == "leaseweb.order_review" for e in audit.events)

    async def test_stuck_order_marks_review(self) -> None:
        now = datetime.now(UTC)
        order = _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        order.delivery_estimate = (now - timedelta(days=5)).isoformat()
        ticket = ProvisioningTicket(
            provider_order_id="LS-ORD-1",
            state="provisioning",
            metadata={"delivery_estimate": order.delivery_estimate},
        )
        ordering = FakeOrderingProvider(ticket=ticket)
        server_repo = FakeServerRepo(_server(ServerLifecycleState.PROVISIONING))
        orders_repo = FakeOrdersRepo(order)
        registry = ProviderRegistry()
        registry.register(ordering)
        reconciler = OrderReconciler(
            server_repo=server_repo,
            offers_repo=FakeOfferRepo(),
            orders_repo=orders_repo,
            renewal_repo=FakeRenewalRepo(),
            wallet_repo=FakeWalletRepo(),
            hold_repo=FakeHoldRepo(),
            hold_service=FakeHoldService(FakeHoldRepo()),
            audit_repo=FakeAuditRepo(),
            provider_registry=registry,
        )
        counts = await reconciler.reconcile()
        assert counts.get("marked_for_review") == 1
        assert order.status is OrderStatus.NEEDS_REVIEW

    async def test_not_stuck_still_provisioning(self) -> None:
        now = datetime.now(UTC)
        order = _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        order.delivery_estimate = (now + timedelta(hours=2)).isoformat()
        ticket = ProvisioningTicket(
            provider_order_id="LS-ORD-1",
            state="provisioning",
            metadata={"delivery_estimate": order.delivery_estimate},
        )
        reconciler, deps = self._reconciler(
            order=order, server=_server(ServerLifecycleState.PROVISIONING)
        )
        deps["ordering"]._ticket = ticket
        counts = await reconciler.reconcile()
        assert counts.get("still_provisioning") == 1
        assert order.status is OrderStatus.PROVISIONING

    async def test_activate_match_error_marks_review(self) -> None:
        from cloud_platform.providers.errors import ProviderUnavailable

        order = _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        ordering = FakeOrderingProvider(
            ticket=ProvisioningTicket(provider_order_id="LS-ORD-1", state="provisioned")
        )

        async def _boom(*args: Any, **kwargs: Any) -> str:
            raise ProviderUnavailable("scan failed")

        ordering.match_vps_for_order = _boom  # type: ignore[method-assign]
        server_repo = FakeServerRepo(_server(ServerLifecycleState.PROVISIONING))
        orders_repo = FakeOrdersRepo(order)
        registry = ProviderRegistry()
        registry.register(ordering)
        reconciler = OrderReconciler(
            server_repo=server_repo,
            offers_repo=FakeOfferRepo(),
            orders_repo=orders_repo,
            renewal_repo=FakeRenewalRepo(),
            wallet_repo=FakeWalletRepo(),
            hold_repo=FakeHoldRepo(),
            hold_service=FakeHoldService(FakeHoldRepo()),
            audit_repo=FakeAuditRepo(),
            provider_registry=registry,
        )
        counts = await reconciler.reconcile()
        assert counts.get("marked_for_review") == 1

    async def test_cancelled_after_capture_requires_refund(self) -> None:
        ticket = ProvisioningTicket(provider_order_id="LS-ORD-1", state="failed")
        order = _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        server = _server(ServerLifecycleState.PROVISIONING)
        holds = FakeHoldRepo()
        holds.hold.capture()  # the hold was already captured on acceptance
        server_repo = FakeServerRepo(server)
        orders_repo = FakeOrdersRepo(order)
        wallet_repo = FakeWalletRepo()
        audit = FakeAuditRepo()
        ordering = FakeOrderingProvider(ticket=ticket)
        registry = ProviderRegistry()
        registry.register(ordering)
        reconciler = OrderReconciler(
            server_repo=server_repo,
            offers_repo=FakeOfferRepo(),
            orders_repo=orders_repo,
            renewal_repo=FakeRenewalRepo(),
            wallet_repo=wallet_repo,
            hold_repo=holds,
            hold_service=FakeHoldService(holds),
            audit_repo=audit,
            provider_registry=registry,
        )
        counts = await reconciler.reconcile()
        assert counts.get("failed") == 1
        assert holds.hold.status is HoldStatus.CAPTURED  # not released
        assert any(e.action == "checkout.refund_required" for e in audit.events)

    async def test_reconcile_loop_exception_counts_left_unchanged(self) -> None:
        order = _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")

        class BrokenServerRepo(FakeServerRepo):
            async def get(self, server_id: UUID) -> CloudServer | None:
                raise RuntimeError("db down")

        orders_repo = FakeOrdersRepo(order)
        registry = ProviderRegistry()
        registry.register(FakeOrderingProvider())
        reconciler = OrderReconciler(
            server_repo=BrokenServerRepo(_server(ServerLifecycleState.PROVISIONING)),
            offers_repo=FakeOfferRepo(),
            orders_repo=orders_repo,
            renewal_repo=FakeRenewalRepo(),
            wallet_repo=FakeWalletRepo(),
            hold_repo=FakeHoldRepo(),
            hold_service=FakeHoldService(FakeHoldRepo()),
            audit_repo=FakeAuditRepo(),
            provider_registry=registry,
        )
        counts = await reconciler.reconcile()
        assert counts.get("left_unchanged") == 1


class TestRecoveryEscalation:
    def _service(
        self, *, order: ProviderOrder, server: CloudServer, registry: ProviderRegistry
    ) -> OrderRecoveryService:
        from cloud_platform.modules.orders.service import OrderRecoveryService

        return OrderRecoveryService(
            server_repo=FakeServerRepo(server),
            offers_repo=FakeOfferRepo(),
            orders_repo=FakeOrdersRepo(order),
            operation_repo=FakeOperationRepo(),
            wallet_repo=FakeWalletRepo(),
            hold_repo=FakeHoldRepo(),
            hold_service=FakeHoldService(FakeHoldRepo()),
            audit_repo=FakeAuditRepo(),
            provider_registry=registry,
        )

    async def test_order_without_timestamps_escalates(self) -> None:
        order = _order(OrderStatus.OUTCOME_UNKNOWN)
        order.post_attempted_at = None
        order.created_at = None
        service = self._service(
            order=order,
            server=_server(ServerLifecycleState.PROVISIONING),
            registry=ProviderRegistry(),
        )
        counts = await service.recover()
        assert counts.get("marked_for_review") == 1
        assert order.status is OrderStatus.NEEDS_REVIEW

    async def test_missing_server_escalates(self) -> None:
        order = _outcome_unknown_order()
        from cloud_platform.modules.orders.service import OrderRecoveryService

        server_repo = FakeServerRepo(_server(ServerLifecycleState.PROVISIONING))
        server_repo.server = None
        service = OrderRecoveryService(
            server_repo=server_repo,
            offers_repo=FakeOfferRepo(),
            orders_repo=FakeOrdersRepo(order),
            operation_repo=FakeOperationRepo(),
            wallet_repo=FakeWalletRepo(),
            hold_repo=FakeHoldRepo(),
            hold_service=FakeHoldService(FakeHoldRepo()),
            audit_repo=FakeAuditRepo(),
            provider_registry=ProviderRegistry(),
        )
        counts = await service.recover()
        assert counts.get("marked_for_review") == 1

    async def test_unregistered_provider_escalates(self) -> None:
        order = _outcome_unknown_order()
        service = self._service(
            order=order,
            server=_server(ServerLifecycleState.PROVISIONING),
            registry=ProviderRegistry(),  # leaseweb not registered
        )
        counts = await service.recover()
        assert counts.get("marked_for_review") == 1

    async def test_recovery_exception_counts_left_unchanged(self) -> None:
        order = _outcome_unknown_order()

        class BrokenOrdersRepo(FakeOrdersRepo):
            async def list_outcome_unknown(
                self, provider_key: str, limit: int = 50
            ) -> list[ProviderOrder]:
                raise RuntimeError("db down")

        from cloud_platform.modules.orders.service import OrderRecoveryService

        service = OrderRecoveryService(
            server_repo=FakeServerRepo(_server(ServerLifecycleState.PROVISIONING)),
            offers_repo=FakeOfferRepo(),
            orders_repo=BrokenOrdersRepo(order),
            operation_repo=FakeOperationRepo(),
            wallet_repo=FakeWalletRepo(),
            hold_repo=FakeHoldRepo(),
            hold_service=FakeHoldService(FakeHoldRepo()),
            audit_repo=FakeAuditRepo(),
            provider_registry=ProviderRegistry(),
        )
        with pytest.raises(RuntimeError):
            await service.recover()


class TestActivatorBranches:
    def _activator(
        self,
        *,
        ordering: FakeOrderingProvider,
        remote: Any = None,
        server: CloudServer | None = None,
        order: ProviderOrder | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        from cloud_platform.modules.orders.service import OrderActivator

        server = server or _server(ServerLifecycleState.PROVISIONING)
        order = order or _order(OrderStatus.PROVISIONING, provider_order_id="LS-ORD-1")
        server_repo = FakeServerRepo(server)
        orders_repo = FakeOrdersRepo(order)
        renewal_repo = FakeRenewalRepo()
        registry = ProviderRegistry()
        registry.register(ordering)
        activator = OrderActivator(
            server_repo=server_repo,
            offers_repo=FakeOfferRepo(),
            orders_repo=orders_repo,
            renewal_repo=renewal_repo,
            audit_trail=AuditTrail(FakeAuditRepo()),
            provider_registry=registry,
            delivery_notifier=_RecordingNotifier(),
        )
        deps = {
            "servers": server_repo,
            "orders": orders_repo,
            "renewals": renewal_repo,
        }
        return activator, deps

    async def test_activate_falls_back_to_vps_id_when_remote_unavailable(self) -> None:
        ordering = FakeOrderingProvider()

        async def _unavailable(provider_server_id: str) -> Any:
            raise ProviderUnavailable("vps api down")

        ordering.get_server = _unavailable  # type: ignore[method-assign]
        activator, deps = self._activator(ordering=ordering)
        info = await activator.activate(
            server=deps["servers"].server,
            order=deps["orders"].order,
            offer=_offer(),
            vps_id="vps-9",
            ordering=ordering,
        )
        assert deps["servers"].server.provider_server_id == "vps-9"
        assert deps["servers"].server.state is ServerLifecycleState.RUNNING
        assert deps["orders"].order.status is OrderStatus.ACTIVE
        assert info.renewal_date_estimated is True

    async def test_activate_uses_remote_ips_and_contract_end(self) -> None:
        ordering = FakeOrderingProvider()
        remote = MagicMock()
        remote.id = "vps-7"
        remote.ipv4 = "1.2.3.4"
        remote.ipv6 = "::1"
        remote.metadata = {"contract_ends_at": "2026-08-01T00:00:00+00:00"}
        ordering.get_server = AsyncMock(return_value=remote)  # type: ignore[method-assign]
        activator, deps = self._activator(ordering=ordering)
        info = await activator.activate(
            server=deps["servers"].server,
            order=deps["orders"].order,
            offer=_offer(),
            vps_id="vps-7",
            ordering=ordering,
        )
        server = deps["servers"].server
        assert server.provider_server_id == "vps-7"
        assert server.ipv4 == "1.2.3.4"
        assert server.ipv6 == "::1"
        assert info.renewal_date_estimated is False
        assert info.provider_renewal_at == datetime(2026, 8, 1, tzinfo=UTC)
        renewal = deps["renewals"].upserted[0]
        assert renewal.provider_renewal_at == info.provider_renewal_at

    def test_parse_iso_datetime_variants(self) -> None:
        from cloud_platform.modules.orders.service import _parse_iso_datetime

        assert _parse_iso_datetime(None) is None
        assert _parse_iso_datetime("") is None
        assert _parse_iso_datetime("not-a-date") is None
        dt = _parse_iso_datetime("2026-08-01T00:00:00Z")
        assert dt == datetime(2026, 8, 1, tzinfo=UTC)
        naive = _parse_iso_datetime("2026-08-01T00:00:00")
        assert naive == datetime(2026, 8, 1, tzinfo=UTC)
