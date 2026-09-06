"""Monthly prepaid checkout (LEASEWEB-MVP).

The checkout command is the financially safe order intent:

1. The user must be ACTIVE.
2. The offer is RELOADED from the sellable-offer price book and must pass
   ALL THREE gates (provider-reported, enabled, explicitly priced).
3. The OS choice is re-validated SERVER-SIDE against the live provider
   product API (free options only by default), so the shown price is exact.
4. Idempotency replay: the same command key returns the original intent —
   repeated Telegram callbacks can never double-charge or double-order.
5. A wallet hold reserves the exact monthly selling price (atomic,
   race-free; the hold's unique key is derived from the command key).
6. The REQUESTED server row (billing model ``prepaid_monthly_fixed``, OS
   recorded), the PENDING_SUBMIT provider-order row and the operation row
   with the deterministic key ``order-create:{server_id}`` are persisted
   BEFORE any provider call. Provisioning is the worker's job.

On any failure after the hold, the hold is released and the intent is
marked ERROR — a failed command never leaves an orphaned reservation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from cloud_platform.core.config import get_settings
from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    CloudServer,
    ServerCreateError,
    ServerCreateIntent,
    ServerLifecycleState,
    ServerRepository,
)
from cloud_platform.modules.offers.domain import (
    SellableOffer,
    SellableOfferRepository,
)
from cloud_platform.modules.operations.domain import OperationRepository, OperationType
from cloud_platform.modules.orders.domain import ProviderOrder, ProviderOrderRepository
from cloud_platform.modules.provider_accounts.domain import (
    ProviderAccountRepository,
)
from cloud_platform.modules.users.domain import User, UserStatus
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldRepository,
    InsufficientHoldBalanceError,
    WalletRepository,
)
from cloud_platform.providers.base import (
    OrderingProvider,
    ordering_support_of,
)
from cloud_platform.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

#: Provider key the MVP sells through (single-provider vertical slice).
MVP_PROVIDER_KEY = "leaseweb"

RESOURCE_TYPE_SERVER_ORDER = "server_order"


class CheckoutError(Exception):
    """Base error for monthly checkout."""


class UserNotActiveError(CheckoutError):
    pass


class OfferUnavailableError(CheckoutError):
    pass


class OsUnavailableError(CheckoutError):
    pass


class NoWalletError(CheckoutError):
    pass


class CheckoutReplayError(CheckoutError):
    """The idempotency key is owned by another user's intent."""


@dataclass(frozen=True, slots=True)
class MonthlyCheckoutResult:
    """Outcome of the checkout command.

    ``replayed`` is True when this call was an idempotent replay of an
    earlier command with the same key.
    """

    server: CloudServer
    order: ProviderOrder
    hold: Hold | None
    offer: SellableOffer
    replayed: bool


def order_operation_key(server_id: UUID) -> str:
    """The deterministic operation/order key for one server's order intent."""
    return f"order-create:{server_id}"


def hold_key(idempotency_key: str) -> str:
    """Deterministic hold idempotency key derived from the command key."""
    return f"leaseweb-order:{idempotency_key}"


