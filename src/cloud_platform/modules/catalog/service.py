"""Pricing ingestion service (M04-005).

Turns a provider's location-aware plan pricing into persistent catalog rows.
Prices come exclusively from the provider payload — nothing is hard-coded —
and all money math stays in Decimal integer minor units.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.catalog.domain import (
    CatalogCountryView,
    CatalogEntrySpec,
    CatalogError,
    CatalogLocationView,
    CatalogOffer,
    CatalogOfferView,
    CatalogRepository,
    IngestedPrice,
    IngestedPricing,
    LocationRecord,
    LocationRepository,
    OfferNotFoundError,
    OfferRef,
    OfferState,
    OsOption,
    OsSelectionView,
    PlanPricing,
    PricingError,
    hourly_minor,
)
from cloud_platform.modules.navigation.domain import (
    Callback,
    CallbackError,
    encode_callback,
)
from cloud_platform.modules.pricing.domain import OfferCost
from cloud_platform.modules.pricing.service import PriceBookService
from cloud_platform.modules.users.domain import Permission, PermissionChecker, User
from cloud_platform.modules.wallet.domain import WalletRepository
from cloud_platform.providers.base import CloudProvider, ProviderImage
from cloud_platform.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class CatalogViewService:
    """Customer-facing catalog browsing: country -> location -> offers (M08-002).

    Acceptance: **only enabled offers are shown.** The service reads the
    catalog rows (offers) and the synced provider locations (country/city),
    groups enabled offers by country, and never leaks a hidden offer, a
    disabled provider location, or a non-offer row (rows without real
    specs, such as bare location markers).

    Everything is provider-neutral: the country code comes from the
    provider's own location data (synced, never assumed). Offers whose
    location has no synced record yet land in the ``None`` ("Other") bucket
    so a sync gap never silently hides a sellable offer.
    """

    def __init__(
        self,
        catalog_repo: CatalogRepository,
        location_repo: LocationRepository,
    ) -> None:
        self._catalog = catalog_repo
        self._locations = location_repo

    async def list_by_country(self) -> list[CatalogCountryView]:
        """All enabled offers grouped by country, then location.

        Countries are sorted by code (the "Other" bucket last); locations
        within a country by name; offers within a location by plan name.
        """
        offers = await self._catalog.list_offers()
        # Only real, enabled offers reach the customer: visibility flag
        # (M04-007) plus a specs sanity filter (synthetic rows have vcpu 0).
        sellable = [o for o in offers if o.enabled and o.vcpu > 0]
        if not sellable:
            return []

        provider_keys = sorted({o.provider_key for o in sellable})
        location_index: dict[tuple[str, str], LocationRecord] = {}
        for key in provider_keys:
            for loc_record in await self._locations.list_for_provider(key):
                location_index[(key, loc_record.location_id)] = loc_record

        by_country: dict[str | None, dict[str, list[CatalogOfferView]]] = {}
        for offer in sellable:
            record = location_index.get((offer.provider_key, offer.location_id))
            country = (record.country_code or None) if record else None
            location_key = f"{offer.location_id}|{record.name if record else offer.location_id}"
            by_country.setdefault(country, {}).setdefault(location_key, []).append(
                CatalogOfferView(
                    offer_id=offer.id,
                    provider_key=offer.provider_key,
                    plan_id=offer.plan_id,
                    location_id=offer.location_id,
                    name=offer.name,
                    architecture=offer.architecture,
                    vcpu=offer.vcpu,
                    memory_mb=offer.memory_mb,
                    disk_gb=offer.disk_gb,
                    currency=offer.currency,
                    price_per_quantum=offer.price_per_quantum,
                    quantum_seconds=offer.quantum_seconds,
                )
            )

        countries: list[CatalogCountryView] = []
        for country in sorted(by_country, key=lambda c: (c is None, c or "")):
            locations: list[CatalogLocationView] = []
            for location_key, offer_views in by_country[country].items():
                location_id, name = location_key.split("|", 1)
                record = location_index.get((offer_views[0].provider_key, location_id))
                locations.append(
                    CatalogLocationView(
                        location_id=location_id,
                        name=name,
                        city=record.city if record else None,
                        offers=tuple(sorted(offer_views, key=lambda o: o.name)),
                    )
                )
            locations.sort(key=lambda loc: (loc.name, loc.city or ""))
            countries.append(CatalogCountryView(country_code=country, locations=tuple(locations)))
        return countries


class PricingIngestionService:
    """Persists a plan's per-location prices to the catalog."""

    def __init__(self, catalog_repo: CatalogRepository) -> None:
        self._catalog = catalog_repo

    async def ingest_plan(self, provider_key: str, plan: PlanPricing) -> IngestedPricing:
        """Upsert one catalog row per (plan, location).

        Raises:
            PricingError: If the provider key is empty.
            ValueError: On invalid plan/price data (via domain validation).
        """
        if not provider_key or not provider_key.strip():
            raise PricingError("provider_key must not be empty")

        prices: list[IngestedPrice] = []
        for entry in plan.prices:
            spec = CatalogEntrySpec(
                provider_key=provider_key,
                plan_id=plan.plan_id,
                location_id=entry.location_id,
                name=plan.name,
                architecture=plan.architecture,
                vcpu=plan.vcpu,
                memory_mb=plan.memory_mb,
                disk_gb=plan.disk_gb,
                currency=entry.currency,
                price_per_quantum=hourly_minor(entry),
                description=plan.description,
                extra_metadata={
                    "cpu_type": plan.cpu_type,
                    "storage_type": plan.storage_type,
                },
            )
            created = await self._catalog.upsert_entry(spec)
            prices.append(
                IngestedPrice(
                    location_id=entry.location_id,
                    currency=entry.currency,
                    hourly_minor=spec.price_per_quantum,
                    created=created,
                )
            )

        result = IngestedPricing(plan_id=plan.plan_id, prices=tuple(prices))
        logger.info(
            "ingested %d location prices for %s/%s",
            len(prices),
            provider_key,
            plan.plan_id,
        )
        return result


