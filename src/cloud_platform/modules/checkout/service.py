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
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from cloud_platform.core.config import get_settings
from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.businesslog.domain import BusinessEventSink, emit_safe
from cloud_platform.modules.businesslog.events import purchase_requested_event
from cloud_platform.modules.catalog.domain import LocationRepository
from cloud_platform.modules.catalog.image_compatibility import (
    image_architecture_conflict,
    image_compatible,
)
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    CloudServer,
    ServerCreateError,
    ServerCreateIntent,
    ServerLifecycleState,
    ServerRepository,
)
from cloud_platform.modules.markets.domain import (
    FAMILY_BILLINGS,
    FAMILY_ORDER,
    MARKET_ORDER,
    ProviderCatalog,
    ProviderProductFamily,
    UnknownMarketError,
    parse_market,
)
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    BILLING_MODEL_MONTHLY,
    HOURLY_MONTHLY_ESTIMATE_HOURS,
    SellableOffer,
    SellableOfferRepository,
    TechnicalSpec,
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
        panel_name: str | None = None,
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
        # 2b. Billing model: this command sells prepaid-monthly products
        # only. Hourly products go through the hourly creation command, so a
        # misrouted hourly offer fails here instead of taking a monthly hold.
        if offer.billing_model != BILLING_MODEL_MONTHLY:
            raise OfferUnavailableError(f"offer {offer_id} is not a monthly plan")

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
                control_panel=panel_name,
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
                "control_panel": panel_name or "",
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
    #: Free control panel chosen during configuration (None = no panel).
    panel_name: str | None = None


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
    #: Unique country codes of the card's locations (order-preserving);
    #: empty when no synced location row states one. Presentation only.
    country_codes: tuple[str, ...] = ()


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


#: Products per rendered page (Telegram screens stay short and readable).
PRODUCTS_PAGE_SIZE = 6


@dataclass(frozen=True, slots=True)
class FamilyOptionView:
    # One commercial product line of a provider on the family screen.
    # ``sellable_count``/``available`` describe CURRENT inventory only: a
    # configured family with zero sellable offers is still shown (as
    # unavailable), never hidden — family existence and inventory are
    # different facts.
    provider_key: str
    family_key: str
    billing_model: str
    display_name: str
    select_callback: str
    sellable_count: int = 0
    available: bool = True


@dataclass(frozen=True, slots=True)
class FamilyLocationView:
    # One location button with normalized display metadata.
    location_id: str
    name: str
    country_code: str | None
    city: str | None
    select_callback: str


@dataclass(frozen=True, slots=True)
class LocationPageView:
    # One page of a provider's locations with pager navigation.
    provider_key: str
    items: tuple[FamilyLocationView, ...]
    page: int
    total_pages: int
    total_count: int
    back_callback: str
    cancel_callback: str
    prev_callback: str | None
    next_callback: str | None


@dataclass(frozen=True, slots=True)
class CityLocationGroup:
    # One customer-facing city: exact provider locations sharing normalized
    # (country_code, city). ``min_price_minor``/``currency`` describe the
    # cheapest sellable offer in the group and are None when the group mixes
    # selling currencies (prices in different currencies are never compared)
    # or has no priced offer. ``select_callback`` enters plans directly for a
    # single-location city and the datacenter screen otherwise.
    country_code: str | None
    city: str
    location_ids: tuple[str, ...]
    plan_count: int
    min_price_minor: int | None
    currency: str | None
    select_callback: str


@dataclass(frozen=True, slots=True)
class CityPageView:
    # One page of a family's city groups with pager navigation.
    provider_key: str
    family_key: str
    billing_model: str
    items: tuple[CityLocationGroup, ...]
    page: int
    total_pages: int
    total_count: int
    back_callback: str
    cancel_callback: str
    prev_callback: str | None
    next_callback: str | None


@dataclass(frozen=True, slots=True)
class DatacenterLocationView:
    # One exact provider datacenter/hall inside a city, differentiated with
    # real catalog facts only (plan count, minimum native price) — never
    # invented hardware differences.
    location_id: str
    name: str
    country_code: str | None
    city: str | None
    plan_count: int
    min_price_minor: int | None
    currency: str | None
    select_callback: str


@dataclass(frozen=True, slots=True)
class DatacenterPageView:
    # The exact halls of one city with pager navigation.
    provider_key: str
    family_key: str
    billing_model: str
    country_code: str | None
    city: str
    items: tuple[DatacenterLocationView, ...]
    page: int
    total_pages: int
    total_count: int
    back_callback: str
    cancel_callback: str
    prev_callback: str | None
    next_callback: str | None


@dataclass(frozen=True, slots=True)
class FamilyPlanView:
    # One plan row at one location: specs, exact price, its own offer.
    offer_id: UUID
    product_id: str
    name: str
    vcpu: int
    ram_gb: int
    disk_gb: int
    traffic: str | None
    monthly_price_minor: int
    currency: str
    select_callback: str
    # Normalized technical facts (storage type for the row label, ...).
    technical_metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanPageView:
    # One page of plans at one location with pager navigation.
    provider_key: str
    location_id: str
    items: tuple[FamilyPlanView, ...]
    page: int
    total_pages: int
    total_count: int
    back_callback: str
    cancel_callback: str
    prev_callback: str | None
    next_callback: str | None


@dataclass(frozen=True, slots=True)
class PlanDetailView:
    # One exact offer opened: full spec, proven technical facts, and the
    # continue action. The location is already fixed — no location choice
    # remains at this point.
    offer: OfferCatalogView
    technical_metadata: dict[str, object]
    location_name: str
    location_country: str | None
    continue_callback: str
    back_callback: str
    cancel_callback: str


@dataclass(frozen=True, slots=True)
class PanelOptionView:
    # One free control-panel choice (None = no panel), leading to confirm.
    name: str | None
    index: int
    select_callback: str