class MonthlyCheckoutService:
    """The monthly purchase command handler (see module docstring)."""

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        offers_repo: SellableOfferRepository,
        account_repo: ProviderAccountRepository,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        orders_repo: ProviderOrderRepository,
        operation_repo: OperationRepository,
        audit_repo: AuditRepository,
        provider_registry: ProviderRegistry,
    ) -> None:
        self._servers = server_repo
        self._offers = offers_repo
        self._accounts = account_repo
        self._wallets = wallet_repo
        self._holds = hold_repo
        self._orders = orders_repo
        self._ops = operation_repo
        self._audit = AuditTrail(audit_repo)
        self._registry = provider_registry

    async def _ordering_provider(self, provider_key: str) -> OrderingProvider:
        try:
            provider = self._registry.get(provider_key)
        except KeyError as exc:
            raise OfferUnavailableError(f"provider {provider_key!r} not configured") from exc
        ordering = ordering_support_of(provider)
        if ordering is None:
            raise OfferUnavailableError(
                f"provider {provider_key!r} does not support asynchronous ordering"
            )
        return ordering

    async def create_order(
        self,
        *,
        user: User,
        offer_id: UUID,
        os_name: str,
        idempotency_key: str,
        at: datetime | None = None,
    ) -> MonthlyCheckoutResult:
        """Validate everything and persist the order intent (no provider calls)."""
        # 1. User.
        if user.id is None:
            raise CheckoutError("a persisted user id is required")
        if user.status is not UserStatus.ACTIVE:
            raise UserNotActiveError(f"user {user.id} is {user.status.value}")

        # 2. Reload the offer: the single gate (available + enabled + priced).
        offer = await self._offers.get(offer_id)
        if offer is None or not offer.sellable:
            raise OfferUnavailableError(f"offer {offer_id} is not sellable")

        # 2.5 Replay: same command key -> the original intent, nothing new.
        existing = await self._servers.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.user_id != user.id:
                raise CheckoutReplayError(
                    f"idempotency key {idempotency_key!r} belongs to another user"
                )
            order = await self._orders.get_by_server(existing.id)
            wallet = await self._wallets.get(user.id)
            wallet_id = wallet.id if wallet is not None else None
            hold: Hold | None = (
                await self._holds.get_by_idempotency(wallet_id, hold_key(idempotency_key))
                if wallet_id is not None
                else None
            )
            if order is None:  # pragma: no cover - created atomically with the server
                raise CheckoutError("server intent exists without a provider order row")
            return MonthlyCheckoutResult(
                server=existing, order=order, hold=hold, offer=offer, replayed=True
            )

        # 3. OS validation SERVER-SIDE against the live product API (free
        #    options only by default — the price shown is the price charged).
        ordering = await self._ordering_provider(offer.provider_key)
        try:
            detail = await ordering.get_product(offer.location_id, offer.product_id)
        except Exception as exc:
            logger.warning("offer %s: product detail unavailable: %s", offer.ref, exc)
            raise OfferUnavailableError("product details currently unavailable") from exc
        if not ordering.os_name_allowed(detail, os_name):
            raise OsUnavailableError(f"OS {os_name!r} is not available for {offer.ref}")

        # 4. Provider account (per-customer link row; created on demand).
        account = await self._accounts.get_or_create_active(user.id, offer.provider_key)

        # 5. Wallet + hold (balance validation lives in the hold).
        wallet = await self._wallets.get(user.id)
        if wallet is None or wallet.id is None:
            raise NoWalletError(f"user {user.id} has no wallet")
        wallet_id = wallet.id
        try:
            hold = await self._holds.create_hold(
                wallet_id,
                offer.selling_price_minor,
                offer.selling_currency,
                hold_key(idempotency_key),
            )
        except InsufficientHoldBalanceError:
            raise

        # 6. Server intent (REQUESTED, prepaid monthly, OS recorded).
        del at  # created_at is assigned by the repository
        server = CloudServer(
            id=uuid4(),
            user_id=user.id,
            provider_key=offer.provider_key,
            provider_account_id=account.id,
            state=ServerLifecycleState.REQUESTED,
            billing_model=BILLING_MODEL_PREPAID_MONTHLY,
            os=os_name,
        )
        intent = ServerCreateIntent(
            catalog_id=None,  # prepaid: offer pinned via provider_orders.offer_id
            cost_minor=offer.provider_cost_minor,
            currency=offer.provider_cost_currency,
            idempotency_key=idempotency_key,
        )
        try:
            created = await self._servers.create(server, intent)
        except ServerCreateError:
            # Concurrent duplicate: resolve to the original intent.
            original = await self._servers.get_by_idempotency_key(idempotency_key)
            if original is not None and original.user_id == user.id:
                order = await self._orders.get_by_server(original.id)
                if order is None:  # pragma: no cover
                    raise CheckoutError("concurrent intent without order row") from None
                return MonthlyCheckoutResult(
                    server=original, order=order, hold=hold, offer=offer, replayed=True
                )
            await self._release_hold(hold)
            raise

        # 7. Provider order row + operation ledger — BOTH before any provider
        #    call. The operation key is the IdempotencyKey of the POST, and
        #    the row snapshots every provider-side fact (exact product id,
        #    location, OS, term, cycle, PROVIDER cost and the customer
        #    selling price as SEPARATE snapshots) so an ambiguous POST can be
        #    recovered with read-only scans that NEVER use the selling price
        #    to identify a provider order (release hardening).
        operation_key = order_operation_key(created.id)
        try:
            order = await self._orders.create(
                server_id=created.id,
                operation_key=operation_key,
                provider_key=offer.provider_key,
                offer_id=offer.id,
                product_id=offer.product_id,
                location_id=offer.location_id,
                os_name=os_name,
                contract_term=get_settings().leaseweb_contract_term,
                billing_cycle=get_settings().leaseweb_billing_cycle,
                provider_cost_minor=offer.provider_cost_minor,
                provider_cost_currency=offer.provider_cost_currency,
                selling_price_minor=offer.selling_price_minor,
                selling_currency=offer.selling_currency,
            )
        except Exception:
            await self._fail_intent(created, hold)
            raise
        try:
            await self._ops.get_or_create(
                operation_key=operation_key,
                operation_type=OperationType.ORDER_CREATE,
                resource_type=RESOURCE_TYPE_SERVER_ORDER,
                resource_id=order.id,
                provider_key=offer.provider_key,
            )
        except Exception:
            await self._fail_intent(created, hold)
            raise

        # 8. Audit.
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            actor_id=user.id,
            action="checkout.monthly_order_requested",
            resource_type="server",
            resource_id=str(created.id),
            reason=f"monthly order {offer.ref}",
            metadata={
                "offer": offer.ref,
                "selling_price_minor": str(offer.selling_price_minor),
                "currency": offer.selling_currency,
                "provider_cost_minor": str(offer.provider_cost_minor),
                "os": os_name,
                "order_id": str(order.id),
                "hold_id": str(hold.id) if hold.id is not None else "",
                "billing_model": BILLING_MODEL_PREPAID_MONTHLY,
            },
        )
        logger.info(
            "monthly order intent %s for server %s (offer %s, %d %s, os=%s, hold %s)",
            order.id,
            created.id,
            offer.ref,
            offer.selling_price_minor,
            offer.selling_currency,
            os_name,
            hold.id,
        )
        return MonthlyCheckoutResult(
            server=created, order=order, hold=hold, offer=offer, replayed=False
        )

    async def _fail_intent(self, server: CloudServer, hold: Hold) -> None:
        """Best-effort compensation: ERROR + release hold."""
        try:
            server.transition_to(ServerLifecycleState.ERROR)
            await self._servers.save(server)
        except Exception:
            logger.exception("failed to mark server %s ERROR after checkout failure", server.id)
        await self._release_hold(hold)

    async def _release_hold(self, hold: Hold) -> None:
        if hold.id is None:
            return
        try:
            await self._holds.release_hold(hold.id)
        except Exception:
            logger.exception("failed to release hold %s after checkout failure", hold.id)