class OfferVisibilityService:
    """Admin-controlled visibility of sellable offers (M04-007).

    An offer is a (provider, plan, location) combination. Hiding it makes it
    unavailable for sale without deleting it (prices and history are kept).
    Every change is admin-authorized and audited with a non-empty reason.
    """

    def __init__(
        self,
        catalog_repo: CatalogRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._catalog = catalog_repo
        self._audit = AuditTrail(audit_repo)

    async def set_offer_visibility(
        self,
        *,
        ref: OfferRef,
        enabled: bool,
        actor: User,
        reason: str,
    ) -> OfferState:
        """Show (``enabled=True``) or hide (``enabled=False``) one offer.

        Idempotent: setting the state the offer already has changes nothing
        and records no audit event.

        Raises:
            PermissionDeniedError: If the actor lacks admin:manage_settings.
            CatalogError: On an empty reason.
            OfferNotFoundError: If the combination is not in the catalog.
        """
        PermissionChecker(actor).require(Permission.ADMIN_MANAGE_SETTINGS)
        if not reason or not reason.strip():
            raise CatalogError("offer visibility changes must carry a non-empty reason")

        current = await self._catalog.get_offer(ref)
        if current is None:
            raise OfferNotFoundError(f"offer {ref.key} not found")

        if current.enabled is enabled:
            return current  # idempotent no-op

        await self._catalog.set_offer_enabled(ref, enabled)
        action = "catalog.offer_show" if enabled else "catalog.offer_hide"
        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN,
            actor_id=actor.id,
            action=action,
            resource_type="catalog_offer",
            resource_id=ref.key,
            reason=reason,
            metadata={
                "provider": ref.provider_key,
                "plan": ref.plan_id,
                "location": ref.location_id,
            },
        )
        logger.info("offer %s now enabled=%s", ref.key, enabled)
        return OfferState(
            id=current.id,
            ref=ref,
            name=current.name,
            enabled=enabled,
            price_per_quantum=current.price_per_quantum,
            currency=current.currency,
        )


class PlanSelectionError(CatalogError):
    """Raised when a plan selection view cannot be built (no offers)."""


