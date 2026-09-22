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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.core.config import get_settings
from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.businesslog.domain import BusinessEventSink, emit_safe
from cloud_platform.modules.businesslog.events import purchase_requested_event
from cloud_platform.modules.catalog.domain import LocationRepository
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    CloudServer,
    ServerCreateError,
    ServerCreateIntent,
    ServerLifecycleState,
    ServerRepository,
)
from cloud_platform.modules.markets.domain import (
    MARKET_ORDER,
    ProviderCatalog,
    UnknownMarketError,
    parse_market,
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
    offer_options_support_of,
    ordering_support_of,
    provisioning_mode_of,
)
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
)
from cloud_platform.providers.registry import ProviderRegistry
from cloud_platform.providers.routing import UnknownCredentialAccountError, provider_for

logger = logging.getLogger(__name__)

#: Provider key of the monthly VPS vertical slice. It is only a DEFAULT for
#: legacy single-provider call sites — the storefront and checkout are
#: provider-neutral and carry the provider key from the offer itself.
MVP_PROVIDER_KEY = "leaseweb"

#: Provider-price drift tolerance (minor units) between the catalog snapshot
#: and a live revalidation read. Mirrors the settlement tolerance used when
#: the worker reconciles provider charges: a cent of rounding is noise, more
#: is a changed product.
_PROVIDER_PRICE_TOLERANCE_MINOR = 1

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
        event_sink: BusinessEventSink | None = None,
        event_market_lookup: Callable[[str], str] | None = None,
        fulfillment_routes: Any | None = None,
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
        self._events = event_sink
        self._market_of = event_market_lookup or (lambda _key: "")
        # LEASEWEB-MULTIACCOUNT: resolves WHICH provider credential account
        # will place the billable order. Optional so a single-credential
        # deployment (and the whole existing test suite) keeps working: with
        # ``None`` the order is placed through the provider's logical adapter
        # and no account is pinned.
        self._routes = fulfillment_routes

    async def _fulfillment_account(self, offer: SellableOffer) -> str | None:
        """Pick the credential account this purchase will be PINNED to.

        The decision is made BEFORE anything is persisted (server, order,
        hold) so the account becomes a durable order fact rather than
        something re-derived later — the worker must never re-decide whose key
        to bill.

        Fail-closed rule: when the provider IS served by credential accounts
        but none currently serves this location/product, the purchase is
        refused. Guessing an account could place a billable order through the
        wrong credential.
        """
        if self._routes is None:
            return None
        resolved: Any = await self._routes.account_for(
            offer.provider_key, offer.location_id, offer.product_id
        )
        if resolved is None and await self._routes.has_any_routes(offer.provider_key):
            raise OfferUnavailableError(
                f"no provider credential account currently serves {offer.ref}"
            )
        return None if resolved is None else str(resolved)

    async def _validate_offer_selection(
        self,
        offer: SellableOffer,
        os_name: str,
        credential_account_id: str | None,
    ) -> None:
        """Prove the selection is still purchasable, provider-neutrally.

        Direct-create providers validate the offer and the selected OS through
        their offer-options port; order-based providers are read through their
        ordering port. Neither branch knows a provider NAME — only which
        capability the port exposes.
        """
        try:
            provider = provider_for(self._registry, offer.provider_key, credential_account_id)
        except UnknownCredentialAccountError as exc:
            raise OfferUnavailableError(str(exc)) from exc
        except KeyError as exc:
            raise OfferUnavailableError(f"provider {offer.provider_key!r} not configured") from exc

        options = offer_options_support_of(provider)
        if options is not None:
            try:
                await options.validate_offer_for_checkout(
                    location_id=offer.location_id,
                    product_id=offer.product_id,
                    os_name=os_name,
                    expected_cost_minor=offer.provider_cost_minor,
                    currency=offer.provider_cost_currency,
                )
            except ProviderConflict as exc:
                logger.warning(
                    "offer %s: provider state moved since catalog sync: %s", offer.ref, exc
                )
                raise OfferUnavailableError("provider price changed since catalog sync") from exc
            except (ProviderNotFound, ProviderAuthError) as exc:
                logger.warning("offer %s: selection no longer offered: %s", offer.ref, exc)
                raise OfferUnavailableError("product details currently unavailable") from exc
            except ProviderError as exc:
                logger.warning("offer %s: selection validation failed: %s", offer.ref, exc)
                raise OfferUnavailableError("product details currently unavailable") from exc
            return

        # The provider is resolved ONCE per validation: resolving it per branch
        # would ask the routing layer (and therefore the credential pin) twice
        # for the same decision.
        ordering: OrderingProvider | None = ordering_support_of(provider)
        if ordering is None:
            raise OfferUnavailableError(
                f"provider {offer.provider_key!r} cannot validate an offer selection"
            )
        try:
            detail = await ordering.get_product(offer.location_id, offer.product_id)
        except Exception as exc:
            logger.warning("offer %s: product detail unavailable: %s", offer.ref, exc)
            raise OfferUnavailableError("product details currently unavailable") from exc
        current_price = detail.product.monthly_price_minor
        if abs(current_price - offer.provider_cost_minor) > _PROVIDER_PRICE_TOLERANCE_MINOR:
            logger.warning(
                "offer %s: provider price moved %s -> %s since catalog sync",
                offer.ref,
                offer.provider_cost_minor,
                current_price,
            )
            raise OfferUnavailableError("provider price changed since catalog sync")
        if detail.product.currency != offer.provider_cost_currency:
            logger.warning("offer %s: provider currency moved since catalog sync", offer.ref)
            raise OfferUnavailableError("provider price changed since catalog sync")
        if not ordering.os_name_allowed(detail, os_name):
            raise OsUnavailableError(f"OS {os_name!r} is not available for {offer.ref}")

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
        #    HOW that proof is obtained is provider-neutral: an order-based
        #    provider is read through its ordering port, a direct-create
        #    provider through its offer-options port. Both prove the same
        #    facts — the offer is still sold at this location, the provider
        #    price/currency have not moved since catalog sync, and the selected
        #    OS really exists — and the price comparison additionally guards
        #    the race between catalog sync and checkout.
        credential_account_id = await self._fulfillment_account(offer)
        await self._validate_offer_selection(offer, os_name, credential_account_id)

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
            credential_account_id=credential_account_id,
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
                credential_account_id=credential_account_id,
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
        # Operator channel: the customer confirmed a purchase and a durable
        # intent exists (server + order + operation + hold). Enqueued only —
        # delivery happens in the worker, never inline.
        await emit_safe(
            self._events,
            purchase_requested_event(
                user=user,
                server_id=created.id,
                order_id=order.id,
                provider_key=offer.provider_key,
                market=self._market_of(offer.provider_key),
                location_id=offer.location_id,
                plan_name=offer.name,
                product_id=offer.product_id,
                os_name=os_name,
                selling_price_minor=offer.selling_price_minor,
                currency=offer.selling_currency,
            ),
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


@dataclass(frozen=True, slots=True)
class MarketOptionView:
    """One market button (``🇮🇷 سرور ایران`` / ``🌍 سرور خارج``)."""

    market: str
    label_key: str
    title_key: str
    select_callback: str


@dataclass(frozen=True, slots=True)
class ProviderOptionView:
    """One provider row of the storefront (customer-facing name only)."""

    provider_key: str
    market: str
    display_name: str
    buyable: bool
    offer_count: int
    select_callback: str | None  # None when the provider cannot sell yet


@dataclass(frozen=True, slots=True)
class LocationOptionView:
    """One location button of a provider."""

    location_id: str
    offer_count: int
    select_callback: str


@dataclass(frozen=True, slots=True)
class ProductGroupView:
    """One customer-facing PRODUCT CARD: a product and where it is available.

    A provider can sell the SAME product at several locations (inventory is
    identified by ``(credential account, location, product)``), but the customer
    sees one card per product/spec/price combination with its locations listed
    underneath — never one duplicated card per location.
    """

    product_id: str
    name: str
    vcpu: int
    ram_gb: int
    disk_gb: int
    traffic: str | None
    monthly_price_minor: int
    currency: str
    locations: tuple[str, ...]
    select_callback: str


@dataclass(frozen=True, slots=True)
class ProductLocationView:
    """One availability option of a product card: a location with its own
    sellable offer (its own price, its own credential account behind it)."""

    location_id: str
    offer_id: UUID
    name: str
    country_code: str | None
    #: Product display name (same for every row of one card).
    product_name: str
    monthly_price_minor: int
    currency: str
    select_callback: str


class OfferCatalogViewService:
    """Builds the customer screens of the monthly purchase flow.

    Every screen re-reads the offer price book — an offer that stops being
    sellable between screens simply disappears/errors instead of being sold.

    Navigation is provider-neutral and market-first::

        store.market -> store.providers:{market} -> store.locations:{provider}
        -> store.plans:{provider}:{location} -> store.os:{offer}
        -> store.confirm:{offer}:{os_index} -> store.buy:{offer}:{os_index}

    Which providers appear under a market comes from the operator
    configuration (``ProviderCatalog``); whether one can actually SELL comes
    from the provider port itself (the provisioning capability), so both
    fulfillment modes are first-class: an order-based provider (asynchronous
    ordering) and a direct-create provider (synchronous compute creation).
    Neither the domain nor this service contains a concrete provider name.
    """

    def __init__(
        self,
        offers_repo: SellableOfferRepository,
        provider_registry: ProviderRegistry,
        wallet_repo: WalletRepository,
        signing_key: str,
        market_catalog: ProviderCatalog | None = None,
        location_repo: LocationRepository | None = None,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        self._locations = location_repo
        self._offers = offers_repo
        self._registry = provider_registry
        self._wallets = wallet_repo
        self._signing_key = signing_key
        self._markets = market_catalog or ProviderCatalog()

    # -- storefront: markets / providers / locations -----------------------

    def _store_callback(self, screen: str, *args: str) -> str:
        from cloud_platform.modules.navigation.domain import Callback, encode_callback

        return encode_callback(Callback(flow="store", screen=screen, args=args), self._signing_key)

    def markets_screen(self) -> list[MarketOptionView]:
        """The two markets the customer chooses between (always both)."""
        return [
            MarketOptionView(
                market=market.value,
                label_key=market.label_key,
                title_key=market.title_key,
                select_callback=self._store_callback("providers", market.value),
            )
            for market in MARKET_ORDER
        ]

    def provider_display_name(self, provider_key: str) -> str:
        """Customer-facing provider name from configuration (never the key)."""
        return self._markets.display_name_of(provider_key)

    def _provisioning_capable(self, provider_key: str) -> bool:
        """Whether the provider can actually provision what we sell.

        Provider-neutral and mode-aware: an order-based provider is capable
        through its ordering port, a direct-create provider through ordinary
        compute creation.
        """
        try:
            provider = self._registry.get(provider_key)
        except KeyError:
            return False
        return provisioning_mode_of(provider) is not None

    async def providers_screen(self, market: str) -> tuple[list[ProviderOptionView], str]:
        """Providers of one market that have sellable offers (+ back callback)."""
        try:
            wanted = parse_market(market)
        except UnknownMarketError as exc:
            raise OfferUnavailableError(str(exc)) from exc
        counts: dict[str, int] = {}
        order: list[str] = []
        for offer in await self._offers.list_sellable():
            provider_key = offer.provider_key
            if not offer.sellable:
                continue
            if self._markets.market_of(provider_key) is not wanted:
                continue
            if not self._markets.is_enabled(provider_key):
                continue
            counts[provider_key] = counts.get(provider_key, 0) + 1
            order.append(provider_key)
        views: list[ProviderOptionView] = []
        for provider_key in dict.fromkeys(order):
            capable = self._provisioning_capable(provider_key)
            views.append(
                ProviderOptionView(
                    provider_key=provider_key,
                    market=wanted.value,
                    display_name=self._markets.display_name_of(provider_key),
                    buyable=capable,
                    offer_count=counts[provider_key],
                    select_callback=(
                        self._store_callback("products", provider_key) if capable else None
                    ),
                )
            )
        return views, self._store_callback("market")

    async def locations_screen(
        self, provider_key: str
    ) -> tuple[list[LocationOptionView], str, str]:
        """Locations of one provider with sellable offers (+ back, cancel)."""
        counts: dict[str, int] = {}
        for offer in await self._offers.list_sellable(provider_key):
            counts[offer.location_id] = counts.get(offer.location_id, 0) + 1
        if not counts:
            raise OfferUnavailableError(f"no sellable offers for provider {provider_key!r}")
        views = [
            LocationOptionView(
                location_id=location_id,
                offer_count=count,
                select_callback=self._store_callback("plans", provider_key, location_id),
            )
            for location_id, count in sorted(counts.items())
        ]
        market = self._markets.market_of(provider_key)
        back = self._store_callback("providers", market.value if market else "")
        cancel = self._store_callback("market")
        return views, back, cancel

    # -- storefront: product-centric catalog -----------------------------

    @staticmethod
    def _product_signature(offer: SellableOffer) -> tuple[object, ...]:
        """What makes two offers the SAME customer-facing product card.

        Product identity PLUS the visible spec and price. Offers that differ in
        either stay separate cards, because they are genuinely different things
        to buy; only identical ones are merged into one card with several
        locations.
        """
        return (
            offer.product_id,
            offer.name,
            offer.vcpu,
            offer.ram_gb,
            offer.disk_gb,
            offer.traffic or "",
            offer.selling_price_minor,
            offer.selling_currency,
        )

    async def products_screen(self, provider_key: str) -> tuple[list[ProductGroupView], str, str]:
        """Aggregated inventory of one provider: one card per product.

        A provider sells from SEVERAL credential accounts, each seeing its own
        locations, so the same product may be sellable in many places. The
        customer sees one card per product with its availability listed as
        locations — never one duplicated card per location, account or key.
        """
        offers = [o for o in await self._offers.list_sellable(provider_key) if o.sellable]
        if not offers:
            raise OfferUnavailableError(f"no sellable offers for provider {provider_key!r}")
        groups: dict[tuple[object, ...], list[SellableOffer]] = {}
        for offer in sorted(offers, key=lambda o: (o.name, o.location_id, str(o.id))):
            groups.setdefault(self._product_signature(offer), []).append(offer)
        views: list[ProductGroupView] = []
        for rows in groups.values():
            head = rows[0]
            # Order-preserving dedup: distinct locations, stable order.
            locations = tuple(dict.fromkeys(row.location_id for row in rows))
            views.append(
                ProductGroupView(
                    product_id=head.product_id,
                    name=head.name,
                    vcpu=head.vcpu,
                    ram_gb=head.ram_gb,
                    disk_gb=head.disk_gb,
                    traffic=head.traffic,
                    monthly_price_minor=head.selling_price_minor,
                    currency=head.selling_currency,
                    locations=locations,
                    # The card is identified by product AND price: two cards
                    # that differ only by price (different provider cost per
                    # credential account) must not lead to the same list.
                    select_callback=self._store_callback(
                        "product_locations",
                        provider_key,
                        head.product_id,
                        str(head.selling_price_minor),
                    ),
                )
            )
        market = self._markets.market_of(provider_key)
        back = self._store_callback("providers", market.value if market else "")
        return views, back, self._store_callback("market")

    async def product_locations_screen(
        self, provider_key: str, product_id: str, price_minor: int | None = None
    ) -> tuple[list[ProductLocationView], str, str]:
        """Where one product is available, each row its own sellable offer.

        ``price_minor`` narrows to the product card the customer tapped (the
        same product can, in principle, carry different prices in different
        locations); omitting it lists every availability of the product.
        """
        offers = [
            o
            for o in await self._offers.list_sellable(provider_key)
            if o.sellable
            and o.product_id == product_id
            and (price_minor is None or o.selling_price_minor == price_minor)
        ]
        if not offers:
            raise OfferUnavailableError(
                f"no sellable offers for product {product_id!r} of provider {provider_key!r}"
            )
        names = await self._location_metadata(provider_key)
        views = [
            ProductLocationView(
                location_id=offer.location_id,
                offer_id=offer.id,
                name=names.get(offer.location_id, (offer.location_id, None))[0],
                country_code=names.get(offer.location_id, (offer.location_id, None))[1],
                product_name=offer.name,
                monthly_price_minor=offer.selling_price_minor,
                currency=offer.selling_currency,
                select_callback=self._store_callback("os", str(offer.id)),
            )
            for offer in sorted(offers, key=lambda o: o.location_id)
        ]
        return (
            views,
            self._store_callback("products", provider_key),
            self._store_callback("market"),
        )

    async def _location_metadata(self, provider_key: str) -> dict[str, tuple[str, str | None]]:
        """Synced display names per location; falls back to the code.

        Presentation only: a location the provider started selling at before
        its catalog row synced still shows (as its code) instead of disappearing.
        """
        if self._locations is None:
            return {}
        try:
            records = await self._locations.list_for_provider(provider_key)
        except Exception:  # pragma: no cover - display metadata is optional
            logger.warning("location display metadata unavailable for %s", provider_key)
            return {}
        return {
            record.location_id: (record.name or record.location_id, record.country_code)
            for record in records
        }

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
        """Live OS options for the offer (server-side filtered).

        A provider that exposes the offer-options capability enumerates its own
        creatable images; an order-based provider is read through its ordering
        product detail. The customer never sees a provider API DTO.
        """
        try:
            provider = self._registry.get(offer.provider_key)
        except KeyError:
            raise OfferUnavailableError(f"provider {offer.provider_key!r} not configured") from None
        options_port = offer_options_support_of(provider)
        if options_port is not None:
            live = await options_port.get_os_options(offer.location_id, offer.product_id)
            return [
                OfferOsOptionView(
                    name=option.name,
                    price_minor=option.price_minor,
                    index=i,
                    select_callback=self._os_select_callback(offer.id, i),
                )
                for i, option in enumerate(live)
            ]
        ordering = ordering_support_of(provider)
        if ordering is None:
            raise OfferUnavailableError(
                f"provider {offer.provider_key!r} cannot enumerate operating systems"
            )
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
        """The signed callback that SELECTING this OS leads to (confirm)."""
        return self._store_callback("confirm", str(offer_id), str(index))

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

        confirm_callback = self._store_callback("buy", str(offer_id), str(os_index))
        back_callback = self._store_callback("os", str(offer_id))
        cancel_callback = self._store_callback("market")
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

        # Back returns to the product's availability list, so the customer
        # stays in product-first navigation (market -> provider -> product).
        back_callback = self._store_callback(
            "product_locations", offer.provider_key, offer.product_id
        )
        cancel_callback = self._store_callback("market")
        return self._view(offer), options, back_callback, cancel_callback

    async def plans_screen(
        self, location_id: str, provider_key: str | None = None
    ) -> tuple[list[OfferCatalogView], str, str]:
        """The plans screen data for one location (sellable offers only).

        ``provider_key`` scopes the location to one provider (the storefront
        always passes it); omitting it keeps the legacy single-location
        behaviour for callers that predate the market selector.
        """
        offers = [
            o
            for o in await self._offers.list_sellable(provider_key)
            if o.location_id == location_id and o.sellable
        ]
        if not offers:
            raise OfferUnavailableError(f"no sellable offers at {location_id}")
        resolved_provider = provider_key or offers[0].provider_key
        back_callback = self._store_callback("locations", resolved_provider)
        cancel_callback = self._store_callback("market")
        views = [self._view(o) for o in sorted(offers, key=lambda o: o.name)]
        return views, back_callback, cancel_callback

    def plan_callback(self, offer_id: UUID) -> str:
        return self._store_callback("os", str(offer_id))
