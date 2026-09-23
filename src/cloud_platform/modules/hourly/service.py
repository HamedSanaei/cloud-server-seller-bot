"""Hourly cloud instance creation (STOREFRONT-REWORK).

The usage-based counterpart of the monthly order intent: it validates the
customer's hourly plan choice and persists a durable creation intent (hourly
``CloudServer`` + immutable hourly price snapshot + ``SERVER_CREATE``
operation) WITHOUT calling the provider and WITHOUT charging upfront.
Hourly money moves later, per quantum, through the existing accrual job,
which reads only the snapshot — never today's catalog price.

Billing-model branching is structural: this command accepts hourly offers
only, and the monthly command accepts monthly offers only. Neither branches
on a provider name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_HOURLY,
    CloudServer,
    ServerCreateError,
    ServerCreateIntent,
    ServerLifecycleState,
)
from cloud_platform.modules.offers.domain import SellableOfferRepository
from cloud_platform.modules.operations.domain import (
    OperationRepository,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.pricing.domain import MarginRule, OfferCost, SellingPrice
from cloud_platform.modules.pricing.service import ServerPriceSnapshotService
from cloud_platform.modules.provider_accounts.domain import (
    ProviderAccountRepository,
)
from cloud_platform.modules.users.domain import User, UserStatus
from cloud_platform.modules.wallet.domain import WalletRepository
from cloud_platform.providers.errors import ProviderError, ProviderOutcomeUnknown

logger = logging.getLogger(__name__)

RESOURCE_TYPE_CLOUD_SERVER = "cloud_server"


class HourlyError(Exception):
    """Base error for hourly cloud creation."""


class HourlyNotAvailableError(HourlyError):
    """The hourly offer/image selection is not currently creatable."""


class HourlyCloudResolver:
    """Provider-neutral port for hourly cloud adapters (multi-account).

    Resolves ``(provider_key, credential_account_id)`` to the exact cloud
    adapter that owns the observation. ``credential_account_id`` is the
    opaque, non-secret account id persisted on the hourly offer at sync
    time (``SellableOffer.provider_account_id``) and pinned on the hourly
    server at creation time. ``None`` resolves to the provider's logical
    default adapter (single-credential deployments and legacy rows).

    Implementations live in infrastructure (the container builds one from
    the Leaseweb cloud account router); domain/application code depends
    only on this port and never on a concrete provider name.
    """

    def adapter_for(self, provider_key: str, credential_account_id: str | None = None) -> Any:
        """The cloud adapter for a pinned credential account."""
        ...


@dataclass(frozen=True, slots=True)
class HourlyCreateResult:
    """Outcome of the hourly creation command."""

    server: CloudServer
    replayed: bool


def hourly_reference_name(server_id: UUID) -> str:
    """Deterministic provider reference for one hourly server.

    Shared by creation and reconciliation: every attempt for this server
    carries the same reference, so the adapter's get-before-create match
    and the reconciler's search identify exactly this intent's resource.
    """
    return f"srv-{server_id.hex[:8]}"


class HourlyCloudService:
    """The hourly creation command handler (no provider calls, no charge)."""

    def __init__(
        self,
        *,
        server_repo: Any,
        offers_repo: SellableOfferRepository,
        account_repo: ProviderAccountRepository,
        wallet_repo: WalletRepository,
        snapshot_service: ServerPriceSnapshotService,
        operation_repo: OperationRepository,
        audit_repo: AuditRepository,
        cloud_providers: dict[str, Any] | None = None,
        cloud_resolver: HourlyCloudResolver | Any | None = None,
    ) -> None:
        self._servers = server_repo
        self._offers = offers_repo
        self._accounts = account_repo
        self._wallets = wallet_repo
        self._snapshots = snapshot_service
        self._ops = operation_repo
        self._audit = AuditTrail(audit_repo)
        self._cloud = dict(cloud_providers or {})
        # Provider-neutral account-aware resolution (multi-account hourly):
        # the resolver maps (provider_key, credential_account_id) to the
        # exact adapter that owns the observation. The plain dict stays as
        # the legacy fallback for single-credential deployments.
        self._cloud_resolver = cloud_resolver

    def _adapter_for(self, provider_key: str, credential_account_id: str | None) -> Any:
        """Exact cloud adapter for a pinned credential account (fail closed).

        The resolver owns account-aware dispatch; the legacy dict is only
        the single-adapter fallback. No provider-name branching here: the
        account id selects the adapter, never an ``if provider == ...``.
        """
        resolver = self._cloud_resolver
        if resolver is not None:
            adapter = resolver.adapter_for(provider_key, credential_account_id)
            # The container resolver is synchronous; accept an awaitable too
            # so test doubles may use either shape.
            if adapter is not None:
                return adapter
        adapter = self._cloud.get(provider_key)
        if adapter is None:
            raise HourlyNotAvailableError(f"no hourly adapter for {provider_key!r}")
        return adapter

    async def create_instance(
        self,
        *,
        user: User,
        offer_id: UUID,
        image_id: str,
        image_label: str,
        idempotency_key: str,
    ) -> HourlyCreateResult:
        """Validate and persist the hourly creation intent."""
        if user.id is None:
            raise HourlyError("a persisted user id is required")
        if user.status is not UserStatus.ACTIVE:
            raise HourlyError(f"user {user.id} is {user.status.value}")

        offer = await self._offers.get(offer_id)
        if offer is None or not offer.sellable:
            raise HourlyNotAvailableError(f"offer {offer_id} is not sellable")
        if offer.billing_model != BILLING_MODEL_HOURLY:
            raise HourlyNotAvailableError(f"offer {offer_id} is not an hourly plan")
        if not image_id or not image_id.strip():
            raise HourlyNotAvailableError("an image id is required")

        existing = await self._servers.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.user_id != user.id:
                raise HourlyError(f"idempotency key {idempotency_key!r} belongs to another user")
            return HourlyCreateResult(server=existing, replayed=True)

        wallet = await self._wallets.get(user.id)
        if wallet is None:
            raise HourlyError(f"user {user.id} has no wallet")

        account = await self._accounts.get_or_create_active(user.id, offer.provider_key)

        # Hourly offer provenance: the credential account that actually
        # supplied/owns the observation (persisted by the multi-account
        # cloud sync in ``SellableOffer.provider_account_id``) is pinned
        # on the server now, before any provider call, so the worker POSTs
        # through exactly that credential — never an arbitrary first key.
        server = CloudServer(
            id=uuid4(),
            user_id=user.id,
            provider_key=offer.provider_key,
            provider_account_id=account.id,
            state=ServerLifecycleState.REQUESTED,
            billing_model=BILLING_MODEL_HOURLY,
            quantum_seconds=3600,
            os=image_label,
            credential_account_id=offer.provider_account_id,
        )
        intent = ServerCreateIntent(
            # No legacy-catalog pin (that FK points at the hourly catalog
            # tables, not the sellable price book): the hourly price
            # snapshot below pins provider/plan/location/cost instead.
            catalog_id=None,
            cost_minor=offer.provider_cost_minor,
            currency=offer.provider_cost_currency,
            idempotency_key=idempotency_key,
        )
        try:
            created = await self._servers.create(server, intent)
        except ServerCreateError:
            original = await self._servers.get_by_idempotency_key(idempotency_key)
            if original is not None and original.user_id == user.id:
                return HourlyCreateResult(server=original, replayed=True)
            raise

        # Immutable hourly price snapshot (billing source of truth): the
        # provider hourly cost and the customer hourly price, fixed now so
        # later catalog moves can never rewrite historical charges.
        fixed = offer.selling_price_minor - offer.provider_cost_minor
        price = SellingPrice(
            offer=OfferCost(
                provider_key=offer.provider_key,
                plan_id=offer.product_id,
                location_id=offer.location_id,
                cost_minor=offer.provider_cost_minor,
                currency=offer.provider_cost_currency,
            ),
            selling_minor=offer.selling_price_minor,
            rule=MarginRule(
                provider=offer.provider_key,
                plan=offer.product_id,
                location=offer.location_id,
                margin_factor=Decimal(1),
                fixed_minor=fixed,
            ),
            book_name="sellable-auto",
            version=1,
            priced_at=datetime.now(UTC),
        )
        try:
            await self._snapshots.create_snapshot(
                server_id=created.id,
                price=price,
                actor=None,
                reason="hourly cloud create request",
            )
        except Exception:
            try:
                created.transition_to(ServerLifecycleState.ERROR)
                await self._servers.save(created)
            except Exception:
                pass
            raise

        await self._ops.get_or_create(
            operation_key=f"server-create:{created.id}",
            operation_type=OperationType.SERVER_CREATE,
            resource_type=RESOURCE_TYPE_CLOUD_SERVER,
            resource_id=created.id,
            provider_key=offer.provider_key,
        )

        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            actor_id=user.id,
            action="hourly.create_requested",
            resource_type="server",
            resource_id=str(created.id),
            reason=f"hourly create {offer.ref}",
            metadata={
                "offer": offer.ref,
                "hourly_price_minor": str(offer.selling_price_minor),
                "currency": offer.selling_currency,
                "provider_cost_minor": str(offer.provider_cost_minor),
                "image": image_label,
                "billing_model": BILLING_MODEL_HOURLY,
            },
        )
        logger.info(
            "hourly create intent %s (offer %s, %d %s/h, image %s)",
            created.id,
            offer.ref,
            offer.selling_price_minor,
            offer.selling_currency,
            image_label,
        )
        return HourlyCreateResult(server=created, replayed=False)

    async def cloud_image_by_index(self, offer: Any, index: int) -> Any:
        # Resolve a callback-encoded image index against a live listing
        # (a stale index simply fails; label and provider id stay apart).
        # The owning credential account selects the adapter, never a
        # first-configured default.
        try:
            adapter = self._adapter_for(
                offer.provider_key, getattr(offer, "provider_account_id", None)
            )
        except HourlyNotAvailableError:
            raise
        except Exception as exc:
            raise HourlyNotAvailableError(f"images currently unavailable for {offer.ref}") from exc
        try:
            images = await adapter.list_images(offer.location_id)
        except Exception as exc:
            raise HourlyNotAvailableError(f"images currently unavailable for {offer.ref}") from exc
        if index < 0 or index >= len(images):
            raise HourlyNotAvailableError(f"image option {index} is not available for {offer.ref}")
        return images[index]

    async def servers_requested(self) -> list[CloudServer]:
        """Hourly servers awaiting creation (the process job's queue)."""
        servers = await self._servers.list_requested()
        return [s for s in servers if not s.is_prepaid_monthly]

    async def servers_for_reconcile(self) -> list[CloudServer]:
        """Hourly servers possibly submitted but unattached (reconcile queue)."""
        servers = await self._servers.list_provisioning()
        return [s for s in servers if not s.is_prepaid_monthly and not s.provider_server_id]

    async def process_server(self, server_id: UUID) -> str:
        """Execute one hourly server's create intent (worker-called).

        Claims the ``server-create`` operation, builds the exact hourly POST
        from the price snapshot (plan/location) and the pinned image label,
        and POSTs once. Ambiguous outcomes become OUTCOME_UNKNOWN (never a
        blind re-POST); definitive failures become FAILED for operator
        review. Returns a short outcome label for job logging.
        """
        server = await self._servers.get(server_id)
        if server is None or server.is_prepaid_monthly:
            return "skipped"
        if server.state is not ServerLifecycleState.REQUESTED:
            return "skipped"
        operation = await self._ops.get_or_create(
            operation_key=f"server-create:{server.id}",
            operation_type=OperationType.SERVER_CREATE,
            resource_type=RESOURCE_TYPE_CLOUD_SERVER,
            resource_id=server.id,
            provider_key=server.provider_key,
        )
        if operation.is_terminal:
            return "skipped"
        claimed = await self._ops.claim(operation.id)
        if claimed is None:
            return "claimed-elsewhere"
        try:
            snapshot = await self._snapshots.require_snapshot(server.id)
        except Exception as exc:
            return await self._fail_operation(claimed, server, f"no price snapshot: {exc}")
        # The exact credential account pinned at creation owns the POST —
        # never an arbitrary configured key.
        try:
            adapter = self._adapter_for(
                server.provider_key, getattr(server, "credential_account_id", None)
            )
        except HourlyNotAvailableError as exc:
            return await self._fail_operation(claimed, server, str(exc))
        try:
            images = await adapter.list_images(snapshot.offer.location_id)
        except Exception as exc:
            return await self._fail_operation(claimed, server, f"images unavailable: {exc}")
        image = next((img for img in images if img.label == server.os), None)
        if image is None:
            return await self._fail_operation(
                claimed, server, f"image {server.os!r} no longer offered"
            )
        reference = hourly_reference_name(server.id)
        try:
            created = await adapter.create_instance(
                instance_type=snapshot.offer.plan_id,
                image_id=image.id,
                region=snapshot.offer.location_id,
                reference=reference,
                idempotency_key=IdempotencyKey(claimed.operation_key),
            )
        except ProviderOutcomeUnknown as exc:
            claimed.mark_outcome_unknown(str(exc))
            await self._ops.save(claimed)
            logger.warning("hourly create %s ambiguous: %s", server.id, exc)
            return "outcome-unknown"
        except ProviderError as exc:
            return await self._fail_operation(claimed, server, str(exc))
        claimed.complete(
            {
                "provider_server_id": created.id,
                "provider_status": created.state,
                "idempotency_key": claimed.operation_key,
            }
        )
        await self._ops.save(claimed)
        server.provider_server_id = created.id
        if server.state is ServerLifecycleState.REQUESTED:
            server.transition_to(ServerLifecycleState.PROVISIONING)
        await self._servers.save(server)
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="hourly.create_submitted",
            resource_type="server",
            resource_id=str(server.id),
            reason=f"hourly instance {created.id} accepted",
            metadata={"provider_server_id": created.id},
        )
        logger.info("hourly server %s -> provider %s", server.id, created.id)
        return "provisioned"

    async def _fail_operation(self, claimed: Any, server: CloudServer, error: str) -> str:
        claimed.fail(error)
        await self._ops.save(claimed)
        try:
            server.transition_to(ServerLifecycleState.ERROR)
            await self._servers.save(server)
        except Exception:
            logger.exception("failed to mark hourly server %s ERROR", server.id)
        logger.warning("hourly create %s failed: %s", server.id, error)
        return "failed"

    async def reconcile_server(self, server_id: UUID) -> str:
        """Attach a proven instance to an ambiguous hourly create, or leave it.

        For OUTCOME_UNKNOWN operations only: an exact reference match proves
        the earlier POST landed and is attached; anything else stays unknown
        for operator review (the operator may requeue, which re-POSTs safely
        through get-before-create). Never attaches by similarity.
        """
        server = await self._servers.get(server_id)
        if server is None or server.is_prepaid_monthly:
            return "skipped"
        if server.provider_server_id:
            return "skipped"
        operation = await self._ops.get_by_key(f"server-create:{server.id}")
        if operation is None or operation.status is not OperationStatus.OUTCOME_UNKNOWN:
            return "skipped"
        try:
            adapter = self._adapter_for(
                server.provider_key, getattr(server, "credential_account_id", None)
            )
        except HourlyNotAvailableError:
            return "skipped"
        try:
            snapshot = await self._snapshots.require_snapshot(server.id)
        except Exception:
            return "skipped"
        found = await adapter.find_by_reference(
            snapshot.offer.location_id, hourly_reference_name(server.id)
        )
        if found is None:
            logger.info("hourly reconcile %s: no instance found; still unknown", server.id)
            return "still-unknown"
        operation.complete(
            {
                "provider_server_id": found.id,
                "provider_status": found.state,
                "reconciled": "reference-match",
            }
        )
        await self._ops.save(operation)
        server.provider_server_id = found.id
        if server.state is ServerLifecycleState.REQUESTED:
            server.transition_to(ServerLifecycleState.PROVISIONING)
        await self._servers.save(server)
        logger.info("hourly reconcile %s attached provider %s", server.id, found.id)
        return "attached"