@dataclass(frozen=True, slots=True)
class CloudConfirmView:
    # Hourly creation confirmation: exact hourly price, monthly estimate,
    # wallet balance and the create action. Billed per quantum by accrual;
    # nothing is charged upfront, so there is no sufficiency gate here —
    # non-payment is handled by the existing low-balance suspension.
    offer: OfferCatalogView
    image_label: str
    hourly_price_minor: int
    monthly_estimate_minor: int
    currency: str
    balance_minor: int
    location_name: str
    location_country: str | None
    confirm_callback: str
    back_callback: str
    cancel_callback: str


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
        cloud_providers: Mapping[str, Any] | None = None,
        cloud_resolver: Any | None = None,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        self._locations = location_repo
        self._offers = offers_repo
        self._registry = provider_registry
        self._wallets = wallet_repo
        self._signing_key = signing_key
        self._markets = market_catalog or ProviderCatalog()
        self._cloud = dict(cloud_providers or {})
        # Provider-neutral hourly-cloud dispatch (multi-account): resolves
        # (provider_key, credential_account_id) to the exact adapter that
        # owns the observation. The plain dict stays as the legacy
        # single-adapter fallback. No provider-name branching here.
        self._cloud_resolver = cloud_resolver

    # -- storefront: markets / providers / locations -----------------------

    def _store_callback(self, screen: str, *args: str) -> str:
        from cloud_platform.modules.navigation.domain import Callback, encode_callback

        return encode_callback(Callback(flow="store", screen=screen, args=args), self._signing_key)

    def _store_nav_callback(self, screen: str, *args: str) -> str:
        """Store callback for Telegram buttons (size-enforced).

        Every callback emitted onto a Telegram button must fit the 64-byte
        limit, so generation fails fast here instead of dying silently in
        Telegram. Offer identity travels as a compact reversible reference
        (see ``encode_offer_ref``), never as a full UUID.
        """
        from cloud_platform.modules.navigation.domain import Callback, encode_telegram_callback

        return encode_telegram_callback(
            Callback(flow="store", screen=screen, args=args), self._signing_key
        )

    @staticmethod
    def _offer_ref(offer_id: UUID) -> str:
        """Compact reversible offer identity for Telegram callbacks."""
        from cloud_platform.modules.navigation.domain import encode_offer_ref

        return encode_offer_ref(offer_id)

    def markets_screen(self) -> list[MarketOptionView]:
        """The two markets the customer chooses between (always both)."""
        return [
            MarketOptionView(
                market=market.value,
                label_key=market.label_key,
                title_key=market.title_key,
                select_callback=self._store_nav_callback("providers", market.value),
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
                        self._store_nav_callback("families", provider_key) if capable else None
                    ),
                )
            )
        return views, self._store_nav_callback("market")

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
                select_callback=self._store_nav_callback("plans", provider_key, location_id),
            )
            for location_id, count in sorted(counts.items())
        ]
        market = self._markets.market_of(provider_key)
        back = self._store_nav_callback("providers", market.value if market else "")
        cancel = self._store_nav_callback("market")
        return views, back, cancel

    # -- storefront: product families ------------------------------------

    # Implicit family keys for providers without configured families, keyed
    # by billing model (short, callback-safe).
    _IMPLICIT_FAMILY_KEYS: ClassVar[dict[str, str]] = {
        BILLING_MODEL_MONTHLY: "monthly",
        BILLING_MODEL_HOURLY: "hourly",
    }

    def _sellable_families(
        self, provider_key: str, offers: list[SellableOffer]
    ) -> list[ProviderProductFamily]:
        # Families with sellable offers: configured ones first (by billing),
        # then implicit ones for billing models without configuration.
        present = sorted({o.billing_model for o in offers if o.sellable})
        configured = {f.billing_model: f for f in self._markets.families_of(provider_key)}
        out: list[ProviderProductFamily] = []
        for billing in present:
            family = configured.get(billing)
            if family is None:
                if billing not in FAMILY_BILLINGS:
                    continue
                family = ProviderProductFamily(
                    provider_key=provider_key,
                    family_key=self._IMPLICIT_FAMILY_KEYS.get(billing, billing),
                    billing_model=billing,
                    display_name=self._markets.display_name_of(provider_key),
                )
            out.append(family)
        order = {billing: index for index, billing in enumerate(FAMILY_ORDER)}
        return sorted(out, key=lambda fam: order.get(fam.billing_model, len(order)))

    async def families_screen(self, provider_key: str) -> tuple[list[FamilyOptionView], str, str]:
        # Commercial product lines of one provider (monthly VPS vs hourly
        # cloud). Configured families are ALWAYS listed — current inventory
        # controls each row's count/availability, never whether the family
        # concept exists. Back goes to the market providers.
        offers = [o for o in await self._offers.list_sellable(provider_key) if o.sellable]
        configured = self._markets.families_of(provider_key)
        if configured:
            counts: dict[str, int] = {}
            for offer in offers:
                counts[offer.billing_model] = counts.get(offer.billing_model, 0) + 1
            views = [
                FamilyOptionView(
                    provider_key=provider_key,
                    family_key=family.family_key,
                    billing_model=family.billing_model,
                    display_name=family.display_name,
                    select_callback=self._store_nav_callback(
                        "family", provider_key, family.family_key
                    ),
                    sellable_count=counts.get(family.billing_model, 0),
                    available=counts.get(family.billing_model, 0) > 0,
                )
                for family in configured
            ]
        else:
            if not offers:
                raise OfferUnavailableError(f"no sellable offers for provider {provider_key!r}")
            views = [
                FamilyOptionView(
                    provider_key=provider_key,
                    family_key=family.family_key,
                    billing_model=family.billing_model,
                    display_name=family.display_name,
                    select_callback=self._store_nav_callback(
                        "family", provider_key, family.family_key
                    ),
                )
                for family in self._sellable_families(provider_key, offers)
            ]
            if not views:
                raise OfferUnavailableError(f"no sellable offers for provider {provider_key!r}")
        market = self._markets.market_of(provider_key)
        back = self._store_nav_callback("providers", market.value if market else "")
        return views, back, self._store_nav_callback("market")

    async def resolve_family(self, provider_key: str, family_key: str) -> ProviderProductFamily:
        # One family by key. A configured family resolves by EXISTENCE even
        # when it currently has zero sellable offers, so its button lands on
        # the explicit unavailable screen instead of reopening the selector;
        # every downstream screen still enforces sellability itself and raises
        # OfferUnavailableError for an empty family.
        for family in self._markets.families_of(provider_key):
            if family.family_key == family_key:
                return family
        offers = [o for o in await self._offers.list_sellable(provider_key) if o.sellable]
        for family in self._sellable_families(provider_key, offers):
            if family.family_key == family_key:
                return family
        raise OfferUnavailableError(
            f"no sellable {family_key!r} offers for provider {provider_key!r}"
        )

    async def family_locations_screen(
        self, provider_key: str, family_key: str, page: int = 1, page_size: int = 6
    ) -> LocationPageView:
        # Locations with sellable offers of one family, paginated, with
        # normalized display metadata. Only locations the configured
        # account(s) can actually discover products for ever appear.
        family = await self.resolve_family(provider_key, family_key)
        counts: dict[str, int] = {}
        for offer in await self._offers.list_sellable(provider_key):
            if offer.sellable and offer.billing_model == family.billing_model:
                counts[offer.location_id] = counts.get(offer.location_id, 0) + 1
        if not counts:
            raise OfferUnavailableError(
                f"no sellable {family_key!r} offers for provider {provider_key!r}"
            )
        names = await self._location_metadata(provider_key)
        ordered = sorted(counts)
        total = len(ordered)
        size = max(1, page_size)
        total_pages = max(1, -(-total // size))
        current = min(max(1, page), total_pages)
        items = tuple(
            FamilyLocationView(
                location_id=code,
                name=names.get(code, (code, None, None))[0],
                country_code=names.get(code, (code, None, None))[1],
                city=None,
                select_callback=self._family_location_callback(family, provider_key, code),
            )
            for code in ordered[(current - 1) * size : current * size]
        )
        market = self._markets.market_of(provider_key)
        return LocationPageView(
            provider_key=provider_key,
            items=items,
            page=current,
            total_pages=total_pages,
            total_count=total,
            # Back skips the family screen: for a single family it would
            # auto-forward straight back here (a loop); multi-family users
            # re-enter families through the provider.
            back_callback=self._store_nav_callback("providers", market.value if market else ""),
            cancel_callback=self._store_nav_callback("market"),
            prev_callback=(
                self._family_locations_callback(family, provider_key, current - 1)
                if current > 1
                else None
            ),
            next_callback=(
                self._family_locations_callback(family, provider_key, current + 1)
                if current < total_pages
                else None
            ),
        )

    def _family_location_callback(
        self, family: ProviderProductFamily, provider_key: str, location_id: str
    ) -> str:
        # Next step depends on billing, never on provider: monthly lists
        # plans, hourly lists plan families.
        if family.billing_model == BILLING_MODEL_HOURLY:
            return self._store_nav_callback(
                "cloud_families", provider_key, family.family_key, location_id
            )
        return self._store_nav_callback(
            "vps_plans", provider_key, family.family_key, location_id, "1"
        )

    def _family_locations_callback(
        self, family: ProviderProductFamily, provider_key: str, page: int
    ) -> str:
        if family.billing_model == BILLING_MODEL_HOURLY:
            return self._store_nav_callback(
                "cloud_locations", provider_key, family.family_key, str(page)
            )
        return self._store_nav_callback("vps_locations", provider_key, family.family_key, str(page))

    # -- storefront: city grouping / datacenter drill-down ---------------

    _CITY_SLUG_CLEANUP = re.compile(r"[^A-Za-z0-9]+")

    #: Placeholder for an empty country inside signed callbacks (callback
    #: fields must be non-empty; "-" decodes back to "").
    _EMPTY_COUNTRY_ARG = "-"

    @classmethod
    def city_slug(cls, city: str) -> str:
        """Compact callback-safe handle for a city label (lookup key only).

        Slugs are recomputed from CURRENT groups on every resolve, so a
        renamed city can at worst show the unavailable screen — never a
        wrong datacenter.
        """
        slug = cls._CITY_SLUG_CLEANUP.sub("-", (city or "").strip()).strip("-").upper()
        return slug or "-"

    @classmethod
    def _country_arg(cls, country_code: str | None) -> str:
        code = (country_code or "").strip().upper()
        return code or cls._EMPTY_COUNTRY_ARG

    @classmethod
    def _country_from_arg(cls, arg: str) -> str:
        if arg == cls._EMPTY_COUNTRY_ARG:
            return ""
        return (arg or "").strip().upper()

    @staticmethod
    def _single_currency_minimum(
        offers: list[SellableOffer],
    ) -> tuple[int | None, str | None]:
        """Cheapest price iff every offer shares one selling currency.

        Prices in different currencies are never compared: a mixed group
        reports no minimum instead of a misleading one.
        """
        best: dict[str, int] = {}
        for offer in offers:
            currency = (offer.selling_currency or "").strip().upper()
            if not currency:
                continue
            if currency not in best or offer.selling_price_minor < best[currency]:
                best[currency] = offer.selling_price_minor
        if len(best) == 1:
            currency, minimum = next(iter(best.items()))
            return minimum, currency
        return None, None

    async def _family_sellable_offers(
        self, provider_key: str, family: ProviderProductFamily
    ) -> list[SellableOffer]:
        return [
            offer
            for offer in await self._offers.list_sellable(provider_key)
            if offer.sellable and offer.billing_model == family.billing_model
        ]

    def _group_offers_by_city(
        self,
        offers: list[SellableOffer],
        facts: Mapping[str, tuple[str, str | None, str | None]],
    ) -> list[tuple[str, str, list[SellableOffer]]]:
        """Group sellable offers by normalized (country, city).

        Provider-neutral: the grouping key comes only from synced
        ``ProviderLocation`` facts, never from provider-specific code
        patterns. A location without city metadata forms its own
        single-location group so inventory is never hidden.
        """
        buckets: dict[tuple[str, str], list[SellableOffer]] = {}
        for offer in offers:
            name, country, city = facts.get(offer.location_id, (offer.location_id, None, None))
            country_norm = (country or "").strip().upper()
            label = (city or name or offer.location_id).strip()
            buckets.setdefault((country_norm, label), []).append(offer)
        return [(country, label, bucket) for (country, label), bucket in sorted(buckets.items())]

    def _family_cities_callback(
        self, family: ProviderProductFamily, provider_key: str, page: int
    ) -> str:
        # City list of one family (monthly and hourly share the screen; the
        # family key in the args keeps the flows apart).
        return self._store_nav_callback("loc_cities", provider_key, family.family_key, str(page))

    def _family_halls_callback(
        self,
        family: ProviderProductFamily,
        provider_key: str,
        country_code: str | None,
        city: str,
        page: int,
    ) -> str:
        return self._store_nav_callback(
            "loc_halls",
            provider_key,
            family.family_key,
            self._country_arg(country_code),
            self.city_slug(city),
            str(page),
        )

    async def cities_screen(
        self, provider_key: str, family_key: str, page: int = 1, page_size: int = 6
    ) -> CityPageView:
        """City groups of one family with sellable offers, paginated.

        The first location step is always country/city — never raw
        datacenter/hall codes. A single-location city enters its plans
        directly; a multi-hall city opens the datacenter screen.
        """
        family = await self.resolve_family(provider_key, family_key)
        offers = await self._family_sellable_offers(provider_key, family)
        if not offers:
            raise OfferUnavailableError(
                f"no sellable {family_key!r} offers for provider {provider_key!r}"
            )
        facts = await self._location_metadata(provider_key)
        groups = self._group_offers_by_city(offers, facts)
        total = len(groups)
        size = max(1, page_size)
        total_pages = max(1, -(-total // size))
        current = min(max(1, page), total_pages)
        items = tuple(
            self._city_group_view(provider_key, family, country, label, bucket)
            for country, label, bucket in groups[(current - 1) * size : current * size]
        )
        # Back skips the family screen when it would auto-forward straight
        # back here (a single family loops); multi-family users re-enter
        # families through the provider.
        market = self._markets.market_of(provider_key)
        providers_back = self._store_nav_callback("providers", market.value if market else "")
        configured = self._markets.families_of(provider_key)
        if configured:
            show_selector = len(configured) > 1
        else:
            all_sellable = [
                offer for offer in await self._offers.list_sellable(provider_key) if offer.sellable
            ]
            show_selector = len(self._sellable_families(provider_key, all_sellable)) > 1
        return CityPageView(
            provider_key=provider_key,
            family_key=family.family_key,
            billing_model=family.billing_model,
            items=items,
            page=current,
            total_pages=total_pages,
            total_count=total,
            back_callback=(
                self._store_nav_callback("families", provider_key)
                if show_selector
                else providers_back
            ),
            cancel_callback=self._store_nav_callback("market"),
            prev_callback=(
                self._family_cities_callback(family, provider_key, current - 1)
                if current > 1
                else None
            ),
            next_callback=(
                self._family_cities_callback(family, provider_key, current + 1)
                if current < total_pages
                else None
            ),
        )

    def _city_group_view(
        self,
        provider_key: str,
        family: ProviderProductFamily,
        country: str,
        label: str,
        bucket: list[SellableOffer],
    ) -> CityLocationGroup:
        locations = tuple(dict.fromkeys(offer.location_id for offer in bucket))
        minimum, currency = self._single_currency_minimum(bucket)
        if len(locations) == 1:
            select = self._family_location_callback(family, provider_key, locations[0])
        else:
            select = self._family_halls_callback(family, provider_key, country or None, label, 1)
        return CityLocationGroup(
            country_code=country or None,
            city=label,
            location_ids=locations,
            plan_count=len(bucket),
            min_price_minor=minimum,
            currency=currency,
            select_callback=select,
        )

    async def city_locations_screen(
        self,
        provider_key: str,
        family_key: str,
        country_arg: str,
        city_slug: str,
        page: int = 1,
        page_size: int = 6,
    ) -> DatacenterPageView:
        """Exact datacenters/halls of one city, with real catalog facts.

        Each row carries its plan count and minimum native price so sibling
        halls read as distinct provider datacenters, not unexplained
        duplicates. An unknown or ambiguous slug fails closed.
        """
        family = await self.resolve_family(provider_key, family_key)
        offers = await self._family_sellable_offers(provider_key, family)
        if not offers:
            raise OfferUnavailableError(
                f"no sellable {family_key!r} offers for provider {provider_key!r}"
            )
        facts = await self._location_metadata(provider_key)
        wanted_country = self._country_from_arg(country_arg)
        matches = [
            (country, label, bucket)
            for country, label, bucket in self._group_offers_by_city(offers, facts)
            if country == wanted_country and self.city_slug(label) == city_slug
        ]
        if len(matches) != 1:
            raise OfferUnavailableError(
                f"unknown city {country_arg!r}/{city_slug!r} for provider {provider_key!r}"
            )
        country, label, _bucket = matches[0]
        per_location: dict[str, list[SellableOffer]] = {}
        for offer in offers:
            name, loc_country, city = facts.get(offer.location_id, (offer.location_id, None, None))
            if (loc_country or "").strip().upper() != wanted_country:
                continue
            if (city or name or offer.location_id).strip() != label:
                continue
            per_location.setdefault(offer.location_id, []).append(offer)
        ordered = sorted(per_location)
        total = len(ordered)
        size = max(1, page_size)
        total_pages = max(1, -(-total // size))
        current = min(max(1, page), total_pages)
        items = tuple(
            self._datacenter_view(provider_key, family, facts, code, per_location[code])
            for code in ordered[(current - 1) * size : current * size]
        )
        return DatacenterPageView(
            provider_key=provider_key,
            family_key=family.family_key,
            billing_model=family.billing_model,
            country_code=country or None,
            city=label,
            items=items,
            page=current,
            total_pages=total_pages,
            total_count=total,
            back_callback=self._family_cities_callback(family, provider_key, 1),
            cancel_callback=self._store_nav_callback("market"),
            prev_callback=(
                self._family_halls_callback(
                    family, provider_key, country or None, label, current - 1
                )
                if current > 1
                else None
            ),
            next_callback=(
                self._family_halls_callback(
                    family, provider_key, country or None, label, current + 1
                )
                if current < total_pages
                else None
            ),
        )

    def _datacenter_view(
        self,
        provider_key: str,
        family: ProviderProductFamily,
        facts: Mapping[str, tuple[str, str | None, str | None]],
        location_id: str,
        bucket: list[SellableOffer],
    ) -> DatacenterLocationView:
        name, country, city = facts.get(location_id, (location_id, None, None))
        minimum, currency = self._single_currency_minimum(bucket)
        return DatacenterLocationView(
            location_id=location_id,
            name=name,
            country_code=(country or "").strip().upper() or None,
            city=(city or "").strip() or None,
            plan_count=len(bucket),
            min_price_minor=minimum,
            currency=currency,
            select_callback=self._family_location_callback(family, provider_key, location_id),
        )

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
        names = await self._location_metadata(provider_key)
        views: list[ProductGroupView] = []
        for rows in groups.values():
            head = rows[0]
            # Order-preserving dedup: distinct locations, stable order.
            locations = tuple(dict.fromkeys(row.location_id for row in rows))
            countries = tuple(
                dict.fromkeys(
                    (names.get(code, (code, None, None))[1] or "").strip().upper()
                    for code in locations
                )
            )
            countries = tuple(code for code in countries if code)
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
                    country_codes=countries,
                    # The card is identified by product AND price AND currency:
                    # equal minor-unit values in different currencies are
                    # different cards and must never cross-match.
                    select_callback=self._store_nav_callback(
                        "product_locations",
                        provider_key,
                        head.product_id,
                        str(head.selling_price_minor),
                        head.selling_currency,
                    ),
                )
            )
        market = self._markets.market_of(provider_key)
        back = self._store_nav_callback("providers", market.value if market else "")
        return views, back, self._store_nav_callback("market")

    async def family_plans_screen(
        self,
        provider_key: str,
        family_key: str,
        location_id: str,
        page: int = 1,
        page_size: int = 6,
    ) -> PlanPageView:
        # Monthly plans at one location, cheapest first, paginated. Each row
        # is its own sellable offer (its own credential account behind it).
        family = await self.resolve_family(provider_key, family_key)
        if family.billing_model != BILLING_MODEL_MONTHLY:
            raise OfferUnavailableError(
                f"family {family_key!r} of provider {provider_key!r} is not monthly"
            )
        offers = sorted(
            (
                o
                for o in await self._offers.list_sellable(provider_key)
                if o.sellable
                and o.location_id == location_id
                and o.billing_model == BILLING_MODEL_MONTHLY
            ),
            key=lambda o: (o.selling_price_minor, o.name, str(o.id)),
        )
        if not offers:
            raise OfferUnavailableError(
                f"no sellable monthly plans at {location_id!r} of provider {provider_key!r}"
            )
        total = len(offers)
        size = max(1, page_size)
        total_pages = max(1, -(-total // size))
        current = min(max(1, page), total_pages)
        items = tuple(
            FamilyPlanView(
                offer_id=offer.id,
                product_id=offer.product_id,
                name=offer.name,
                vcpu=offer.vcpu,
                ram_gb=offer.ram_gb,
                disk_gb=offer.disk_gb,
                traffic=offer.traffic,
                monthly_price_minor=offer.selling_price_minor,
                currency=offer.selling_currency,
                select_callback=self._store_nav_callback(
                    "plan_detail", provider_key, location_id, offer.product_id
                ),
                technical_metadata=dict(offer.technical_metadata or {}),
            )
            for offer in offers[(current - 1) * size : current * size]
        )
        return PlanPageView(
            provider_key=provider_key,
            location_id=location_id,
            items=items,
            page=current,
            total_pages=total_pages,
            total_count=total,
            back_callback=self._store_nav_callback("vps_locations", provider_key, family_key, "1"),
            cancel_callback=self._store_nav_callback("market"),
            prev_callback=(
                self._store_nav_callback(
                    "vps_plans", provider_key, family_key, location_id, str(current - 1)
                )
                if current > 1
                else None
            ),
            next_callback=(
                self._store_nav_callback(
                    "vps_plans", provider_key, family_key, location_id, str(current + 1)
                )
                if current < total_pages
                else None
            ),
        )

    async def plan_detail_screen(
        self, provider_key: str, location_id: str, product_id: str
    ) -> PlanDetailView:
        # One exact plan opened: full spec, proven technical facts, and the
        # continue action into OS selection. The location is already fixed.
        # Monthly offers only — an hourly row never belongs on this screen.
        matches = [
            o
            for o in await self._offers.list_sellable(provider_key)
            if o.sellable
            and o.location_id == location_id
            and o.product_id == product_id
            and o.billing_model == BILLING_MODEL_MONTHLY
        ]
        if len(matches) != 1:
            raise OfferUnavailableError(
                f"no unique monthly plan {product_id!r} at {location_id!r} "
                f"of provider {provider_key!r}"
            )
        offer = matches[0]
        names = await self._location_metadata(provider_key)
        location_name = names.get(location_id, (location_id, None, None))[0]
        location_country = names.get(location_id, (location_id, None, None))[1]
        family_key = self._family_key_for_billing(provider_key, offer.billing_model)
        return PlanDetailView(
            offer=self._view(offer),
            technical_metadata=dict(offer.technical_metadata or {}),
            location_name=location_name,
            location_country=location_country,
            continue_callback=self._store_nav_callback("os", self._offer_ref(offer.id)),
            back_callback=self._store_nav_callback(
                "vps_plans", provider_key, family_key, location_id, "1"
            ),
            cancel_callback=self._store_nav_callback("market"),
        )

    def _family_key_for_billing(self, provider_key: str, billing_model: str) -> str:
        # Family key owning one billing model (configured or implicit).
        for family in self._markets.families_of(provider_key):
            if family.billing_model == billing_model:
                return family.family_key
        return self._IMPLICIT_FAMILY_KEYS.get(billing_model, billing_model)

    async def panel_names(self, offer: SellableOffer) -> list[str | None]:
        # Free control panels of one offer plus the explicit no-panel choice
        # (index 0). The provider payload carries no OS-to-panel
        # compatibility mapping, so none is fabricated: every free panel is
        # offered and the customer picks.
        panels: list[str] = []
        try:
            provider = self._registry.get(offer.provider_key)
        except KeyError:
            provider = None
        if provider is not None:
            ordering = ordering_support_of(provider)
            if ordering is not None:
                try:
                    detail = await ordering.get_product(offer.location_id, offer.product_id)
                    panels = [o.name for o in detail.control_panels if o.is_free and o.name]
                except Exception:
                    panels = []
        return [None, *panels]

    def _panel_select_callback(self, offer_id: UUID, os_index: int, panel_index: int) -> str:
        # Selecting a panel leads to confirmation (panel 0 = no panel).
        return self._store_nav_callback(
            "confirm", self._offer_ref(offer_id), str(os_index), str(panel_index)
        )

    async def panel_screen(
        self, *, offer_id: UUID, os_index: int
    ) -> tuple[OfferCatalogView, list[PanelOptionView], str, str]:
        # Configuration step between OS and confirmation (free panels only,
        # so the confirmed price never moves).
        offer = await self._offers.get(offer_id)
        if offer is None or not offer.sellable:
            raise OfferUnavailableError(f"offer {offer_id} is not sellable")
        if offer.billing_model != BILLING_MODEL_MONTHLY:
            raise OfferUnavailableError(f"offer {offer_id} is not a monthly plan")
        await self.os_by_index(offer, os_index)
        names = await self.panel_names(offer)
        options = [
            PanelOptionView(
                name=name,
                index=index,
                select_callback=self._panel_select_callback(offer_id, os_index, index),
            )
            for index, name in enumerate(names)
        ]
        back_callback = self._store_nav_callback("os", self._offer_ref(offer_id))
        cancel_callback = self._store_nav_callback("market")
        return self._view(offer), options, back_callback, cancel_callback

    async def panel_name_by_index(self, offer: SellableOffer, index: int) -> str | None:
        # Resolve a callback-encoded panel index (re-validated server-side).
        names = await self.panel_names(offer)
        if index < 0 or index >= len(names):
            raise OsUnavailableError(f"panel option {index} is not available for {offer.ref}")
        return names[index]

    # -- storefront: hourly cloud ----------------------------------------

    def _hourly_provider(self, provider_key: str, credential_account_id: str | None = None) -> Any:
        # Hourly cloud adapter for live image reads (screens only; sync and
        # creation resolve their own adapter from configuration). The owning
        # credential account selects the adapter — never a first-configured
        # default — with the legacy dict as the single-adapter fallback.
        resolver = getattr(self, "_cloud_resolver", None)
        if resolver is not None:
            try:
                adapter = resolver.adapter_for(provider_key, credential_account_id)
            except Exception as exc:
                raise OfferUnavailableError(
                    f"provider {provider_key!r} has no hourly cloud adapter"
                ) from exc
            if adapter is not None:
                return adapter
        try:
            return self._cloud[provider_key]
        except KeyError:
            raise OfferUnavailableError(
                f"provider {provider_key!r} has no hourly cloud adapter"
            ) from None

    def _plan_family_of(self, offer: SellableOffer) -> tuple[str, str]:
        # Normalized instance family from the synced technical metadata
        # (adapter-classified, never invented by the storefront).
        meta = offer.technical_metadata or {}
        key = str(meta.get("plan_family") or "other")
        name = str(meta.get("plan_family_name") or key)
        return key, name

    async def cloud_locations_screen(
        self, provider_key: str, family_key: str, page: int = 1, page_size: int = 6
    ) -> LocationPageView:
        # Hourly locations with sellable offers, paginated, with normalized
        # display metadata. Only API-discovered regions ever appear.
        family = await self.resolve_family(provider_key, family_key)
        if family.billing_model != BILLING_MODEL_HOURLY:
            raise OfferUnavailableError(
                f"family {family_key!r} of provider {provider_key!r} is not hourly"
            )
        counts: dict[str, int] = {}
        for offer in await self._offers.list_sellable(provider_key):
            if offer.sellable and offer.billing_model == BILLING_MODEL_HOURLY:
                counts[offer.location_id] = counts.get(offer.location_id, 0) + 1
        if not counts:
            raise OfferUnavailableError(f"no sellable hourly offers for provider {provider_key!r}")
        names = await self._location_metadata(provider_key)
        ordered = sorted(counts)
        total = len(ordered)
        size = max(1, page_size)
        total_pages = max(1, -(-total // size))
        current = min(max(1, page), total_pages)
        items = tuple(
            FamilyLocationView(
                location_id=code,
                name=names.get(code, (code, None, None))[0],
                country_code=names.get(code, (code, None, None))[1],
                city=None,
                select_callback=self._store_nav_callback(
                    "cloud_families", provider_key, family_key, code
                ),
            )
            for code in ordered[(current - 1) * size : current * size]
        )
        return LocationPageView(
            provider_key=provider_key,
            items=items,
            page=current,
            total_pages=total_pages,
            total_count=total,
            back_callback=self._store_nav_callback("families", provider_key),
            cancel_callback=self._store_nav_callback("market"),
            prev_callback=(
                self._store_nav_callback(
                    "cloud_locations", provider_key, family_key, str(current - 1)
                )
                if current > 1
                else None
            ),
            next_callback=(
                self._store_nav_callback(
                    "cloud_locations", provider_key, family_key, str(current + 1)
                )
                if current < total_pages
                else None
            ),
        )

    async def cloud_plan_families_screen(
        self, provider_key: str, family_key: str, location_id: str
    ) -> tuple[list[FamilyOptionView], str, str]:
        # Instance-type families at one region, as the provider classified
        # them (plus the explicit other bucket). Never invented here.
        family = await self.resolve_family(provider_key, family_key)
        if family.billing_model != BILLING_MODEL_HOURLY:
            raise OfferUnavailableError(
                f"family {family_key!r} of provider {provider_key!r} is not hourly"
            )
        groups: dict[str, tuple[str, int]] = {}
        for offer in await self._offers.list_sellable(provider_key):
            if (
                offer.sellable
                and offer.location_id == location_id
                and offer.billing_model == BILLING_MODEL_HOURLY
            ):
                key, name = self._plan_family_of(offer)
                _seen_name, count = groups.get(key, (name, 0))
                groups[key] = (name, count + 1)
        if not groups:
            raise OfferUnavailableError(
                f"no sellable hourly plans at {location_id!r} of provider {provider_key!r}"
            )
        views = [
            FamilyOptionView(
                provider_key=provider_key,
                family_key=key,
                billing_model=BILLING_MODEL_HOURLY,
                display_name=name,
                select_callback=self._store_nav_callback(
                    "cloud_plans", provider_key, location_id, key, "1"
                ),
                sellable_count=count,
                available=True,
            )
            for key, (name, count) in sorted(groups.items())
        ]
        return (
            views,
            self._store_nav_callback("cloud_locations", provider_key, family_key, "1"),
            self._store_nav_callback("market"),
        )

    async def cloud_plans_screen(
        self,
        provider_key: str,
        location_id: str,
        plan_family: str,
        page: int = 1,
        page_size: int = 6,
    ) -> PlanPageView:
        # Hourly plans of one instance family at one region, cheapest first,
        # paginated. Hourly price is authoritative; the monthly figure shown
        # beside it is a display estimate only. The commercial family rides
        # along implicitly: every cloud_* screen is hourly by construction.
        offers = sorted(
            (
                o
                for o in await self._offers.list_sellable(provider_key)
                if o.sellable
                and o.location_id == location_id
                and o.billing_model == BILLING_MODEL_HOURLY
                and self._plan_family_of(o)[0] == plan_family
            ),
            key=lambda o: (o.selling_price_minor, o.name, str(o.id)),
        )
        if not offers:
            raise OfferUnavailableError(
                f"no sellable hourly plans at {location_id!r} of provider {provider_key!r}"
            )
        total = len(offers)
        size = max(1, page_size)
        total_pages = max(1, -(-total // size))
        current = min(max(1, page), total_pages)
        items = tuple(
            FamilyPlanView(
                offer_id=offer.id,
                product_id=offer.product_id,
                name=offer.name,
                vcpu=offer.vcpu,
                ram_gb=offer.ram_gb,
                disk_gb=offer.disk_gb,
                traffic=offer.traffic,
                monthly_price_minor=offer.selling_price_minor,
                currency=offer.selling_currency,
                # A concrete plan goes STRAIGHT to the OS/image picker: the
                # plan is already fully identified by this button, so an
                # extra detail screen would only add a click. The legacy
                # ``cloud_detail`` route stays decodable for buttons already
                # sitting in customer chats.
                select_callback=self._store_nav_callback("cloud_images", self._offer_ref(offer.id)),
                technical_metadata=dict(offer.technical_metadata or {}),
            )
            for offer in offers[(current - 1) * size : current * size]
        )
        back_family = self._family_key_for_billing(provider_key, BILLING_MODEL_HOURLY)
        return PlanPageView(
            provider_key=provider_key,
            location_id=location_id,
            items=items,
            page=current,
            total_pages=total_pages,
            total_count=total,
            back_callback=self._store_nav_callback(
                "cloud_families", provider_key, back_family, location_id
            ),
            cancel_callback=self._store_nav_callback("market"),
            prev_callback=(
                self._store_nav_callback(
                    "cloud_plans",
                    provider_key,
                    location_id,
                    plan_family,
                    str(current - 1),
                )
                if current > 1
                else None
            ),
            next_callback=(
                self._store_nav_callback(
                    "cloud_plans",
                    provider_key,
                    location_id,
                    plan_family,
                    str(current + 1),
                )
                if current < total_pages
                else None
            ),
        )

    async def cloud_detail_screen(
        self, provider_key: str, location_id: str, product_id: str
    ) -> PlanDetailView:
        # One exact hourly plan: full specs, hourly price plus the monthly
        # estimate (display only), then the continue action into images.
        matches = [
            o
            for o in await self._offers.list_sellable(provider_key)
            if o.sellable
            and o.location_id == location_id
            and o.product_id == product_id
            and o.billing_model == BILLING_MODEL_HOURLY
        ]
        if len(matches) != 1:
            raise OfferUnavailableError(
                f"no unique hourly plan {product_id!r} at {location_id!r} "
                f"of provider {provider_key!r}"
            )
        offer = matches[0]
        names = await self._location_metadata(provider_key)
        return PlanDetailView(
            offer=self._view(offer),
            technical_metadata=dict(offer.technical_metadata or {}),
            location_name=names.get(location_id, (location_id, None, None))[0],
            location_country=names.get(location_id, (location_id, None, None))[1],
            continue_callback=self._store_nav_callback("cloud_images", self._offer_ref(offer.id)),
            back_callback=self._store_nav_callback(
                "cloud_plans",
                provider_key,
                location_id,
                self._plan_family_of(offer)[0],
                "1",
            ),
            cancel_callback=self._store_nav_callback("market"),
        )

    @staticmethod
    def _selectable_images(offer: SellableOffer, images: list[Any]) -> list[Any]:
        """Images this exact offer may actually be created with.

        Provider-neutral and deliberately conservative:

        * a restriction the provider states (plan/location/account) is honoured
          fail-closed, exactly like the creation gate;
        * an architecture is dropped only on a POSITIVE mismatch — a provider
          that does not state an architecture for its images must not lose
          every image because a plan states one.
        """
        architecture = TechnicalSpec.from_metadata(offer.technical_metadata).architecture
        account_id = getattr(offer, "provider_account_id", None)
        return [
            image
            for image in images
            if image_compatible(
                image,
                plan_id=offer.product_id,
                location_id=offer.location_id,
                account_id=account_id,
            )
            and not image_architecture_conflict(image, architecture)
        ]

    async def cloud_images_screen(
        self, offer_id: UUID
    ) -> tuple[OfferCatalogView, list[PanelOptionView], str, str]:
        # Supported images, read live from the provider (label and provider
        # id stay separate; selection travels as an index and is re-resolved
        # server-side against the same filtered list, so an index can never
        # point at an image the pinned offer cannot use).
        offer = await self._offers.get(offer_id)
        if offer is None or not offer.sellable:
            raise OfferUnavailableError(f"offer {offer_id} is not sellable")
        if offer.billing_model != BILLING_MODEL_HOURLY:
            raise OfferUnavailableError(f"offer {offer_id} is not an hourly plan")
        provider = self._hourly_provider(
            offer.provider_key, getattr(offer, "provider_account_id", None)
        )
        try:
            images = self._selectable_images(offer, await provider.list_images(offer.location_id))
        except Exception as exc:
            # Not "this offer is unavailable": the plan is sellable and only
            # the IMAGE catalog is unreadable right now, which is the OS
            # question the customer is actually answering.
            raise OsUnavailableError(f"images currently unavailable for {offer.ref}") from exc
        if not images:
            # Same distinction, empty catalog instead of a failed read.
            raise OsUnavailableError(f"no images for {offer.ref}")
        options = [
            PanelOptionView(
                name=f"{image.label} ({image.architecture})" if image.architecture else image.label,
                index=index,
                select_callback=self._store_nav_callback(
                    "cloud_confirm", self._offer_ref(offer.id), str(index)
                ),
            )
            for index, image in enumerate(images)
        ]
        # Back returns to the plan list of this plan's own instance family at
        # the same location — the context the customer came from.
        back_callback = self._store_nav_callback(
            "cloud_plans",
            offer.provider_key,
            offer.location_id,
            self._plan_family_of(offer)[0],
            "1",
        )
        cancel_callback = self._store_nav_callback("market")
        return self._view(offer), options, back_callback, cancel_callback

    async def cloud_image_by_index(self, offer: SellableOffer, index: int) -> Any:
        # Resolve a callback-encoded image index back to its record
        # (re-fetched live and filtered exactly like the picker, so a stale
        # index simply fails).
        provider = self._hourly_provider(
            offer.provider_key, getattr(offer, "provider_account_id", None)
        )
        try:
            images = self._selectable_images(offer, await provider.list_images(offer.location_id))
        except Exception as exc:
            raise OsUnavailableError(f"images currently unavailable for {offer.ref}") from exc
        if index < 0 or index >= len(images):
            raise OsUnavailableError(f"image option {index} is not available for {offer.ref}")
        return images[index]

    async def cloud_confirmation(
        self, *, user_id: UUID, offer_id: UUID, image_index: int
    ) -> CloudConfirmView:
        # Hourly creation confirmation: exact hourly price, monthly estimate,
        # wallet balance and the explicit delete-to-stop-billing warning
        # (rendered by the UI). Nothing is charged upfront.
        offer = await self._offers.get(offer_id)
        if offer is None or not offer.sellable:
            raise OfferUnavailableError(f"offer {offer_id} is not sellable")
        if offer.billing_model != BILLING_MODEL_HOURLY:
            raise OfferUnavailableError(f"offer {offer_id} is not an hourly plan")
        image = await self.cloud_image_by_index(offer, image_index)
        wallet = await self._wallets.get(user_id)
        balance = wallet.balance if wallet is not None else 0
        names = await self._location_metadata(offer.provider_key)
        return CloudConfirmView(
            offer=self._view(offer),
            image_label=image.label,
            hourly_price_minor=offer.selling_price_minor,
            monthly_estimate_minor=offer.selling_price_minor * HOURLY_MONTHLY_ESTIMATE_HOURS,
            currency=offer.selling_currency,
            balance_minor=balance,
            location_name=names.get(offer.location_id, (offer.location_id, None, None))[0],
            location_country=names.get(offer.location_id, (offer.location_id, None, None))[1],
            confirm_callback=self._store_nav_callback(
                "cloud_buy", self._offer_ref(offer.id), str(image_index)
            ),
            back_callback=self._store_nav_callback("cloud_images", self._offer_ref(offer.id)),
            cancel_callback=self._store_nav_callback("market"),
        )

    async def _card_offers(
        self,
        provider_key: str,
        product_id: str,
        price_minor: int | None,
        currency: str | None,
    ) -> list[SellableOffer]:
        """Offers of one exact product card (detail and locations screens share it).

        The card identity is product AND price AND currency. A call that omits
        ``currency`` is only honoured when it resolves to exactly one
        (price, currency) pair; anything ambiguous is rejected instead of
        silently crossing currencies. Nothing is ever inferred or converted.
        """
        candidates = [
            o
            for o in await self._offers.list_sellable(provider_key)
            if o.sellable and o.product_id == product_id
        ]
        if not candidates:
            raise OfferUnavailableError(
                f"no sellable offers for product {product_id!r} of provider {provider_key!r}"
            )
        if currency is not None:
            wanted = currency.strip().upper()
            if not wanted:
                raise OfferUnavailableError(
                    f"no sellable offers for product {product_id!r} of provider {provider_key!r}"
                )
            offers = [
                o
                for o in candidates
                if (price_minor is None or o.selling_price_minor == price_minor)
                and o.selling_currency.upper() == wanted
            ]
        else:
            scoped = [
                o for o in candidates if price_minor is None or o.selling_price_minor == price_minor
            ]
            if not scoped:
                raise OfferUnavailableError(
                    f"no sellable offers for product {product_id!r} of provider {provider_key!r}"
                )
            distinct = {(o.selling_price_minor, o.selling_currency.upper()) for o in scoped}
            if len(distinct) > 1:
                raise OfferUnavailableError(
                    f"ambiguous product card for {product_id!r} of provider {provider_key!r}: "
                    "a legacy callback without currency must not cross currencies"
                )
            offers = scoped
        if not offers:
            raise OfferUnavailableError(
                f"no sellable offers for product {product_id!r} of provider {provider_key!r}"
            )
        return offers

    async def product_locations_screen(
        self,
        provider_key: str,
        product_id: str,
        price_minor: int | None = None,
        currency: str | None = None,
    ) -> tuple[list[ProductLocationView], str, str]:
        """Where one product is available, each row its own sellable offer.

        The product card is identified by product AND price AND currency:
        ``price_minor`` together with ``currency`` narrow to the exact card
        the customer tapped. A legacy callback that omits ``currency`` is
        only honoured when it resolves to exactly one currency; an ambiguous
        legacy callback is rejected instead of silently crossing currencies.
        No currency is ever inferred or converted here.
        """
        offers = await self._card_offers(provider_key, product_id, price_minor, currency)
        names = await self._location_metadata(provider_key)
        views = [
            ProductLocationView(
                location_id=offer.location_id,
                offer_id=offer.id,
                name=names.get(offer.location_id, (offer.location_id, None, None))[0],
                country_code=names.get(offer.location_id, (offer.location_id, None, None))[1],
                product_name=offer.name,
                monthly_price_minor=offer.selling_price_minor,
                currency=offer.selling_currency,
                select_callback=self._store_nav_callback("os", self._offer_ref(offer.id)),
            )
            for offer in sorted(offers, key=lambda o: o.location_id)
        ]
        return (
            views,
            self._store_nav_callback("products", provider_key),
            self._store_nav_callback("market"),
        )

    async def _location_metadata(
        self, provider_key: str
    ) -> dict[str, tuple[str, str | None, str | None]]:
        """Synced display facts per location: (name, country_code, city).

        Falls back to the code. Presentation only: a location the provider
        started selling at before its catalog row synced still shows (as its
        code) instead of disappearing. City is None until the catalog sync
        reconciles the row — grouping treats such locations as their own
        single-location group so inventory is never hidden.
        """
        if self._locations is None:
            return {}
        try:
            records = await self._locations.list_for_provider(provider_key)
        except Exception:  # pragma: no cover - display metadata is optional
            logger.warning("location display metadata unavailable for %s", provider_key)
            return {}
        return {
            record.location_id: (
                record.name or record.location_id,
                record.country_code,
                getattr(record, "city", None),
            )
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
        """The signed callback that SELECTING this OS leads to (panels)."""
        return self._store_nav_callback("panel", self._offer_ref(offer_id), str(index))

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
        panel_index: int | None = None,
    ) -> OfferConfirmView:
        """The exact-price confirmation view (enforcement lives in checkout).

        The OS and panel indexes are re-resolved against the live product
        API — a stale or tampered index simply fails. Monthly offers only:
        hourly products confirm through their own screen.
        """
        offer = await self._offers.get(offer_id)
        if offer is None or not offer.sellable:
            raise OfferUnavailableError(f"offer {offer_id} is not sellable")
        if offer.billing_model != BILLING_MODEL_MONTHLY:
            raise OfferUnavailableError(f"offer {offer_id} is not a monthly plan")
        os_name = await self.os_by_index(offer, os_index)
        panel_name = (
            await self.panel_name_by_index(offer, panel_index) if panel_index is not None else None
        )

        wallet = await self._wallets.get(user_id)
        balance = wallet.balance if wallet is not None else 0

        confirm_callback = self._store_nav_callback(
            "buy",
            self._offer_ref(offer_id),
            str(os_index),
            str(panel_index if panel_index is not None else 0),
        )
        back_callback = self._store_nav_callback("panel", self._offer_ref(offer_id), str(os_index))
        cancel_callback = self._store_nav_callback("market")
        return OfferConfirmView(
            offer=self._view(offer),
            os_name=os_name,
            balance_minor=balance,
            currency=offer.selling_currency,
            sufficient=balance >= offer.selling_price_minor,
            confirm_callback=confirm_callback,
            back_callback=back_callback,
            cancel_callback=cancel_callback,
            panel_name=panel_name,
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

        # Back returns to the plan's detail screen (location is fixed by
        # the offer itself, so no card identity is needed).
        back_callback = self._store_nav_callback(
            "plan_detail",
            offer.provider_key,
            offer.location_id,
            offer.product_id,
        )
        cancel_callback = self._store_nav_callback("market")
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
        back_callback = self._store_nav_callback("locations", resolved_provider)
        cancel_callback = self._store_nav_callback("market")
        views = [self._view(o) for o in sorted(offers, key=lambda o: o.name)]
        return views, back_callback, cancel_callback
