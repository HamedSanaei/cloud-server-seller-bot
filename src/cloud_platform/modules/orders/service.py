"""Leaseweb ordering worker + reconciler (LEASEWEB-MVP).

**Worker** (``OrderWorker``): executes PENDING_SUBMIT order intents. Each
intent is claimed through the operation ledger (atomic PENDING -> IN_FLIGHT),
so two workers can never POST the same order; the operation key is the
IdempotencyKey of the provider POST and is deterministic per server
(``order-create:{server_id}``). On provider acceptance (201) the provider
order id is persisted FIRST, then the wallet hold is captured exactly once
(``HoldService.capture_hold`` is idempotent, ledger key unique).

- Definitive provider rejection (4xx before acceptance): operation FAILED,
  order FAILED, server ERROR, hold RELEASED (funds return).
- Transient failure (timeout/5xx/429): operation re-queued with the SAME key
  — a re-send can never create a second order because the provider order id
  is only recorded after a successful POST and the get-before-create scan
  inside the adapter returns the earlier order.

**Reconciler** (``OrderReconciler``): polls SUBMITTED/PROVISIONING orders.
It NEVER POSTs — it only inspects orders and VPSes. When the order's
service turns ACTIVE it resolves the provisioned VPS (equipment id, else
datacenter+pack+startedAt matching; ambiguity -> NEEDS_REVIEW), writes the
server's provider id/IPs, transitions it RUNNING, creates the renewal
record (contract endsAt when available) and delivers the server details to
the owning user. Orders stuck past their delivery estimate go to
NEEDS_REVIEW for a human.

The activation step is shared (``OrderActivator``) so both paths converge
on one implementation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
    ServerRepository,
)
from cloud_platform.modules.offers.domain import SellableOffer, SellableOfferRepository
from cloud_platform.modules.operations.domain import (
    OperationRepository,
    OperationStatus,
)
from cloud_platform.modules.orders.domain import (
    OrderStatus,
    ProviderOrder,
    ProviderOrderRepository,
)
from cloud_platform.modules.renewals.domain import (
    RenewalRecord,
    RenewalRepository,
    RenewalStatus,
)
from cloud_platform.modules.wallet.domain import (
    HoldRepository,
    HoldStatus,
    WalletRepository,
)
from cloud_platform.modules.wallet.repository import HoldService
from cloud_platform.observability.metrics import metrics
from cloud_platform.providers.base import (
    CreateServerRequest,
    OrderingProvider,
    ProviderServer,
    ordering_support_of,
)
from cloud_platform.providers.errors import ProviderError, ProviderNotFound
from cloud_platform.providers.registry import ProviderRegistry
from cloud_platform.providers.retry import ErrorClass, classify_provider_error

logger = logging.getLogger(__name__)

#: How long an open order may sit without any provider movement before a
#: human is asked to look at it.
STUCK_ORDER_GRACE = timedelta(hours=72)

#: An IN_FLIGHT order-create operation older than this is presumed crashed:
#: it is re-queued with the SAME operation key and the adapter's
#: get-before-create scan deduplicates the provider POST.
STALE_IN_FLIGHT_GRACE = timedelta(minutes=30)

MONTHLY_ESTIMATE_DAYS = 30


class OrderWorkerOutcome(StrEnum):
    SUBMITTED = "submitted"
    REQUEUED = "requeued"
    FAILED = "failed"
    SKIPPED_IN_FLIGHT = "skipped_in_flight"
    SKIPPED_STATE = "skipped_state"
    ALREADY_ACTIVE = "already_active"


class ReconciliationOutcome(StrEnum):
    PROVISIONED = "provisioned"
    STILL_PROVISIONING = "still_provisioning"
    FAILED = "failed"
    MARKED_FOR_REVIEW = "marked_for_review"
    LEFT_UNCHANGED = "left_unchanged"


@dataclass(frozen=True, slots=True)
class RenewalInfo:
    """Renewal facts recorded when a server is provisioned."""

    provider_contract_id: str | None
    provider_order_ref: str | None
    purchased_at: datetime
    provider_renewal_at: datetime | None
    renewal_date_estimated: bool
    customer_price_minor: int
    currency: str


class OrderDeliveryNotifier(Protocol):
    """Delivers the provisioned server details to the owning user."""

    async def deliver(
        self, *, server: CloudServer, offer: SellableOffer, renewal: RenewalInfo
    ) -> None: ...


class _LoggingOrderDeliveryNotifier:
    """Default notifier: structured log line (Telegram integration replaces it)."""

    async def deliver(
        self, *, server: CloudServer, offer: SellableOffer, renewal: RenewalInfo
    ) -> None:
        logger.info(
            "monthly server delivered: server=%s user=%s provider_server=%s offer=%s",
            server.id,
            server.user_id,
            server.provider_server_id,
            offer.ref,
        )


def _parse_iso_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class OrderActivator:
    """Shared activation: order ACTIVE -> server RUNNING + renewal + delivery."""

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        offers_repo: SellableOfferRepository,
        orders_repo: ProviderOrderRepository,
        renewal_repo: RenewalRepository,
        audit_trail: AuditTrail,
        provider_registry: ProviderRegistry,
        delivery_notifier: OrderDeliveryNotifier,
    ) -> None:
        self._servers = server_repo
        self._offers = offers_repo
        self._orders = orders_repo
        self._renewals = renewal_repo
        self._audit = audit_trail
        self._registry = provider_registry
        self._delivery = delivery_notifier

    async def activate(
        self,
        *,
        server: CloudServer,
        order: ProviderOrder,
        offer: SellableOffer,
        vps_id: str,
        ordering: OrderingProvider,
    ) -> RenewalInfo:
        """Write the provider resource onto the server and finalize."""
        remote: ProviderServer | None = None
        try:
            remote = await ordering.get_server(vps_id)
        except ProviderError:
            pass
        if remote is not None:
            server.provider_server_id = remote.id
            server.ipv4 = remote.ipv4
            server.ipv6 = remote.ipv6
        else:
            server.provider_server_id = vps_id
        if server.state in (
            ServerLifecycleState.REQUESTED,
            ServerLifecycleState.PROVISIONING,
            ServerLifecycleState.ERROR,
        ):
            try:
                server.transition_to(ServerLifecycleState.RUNNING)
            except Exception:
                logger.exception("could not transition server %s to RUNNING", server.id)
        await self._servers.save(server)

        order.mark_active()
        await self._orders.save(order)

        renewal = await self._upsert_renewal(server, offer, order)
        logger.info(
            "leaseweb server provisioned: server=%s provider_server=%s ipv4=%s ipv6=%s",
            server.id,
            server.provider_server_id,
            server.ipv4,
            server.ipv6,
        )
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="leaseweb.server_provisioned",
            resource_type="server",
            resource_id=str(server.id),
            reason=f"provider server {server.provider_server_id} discovered",
            metadata={
                "provider_order_id": order.provider_order_id or "",
                "provider_server_id": server.provider_server_id or "",
                "ipv4": server.ipv4 or "",
                "ipv6": server.ipv6 or "",
            },
        )
        await self._delivery.deliver(server=server, offer=offer, renewal=renewal)
        return renewal

    async def _upsert_renewal(
        self, server: CloudServer, offer: SellableOffer, order: ProviderOrder
    ) -> RenewalInfo:
        contract_ends_at: object = None
        if server.provider_server_id:
            try:
                provider = self._registry.get(server.provider_key)
                remote = await provider.get_server(server.provider_server_id)
                if remote is not None:
                    contract_ends_at = remote.metadata.get("contract_ends_at")
            except Exception:
                logger.warning("renewal contract lookup failed for server %s", server.id)
        provider_renewal_at = _parse_iso_datetime(contract_ends_at)
        estimated = provider_renewal_at is None
        if provider_renewal_at is None:
            provider_renewal_at = (server.created_at or datetime.now(UTC)) + timedelta(
                days=MONTHLY_ESTIMATE_DAYS
            )
        info = RenewalInfo(
            provider_contract_id=order.provider_contract_id,
            provider_order_ref=order.provider_order_id,
            purchased_at=server.created_at or datetime.now(UTC),
            provider_renewal_at=provider_renewal_at,
            renewal_date_estimated=estimated,
            customer_price_minor=offer.selling_price_minor,
            currency=offer.selling_currency,
        )
        existing = await self._renewals.get(server.id)
        record = RenewalRecord(
            server_id=server.id,
            provider_contract_id=info.provider_contract_id,
            provider_order_ref=info.provider_order_ref,
            purchased_at=info.purchased_at,
            provider_renewal_at=info.provider_renewal_at,
            renewal_date_estimated=info.renewal_date_estimated,
            customer_price_minor=info.customer_price_minor,
            currency=info.currency,
            status=existing.status if existing is not None else RenewalStatus.ACTIVE,
            auto_charge_enabled=(existing.auto_charge_enabled if existing is not None else True),
        )
        await self._renewals.upsert(record)
        return info


class OrderWorker:
    """Executes PENDING_SUBMIT provider order intents exactly once each."""

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        offers_repo: SellableOfferRepository,
        orders_repo: ProviderOrderRepository,
        operation_repo: OperationRepository,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        hold_service: HoldService,
        audit_repo: AuditRepository,
        provider_registry: ProviderRegistry,
        renewal_repo: RenewalRepository,
        delivery_notifier: OrderDeliveryNotifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._servers = server_repo
        self._offers = offers_repo
        self._orders = orders_repo
        self._ops = operation_repo
        self._wallets = wallet_repo
        self._holds = hold_repo
        self._hold_service = hold_service
        self._audit = AuditTrail(audit_repo)
        self._registry = provider_registry
        self._renewals = renewal_repo
        self._delivery = delivery_notifier or _LoggingOrderDeliveryNotifier()
        self._now = clock or (lambda: datetime.now(UTC))

    def _activator(self) -> OrderActivator:
        return OrderActivator(
            server_repo=self._servers,
            offers_repo=self._offers,
            orders_repo=self._orders,
            renewal_repo=self._renewals,
            audit_trail=self._audit,
            provider_registry=self._registry,
            delivery_notifier=self._delivery,
        )

    async def process_pending(self, limit: int = 10) -> dict[OrderWorkerOutcome, int]:
        counts: dict[OrderWorkerOutcome, int] = {}

        def bump(outcome: OrderWorkerOutcome) -> None:
            counts[outcome] = counts.get(outcome, 0) + 1

        servers = (await self._servers.list_requested_prepaid())[:limit]
        for server in servers:
            try:
                bump(await self._process_server(server))
            except Exception:
                logger.exception("order worker failed for server %s", server.id)
                counts[OrderWorkerOutcome.REQUEUED] = counts.get(OrderWorkerOutcome.REQUEUED, 0) + 1
        return counts

    async def _process_server(self, server: CloudServer) -> OrderWorkerOutcome:
        order = await self._orders.get_by_server(server.id)
        if order is None:
            await self._fail_permanent(
                server, None, "provider order row missing for requested server"
            )
            return OrderWorkerOutcome.FAILED
        if order.status is OrderStatus.ACTIVE:
            return await self._recover_active(server, order)
        if order.status is not OrderStatus.PENDING_SUBMIT:
            return OrderWorkerOutcome.SKIPPED_STATE

        operation = await self._ops.get_by_key(order.operation_key)
        if operation is None:
            await self._fail_permanent(
                server, order, "operation ledger row missing for order intent"
            )
            return OrderWorkerOutcome.FAILED
        if operation.is_terminal:
            return OrderWorkerOutcome.SKIPPED_STATE
        if operation.status is OperationStatus.IN_FLIGHT:
            # A worker may have crashed mid-POST. After a grace period the
            # attempt is re-queued with the SAME key: the provider POST is
            # deduplicated by the adapter's get-before-create scan, so this
            # can never place a second order.
            updated = operation.updated_at or operation.created_at or self._now()
            if self._now() - updated <= STALE_IN_FLIGHT_GRACE:
                return OrderWorkerOutcome.SKIPPED_IN_FLIGHT
            operation.requeue("stale in-flight recovered after worker crash")
            operation.updated_at = self._now()
            await self._ops.save(operation)
            logger.warning(
                "operation %s re-queued after stale in-flight (server %s); "
                "re-attempt will use get-before-create",
                operation.id,
                server.id,
            )
            return OrderWorkerOutcome.REQUEUED
        claimed = await self._ops.claim(operation.id)
        if claimed is None:
            return OrderWorkerOutcome.SKIPPED_IN_FLIGHT

        offer = await self._offers.get(order.offer_id) if order.offer_id else None
        if offer is None:
            return await self._fail_permanent(
                server, order, "sellable offer row missing for order intent"
            )
        if server.os is None:
            return await self._fail_permanent(server, order, "server has no recorded OS")

        try:
            provider = self._registry.get(server.provider_key)
        except KeyError:
            return await self._fail_permanent(
                server, order, f"provider {server.provider_key!r} not configured"
            )
        ordering = ordering_support_of(provider)
        if ordering is None:
            return await self._fail_permanent(
                server, order, f"provider {server.provider_key!r} has no ordering port"
            )

        request = CreateServerRequest(
            name=f"srv-{server.id.hex[:8]}",
            plan_id=offer.product_id,
            image_id=server.os,
            location_id=offer.location_id,
            labels={
                "price_minor": str(offer.selling_price_minor),
                "platform_server_id": str(server.id),
            },
        )
        try:
            ticket = await ordering.place_order(request, IdempotencyKey(claimed.operation_key))
        except ProviderError as exc:
            if classify_provider_error(exc) is ErrorClass.RETRYABLE:
                claimed.requeue(str(exc))
                await self._ops.save(claimed)
                order.attempts += 1
                order.error = str(exc)
                await self._orders.save(order)
                logger.warning("order %s requeued (retryable): %s", order.id, exc)
                return OrderWorkerOutcome.REQUEUED
            return await self._fail_permanent(server, order, str(exc))

        # Accepted: persist the provider order id BEFORE moving money.
        order.mark_submitted(ticket.provider_order_id)
        order.attempts += 1
        order.error = None
        await self._orders.save(order)

        claimed.complete(
            {
                "provider_order_id": ticket.provider_order_id,
                "provider_state": ticket.state,
                "idempotency_key": claimed.operation_key,
            }
        )
        await self._ops.save(claimed)

        # Capture the hold exactly once (idempotent by key).
        await self._capture_hold(server)

        if server.state is ServerLifecycleState.REQUESTED:
            server.transition_to(ServerLifecycleState.PROVISIONING)
        await self._servers.save(server)
        if ticket.state == "provisioning":
            order.mark_provisioning(
                delivery_estimate=str(ticket.metadata.get("delivery_estimate") or "")
                if ticket.metadata.get("delivery_estimate")
                else None
            )
            await self._orders.save(order)

        logger.info(
            "leaseweb order accepted: server=%s order_id=%s provider_order=%s",
            server.id,
            order.id,
            ticket.provider_order_id,
        )
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="leaseweb.order_accepted",
            resource_type="server_order",
            resource_id=str(order.id),
            reason=f"provider order {ticket.provider_order_id} accepted",
            metadata={
                "server_id": str(server.id),
                "provider_order_id": ticket.provider_order_id,
                "operation_key": claimed.operation_key,
            },
        )
        return OrderWorkerOutcome.SUBMITTED

    async def _capture_hold(self, server: CloudServer) -> None:
        """Capture the checkout hold exactly once (idempotent)."""
        if server.idempotency_key is None:
            return
        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.id is None:
            logger.error("server %s: no wallet to capture hold", server.id)
            return
        from cloud_platform.modules.checkout.service import hold_key

        hold = await self._holds.get_by_idempotency(wallet.id, hold_key(server.idempotency_key))
        if hold is None or hold.id is None or hold.status is not HoldStatus.CREATED:
            return
        try:
            await self._hold_service.capture_hold(
                wallet.id, hold.id, hold_key(server.idempotency_key)
            )
        except Exception:
            logger.exception("failed to capture hold %s for server %s", hold.id, server.id)
        else:
            logger.info("hold %s captured for server %s (order accepted)", hold.id, server.id)

    async def _recover_active(
        self, server: CloudServer, order: ProviderOrder
    ) -> OrderWorkerOutcome:
        """An already-ACTIVE order: make sure the server row reflects it."""
        if server.state is ServerLifecycleState.RUNNING and server.provider_server_id:
            return OrderWorkerOutcome.ALREADY_ACTIVE
        offer = await self._offers.get(order.offer_id) if order.offer_id else None
        if offer is None:
            return OrderWorkerOutcome.SKIPPED_STATE
        try:
            provider = self._registry.get(server.provider_key)
        except KeyError:
            return OrderWorkerOutcome.SKIPPED_STATE
        ordering = ordering_support_of(provider)
        if ordering is None or not order.provider_order_id:
            return OrderWorkerOutcome.SKIPPED_STATE
        try:
            vps_id = await ordering.match_vps_for_order(
                order.provider_order_id,
                location=offer.location_id,
                product_name=offer.name,
                since=server.created_at or datetime.now(UTC),
            )
        except ProviderError:
            return OrderWorkerOutcome.SKIPPED_STATE
        await self._activator().activate(
            server=server, order=order, offer=offer, vps_id=vps_id, ordering=ordering
        )
        return OrderWorkerOutcome.ALREADY_ACTIVE

    async def _fail_permanent(
        self, server: CloudServer, order: ProviderOrder | None, reason: str
    ) -> OrderWorkerOutcome:
        if order is not None:
            order.mark_failed(reason)
            await self._orders.save(order)
            op = await self._ops.get_by_key(order.operation_key)
            if op is not None and not op.is_terminal:
                op.fail(reason)
                await self._ops.save(op)
        metrics.record_provisioning_failure("order_worker")
        if server.state in (
            ServerLifecycleState.REQUESTED,
            ServerLifecycleState.PROVISIONING,
        ):
            try:
                server.transition_to(ServerLifecycleState.ERROR)
                await self._servers.save(server)
            except Exception:
                logger.exception("failed to mark server %s ERROR", server.id)
        await self._release_hold(server)
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="leaseweb.order_failed",
            resource_type="server",
            resource_id=str(server.id),
            reason=reason,
            metadata={"order_id": str(order.id) if order is not None else ""},
        )
        logger.error("leaseweb order permanently failed for server %s: %s", server.id, reason)
        return OrderWorkerOutcome.FAILED

    async def _release_hold(self, server: CloudServer) -> None:
        if server.idempotency_key is None:
            return
        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.id is None:
            return
        from cloud_platform.modules.checkout.service import hold_key

        hold = await self._holds.get_by_idempotency(wallet.id, hold_key(server.idempotency_key))
        if hold is None or hold.id is None or hold.status is not HoldStatus.CREATED:
            return
        try:
            await self._hold_service.release_hold(
                wallet.id, hold.id, hold_key(server.idempotency_key)
            )
        except Exception:
            logger.exception("failed to release hold %s for server %s", hold.id, server.id)


class OrderReconciler:
    """Polls open provider orders; NEVER POSTs anything."""

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        offers_repo: SellableOfferRepository,
        orders_repo: ProviderOrderRepository,
        renewal_repo: RenewalRepository,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        hold_service: HoldService,
        audit_repo: AuditRepository,
        provider_registry: ProviderRegistry,
        delivery_notifier: OrderDeliveryNotifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._servers = server_repo
        self._offers = offers_repo
        self._orders = orders_repo
        self._renewals = renewal_repo
        self._wallets = wallet_repo
        self._holds = hold_repo
        self._hold_service = hold_service
        self._audit = AuditTrail(audit_repo)
        self._registry = provider_registry
        self._delivery = delivery_notifier or _LoggingOrderDeliveryNotifier()
        self._now = clock or (lambda: datetime.now(UTC))

    async def reconcile(
        self, provider_key: str = "leaseweb", limit: int = 100
    ) -> dict[ReconciliationOutcome, int]:
        counts: dict[ReconciliationOutcome, int] = {}

        def bump(outcome: ReconciliationOutcome) -> None:
            counts[outcome] = counts.get(outcome, 0) + 1
            metrics.record_reconciliation("orders", outcome.value)

        orders = (await self._orders.list_open(provider_key))[:limit]
        for order in orders:
            try:
                bump(await self._reconcile_one(order))
            except Exception:
                logger.exception("order reconciliation failed for order %s", order.id)
                counts[ReconciliationOutcome.LEFT_UNCHANGED] = (
                    counts.get(ReconciliationOutcome.LEFT_UNCHANGED, 0) + 1
                )
        return counts

    async def _reconcile_one(self, order: ProviderOrder) -> ReconciliationOutcome:
        if not order.provider_order_id:
            order.mark_needs_review("open order without a provider order id")
            await self._orders.save(order)
            return ReconciliationOutcome.MARKED_FOR_REVIEW

        server = await self._servers.get(order.server_id)
        if server is None:
            order.mark_failed("server row missing during order reconciliation")
            await self._orders.save(order)
            return ReconciliationOutcome.FAILED

        offer = await self._offers.get(order.offer_id) if order.offer_id else None
        if offer is None:
            order.mark_needs_review("sellable offer row missing for open order")
            await self._orders.save(order)
            return ReconciliationOutcome.MARKED_FOR_REVIEW

        try:
            provider = self._registry.get(server.provider_key)
        except KeyError:
            return ReconciliationOutcome.LEFT_UNCHANGED
        ordering = ordering_support_of(provider)
        if ordering is None:
            return ReconciliationOutcome.LEFT_UNCHANGED

        try:
            ticket = await ordering.get_order(order.provider_order_id)
        except ProviderError as exc:
            if isinstance(exc, ProviderNotFound):
                order.mark_needs_review(
                    f"provider order {order.provider_order_id} not found; "
                    "verify manually before releasing funds"
                )
                await self._orders.save(order)
                await self._audit.record_mutation(
                    actor_type=ActorType.SYSTEM,
                    actor_id=None,
                    action="leaseweb.order_review",
                    resource_type="server_order",
                    resource_id=str(order.id),
                    reason=str(exc),
                )
                return ReconciliationOutcome.MARKED_FOR_REVIEW
            # Transient (timeout/5xx/429): retry next round, never guess.
            return ReconciliationOutcome.LEFT_UNCHANGED

        order.last_polled_at = self._now()
        if ticket.state == "provisioned":
            return await self._activate(order, server, offer, ordering)

        if ticket.state == "failed":
            return await self._fail(order, server)

        # Still provisioning: refresh estimate; flag stuck orders for review.
        estimate = str(ticket.metadata.get("delivery_estimate") or "") or None
        order.mark_provisioning(delivery_estimate=estimate)
        await self._orders.save(order)
        if self._stuck(order):
            order.mark_needs_review("order stuck past its delivery estimate")
            await self._orders.save(order)
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="leaseweb.order_review",
                resource_type="server_order",
                resource_id=str(order.id),
                reason="delivery estimate exceeded",
                metadata={"delivery_estimate": order.delivery_estimate or ""},
            )
            return ReconciliationOutcome.MARKED_FOR_REVIEW
        return ReconciliationOutcome.STILL_PROVISIONING

    def _stuck(self, order: ProviderOrder) -> bool:
        """True when the order outlived its delivery estimate + grace."""
        estimate = order.delivery_estimate
        if not estimate:
            return False
        deadline = _parse_iso_datetime(estimate)
        if deadline is None:
            return False
        return self._now() > deadline + STUCK_ORDER_GRACE

    async def _activate(
        self,
        order: ProviderOrder,
        server: CloudServer,
        offer: SellableOffer,
        ordering: OrderingProvider,
    ) -> ReconciliationOutcome:
        try:
            vps_id = await ordering.match_vps_for_order(
                order.provider_order_id or "",
                location=offer.location_id,
                product_name=offer.name,
                since=server.created_at or datetime.now(UTC),
            )
        except ProviderNotFound:
            # Provider says ACTIVE but no VPS is visible yet: keep polling.
            order.mark_provisioning()
            await self._orders.save(order)
            return ReconciliationOutcome.STILL_PROVISIONING
        except ProviderError:
            order.mark_needs_review("VPS match failed during reconciliation")
            await self._orders.save(order)
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="leaseweb.order_review",
                resource_type="server_order",
                resource_id=str(order.id),
                reason="ambiguous or failed VPS match",
            )
            return ReconciliationOutcome.MARKED_FOR_REVIEW

        order.mark_provisioning()
        await self._orders.save(order)
        activator = OrderActivator(
            server_repo=self._servers,
            offers_repo=self._offers,
            orders_repo=self._orders,
            renewal_repo=self._renewals,
            audit_trail=self._audit,
            provider_registry=self._registry,
            delivery_notifier=self._delivery,
        )
        await activator.activate(
            server=server, order=order, offer=offer, vps_id=vps_id, ordering=ordering
        )
        return ReconciliationOutcome.PROVISIONED

    async def _fail(self, order: ProviderOrder, server: CloudServer) -> ReconciliationOutcome:
        order.mark_failed("provider cancelled the order before provisioning")
        await self._orders.save(order)
        metrics.record_provisioning_failure("order_reconciler")
        if server.state in (
            ServerLifecycleState.REQUESTED,
            ServerLifecycleState.PROVISIONING,
        ):
            try:
                server.transition_to(ServerLifecycleState.ERROR)
                await self._servers.save(server)
            except Exception:
                logger.exception("failed to mark server %s ERROR", server.id)
        if server.idempotency_key is not None:
            wallet = await self._wallets.get(server.user_id)
            if wallet is not None and wallet.id is not None:
                from cloud_platform.modules.checkout.service import hold_key

                hold = await self._holds.get_by_idempotency(
                    wallet.id, hold_key(server.idempotency_key)
                )
                if hold is not None and hold.id is not None and hold.status is HoldStatus.CREATED:
                    try:
                        await self._hold_service.release_hold(
                            wallet.id, hold.id, hold_key(server.idempotency_key)
                        )
                    except Exception:
                        logger.exception("failed to release hold %s", hold.id)
                elif hold is not None and hold.status is HoldStatus.CAPTURED:
                    logger.error(
                        "server %s: order cancelled AFTER hold capture; manual refund "
                        "required (hold %s)",
                        server.id,
                        hold.id,
                    )
                    await self._audit.record_mutation(
                        actor_type=ActorType.SYSTEM,
                        actor_id=None,
                        action="checkout.refund_required",
                        resource_type="server",
                        resource_id=str(server.id),
                        reason="provider cancelled an accepted order; refund required",
                        metadata={"hold_id": str(hold.id)},
                    )
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="leaseweb.order_failed",
            resource_type="server_order",
            resource_id=str(order.id),
            reason="provider cancelled the order",
            metadata={"server_id": str(server.id)},
        )
        return ReconciliationOutcome.FAILED