# ---------------------------------------------------------------------------
# Buy-flow screens (M08-002/M08-003): locations + plans, with stable,
# tamper-resistant callbacks per the M08-001 scheme.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocationOption:
    """One sellable location row on the buy.locations screen."""

    provider_key: str
    location_id: str
    name: str
    city: str | None
    offer_count: int
    select_callback: str  # buy.locations --select--> buy.plans for this location


@dataclass(frozen=True, slots=True)
class CountrySelectionView:
    """The buy.locations screen, one country group (None = "Other")."""

    country_code: str | None
    options: tuple[LocationOption, ...]

    @property
    def label(self) -> str:
        return self.country_code or "Other"


@dataclass(frozen=True, slots=True)
class PlanSelectionView:
    """The buy.plans screen: the enabled plans of one location.

    ``plans`` and ``plan_callbacks`` are parallel: the i-th callback is the
    stable, signed button payload that selects the i-th plan (decoded by the
    UI layer through ``decode_callback``; any tampering is rejected).
    """

    provider_key: str
    location_id: str
    location_name: str
    city: str | None
    plans: tuple[CatalogOfferView, ...]
    plan_callbacks: tuple[str, ...]
    back_callback: str
    cancel_callback: str

    def render(self) -> str:
        """ASCII rendering for logs and the review UI (no float money)."""
        place = self.city or self.location_name
        lines = [f"{self.location_name} ({place}) - {len(self.plans)} plan(s)"]
        for offer in self.plans:
            major, minor = divmod(offer.price_per_quantum, 100)
            lines.append(
                f"  - {offer.name} [{offer.spec_label}] "
                f"{major}.{minor:02d} {offer.currency}/{offer.quantum_seconds // 60}min"
            )
        return "\n".join(lines)


class PurchaseConfirmationError(CatalogError):
    """Raised when the purchase confirmation view cannot be built."""


@dataclass(frozen=True, slots=True)
class PurchasePolicy:
    """The EXACT price policy shown at confirmation (M08-005).

    Everything the customer will be billed with, derived from the versioned
    price book (the same derivation the create command and the billing
    snapshot use), so what is shown is what is charged:

    - ``selling_minor_per_quantum`` - price per billing quantum (minor
      units), for the exact book version effective now;
    - ``quantum_seconds`` - the billing quantum of the offer;
    - ``margin_factor`` / ``book_version`` - the rule and version behind
      the price (auditability);
    - ``monthly_cap_minor`` - the per-usage-month cap when the rule has
      one (accrual beyond it is not billed);
    - ``hold_minor`` - the funds reserved at order time: the first
      quantum (the create command holds exactly this).
    """

    selling_minor_per_quantum: int
    currency: str
    quantum_seconds: int
    margin_factor: Decimal
    book_name: str
    book_version: int
    monthly_cap_minor: int | None
    hold_minor: int


@dataclass(frozen=True, slots=True)
class WalletImpact:
    """What the order does to the user's wallet (M08-005).

    The hold reserves the first quantum at order time (available balance
    drops by the hold; usage is then captured from it). ``sufficient`` is
    what the create command itself will enforce - the view only shows it.
    """

    has_wallet: bool
    balance_minor: int
    hold_minor: int
    balance_after_hold_minor: int
    sufficient: bool


@dataclass(frozen=True, slots=True)
class PurchaseConfirmationView:
    """The buy.confirm screen: offer + OS + exact policy + wallet impact."""

    provider_key: str
    location_id: str
    offer: CatalogOfferView
    image_id: str | None
    policy: PurchasePolicy
    wallet: WalletImpact
    confirm_callback: str  # buy.confirm --confirm--> done -> create command
    back_callback: str
    cancel_callback: str

    def render(self) -> str:
        """ASCII rendering for logs and the review UI (no float money)."""
        major, minor = divmod(self.policy.selling_minor_per_quantum, 100)
        lines = [
            f"Confirm: {self.offer.name} @ {self.location_id} "
            f"({self.offer.architecture})" + (f", OS {self.image_id}" if self.image_id else ""),
            (
                f"  price: {major}.{minor:02d} {self.policy.currency}/"
                f"{self.policy.quantum_seconds // 60}min "
                f"(book {self.policy.book_name} v{self.policy.book_version}, "
                f"margin {self.policy.margin_factor})"
            ),
        ]
        if self.policy.monthly_cap_minor is not None:
            cap_major, cap_minor = divmod(self.policy.monthly_cap_minor, 100)
            lines.append(f"  monthly cap: {cap_major}.{cap_minor:02d} {self.policy.currency}")
        if self.wallet.has_wallet:
            b_major, b_minor = divmod(self.wallet.balance_minor, 100)
            after_major, after_minor = divmod(self.wallet.balance_after_hold_minor, 100)
            lines.append(
                f"  wallet: {b_major}.{b_minor:02d} -> "
                f"{after_major}.{after_minor:02d} "
                f"{'(insufficient!)' if not self.wallet.sufficient else '(ok)'}"
            )
        else:
            lines.append("  wallet: none (order will fail)")
        return "\n".join(lines)


