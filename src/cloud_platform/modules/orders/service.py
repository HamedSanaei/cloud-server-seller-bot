"""Leaseweb ordering worker + reconciler + recovery (LEASEWEB-MVP).

**Worker** (``OrderWorker``): executes PENDING_SUBMIT order intents. Each
intent is claimed through the operation ledger (atomic PENDING -> IN_FLIGHT),
so two workers can never POST the same order; the operation key is the
IdempotencyKey of the provider POST and is deterministic per server
(``order-create:{server_id}``). On provider acceptance (201) the provider
order id is persisted FIRST (never lost), then the operation is completed,
then the LOCAL payment settlement runs (``OrderSettlementService``).

**Payment settlement barrier** (release hardening): provider acceptance and
local charge settlement are DIFFERENT facts. The wallet hold is captured
exactly once (``HoldService.capture_hold`` is idempotent with a
deterministic CHARGE ledger key, and re-running it repairs a missing CHARGE
without a second wallet debit). A capture failure NEVER marks the provider
order failed, NEVER releases the hold and NEVER re-POSTs: the order keeps
its provider order id, settlement stays pending, and the reconciler retries
the LOCAL capture only. Activation and delivery are blocked until
``settlement_status`` is COMPLETE (hold CAPTURED + exactly one CHARGE).

- Definitive provider rejection (4xx before acceptance): operation FAILED,
  order FAILED, server ERROR, hold RELEASED (funds return).
- Genuinely-not-sent transport failure (connect refused/timeout, pool
  timeout — the ONLY transport errors that prove nothing was transmitted):
  operation re-queued with the SAME key; the retry is a fresh POST of the
  same operation identity (no account-wide similarity heuristics — the
  ledger owns dedup).
- AMBIGUOUS outcome (read/write timeout, dropped connection, 5xx after
  transmission, mutating 429, missing orderId): the order and operation
  become OUTCOME_UNKNOWN — the worker NEVER automatically re-POSTs. A
  READ-ONLY recovery scan (or a human) resolves it, per the safety
  preference manual review > two VPSes.
- Stale IN_FLIGHT (worker died mid-POST, > 30 min): SAME treatment — the
  attempt becomes OUTCOME_UNKNOWN and is resolved read-only, never by a
  blind second POST.

**Dedup ownership** (release hardening): the durable local operation ledger
is the ONLY dedup mechanism for fresh orders. ``place_order`` always POSTs
for a claimed operation and never suppresses a POST because a similar
recent account order exists — two customers buying the same plan at the
same price produce two independent provider POSTs.

**Recovery** (``OrderRecoveryService``): resolves OUTCOME_UNKNOWN orders
with READ-ONLY provider scans only (``OrderingProvider.recover_order``).
The Leaseweb adapter NEVER reports MATCHED: the Orders API exposes no
identifier that proves an order belongs to a local operation (no exact
product id, location, OS or client reference), so any candidate count
escalates to NEEDS_REVIEW for a human. (MATCHED remains part of the port
contract for providers that CAN prove identity.)

**Reconciler** (``OrderReconciler``): polls SUBMITTED/PROVISIONING orders.
It NEVER POSTs — it only inspects orders and VPSes, and it also repairs
accepted-but-unsettled orders (local capture only). When the order's
service turns ACTIVE it resolves the provisioned VPS using the ONLY
provider-supported identity — the order's ``equipmentId`` confirmed by a
GET of that exact VPS (``match_vps_for_order``). Datacenter/pack similarity
against the account VPS list is diagnostic evidence only and NEVER
auto-attaches a VPS; without a usable ``equipmentId`` the order keeps
polling and escalates to NEEDS_REVIEW after a bounded grace period. Once
settled and provably provisioned it writes the server's provider id/IPs,
transitions it RUNNING, creates the renewal record (contract endsAt when
available) and delivers the server details to the owning user. Orders stuck
past their delivery estimate go to NEEDS_REVIEW for a human.

The activation step is shared (``OrderActivator``) so all paths converge
on one implementation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

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
    InvalidOperationTransition,
    Operation,
    OperationRepository,
    OperationStatus,
)
from cloud_platform.modules.orders.domain import (
    OrderStatus,
    ProviderOrder,
    ProviderOrderRepository,
    SettlementStatus,
)
from cloud_platform.modules.renewals.domain import (
    RenewalRecord,
    RenewalRepository,
    RenewalStatus,
)
from cloud_platform.modules.wallet.domain import (
    HoldRepository,
    HoldStateConflictError,
    HoldStatus,
    InsufficientBalanceError,
    LedgerRepository,
    WalletRepository,
)
from cloud_platform.modules.wallet.repository import HoldService
from cloud_platform.observability.metrics import metrics
from cloud_platform.providers.base import (
    CreateServerRequest,
    OrderingProvider,
    OrderRecoveryResult,
    OrderRecoveryVerdict,
    ProviderServer,
    ProvisioningTicket,
    ordering_support_of,
)
from cloud_platform.providers.errors import ProviderError, ProviderNotFound, ProviderOutcomeUnknown
from cloud_platform.providers.registry import ProviderRegistry
from cloud_platform.providers.retry import ErrorClass, classify_provider_error

logger = logging.getLogger(__name__)

#: How long an open order may sit without any provider movement before a
#: human is asked to look at it.
STUCK_ORDER_GRACE = timedelta(hours=72)

#: An IN_FLIGHT order-create operation older than this is presumed crashed
#: mid-POST. It is NEVER blindly re-POSTed: the order/operation become
#: OUTCOME_UNKNOWN and a READ-ONLY recovery scan (or a human) resolves them.
STALE_IN_FLIGHT_GRACE = timedelta(minutes=30)

#: Read-only recovery scans attempted before an unresolved OUTCOME_UNKNOWN
#: order is escalated to NEEDS_REVIEW for a human.
MAX_RECOVERY_SCANS = 5

#: Local settlement repair attempts before an accepted-but-unsettled order
#: is escalated to NEEDS_REVIEW (financial attention). The retries only
#: touch the wallet/ledger — never the provider.
MAX_SETTLEMENT_ATTEMPTS = 5

#: Bounded wait for a provider order to expose a usable ``equipmentId``
#: before the reconciler escalates to NEEDS_REVIEW instead of guessing a
#: VPS from account-wide similarity.
MAX_EQUIPMENT_WAIT = timedelta(hours=72)

MONTHLY_ESTIMATE_DAYS = 30


class OrderWorkerOutcome(StrEnum):
    SUBMITTED = "submitted"
    REQUEUED = "requeued"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SKIPPED_IN_FLIGHT = "skipped_in_flight"
    SKIPPED_STATE = "skipped_state"
    ALREADY_ACTIVE = "already_active"


class ReconciliationOutcome(StrEnum):
    PROVISIONED = "provisioned"
    STILL_PROVISIONING = "still_provisioning"
    FAILED = "failed"
    MARKED_FOR_REVIEW = "marked_for_review"
    LEFT_UNCHANGED = "left_unchanged"


class RecoveryOutcome(StrEnum):
    RECOVERED = "recovered"  # read-only scan attached exactly one provider order
    MARKED_FOR_REVIEW = "marked_for_review"  # ambiguous / no proof / scan exhausted
    LEFT_UNCHANGED = "left_unchanged"  # transient scan failure; try next round


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
        # REQUESTED -> RUNNING is not a legal single transition: route
        # through PROVISIONING first (covers manual resolve-vps and the
        # crash-recovery path where the server never left REQUESTED).
        if server.state is ServerLifecycleState.REQUESTED:
            server.transition_to(ServerLifecycleState.PROVISIONING)
        if server.state in (
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


class SettlementVerdict(StrEnum):
    """Outcome of one :meth:`OrderSettlementService.ensure_order_payment_settled`
    pass."""

    SETTLED = "settled"  # hold CAPTURED + exactly one CHARGE ledger entry
    RETRY_LATER = "retry_later"  # transient local failure; repair next pass
    NEEDS_REVIEW = "needs_review"  # financial attention (hold missing/released)


class OrderSettlementService:
    """The local payment settlement barrier for accepted provider orders.

    Provider acceptance and local charge settlement are DIFFERENT facts:
    the provider purchase already exists (its id is durably persisted
    BEFORE this service runs), so this service ONLY touches the wallet and
    ledger — it never contacts the provider with a mutating request and
    never re-POSTs. Its job is to prove, before any activation/delivery:

    1. the checkout hold exists and is not RELEASED,
    2. the hold is CAPTURED (idempotent capture — the wallet can never be
       debited twice),
    3. exactly one CHARGE ledger entry exists (a missing entry is repaired
       by re-running the idempotent capture path without a second debit),
    4. a stale operation row (e.g. worker crashed after the order id was
       saved but before the operation completed) is completed.

    The verdict is durable: ``settlement_status`` is persisted on the order
    (pending | complete | needs_review) so an accepted-but-unsettled order
    survives restarts and is repaired by the reconciler.
    """

    def __init__(
        self,
        *,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        hold_service: HoldService,
        ledger_repo: LedgerRepository,
        orders_repo: ProviderOrderRepository,
        operation_repo: OperationRepository,
        audit_repo: AuditRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._wallets = wallet_repo
        self._holds = hold_repo
        self._hold_service = hold_service
        self._ledger = ledger_repo
        self._orders = orders_repo
        self._ops = operation_repo
        self._audit = AuditTrail(audit_repo)
        self._now = clock or (lambda: datetime.now(UTC))

    async def ensure_order_payment_settled(
        self, order: ProviderOrder, server: CloudServer
    ) -> SettlementVerdict:
        """Idempotently prove/repair the local charge for an ACCEPTED order.

        Safe to invoke repeatedly (worker acceptance path, reconciler repair,
        manual resolution). Returns the verdict; escalates financially unsafe
        states (missing/released hold) to NEEDS_REVIEW without ever touching
        the provider.
        """
        now = self._now()
        if server.idempotency_key is None:
            return await self._needs_review(
                order, now, "server has no idempotency key; cannot locate the checkout hold"
            )
        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.id is None:
            return await self._needs_review(order, now, f"wallet missing for user {server.user_id}")
        from cloud_platform.modules.checkout.service import hold_key

        hold_ik = hold_key(server.idempotency_key)
        hold = await self._holds.get_by_idempotency(wallet.id, hold_ik)
        if hold is None or hold.id is None:
            return await self._needs_review(
                order,
                now,
                "checkout hold missing although the provider order was accepted",
            )
        if hold.status is HoldStatus.RELEASED:
            return await self._needs_review(
                order,
                now,
                f"hold {hold.id} was RELEASED although the provider order was accepted",
            )

        # Idempotent capture: debits the wallet at most once and posts the
        # CHARGE under a deterministic key, so a re-run repairs a missing
        # CHARGE entry without a second wallet debit.
        try:
            await self._hold_service.capture_hold(wallet.id, hold.id, hold_ik)
        except HoldStateConflictError:
            return await self._needs_review(
                order,
                now,
                f"hold {hold.id} released while settling an accepted provider order",
            )
        except InsufficientBalanceError:
            return await self._needs_review(
                order,
                now,
                f"hold {hold.id} cannot be captured: wallet balance below the held amount",
            )
        except Exception as exc:
            return await self._retry_later(
                order,
                now,
                f"hold capture failed transiently ({type(exc).__name__}): {exc}",
            )

        # Prove the CHARGE ledger entry exists (hold CAPTURED + missing
        # CHARGE = incomplete settlement; the next pass repairs it).
        charge_key = f"capture-{hold_ik}"
        try:
            charge = await self._ledger.get_entry_by_idempotency(wallet.id, charge_key)
        except Exception:
            charge = None
        if charge is None:
            return await self._retry_later(
                order,
                now,
                "hold captured but the CHARGE ledger entry is missing; "
                "the idempotent capture will repair it on the next pass",
            )

        order.settlement_status = SettlementStatus.COMPLETE
        order.settlement_attempted_at = now
        order.settlement_attempts = 0
        order.settlement_error = None
        await self._orders.save(order)
        await self._repair_operation(order)
        logger.info(
            "order %s settlement COMPLETE (hold %s captured, CHARGE posted)",
            order.id,
            hold.id,
        )
        return SettlementVerdict.SETTLED

    async def _retry_later(
        self, order: ProviderOrder, now: datetime, reason: str
    ) -> SettlementVerdict:
        """Record a transient settlement failure; escalate after bounded
        attempts. Never touches the provider."""
        order.settlement_attempts += 1
        order.settlement_attempted_at = now
        order.settlement_error = reason
        if order.settlement_attempts >= MAX_SETTLEMENT_ATTEMPTS:
            return await self._needs_review(
                order,
                now,
                f"settlement retries exhausted ({MAX_SETTLEMENT_ATTEMPTS}): {reason}",
            )
        await self._orders.save(order)
        logger.warning(
            "order %s settlement pending (attempt %d/%d): %s",
            order.id,
            order.settlement_attempts,
            MAX_SETTLEMENT_ATTEMPTS,
            reason,
        )
        return SettlementVerdict.RETRY_LATER

    async def _needs_review(
        self, order: ProviderOrder, now: datetime, reason: str
    ) -> SettlementVerdict:
        """Escalate a financially unsafe state for human attention."""
        order.settlement_status = SettlementStatus.NEEDS_REVIEW
        order.settlement_attempted_at = now
        order.settlement_error = reason
        await self._orders.save(order)
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="orders.settlement_review",
            resource_type="server_order",
            resource_id=str(order.id),
            reason=reason,
            metadata={
                "server_id": str(order.server_id),
                "provider_order_id": order.provider_order_id or "",
            },
        )
        logger.error(
            "order %s settlement NEEDS_REVIEW: %s (no delivery; no provider POST)",
            order.id,
            reason,
        )
        return SettlementVerdict.NEEDS_REVIEW

    async def _repair_operation(self, order: ProviderOrder) -> None:
        """Complete a stale operation row for a settled order.

        Covers the crash window where the provider order id was persisted
        but the operation-complete save failed (or the process died between
        the two saves): the settlement is proven and the operation metadata
        is repaired WITHOUT any provider call.
        """
        op = await self._ops.get_by_key(order.operation_key)
        if op is None or op.is_terminal:
            return
        try:
            op.complete(
                {
                    "provider_order_id": order.provider_order_id or "",
                    "settled": True,
                    "operation_key": op.operation_key,
                }
            )
        except InvalidOperationTransition:
            return  # e.g. PENDING; not ours to claim again
        await self._ops.save(op)


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
        ledger_repo: LedgerRepository,
        audit_repo: AuditRepository,
        provider_registry: ProviderRegistry,
        renewal_repo: RenewalRepository,
        delivery_notifier: OrderDeliveryNotifier | None = None,
        settlement: OrderSettlementService | None = None,
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
        self._settlement = settlement or OrderSettlementService(
            wallet_repo=wallet_repo,
            hold_repo=hold_repo,
            hold_service=hold_service,
            ledger_repo=ledger_repo,
            orders_repo=orders_repo,
            operation_repo=operation_repo,
            audit_repo=audit_repo,
            clock=clock,
        )
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
            # outcome is UNKNOWN — the POST is NEVER blindly repeated. The
            # order + operation move to OUTCOME_UNKNOWN and a READ-ONLY
            # recovery scan (or a human) resolves them.
            updated = operation.updated_at or operation.created_at or self._now()
            if self._now() - updated <= STALE_IN_FLIGHT_GRACE:
                return OrderWorkerOutcome.SKIPPED_IN_FLIGHT
            await self._mark_outcome_unknown(
                server,
                order,
                operation,
                "worker crashed around the order POST; outcome unknown, "
                "recovery requires a read-only scan",
            )
            return OrderWorkerOutcome.OUTCOME_UNKNOWN
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

        # The request carries PROVIDER-side facts only (provider cost
        # snapshot, currency, term, cycle). The customer selling price is
        # NEVER sent to the provider or used to identify a provider order.
        request = CreateServerRequest(
            name=f"srv-{server.id.hex[:8]}",
            plan_id=order.product_id or offer.product_id,
            image_id=server.os,
            location_id=order.location_id or offer.location_id,
            labels={
                "provider_price_minor": str(offer.provider_cost_minor),
                "provider_currency": offer.provider_cost_currency,
                "contract_term": order.contract_term or "1_MONTH",
                "billing_cycle": order.billing_cycle or "1_MONTH",
                "platform_server_id": str(server.id),
            },
        )
        # Persist the POST-attempt instant BEFORE the chargeable call: the
        # read-only recovery scan window starts here (durable across crashes).
        order.post_attempted_at = claimed.updated_at or self._now()
        await self._orders.save(order)
        try:
            ticket = await ordering.place_order(request, IdempotencyKey(claimed.operation_key))
        except ProviderOutcomeUnknown as exc:
            # The POST may have been accepted: NEVER automatically re-POST.
            # Order + operation -> OUTCOME_UNKNOWN; the hold stays reserved;
            # a READ-ONLY recovery scan (or a human) resolves the outcome.
            await self._mark_outcome_unknown(server, order, claimed, str(exc))
            return OrderWorkerOutcome.OUTCOME_UNKNOWN
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

        # Payment settlement barrier: the provider purchase EXISTS — only
        # the LOCAL charge may still be pending. NEVER re-POST, NEVER
        # release the hold; repair the settlement locally only. Delivery is
        # blocked until settlement is COMPLETE.
        verdict = await self._settlement.ensure_order_payment_settled(order, server)
        if verdict is SettlementVerdict.NEEDS_REVIEW:
            order.mark_needs_review(
                order.settlement_error or "payment settlement requires manual review"
            )
            await self._orders.save(order)
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="leaseweb.order_settlement_review",
                resource_type="server_order",
                resource_id=str(order.id),
                reason=order.settlement_error or "settlement requires review",
                metadata={
                    "server_id": str(server.id),
                    "provider_order_id": ticket.provider_order_id,
                },
            )
            logger.error(
                "leaseweb order %s accepted but settlement NEEDS_REVIEW: %s",
                order.id,
                order.settlement_error,
            )
            return OrderWorkerOutcome.SUBMITTED
        if verdict is SettlementVerdict.RETRY_LATER:
            # The server stays REQUESTED: no delivery, no PROVISIONING. The
            # reconciler repairs the LOCAL settlement (zero provider POSTs)
            # and the provider order id remains durably attached.
            logger.warning(
                "leaseweb order %s accepted but settlement pending: %s",
                order.id,
                order.settlement_error,
            )
            return OrderWorkerOutcome.SUBMITTED

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
            "leaseweb order accepted: server=%s order_id=%s provider_order=%s settlement=complete",
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
            reason=f"provider order {ticket.provider_order_id} accepted and settled",
            metadata={
                "server_id": str(server.id),
                "provider_order_id": ticket.provider_order_id,
                "operation_key": claimed.operation_key,
            },
        )
        return OrderWorkerOutcome.SUBMITTED

    async def _mark_outcome_unknown(
        self,
        server: CloudServer,
        order: ProviderOrder,
        operation: Operation,
        reason: str,
    ) -> None:
        """Record a billable POST whose outcome is unknown (release
        hardening). The hold stays reserved; no automatic re-POST follows;
        a READ-ONLY recovery scan (or a human) resolves the order."""
        order.mark_outcome_unknown(reason)
        order.attempts += 1
        await self._orders.save(order)
        if operation.status is not OperationStatus.OUTCOME_UNKNOWN:
            operation.mark_outcome_unknown(reason)
        await self._ops.save(operation)
        metrics.record_provisioning_failure("order_worker_outcome_unknown")
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="leaseweb.order_outcome_unknown",
            resource_type="server_order",
            resource_id=str(order.id),
            reason=reason,
            metadata={
                "server_id": str(server.id),
                "operation_key": operation.operation_key,
                "post_attempted_at": order.post_attempted_at.isoformat()
                if order.post_attempted_at
                else "",
            },
        )
        logger.error(
            "leaseweb order %s outcome UNKNOWN (server %s): %s; "
            "no automatic re-POST; read-only recovery required",
            order.id,
            server.id,
            reason,
        )

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
        operation_repo: OperationRepository,
        renewal_repo: RenewalRepository,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        hold_service: HoldService,
        ledger_repo: LedgerRepository,
        audit_repo: AuditRepository,
        provider_registry: ProviderRegistry,
        delivery_notifier: OrderDeliveryNotifier | None = None,
        settlement: OrderSettlementService | None = None,
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
        self._settlement = settlement or OrderSettlementService(
            wallet_repo=wallet_repo,
            hold_repo=hold_repo,
            hold_service=hold_service,
            ledger_repo=ledger_repo,
            orders_repo=orders_repo,
            operation_repo=operation_repo,
            audit_repo=audit_repo,
            clock=clock,
        )
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

        # Settlement barrier: an accepted provider order whose LOCAL charge
        # is not settled must not be activated or delivered. Repair the
        # local settlement (never a provider POST); escalate financially
        # unsafe states for a human.
        if order.settlement_status is not SettlementStatus.COMPLETE:
            verdict = await self._settlement.ensure_order_payment_settled(order, server)
            if verdict is SettlementVerdict.NEEDS_REVIEW:
                order.mark_needs_review(
                    order.settlement_error or "payment settlement requires manual review"
                )
                await self._orders.save(order)
                await self._audit.record_mutation(
                    actor_type=ActorType.SYSTEM,
                    actor_id=None,
                    action="leaseweb.order_settlement_review",
                    resource_type="server_order",
                    resource_id=str(order.id),
                    reason=order.settlement_error or "settlement requires review",
                    metadata={"server_id": str(order.server_id)},
                )
                return ReconciliationOutcome.MARKED_FOR_REVIEW
            if verdict is SettlementVerdict.RETRY_LATER:
                # Local-only repair continues on the next poll; the server
                # stays un-delivered and the provider order id stays attached.
                return ReconciliationOutcome.LEFT_UNCHANGED
            if server.state is ServerLifecycleState.REQUESTED:
                server.transition_to(ServerLifecycleState.PROVISIONING)
                await self._servers.save(server)

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
            # Provider says ACTIVE but no provable VPS identity yet
            # (equipmentId absent or its exact GET failed): keep polling —
            # NEVER auto-attach a similar account VPS. After a bounded wait
            # the order escalates to a human instead of guessing.
            since = order.post_attempted_at or order.created_at or datetime.now(UTC)
            if self._now() - since > MAX_EQUIPMENT_WAIT:
                order.mark_needs_review(
                    "no usable provider equipmentId within the bounded wait; "
                    "verify the provisioned VPS manually (orders resolve-vps)"
                )
                await self._orders.save(order)
                await self._audit.record_mutation(
                    actor_type=ActorType.SYSTEM,
                    actor_id=None,
                    action="leaseweb.order_review",
                    resource_type="server_order",
                    resource_id=str(order.id),
                    reason="equipmentId wait exceeded",
                    metadata={"server_id": str(order.server_id)},
                )
                return ReconciliationOutcome.MARKED_FOR_REVIEW
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


class OrderRecoveryService:
    """READ-ONLY recovery of OUTCOME_UNKNOWN orders (release hardening).

    Resolves orders whose billable POST had an ambiguous outcome. It NEVER
    POSTs anything: it asks the ordering provider for a read-only recovery
    scan (provider-side facts only) and applies the verdict:

    - MATCHED  -> attach the provider order id, complete the operation,
      capture the hold exactly once; the normal reconciler takes over.
    - AMBIGUOUS -> NEEDS_REVIEW (a human decides; never guess).
    - NO_MATCH -> NEEDS_REVIEW (absence cannot be proven: the order may
      exist but be invisible; a human verifies before any second POST).
    - SCAN_FAILED -> stay OUTCOME_UNKNOWN; retry next round, escalating to
      NEEDS_REVIEW after MAX_RECOVERY_SCANS bounded attempts.
    """

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
        self._now = clock or (lambda: datetime.now(UTC))

    async def recover(
        self, provider_key: str = "leaseweb", limit: int = 50
    ) -> dict[RecoveryOutcome, int]:
        counts: dict[RecoveryOutcome, int] = {}
        orders = (await self._orders.list_outcome_unknown(provider_key))[:limit]
        for order in orders:
            try:
                outcome = await self._recover_one(order)
            except Exception:
                logger.exception("order recovery failed for order %s", order.id)
                outcome = RecoveryOutcome.LEFT_UNCHANGED
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts

    async def _recover_one(self, order: ProviderOrder) -> RecoveryOutcome:
        if not order.post_attempted_at and not order.created_at:
            return await self._escalate(order, "order has no attempt timestamp; cannot scan")
        server = await self._servers.get(order.server_id)
        if server is None:
            return await self._escalate(order, "server row missing during recovery")
        try:
            provider = self._registry.get(server.provider_key)
        except KeyError:
            return await self._escalate(order, f"provider {server.provider_key!r} not configured")
        from cloud_platform.providers.base import ordering_support_of

        ordering = ordering_support_of(provider)
        if ordering is None or not callable(getattr(ordering, "recover_order", None)):
            return await self._escalate(
                order, f"provider {server.provider_key!r} has no read-only recovery port"
            )

        offer = await self._offers.get(order.offer_id) if order.offer_id else None
        cost_minor = (
            order.provider_cost_minor
            if order.provider_cost_minor
            else (offer.provider_cost_minor if offer else None)
        )
        if not cost_minor:
            return await self._escalate(
                order, "no provider cost snapshot; recovery cannot correlate safely"
            )
        result: OrderRecoveryResult = await ordering.recover_order(
            provider_cost_minor=cost_minor,
            currency=order.provider_cost_currency
            or (offer.provider_cost_currency if offer else "")
            or "EUR",
            contract_term=order.contract_term or "1_MONTH",
            billing_cycle=order.billing_cycle or "1_MONTH",
            since=order.post_attempted_at or order.created_at or datetime.now(UTC),
        )

        if result.verdict is OrderRecoveryVerdict.MATCHED and result.provider_order_id:
            return await self._attach(order, result.provider_order_id, result.reason)
        if result.verdict is OrderRecoveryVerdict.AMBIGUOUS:
            return await self._escalate(
                order, f"recovery ambiguous: {result.reason or 'several matching orders'}"
            )
        if result.verdict is OrderRecoveryVerdict.NO_MATCH:
            # A clean scan found nothing, but absence is NOT provable: the
            # order may exist but be invisible. A human verifies manually
            # before any second chargeable POST is ever considered.
            return await self._escalate(
                order, f"recovery found no matching order; {result.reason or ''}"
            )
        # SCAN_FAILED: transient; bounded retries, then escalate.
        order.attempts += 1
        await self._orders.save(order)
        if order.attempts >= MAX_RECOVERY_SCANS:
            return await self._escalate(
                order, f"recovery scans exhausted; last scan failed: {result.reason or ''}"
            )
        logger.warning(
            "order %s recovery scan failed (attempt %d/%d); retrying next round: %s",
            order.id,
            order.attempts,
            MAX_RECOVERY_SCANS,
            result.reason,
        )
        return RecoveryOutcome.LEFT_UNCHANGED

    async def _attach(
        self, order: ProviderOrder, provider_order_id: str, reason: str
    ) -> RecoveryOutcome:
        """Attach the proven provider order id; the normal reconciler continues."""
        order.mark_submitted(provider_order_id)
        order.error = None
        await self._orders.save(order)

        operation = await self._ops.get_by_key(order.operation_key)
        if operation is not None and operation.status is OperationStatus.OUTCOME_UNKNOWN:
            operation.complete(
                {
                    "provider_order_id": provider_order_id,
                    "recovered": True,
                    "operation_key": operation.operation_key,
                }
            )
            await self._ops.save(operation)

        await self._capture_hold(order)
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="leaseweb.order_recovered",
            resource_type="server_order",
            resource_id=str(order.id),
            reason=f"read-only recovery attached provider order {provider_order_id}",
            metadata={
                "server_id": str(order.server_id),
                "provider_order_id": provider_order_id,
                "detail": reason,
            },
        )
        logger.info(
            "leaseweb order %s recovered read-only: provider_order=%s",
            order.id,
            provider_order_id,
        )
        return RecoveryOutcome.RECOVERED

    async def _escalate(self, order: ProviderOrder, reason: str) -> RecoveryOutcome:
        order.mark_needs_review(reason)
        await self._orders.save(order)
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="leaseweb.order_review",
            resource_type="server_order",
            resource_id=str(order.id),
            reason=reason,
            metadata={"server_id": str(order.server_id)},
        )
        logger.error("leaseweb order %s escalated for manual review: %s", order.id, reason)
        return RecoveryOutcome.MARKED_FOR_REVIEW

    async def _capture_hold(self, order: ProviderOrder) -> None:
        """Capture the checkout hold exactly once (idempotent by key)."""
        server = await self._servers.get(order.server_id)
        if server is None or server.idempotency_key is None:
            return
        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.id is None:
            logger.error("server %s: no wallet to capture hold on recovery", server.id)
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


class OrderManualResolutionError(Exception):
    """An operator resolution request is not safe to apply."""


class OrderManualResolutionService:
    """Operator-driven resolution of provider orders (LEASEWEB-MVP).

    Every method here is a MANUAL action with audit + explicit reason; none
    of them POSTs anything. Leaseweb ordering has NO provider-side
    idempotency, so the output never claims provider deduplication — safety
    comes from the operator's verification:

    - :meth:`retry_failed` — the order FAILED DEFINITIVELY (the provider
      rejected the POST; nothing was created). Reopens the SAME local
      operation identity (FAILED -> PENDING) and re-queues the server.
    - :meth:`resolve_existing` — an ambiguous POST (OUTCOME_UNKNOWN /
      NEEDS_REVIEW) that the operator PROVED at the provider created this
      exact order. Attaches the provider order id after a READ-ONLY
      validation, completes the operation and runs the SAME payment
      settlement barrier as the worker: the hold is captured exactly once;
      if the capture fails, the provider id stays attached and the
      settlement is repaired by the reconciler (never a provider POST).
    - :meth:`resolve_not_created` — an ambiguous POST that the operator
      PROVED created NOTHING. Returns the intent to the retryable queue
      (OUTCOME_UNKNOWN -> PENDING, same key).
    - :meth:`resolve_vps` — a SUBMITTED/PROVISIONING order whose exact VPS
      the operator verified at the provider portal. Validates the VPS
      READ-ONLY (existence + provider-side datacenter/pack cross-check),
      requires settlement COMPLETE, then activates + delivers through the
      normal activator.

    All preconditions are validated BEFORE any row is saved.
    """

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        offers_repo: SellableOfferRepository,
        orders_repo: ProviderOrderRepository,
        operation_repo: OperationRepository,
        renewal_repo: RenewalRepository,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        hold_service: HoldService,
        ledger_repo: LedgerRepository,
        audit_repo: AuditRepository,
        provider_registry: ProviderRegistry,
        delivery_notifier: OrderDeliveryNotifier | None = None,
        settlement: OrderSettlementService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._servers = server_repo
        self._offers = offers_repo
        self._orders = orders_repo
        self._ops = operation_repo
        self._renewals = renewal_repo
        self._wallets = wallet_repo
        self._holds = hold_repo
        self._hold_service = hold_service
        self._audit = AuditTrail(audit_repo)
        self._registry = provider_registry
        self._delivery = delivery_notifier or _LoggingOrderDeliveryNotifier()
        self._settlement = settlement or OrderSettlementService(
            wallet_repo=wallet_repo,
            hold_repo=hold_repo,
            hold_service=hold_service,
            ledger_repo=ledger_repo,
            orders_repo=orders_repo,
            operation_repo=operation_repo,
            audit_repo=audit_repo,
            clock=clock,
        )

    @staticmethod
    def _actor_context(actor: Any | None) -> tuple[ActorType, Any | None]:
        if actor is None:
            return ActorType.SYSTEM, None
        return ActorType.ADMIN, getattr(actor, "id", None)

    def _require_reason(self, reason: str) -> None:
        if not reason or not reason.strip():
            raise OrderManualResolutionError("an explicit non-empty reason is required")

    async def _order_and_operation(
        self, order_id: UUID
    ) -> tuple[ProviderOrder, Operation, CloudServer]:
        order = await self._orders.get(order_id)
        if order is None:
            raise LookupError(f"order {order_id} not found")
        operation = await self._ops.get_by_key(order.operation_key)
        if operation is None:
            raise OrderManualResolutionError(
                f"order {order_id}: operation ledger row missing; cannot resolve"
            )
        server = await self._servers.get(order.server_id)
        if server is None:
            raise OrderManualResolutionError(
                f"order {order_id}: server row missing; cannot resolve"
            )
        return order, operation, server

    # -- A) definitive FAILED retry ---------------------------------------

    async def retry_failed(
        self, order_id: UUID, *, actor: Any | None = None, reason: str
    ) -> tuple[ProviderOrder, Operation]:
        """Reopen a DEFINITIVELY FAILED order for a retry of the same local
        operation identity. Refuses ambiguous orders: those need
        ``resolve-existing`` / ``resolve-not-created`` after portal
        verification."""
        self._require_reason(reason)
        order, operation, server = await self._order_and_operation(order_id)
        # Validate EVERY precondition before mutating/saving any row: the
        # repositories commit independently, so a late validation failure
        # must not leave a partially transitioned local state.
        if order.status is not OrderStatus.FAILED:
            raise OrderManualResolutionError(
                f"order {order_id} is {order.status.value}; only orders that FAILED "
                "definitively (provider rejection, nothing created) may be retried. "
                "Ambiguous orders (OUTCOME_UNKNOWN/NEEDS_REVIEW) must be resolved with "
                "`orders resolve-existing` or `orders resolve-not-created` after "
                "verifying at the provider portal."
            )
        if operation.status not in (OperationStatus.FAILED, OperationStatus.PENDING):
            raise OrderManualResolutionError(
                f"order {order_id}: operation is {operation.status.value}; cannot retry "
                "(expected FAILED, or PENDING when an earlier retry partially saved)"
            )
        if server.state not in (ServerLifecycleState.ERROR, ServerLifecycleState.REQUESTED):
            raise OrderManualResolutionError(
                f"order {order_id}: server is {server.state.value}; cannot re-queue "
                "(expected ERROR or REQUESTED)"
            )

        # Idempotent durable transitions: a re-run after a partial save
        # (crash between the independent repository commits) completes the
        # remaining steps instead of failing halfway.
        if operation.status is OperationStatus.FAILED:
            operation.reopen_for_retry()  # FAILED -> PENDING, SAME key (manual-only)
            await self._ops.save(operation)
        if order.status is OrderStatus.FAILED:
            order.reset_to_pending_submit()  # FAILED -> PENDING_SUBMIT (manual-only)
            await self._orders.save(order)
        if server.state is ServerLifecycleState.ERROR:
            server.reset_to_requested()  # ERROR -> REQUESTED (manual-only)
            await self._servers.save(server)

        actor_type, actor_id = self._actor_context(actor)
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="order.retry",
            resource_type="server_order",
            resource_id=str(order.id),
            reason=reason,
            metadata={
                "server_id": str(order.server_id),
                "operation_key": order.operation_key,
                "provider": order.provider_key,
                "attempts": str(operation.attempts),
            },
        )
        logger.warning("order %s retried by operator (definitive FAILED): %s", order.id, reason)
        return order, operation

    # -- B) verified existing provider order ------------------------------

    async def resolve_existing(
        self,
        order_id: UUID,
        provider_order_id: str,
        *,
        actor: Any | None = None,
        reason: str,
    ) -> tuple[ProviderOrder, Operation]:
        """Attach the provider order id a human PROVED belongs to this
        ambiguous POST. Performs a READ-ONLY validation first; NEVER POSTs.
        The provider id is attached durably FIRST; then the SAME payment
        settlement barrier as the worker runs — the hold is captured exactly
        once, and a capture failure leaves the provider id attached with
        settlement pending (repaired by the reconciler, never re-POSTed).

        Returns (order, operation); the caller must read
        ``order.settlement_status`` to report whether settlement is complete.
        """
        self._require_reason(reason)
        if not provider_order_id or not provider_order_id.strip():
            raise OrderManualResolutionError("a non-empty provider order id is required")
        order, operation, server = await self._order_and_operation(order_id)
        if order.status not in (OrderStatus.OUTCOME_UNKNOWN, OrderStatus.NEEDS_REVIEW):
            raise OrderManualResolutionError(
                f"order {order_id} is {order.status.value}; only ambiguous orders "
                "(OUTCOME_UNKNOWN/NEEDS_REVIEW) may be resolved against an existing "
                "provider order"
            )
        if operation.status is not OperationStatus.OUTCOME_UNKNOWN:
            raise OrderManualResolutionError(
                f"order {order_id}: operation is {operation.status.value}, not "
                "OUTCOME_UNKNOWN; cannot attach a manually verified order id"
            )

        ordering = self._ordering_for(order, server)
        # READ-ONLY validation of the operator-supplied provider order id.
        try:
            ticket = await ordering.get_order(provider_order_id)
        except ProviderError as exc:
            raise OrderManualResolutionError(
                f"read-only validation of provider order {provider_order_id} failed "
                f"({type(exc).__name__}): {exc}"
            ) from exc
        inconsistency = self._inconsistency(order, ticket)
        if inconsistency:
            raise OrderManualResolutionError(
                f"provider order {provider_order_id} is not internally consistent "
                f"with order {order_id}: {inconsistency}"
            )

        order.resolve_submitted(provider_order_id)  # manual-only transition
        await self._orders.save(order)
        operation.complete(
            {
                "provider_order_id": provider_order_id,
                "resolved_by": "manual",
                "reason": reason,
                "operation_key": operation.operation_key,
            }
        )
        await self._ops.save(operation)

        # Settlement barrier (same as the worker): the provider purchase
        # already exists; never re-POST, never release the hold. A pending
        # capture leaves the provider id attached; the reconciler repairs.
        await self._settlement.ensure_order_payment_settled(order, server)

        actor_type, actor_id = self._actor_context(actor)
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="order.resolve_existing",
            resource_type="server_order",
            resource_id=str(order.id),
            reason=reason,
            metadata={
                "server_id": str(order.server_id),
                "provider_order_id": provider_order_id,
                "operation_key": order.operation_key,
                "provider": order.provider_key,
            },
        )
        logger.info(
            "order %s resolved manually: attached provider order %s", order.id, provider_order_id
        )
        return order, operation

    # -- C) verified absence -----------------------------------------------

    async def resolve_not_created(
        self, order_id: UUID, *, actor: Any | None = None, reason: str
    ) -> tuple[ProviderOrder, Operation]:
        """The operator PROVED the ambiguous POST created NOTHING at the
        provider: return the SAME local operation identity to the retryable
        queue (OUTCOME_UNKNOWN -> PENDING). The retry is a NEW provider POST
        — there is no provider-side idempotency — so this is safe ONLY
        because a human verified absence."""
        self._require_reason(reason)
        order, operation, server = await self._order_and_operation(order_id)
        if order.status not in (OrderStatus.OUTCOME_UNKNOWN, OrderStatus.NEEDS_REVIEW):
            raise OrderManualResolutionError(
                f"order {order_id} is {order.status.value}; only ambiguous orders "
                "(OUTCOME_UNKNOWN/NEEDS_REVIEW) may be marked as not created"
            )
        if operation.status is not OperationStatus.OUTCOME_UNKNOWN:
            raise OrderManualResolutionError(
                f"order {order_id}: operation is {operation.status.value}, not "
                "OUTCOME_UNKNOWN; cannot re-queue after verified absence"
            )
        if server.state is not ServerLifecycleState.REQUESTED:
            raise OrderManualResolutionError(
                f"order {order_id}: server is {server.state.value}; expected REQUESTED "
                "for an ambiguous POST"
            )

        operation.mark_verified_absent()  # OUTCOME_UNKNOWN -> PENDING, SAME key
        await self._ops.save(operation)
        order.reset_to_pending_submit()  # manual-only transition
        await self._orders.save(order)

        actor_type, actor_id = self._actor_context(actor)
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="order.resolve_not_created",
            resource_type="server_order",
            resource_id=str(order.id),
            reason=reason,
            metadata={
                "server_id": str(order.server_id),
                "operation_key": order.operation_key,
                "provider": order.provider_key,
            },
        )
        logger.warning(
            "order %s re-queued after operator verified NON-creation (same key): %s",
            order.id,
            reason,
        )
        return order, operation

    # -- B.2) verified provider VPS resource -------------------------------

    async def resolve_vps(
        self,
        order_id: UUID,
        vps_id: str,
        *,
        actor: Any | None = None,
        reason: str,
    ) -> tuple[ProviderOrder, Operation]:
        """Attach the provisioned VPS id the operator VERIFIED at the
        provider portal for an order whose exact resource identity never
        became provable automatically (no usable ``equipmentId``).

        - READ-ONLY toward the provider: validates the VPS exists and, when
          the VPS API exposes them, cross-checks datacenter/pack against the
          offer's provider-side facts (never guesses missing fields).
        - Requires settlement COMPLETE (hold CAPTURED + CHARGE): delivery
          must never happen for an unsettled purchase.
        - Activates through the normal ``OrderActivator`` (server RUNNING,
          renewal record, delivery) and audits the manual resolution.
        - NEVER POSTs anything.
        """
        self._require_reason(reason)
        if not vps_id or not vps_id.strip():
            raise OrderManualResolutionError("a non-empty provider VPS id is required")
        order, operation, server = await self._order_and_operation(order_id)
        if not order.provider_order_id:
            raise OrderManualResolutionError(
                f"order {order_id} has no provider order id; use `orders resolve-existing` first"
            )
        if order.status not in (
            OrderStatus.SUBMITTED,
            OrderStatus.PROVISIONING,
            OrderStatus.NEEDS_REVIEW,
        ):
            raise OrderManualResolutionError(
                f"order {order_id} is {order.status.value}; resolve-vps requires a "
                "SUBMITTED/PROVISIONING order with an attached provider order id"
            )
        if server.provider_server_id:
            raise OrderManualResolutionError(
                f"server {server.id} already has provider_server_id "
                f"{server.provider_server_id!r}; nothing to resolve"
            )
        offer = await self._offers.get(order.offer_id) if order.offer_id else None
        if offer is None:
            raise OrderManualResolutionError(
                f"order {order_id}: sellable offer row missing; cannot activate"
            )

        ordering = self._ordering_for(order, server)
        # READ-ONLY validation of the operator-supplied VPS id.
        try:
            remote = await ordering.get_server(vps_id)
        except ProviderError as exc:
            raise OrderManualResolutionError(
                f"read-only VPS lookup of {vps_id} failed ({type(exc).__name__}): {exc}"
            ) from exc
        if remote is None:
            raise OrderManualResolutionError(
                f"provider VPS {vps_id} does not exist (read-only lookup)"
            )
        meta = getattr(remote, "metadata", None) or {}
        datacenter = str(meta.get("datacenter") or "")
        pack = str(meta.get("pack") or "")
        if datacenter and offer.location_id and datacenter != offer.location_id:
            raise OrderManualResolutionError(
                f"VPS {vps_id} datacenter {datacenter!r} does not match the offer "
                f"location {offer.location_id!r}"
            )
        if pack and offer.name and pack != offer.name:
            raise OrderManualResolutionError(
                f"VPS {vps_id} pack {pack!r} does not match the offer product {offer.name!r}"
            )

        # Settlement barrier: never deliver an unsettled purchase.
        verdict = await self._settlement.ensure_order_payment_settled(order, server)
        if verdict is not SettlementVerdict.SETTLED:
            if verdict is SettlementVerdict.NEEDS_REVIEW:
                order.mark_needs_review(
                    order.settlement_error or "payment settlement requires manual review"
                )
                await self._orders.save(order)
            raise OrderManualResolutionError(
                "payment settlement is not COMPLETE (hold must be CAPTURED with "
                "a CHARGE ledger entry); resolve the financial state before "
                "attaching the VPS"
            )

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

        actor_type, actor_id = self._actor_context(actor)
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="order.resolve_vps",
            resource_type="server_order",
            resource_id=str(order.id),
            reason=reason,
            metadata={
                "server_id": str(order.server_id),
                "provider_order_id": order.provider_order_id or "",
                "provider_vps_id": vps_id,
                "operation_key": order.operation_key,
            },
        )
        logger.warning(
            "order %s manually resolved to VPS %s (settled): %s", order.id, vps_id, reason
        )
        return order, operation

    # -- helpers ------------------------------------------------------------

    def _ordering_for(self, order: ProviderOrder, server: CloudServer) -> OrderingProvider:
        try:
            provider = self._registry.get(server.provider_key)
        except KeyError as exc:
            raise OrderManualResolutionError(
                f"order {order.id}: provider {server.provider_key!r} not configured"
            ) from exc
        from cloud_platform.providers.base import ordering_support_of

        ordering = ordering_support_of(provider)
        if ordering is None:
            raise OrderManualResolutionError(
                f"order {order.id}: provider {server.provider_key!r} has no ordering port"
            )
        return ordering

    @staticmethod
    def _normalize(value: Any) -> str:
        """Provider-neutral term/cycle normalization (mirrors the adapter's:
        ``1_MONTH`` == ``1 MONTH``)."""
        return str(value or "").strip().upper().replace("_", "").replace(" ", "").replace("-", "")

    def _inconsistency(self, order: ProviderOrder, ticket: ProvisioningTicket) -> str | None:
        """Provider-side consistency of a candidate order against the local
        snapshots (price/currency/term/cycle/product family). Returns an
        error message, or None when every comparable fact matches. Facts the
        provider response does not carry are skipped, never guessed."""
        meta = ticket.metadata
        product = meta.get("product_id")
        if product and str(product) != "VIRTUAL_SERVER":
            return f"provider order is product {product!r}, not VIRTUAL_SERVER"

        price = meta.get("price_per_frequency_minor")
        if (
            price is not None
            and order.provider_cost_minor
            and abs(int(price) - order.provider_cost_minor) > 1
        ):
            return (
                f"price {int(price)} does not match the provider cost snapshot "
                f"{order.provider_cost_minor}"
            )
        currency = meta.get("currency")
        if currency and order.provider_cost_currency:
            if str(currency).upper() != order.provider_cost_currency.upper():
                return (
                    f"currency {currency!r} does not match snapshot "
                    f"{order.provider_cost_currency!r}"
                )
        term = meta.get("contract_term")
        if term and order.contract_term:
            if self._normalize(term) != self._normalize(order.contract_term):
                return f"contract term {term!r} does not match snapshot {order.contract_term!r}"
        cycle = meta.get("billing_cycle")
        if cycle and order.billing_cycle:
            if self._normalize(cycle) != self._normalize(order.billing_cycle):
                return f"billing cycle {cycle!r} does not match snapshot {order.billing_cycle!r}"
        return None