# ---------------------------------------------------------------------------
# Customer-facing offer catalog views (browse / confirm)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OfferOsOptionView:
    """One selectable OS for an offer (free options only by default).

    ``index`` is the stable position within the offer's option list; it is
    what rides the signed callback (OS names may contain spaces, which the
    callback wire forbids) and is re-resolved SERVER-SIDE at confirm time.
    """

    name: str
    price_minor: int
    index: int
    select_callback: str  # offers.os --select--> offers.confirm for this OS


@dataclass(frozen=True, slots=True)
class OfferCatalogView:
    """One sellable offer as the customer sees it (specs + monthly price)."""

    offer_id: UUID
    provider_key: str
    product_id: str
    location_id: str
    name: str
    vcpu: int
    ram_gb: int
    disk_gb: int
    traffic: str | None
    monthly_price_minor: int
    currency: str
    billing_model: str = BILLING_MODEL_PREPAID_MONTHLY


@dataclass(frozen=True, slots=True)
class OfferConfirmView:
    """The confirmation screen: offer + OS + exact monthly price + wallet."""

    offer: OfferCatalogView
    os_name: str
    balance_minor: int
    currency: str
    sufficient: bool
    confirm_callback: str
    back_callback: str
    cancel_callback: str


class OfferCatalogViewService:
    """Builds the customer screens of the monthly purchase flow.

    Every screen re-reads the offer price book — an offer that stops being
    sellable between screens simply disappears/errors instead of being sold.
    """

    def __init__(
        self,
        offers_repo: SellableOfferRepository,
        provider_registry: ProviderRegistry,
        wallet_repo: WalletRepository,
        signing_key: str,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        self._offers = offers_repo
        self._registry = provider_registry
        self._wallets = wallet_repo
        self._signing_key = signing_key

    @staticmethod
    def _view(offer: SellableOffer) -> OfferCatalogView:
        return OfferCatalogView(
            offer_id=offer.id,
            provider_key=offer.provider_key,
            product_id=offer.product_id,
            location_id=offer.location_id,
            name=offer.name,
            vcpu=offer.vcpu,
            ram_gb=offer.ram_gb,
            disk_gb=offer.disk_gb,
            traffic=offer.traffic,
            monthly_price_minor=offer.selling_price_minor,
            currency=offer.selling_currency,
        )

    async def os_options(self, offer: SellableOffer) -> list[OfferOsOptionView]:
        """Live OS options for the offer (server-side filtered)."""
        try:
            provider = self._registry.get(offer.provider_key)
        except KeyError:
            raise OfferUnavailableError(f"provider {offer.provider_key!r} not configured") from None
        ordering = ordering_support_of(provider)
        if ordering is None:
            raise OfferUnavailableError("provider does not support ordering")
        detail = await ordering.get_product(offer.location_id, offer.product_id)
        options = (
            detail.free_os_options()
            if get_settings().leaseweb_order_os_only_free
            else detail.os_options
        )
        return [
            OfferOsOptionView(
                name=option.name,
                price_minor=option.price_minor,
                index=i,
                select_callback=self._os_select_callback(offer.id, i),
            )
            for i, option in enumerate(options)
        ]

    def _os_select_callback(self, offer_id: UUID, index: int) -> str:
        from cloud_platform.modules.navigation.domain import Callback, encode_callback

        return encode_callback(
            Callback(flow="offers", screen="os", args=(str(offer_id), str(index))),
            self._signing_key,
        )

    async def os_by_index(self, offer: SellableOffer, index: int) -> str:
        """Resolve a callback-encoded OS index back to its name (re-validated)."""
        options = await self.os_options(offer)
        if index < 0 or index >= len(options):
            raise OsUnavailableError(f"OS option {index} is not available for {offer.ref}")
        return options[index].name

    async def confirmation(
        self,
        *,
        user_id: UUID,
        offer_id: UUID,
        os_index: int,
    ) -> OfferConfirmView:
        """The exact-price confirmation view (enforcement lives in checkout).

        The OS index is re-resolved against the live product API — a stale
        or tampered index simply fails.
        """
        offer = await self._offers.get(offer_id)
        if offer is None or not offer.sellable:
            raise OfferUnavailableError(f"offer {offer_id} is not sellable")
        os_name = await self.os_by_index(offer, os_index)

        wallet = await self._wallets.get(user_id)
        balance = wallet.balance if wallet is not None else 0

        from cloud_platform.modules.navigation.domain import Callback, encode_callback

        args = (str(offer_id), str(os_index))
        confirm_callback = encode_callback(
            Callback(flow="offers", screen="confirm", args=args), self._signing_key
        )
        back_callback = encode_callback(
            Callback(flow="offers", screen="os", args=args), self._signing_key
        )
        cancel_callback = encode_callback(Callback(flow="main", screen="menu"), self._signing_key)
        return OfferConfirmView(
            offer=self._view(offer),
            os_name=os_name,
            balance_minor=balance,
            currency=offer.selling_currency,
            sufficient=balance >= offer.selling_price_minor,
            confirm_callback=confirm_callback,
            back_callback=back_callback,
            cancel_callback=cancel_callback,
        )

    async def os_screen(
        self,
        *,
        offer_id: UUID,
    ) -> tuple[OfferCatalogView, list[OfferOsOptionView], str, str]:
        """The OS picker screen data (offer, options, back, cancel)."""
        offer = await self._offers.get(offer_id)
        if offer is None or not offer.sellable:
            raise OfferUnavailableError(f"offer {offer_id} is not sellable")
        options = await self.os_options(offer)
        if not options:
            raise OsUnavailableError(f"no free OS options for {offer.ref}")

        from cloud_platform.modules.navigation.domain import Callback, encode_callback

        back_callback = encode_callback(
            Callback(flow="offers", screen="plans", args=(str(offer_id),)),
            self._signing_key,
        )
        cancel_callback = encode_callback(Callback(flow="main", screen="menu"), self._signing_key)
        return self._view(offer), options, back_callback, cancel_callback

    async def plans_screen(self, location_id: str) -> tuple[list[OfferCatalogView], str, str]:
        """The plans screen data for one location (sellable offers only)."""
        offers = [
            o
            for o in await self._offers.list_sellable(MVP_PROVIDER_KEY)
            if o.location_id == location_id
        ]
        if not offers:
            raise OfferUnavailableError(f"no sellable offers at {location_id}")
        from cloud_platform.modules.navigation.domain import Callback, encode_callback

        back_callback = encode_callback(
            Callback(flow="offers", screen="locations", args=(location_id,)),
            self._signing_key,
        )
        cancel_callback = encode_callback(Callback(flow="main", screen="menu"), self._signing_key)
        views = [self._view(o) for o in sorted(offers, key=lambda o: o.name)]
        return views, back_callback, cancel_callback

    def plan_callback(self, offer_id: UUID) -> str:
        from cloud_platform.modules.navigation.domain import Callback, encode_callback

        return encode_callback(
            Callback(flow="offers", screen="plans", args=(str(offer_id),)), self._signing_key
        )