class PurchaseConfirmationService:
    """The buy.confirm screen (M08-005): exact price policy + wallet impact.

    Acceptance: **shows the exact price policy and wallet impact.** The
    selling price is derived from the SAME versioned price book the create
    command and the immutable server price snapshot use, so the confirmation
    cannot show a different price than the one that would be charged. The
    wallet impact is read from the user's actual wallet (available balance
    minus the first-quantum hold the order would place) and only DISPLAYED:
    all enforcement (balance, quota, terms, maintenance) stays in the create
    command, never here.
    """

    def __init__(
        self,
        catalog_repo: CatalogRepository,
        price_book_service: PriceBookService,
        wallet_repo: WalletRepository,
        signing_key: str,
        book_name: str,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        if not book_name or not book_name.strip():
            raise ValueError("book_name must not be empty")
        self._catalog = catalog_repo
        self._books = price_book_service
        self._wallets = wallet_repo
        self._signing_key = signing_key
        self._book_name = book_name

    async def confirmation(
        self,
        user_id: UUID,
        provider_key: str,
        location_id: str,
        offer_id: UUID,
        image_id: str | None = None,
    ) -> PurchaseConfirmationView:
        """Build the confirmation screen for a chosen offer (+ optional OS)."""
        offer = await self._catalog.get_by_id(offer_id)
        if offer is None or offer.provider_key != provider_key or offer.location_id != location_id:
            raise PurchaseConfirmationError(
                f"offer {offer_id} not found at {provider_key}/{location_id}"
            )
        if not offer.enabled or offer.vcpu <= 0:
            raise PurchaseConfirmationError(f"offer {offer_id} is not sellable")

        # Exact price: the versioned price book, now (pricing errors such as
        # no active version or no matching rule propagate - without a price
        # there is nothing to confirm and the order would fail anyway).
        cost = OfferCost(
            offer.provider_key,
            offer.plan_id,
            offer.location_id,
            offer.price_per_quantum,
            offer.currency,
        )
        price = await self._books.sell_price(
            book_name=self._book_name, offer=cost, at=datetime.now(UTC)
        )
        policy = PurchasePolicy(
            selling_minor_per_quantum=price.selling_minor,
            currency=offer.currency,
            quantum_seconds=offer.quantum_seconds,
            margin_factor=price.rule.margin_factor,
            book_name=price.book_name,
            book_version=price.version,
            monthly_cap_minor=price.rule.monthly_cap_minor,
            hold_minor=price.selling_minor,  # the create command holds the first quantum
        )

        wallet = await self._wallets.get(user_id)
        if wallet is None:
            impact = WalletImpact(False, 0, policy.hold_minor, 0, False)
        else:
            after = wallet.balance - policy.hold_minor
            impact = WalletImpact(
                has_wallet=True,
                balance_minor=wallet.balance,
                hold_minor=policy.hold_minor,
                balance_after_hold_minor=after,
                sufficient=wallet.balance >= policy.hold_minor,
            )

        image_field = image_id or "none"
        args = (provider_key, location_id, str(offer_id), image_field)
        return PurchaseConfirmationView(
            provider_key=provider_key,
            location_id=location_id,
            offer=BuyFlowViewService._offer_view(offer),
            image_id=image_id,
            policy=policy,
            wallet=impact,
            confirm_callback=encode_callback(
                Callback(flow="buy", screen="confirm", args=args), self._signing_key
            ),
            back_callback=encode_callback(
                Callback(flow="buy", screen="os", args=args), self._signing_key
            ),
            cancel_callback=encode_callback(
                Callback(flow="main", screen="menu"), self._signing_key
            ),
        )


class OsSelectionError(CatalogError):
    """Raised when the OS selection view cannot be built for an offer."""


def compatible_os_options(images: list[ProviderImage], architecture: str) -> list[ProviderImage]:
    """Server-side architecture gate (M08-004).

    Returns the images whose architecture matches ``architecture``
    (case-insensitive; both values are provider-reported), sorted by name.
    An incompatible image never reaches the UI, so it can never be selected.
    """
    target = architecture.strip().lower()
    if not target:
        return []
    return sorted(
        (image for image in images if image.architecture.strip().lower() == target),
        key=lambda image: image.name,
    )


class OsSelectionService:
    """The buy.os screen (M08-004): OS selection for a chosen offer.

    Acceptance: **architecture compatibility enforced server-side.** The
    offer's architecture (from the catalog row) is compared against each
    image's architecture (from the provider) HERE, in the domain service —
    the UI only ever receives compatible images, each with its own stable,
    signed selection callback (M08-001 scheme).
    """

    def __init__(
        self,
        catalog_repo: CatalogRepository,
        provider_registry: ProviderRegistry,
        signing_key: str,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        self._catalog = catalog_repo
        self._registry = provider_registry
        self._signing_key = signing_key

    async def os_screen(
        self, provider_key: str, location_id: str, offer_id: UUID
    ) -> OsSelectionView:
        """Build the OS screen for the offer selected on buy.plans."""
        offer = await self._catalog.get_by_id(offer_id)
        if offer is None or offer.provider_key != provider_key or offer.location_id != location_id:
            raise OsSelectionError(f"offer {offer_id} not found at {provider_key}/{location_id}")
        if not offer.enabled or offer.vcpu <= 0:
            raise OsSelectionError(f"offer {offer_id} is not sellable")

        try:
            provider: CloudProvider = self._registry.get(provider_key)
        except KeyError:
            raise OsSelectionError(f"unknown provider {provider_key!r}") from None

        images = await provider.list_images()
        compatible = compatible_os_options(images, offer.architecture)
        if not compatible:
            raise OsSelectionError(f"no {offer.architecture} images for {provider_key}")

        options: list[OsOption] = []
        for image in compatible:
            try:
                callback = encode_callback(
                    Callback(
                        flow="buy",
                        screen="os",
                        args=(provider_key, location_id, str(offer_id), image.id),
                    ),
                    self._signing_key,
                )
            except CallbackError:
                # An image id that cannot ride the callback wire (invalid
                # field characters) is unusable from a Telegram button.
                logger.warning(
                    "image %r of %s has an id unusable in callbacks; skipping",
                    image.id,
                    provider_key,
                )
                continue
            options.append(
                OsOption(
                    image_id=image.id,
                    name=image.name,
                    os_family=image.os_family,
                    architecture=image.architecture,
                    select_callback=callback,
                )
            )
        if not options:
            raise OsSelectionError(
                f"no {offer.architecture} image of {provider_key} has a usable id"
            )

        return OsSelectionView(
            provider_key=provider_key,
            location_id=location_id,
            offer=BuyFlowViewService._offer_view(offer),
            options=tuple(options),
            back_callback=encode_callback(
                Callback(
                    flow="buy",
                    screen="plans",
                    args=(provider_key, location_id, str(offer_id)),
                ),
                self._signing_key,
            ),
            cancel_callback=encode_callback(
                Callback(flow="main", screen="menu"), self._signing_key
            ),
        )


class BuyFlowViewService:
    """Builds the customer purchase-flow screens with signed callbacks.

    The screens are the data side of the M08-001 state machine: the UI layer
    renders them and maps Telegram callback data back through
    ``decode_callback``. The callback scheme guarantees:

    - **Stability** — the same screen/args always yield the same string, so
      button caches and deep links stay valid;
    - **Tamper resistance** — each callback carries a truncated HMAC
      (keyed by the server secret); a modified flow, screen or arg is
      rejected with ``CallbackError`` and rendered as "link expired";
    - **Enabled only** — the same sellable filter as the country view.
    """

    def __init__(
        self,
        catalog_repo: CatalogRepository,
        location_repo: LocationRepository,
        signing_key: str,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        self._catalog = catalog_repo
        self._locations = location_repo
        self._signing_key = signing_key

    # -- helpers -----------------------------------------------------------

    def _encode(self, callback: Callback) -> str:
        return encode_callback(callback, self._signing_key)

    async def _sellable_offers(self) -> list[CatalogOffer]:
        offers = await self._catalog.list_offers()
        return [o for o in offers if o.enabled and o.vcpu > 0]

    async def _location_index(
        self, offers: list[CatalogOffer]
    ) -> dict[tuple[str, str], LocationRecord]:
        index: dict[tuple[str, str], LocationRecord] = {}
        for key in sorted({o.provider_key for o in offers}):
            for record in await self._locations.list_for_provider(key):
                index[(key, record.location_id)] = record
        return index

    @staticmethod
    def _offer_view(offer: CatalogOffer) -> CatalogOfferView:
        return CatalogOfferView(
            offer_id=offer.id,
            provider_key=offer.provider_key,
            plan_id=offer.plan_id,
            location_id=offer.location_id,
            name=offer.name,
            architecture=offer.architecture,
            vcpu=offer.vcpu,
            memory_mb=offer.memory_mb,
            disk_gb=offer.disk_gb,
            currency=offer.currency,
            price_per_quantum=offer.price_per_quantum,
            quantum_seconds=offer.quantum_seconds,
        )

    # -- buy.locations screen ----------------------------------------------

    async def locations_screen(self) -> list[CountrySelectionView]:
        """Enabled offers grouped by country, one selectable option per
        (provider, location). Countries sorted by code, "Other" last."""
        offers = await self._sellable_offers()
        if not offers:
            return []
        index = await self._location_index(offers)

        groups: dict[str | None, dict[tuple[str, str], list[CatalogOffer]]] = {}
        for offer in offers:
            record = index.get((offer.provider_key, offer.location_id))
            country = record.country_code if record else None
            groups.setdefault(country, {}).setdefault(
                (offer.provider_key, offer.location_id), []
            ).append(offer)

        views: list[CountrySelectionView] = []
        for country in sorted(groups, key=lambda c: (c is None, c or "")):
            options: list[LocationOption] = []
            for (provider_key, location_id), rows in sorted(groups[country].items()):
                record = index.get((provider_key, location_id))
                options.append(
                    LocationOption(
                        provider_key=provider_key,
                        location_id=location_id,
                        name=record.name if record else location_id,
                        city=record.city if record else None,
                        offer_count=len(rows),
                        select_callback=self._encode(
                            Callback(
                                flow="buy",
                                screen="locations",
                                args=(provider_key, location_id),
                            )
                        ),
                    )
                )
            options.sort(key=lambda o: (o.name, o.city or ""))
            views.append(CountrySelectionView(country_code=country, options=tuple(options)))
        return views

    # -- buy.plans screen ----------------------------------------------------

    async def plans_screen(self, provider_key: str, location_id: str) -> PlanSelectionView:
        """The enabled plans sold at one location, each with its own signed
        selection callback plus the screen's back/cancel callbacks."""
        offers = [
            o
            for o in await self._sellable_offers()
            if o.provider_key == provider_key and o.location_id == location_id
        ]
        if not offers:
            raise PlanSelectionError(f"no sellable offers at {provider_key}/{location_id}")

        index = await self._location_index(offers)
        record = index.get((provider_key, location_id))
        plans = tuple(sorted((self._offer_view(o) for o in offers), key=lambda o: o.name))
        return PlanSelectionView(
            provider_key=provider_key,
            location_id=location_id,
            location_name=record.name if record else location_id,
            city=record.city if record else None,
            plans=plans,
            plan_callbacks=tuple(
                self._encode(
                    Callback(
                        flow="buy",
                        screen="plans",
                        args=(provider_key, location_id, str(offer.offer_id)),
                    )
                )
                for offer in plans
            ),
            back_callback=self._encode(
                Callback(flow="buy", screen="locations", args=(provider_key, location_id))
            ),
            cancel_callback=self._encode(Callback(flow="main", screen="menu")),
        )
