"""Leaseweb ordering-VPS catalog sync (LEASEWEB-MVP + LEASEWEB-MULTIACCOUNT).

Dynamic eligibility discovery replaces the old "configured locations are
sellable locations" model::

    discovered candidates (seeds + persisted + provider responses)
        -> read-only eligibility probe per location PER CREDENTIAL ACCOUNT
        -> currently eligible locations -> current products
        -> enabled + operator-priced offers -> Telegram storefront

Configured locations (``LEASEWEB_LOCATIONS``) are DISCOVERY SEEDS ONLY:
worth probing, never an authorization allowlist. The source of truth for
account eligibility is always the live Leaseweb response.

**Multiple credential accounts.** A Leaseweb API key / Sales Organization has a
narrow location scope, so the sync runs the discovery pipeline once per
configured account and MERGES the observations into one customer-facing
catalog::

    lw-eu    -> FRA-01, AMS-01
    lw-asia  -> SIN-01
    storefront: FRA-01, AMS-01, SIN-01

Per-account failure isolation is the whole point:

- one account's transient failure (429/5xx/timeout) preserves the
  last-known-good offers THAT account owns;
- one account's definitive 403 removes that account's route for new
  purchases but leaves every other account's locations alone;
- a 401 marks only THAT account ``auth_failed`` — the provider stays
  operational while any other account works;
- a location is only hidden when EVERY account that knows it gave a
  definitive negative answer. If any account's view is unknown, last-known
  availability is preserved.

Every run also persists the observation into ``provider_routes`` so a checkout
can PIN the fulfillment account deterministically before any billable call.

Sync NEVER touches the operator-owned fields (``enabled``,
``selling_price_minor``) — it only refreshes provider-reported data and the
``provider_available`` flag. Products Leaseweb no longer reports for a
location are flagged unavailable, which automatically removes them from the
customer browse view without deleting history.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.offers.domain import BILLING_MODEL_MONTHLY, OfferSpecUpdate
from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
from cloud_platform.modules.provider_routes.domain import RouteObservation, RouteState
from cloud_platform.modules.provider_routes.repository import (
    SqlAlchemyProviderRouteRepository,
)
from cloud_platform.providers.leaseweb.errors import (
    LeasewebAuthenticationError,
)
from cloud_platform.providers.leaseweb.ordering import (
    KNOWN_VPS_DATACENTERS,
    LeaseWebOrderingProvider,
    LeasewebProduct,
    LeasewebProductDetail,
    LocationEligibility,
    LocationProbe,
    merge_candidates,
    normalize_technical_spec,
)
from cloud_platform.providers.routing import DEFAULT_CREDENTIAL_ACCOUNT, CredentialAccountState

logger = logging.getLogger(__name__)

PROVIDER_KEY = "leaseweb"
CURRENCY = "EUR"

#: Eligibility verdict -> durable route state.
_ROUTE_STATE: dict[LocationEligibility, RouteState] = {
    LocationEligibility.ELIGIBLE_AVAILABLE: RouteState.ELIGIBLE_AVAILABLE,
    LocationEligibility.ELIGIBLE_EMPTY: RouteState.ELIGIBLE_EMPTY,
    LocationEligibility.INELIGIBLE_ACCOUNT: RouteState.INELIGIBLE,
    LocationEligibility.TRANSIENT_THROTTLED: RouteState.TRANSIENT_UNKNOWN,
    LocationEligibility.TRANSIENT_UNKNOWN: RouteState.TRANSIENT_UNKNOWN,
    LocationEligibility.FATAL_AUTHENTICATION: RouteState.AUTH_FAILED,
}

#: Verdicts that DEFINITIVELY answer "this account cannot serve this location".
_DEFINITIVE_NEGATIVE_VERDICTS: frozenset[LocationEligibility] = frozenset(
    {LocationEligibility.ELIGIBLE_EMPTY, LocationEligibility.INELIGIBLE_ACCOUNT}
)


def ordering_provider_from_settings(settings: Any) -> LeaseWebOrderingProvider:
    """Build the ordering adapter from settings (shared construction).

    Used by the periodic worker refresh, the manual ``leaseweb sync-offers``
    command and the application container. Configured locations are discovery
    seeds only (possibly empty) — never the sellability authority.

    This is the SINGLE-credential constructor; multi-credential deployments
    build a :class:`~cloud_platform.providers.leaseweb.accounts.LeasewebAccountRouter`
    instead (one adapter per account).
    """
    return LeaseWebOrderingProvider(
        api_key=settings.leaseweb_api_key,
        base_url=settings.leaseweb_api_base_url,
        locations=tuple(
            part.strip() for part in (settings.leaseweb_locations or "").split(",") if part.strip()
        ),
        os_allowlist=tuple(
            part.strip()
            for part in (settings.leaseweb_os_allowlist or "").split(",")
            if part.strip()
        ),
        order_os_only_free=settings.leaseweb_order_os_only_free,
        contract_term=settings.leaseweb_contract_term,
        billing_cycle=settings.leaseweb_billing_cycle,
        timeout_seconds=settings.leaseweb_timeout_seconds,
    )


@dataclass(frozen=True, slots=True)
class AccountCatalogProbe:
    """Read-only catalog discovery of ONE credential account (doctor output).

    Product counts are keyed by location and come from the location-scoped
    LIST endpoint, which is the authority for what this credential may sell
    where. ``detail_warnings`` names the locations whose optional per-product
    DETAIL endpoint failed — information for the operator, never a reason to
    drop an offer.
    """

    account_id: str
    authenticated: bool
    products_by_location: dict[str, int] = field(default_factory=dict)
    detail_warnings: tuple[str, ...] = ()
    error_class: str | None = None
    #: Whether the location-LESS catalog read was accepted. Advisory only: the
    #: unscoped endpoint can be refused while every scoped read succeeds, so it
    #: never decides this credential's verdict (it is reported for diagnosis).
    unscoped_available: bool = True
    unscoped_error_class: str | None = None

    @property
    def location_count(self) -> int:
        return len(self.products_by_location)

    @property
    def product_count(self) -> int:
        return sum(self.products_by_location.values())


async def probe_account_catalog(
    provider: LeaseWebOrderingProvider,
    account_id: str = DEFAULT_CREDENTIAL_ACCOUNT,
    *,
    seeds: tuple[str, ...] = (),
    persisted: tuple[str, ...] = (),
) -> AccountCatalogProbe:
    """Discover what ONE credential can sell where, read-only.

    Shared by ``leaseweb accounts doctor`` and the multi-account sync so both
    answer the same question the same way: the account is authenticated, then
    every candidate location is probed with a single LIST read, and only the
    locations that actually answered contribute offers.

    Locations are DISCOVERED (provider responses first, then configured seeds,
    built-in datacenter seeds and previously observed routes) — never assumed
    to be globally available, and never hard-coded per account.
    """
    unscoped_available = True
    unscoped_error_class: str | None = None
    try:
        unscoped = await provider.list_products_unscoped()
    except LeasewebAuthenticationError as exc:
        # Advisory, NOT fatal: the scoped probes below are the authority. Some
        # Sales Organizations are simply not allowed the unscoped listing.
        logger.info(
            "leaseweb account %s: unscoped catalog refused (%s); judging the "
            "credential by scoped reads only",
            account_id,
            type(exc).__name__,
        )
        unscoped = []
        unscoped_available = False
        unscoped_error_class = type(exc).__name__
    except Exception as exc:
        # A transport failure is not proof of a bad credential; the per-location
        # probes below still decide what is sellable.
        logger.info("leaseweb account %s unscoped catalog unavailable: %s", account_id, exc)
        unscoped = []
        unscoped_available = False
        unscoped_error_class = type(exc).__name__

    candidates = merge_candidates(
        tuple(seeds or getattr(provider, "discovery_seeds", ())),
        KNOWN_VPS_DATACENTERS,
        tuple(persisted),
        tuple(product.location for product in unscoped if product.location),
    )
    products_by_location: dict[str, int] = {}
    detail_warnings: list[str] = []
    probed: set[str] = set()
    queue = list(candidates)
    while queue:
        location = queue.pop(0)
        if location in probed:
            continue
        probed.add(location)
        try:
            probe = await provider.probe_location(location)
        except LeasewebAuthenticationError:
            return AccountCatalogProbe(
                account_id=account_id,
                authenticated=False,
                products_by_location=products_by_location,
                detail_warnings=tuple(detail_warnings),
                error_class="AuthenticationError",
            )
        except Exception as exc:
            logger.warning(
                "leaseweb account %s probe of %s failed inconclusively: %s",
                account_id,
                location,
                type(exc).__name__,
            )
            continue
        for extra in probe.discovered_locations:
            if extra not in probed and extra not in queue:
                queue.append(extra)
        if probe.eligibility is LocationEligibility.FATAL_AUTHENTICATION:
            return AccountCatalogProbe(
                account_id=account_id,
                authenticated=False,
                products_by_location=products_by_location,
                detail_warnings=tuple(detail_warnings),
                error_class="AuthenticationError",
            )
        if probe.eligibility is not LocationEligibility.ELIGIBLE_AVAILABLE:
            continue
        products_by_location[location] = len(probe.products)
        if probe.products and not await _detail_probe(
            account_id, provider, location, probe.products[0].id
        ):
            detail_warnings.append(location)
    return AccountCatalogProbe(
        account_id=account_id,
        authenticated=True,
        products_by_location=products_by_location,
        detail_warnings=tuple(detail_warnings),
        unscoped_available=unscoped_available,
        unscoped_error_class=unscoped_error_class,
    )


async def _detail_probe(
    account_id: str, provider: LeaseWebOrderingProvider, location: str, product_id: str
) -> bool:
    """Whether the OPTIONAL detail endpoint works for one product/location.

    Read-only and advisory: it exists so an operator can see that Leaseweb's
    detail endpoint is unavailable for a location the LIST endpoint serves
    normally. It never changes the catalog.
    """
    try:
        await provider.get_product(location, product_id)
    except Exception as exc:
        logger.warning(
            "leaseweb detail endpoint unavailable for %s at %s (%s)",
            product_id,
            location,
            type(exc).__name__,
        )
        return False
    return True


@dataclass(frozen=True, slots=True)
class SyncResult:
    total_fetched: int
    total_upserted: int
    total_skipped: int
    errors: list[str]
    #: Per-credential, per-location product counts for this run
    #: (``{account_id: {location_id: products}}``) — operator evidence that a
    #: credential really does serve the locations it was probed for.
    account_locations: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Provider-data advisories (an observation we deliberately did NOT write,
    #: e.g. a response that omitted the currency). NOT persistence failures.
    warnings: list[str] = field(default_factory=list)
    #: Durable-write failures (offer upserts, provider routes). Any entry here
    #: means the run is NOT a successful sync and must not be reported as one.
    persistence_failures: list[str] = field(default_factory=list)
    #: How many routing observations were written this run.
    routes_persisted: int = 0
    #: Offers successfully written / rejected by a durable write.
    offers_persisted: int = 0
    offers_failed: int = 0
    #: Set when availability reconciliation ran; False means it was SKIPPED.
    availability_reconciled: bool = False
    marked_unavailable: int = 0
    #: (product_id, location_id) pairs successfully persisted as AVAILABLE
    #: this run — the only rows automatic pricing/publication may touch.
    verified: frozenset[tuple[str, str]] = frozenset()

    @property
    def location_count(self) -> int:
        """Distinct (credential, location) pairs observed this run."""
        return sum(len(locations) for locations in self.account_locations.values())

    @property
    def persistence_ok(self) -> bool:
        """Whether every durable write this run succeeded."""
        return not self.persistence_failures

    @property
    def offer_persistence_failures(self) -> list[str]:
        """Offer upserts that failed (each is a durable-write failure)."""
        return [failure for failure in self.persistence_failures if failure.startswith("upsert ")]

    @property
    def route_persistence_failures(self) -> list[str]:
        """Routing observations that could not be persisted."""
        return [
            failure
            for failure in self.persistence_failures
            if failure.startswith("provider routes")
        ]


@dataclass(slots=True)
class _AccountDiscovery:
    """One credential account's read-only discovery outcome for one run."""

    account_id: str
    probes: dict[str, LocationProbe] = field(default_factory=dict)
    details: dict[tuple[str, str], LeasewebProductDetail] = field(default_factory=dict)
    #: (product_id, location) pairs whose DETAIL read failed. Leaseweb's detail
    #: endpoint is documented to return HTTP 500 for locations whose LIST
    #: endpoint works fine, so this is enrichment loss only: the product stays
    #: in the catalog on the strength of the list response, plus a warning.
    detail_failures: set[tuple[str, str]] = field(default_factory=set)
    auth_failed: bool = False
    note: str = ""

    def probe_state(self, location: str) -> LocationEligibility | None:
        probe = self.probes.get(location)
        return probe.eligibility if probe is not None else None

    def product_counts(self) -> dict[str, int]:
        """Products this credential serves per eligible location."""
        return {
            location: len(probe.products)
            for location, probe in sorted(self.probes.items())
            if probe.eligibility is LocationEligibility.ELIGIBLE_AVAILABLE
        }


class LeaseWebOrderingCatalogSyncer:
    """Syncs ordering products of EVERY credential account into one price book."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        provider: LeaseWebOrderingProvider | None = None,
        *,
        accounts: Mapping[str, LeaseWebOrderingProvider] | None = None,
        account_priorities: Mapping[str, int] | None = None,
        account_states: Mapping[str, CredentialAccountState] | None = None,
    ) -> None:
        self._session_factory = session_factory
        if accounts:
            # Deterministic account order is the caller's contract (the router
            # hands over ``(priority, account_id)`` order); ``dict`` preserves
            # insertion order, so the same configuration always aggregates the
            # same way.
            self._accounts: dict[str, LeaseWebOrderingProvider] = dict(accounts)
        elif provider is not None:
            self._accounts = {DEFAULT_CREDENTIAL_ACCOUNT: provider}
        else:
            raise ValueError("either provider= or accounts= must be supplied")
        self._priorities = dict(account_priorities or {})
        self._states = dict(account_states or {})
        #: Back-compat accessor for the single-account call sites/tests.
        self._provider = provider or next(iter(self._accounts.values()))

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    @property
    def account_ids(self) -> tuple[str, ...]:
        return tuple(self._accounts)

    def _priority_of(self, account_id: str) -> int:
        return int(self._priorities.get(account_id, 100))

    def _state_of(self, account_id: str) -> CredentialAccountState:
        return self._states.get(account_id, CredentialAccountState.ACTIVE)

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------

    async def sync_locations(self) -> SyncResult:
        from cloud_platform.modules.catalog.repository import SqlAlchemyLocationRepository

        repo = SqlAlchemyLocationRepository(self._session_factory)
        errors: list[str] = []
        upserted = 0
        skipped = 0
        fetched = 0
        seen: set[str] = set()
        for account_id, provider in self._accounts.items():
            try:
                locations = await provider.list_locations()
            except Exception as exc:
                # One account's seed list failing is not fatal: the union of
                # the others still drives discovery.
                logger.warning("leaseweb account %s locations unavailable: %s", account_id, exc)
                errors.append(f"locations[{account_id}]: {exc}")
                continue
            for loc in locations:
                if loc.id in seen:
                    continue
                seen.add(loc.id)
                fetched += 1
                try:
                    created = await repo.upsert(
                        LocationRecord(
                            provider_key=PROVIDER_KEY,
                            location_id=loc.id,
                            name=loc.name,
                            country_code=loc.country_code or None,
                            city=loc.city,
                        )
                    )
                except Exception as exc:
                    errors.append(f"location {loc.id}: {exc}")
                    continue
                upserted += created
                skipped += 0 if created else 1
        return SyncResult(fetched, upserted, skipped, errors)

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    async def sync_products(self) -> SyncResult:
        offers_repo = SqlAlchemySellableOfferRepository(self._session_factory)
        route_repo = SqlAlchemyProviderRouteRepository(self._session_factory)
        errors: list[str] = []
        # Provider-data advisories vs DURABLE-WRITE failures are different
        # facts with different consequences: only the latter make the run
        # unusable, and only the latter suppress availability reconciliation.
        warnings: list[str] = []
        persistence_failures: list[str] = []
        fetched = 0
        upserted = 0
        skipped = 0

        # Locations with a DEFINITIVE aggregate outcome this run. Only their
        # offers may be hidden; every other currently-available row is
        # preserved (transient failures and unprobed accounts keep last-known
        # state).
        resolved: set[str] = set()
        available: set[tuple[str, str]] = set()

        from cloud_platform.modules.catalog.repository import SqlAlchemyLocationRepository

        loc_repo = SqlAlchemyLocationRepository(self._session_factory)
        try:
            await loc_repo.list_for_provider(PROVIDER_KEY)
        except Exception as exc:
            logger.warning("leaseweb persisted locations unreadable: %s", exc)

        # Last-known availability (transient preservation) and last-known
        # per-account eligibility (so an account that fails this run does not
        # erase what it previously proved).
        try:
            current_rows = await offers_repo.list_all()
            current_available: set[tuple[str, str]] | None = {
                (str(row.product_id), str(row.location_id))
                for row in current_rows
                if row.provider_key == PROVIDER_KEY and row.provider_available
            }
        except Exception as exc:
            errors.append(f"current offers unreadable, availability left untouched: {exc}")
            current_available = None
        try:
            prior_available_locations = {
                str(row.location_id)
                for row in (current_rows if current_available is not None else [])
                if row.provider_key == PROVIDER_KEY and row.provider_available
            }
        except Exception:  # pragma: no cover - defensive
            prior_available_locations = set()

        prior_states: dict[str, dict[str, LocationEligibility]] = {}
        try:
            for route in await route_repo.list_for_provider(PROVIDER_KEY):
                for verdict, state in _ROUTE_STATE.items():
                    if state is route.state:
                        prior_states.setdefault(route.location_id, {})[
                            route.credential_account_id
                        ] = verdict
                        break
        except Exception as exc:
            errors.append(f"provider routes unreadable, discovery starts fresh: {exc}")

        # --- Per-account discovery -------------------------------------
        discoveries: dict[str, _AccountDiscovery] = {}
        for account_id, provider in self._accounts.items():
            discovery = await self._discover_account(
                account_id, provider, loc_repo, prior_states, errors
            )
            discoveries[account_id] = discovery
            # DISCOVERED counts the provider's own LIST observations — the
            # authoritative catalog source. The per-product DETAIL reads are
            # optional enrichment, so counting THOSE would report "0 products"
            # for a sync that listed six products per location while every
            # detail read hit HTTP 500 (the production outage).
            fetched += sum(
                len(probe.products)
                for probe in discovery.probes.values()
                if probe.eligibility is LocationEligibility.ELIGIBLE_AVAILABLE
            )

        # --- Aggregate per location -----------------------------------
        candidates: dict[str, list[str]] = {}
        for account_id, discovery in discoveries.items():
            for location in discovery.probes:
                candidates.setdefault(location, []).append(account_id)

        verified_pairs: set[tuple[str, str]] = set()
        for location, probing_accounts in candidates.items():
            verdicts: dict[str, LocationEligibility] = {}
            for account_id in self._accounts:
                probed: LocationEligibility | None = discoveries[account_id].probe_state(location)
                if probed is not None:
                    verdicts[account_id] = probed
                    continue
                prior = prior_states.get(location, {})
                if account_id in prior:
                    verdicts[account_id] = prior[account_id]
            unknown = [
                account_id
                for account_id in self._accounts
                if account_id not in verdicts and account_id not in probing_accounts
            ]
            serving = [
                account_id
                for account_id, verdict in verdicts.items()
                # A DRAINING account keeps serving the resources it owns, but
                # new purchases must not route to it.
                if verdict is LocationEligibility.ELIGIBLE_AVAILABLE
                and self._state_of(account_id) is not CredentialAccountState.DISABLED
            ]
            if serving:
                resolved.add(location)
                upserted += await self._upsert_location_products(
                    location,
                    serving,
                    discoveries,
                    offers_repo,
                    available,
                    current_available,
                    errors,
                    warnings,
                    persistence_failures,
                    verified_pairs,
                )
            elif (
                verdicts
                and not unknown
                and all(verdict in _DEFINITIVE_NEGATIVE_VERDICTS for verdict in verdicts.values())
            ):
                # Every account that knows this location says "not available":
                # definitive, so its offers are hidden until it comes back.
                resolved.add(location)
            else:
                logger.info(
                    "leaseweb location %s left at last-known availability (unknown accounts=%s)",
                    location,
                    ",".join(sorted(unknown)) or "-",
                )
            for extra in self._discovered_extras(discoveries, location):
                if extra not in candidates:
                    candidates.setdefault(extra, []).append("")

        # A location whose products all vanished but whose discovery was
        # definitive is handled above; locations that no account probed at all
        # this run keep last-known state (they are simply absent from
        # ``resolved``).
        skipped = max(0, len(prior_available_locations - resolved))

        # --- Persist the routing observations -------------------------
        observations = self._route_observations(discoveries)
        routes_persisted = 0
        try:
            await route_repo.upsert_observations(
                provider_key=PROVIDER_KEY,
                observations=observations,
                priority_of=self._priority_of,
                account_state_of=self._state_of,
            )
            routes_persisted = len(observations)
        except Exception as exc:
            # A durable-write failure is NOT a normal sync outcome: without
            # routes, checkout cannot pin a fulfillment account, so the run
            # must be reported as failed (and must not retire offers).
            persistence_failures.append(f"provider routes write failed: {exc}")

        # --- Hide what is definitively gone ---------------------------
        availability_reconciled = False
        marked_count = 0
        if not any(discovery.probes for discovery in discoveries.values()):
            # NO credential account produced a usable view this run (every key
            # rejected, or the transport is down). With no provider evidence at
            # all, availability is left completely untouched — a broken
            # deployment must never empty the storefront.
            errors.append(
                "authentication failed: no credential account could be probed; "
                "availability left untouched"
            )
        elif current_available is None:
            warnings.append("skipped mark_unavailable: current availability unreadable")
        elif persistence_failures:
            # A failed persistence phase must NEVER turn previously healthy
            # offers unavailable: this run's view of the catalog is incomplete.
            warnings.append(
                "skipped mark_unavailable: the persistence phase of this run failed "
                f"({len(persistence_failures)} error(s)); no offer was retired"
            )
        else:
            for pair in current_available:
                if pair[1] not in resolved:
                    available.add(pair)
            availability_reconciled = True
            try:
                marked_count = await offers_repo.mark_unavailable(
                    PROVIDER_KEY, available, billing_model=BILLING_MODEL_MONTHLY
                )
            except Exception as exc:
                persistence_failures.append(f"mark_unavailable: {exc}")
            else:
                if marked_count:
                    logger.info(
                        "leaseweb ordering sync marked %d products unavailable", marked_count
                    )

        # --- Guarantee normalized location metadata ---------------------
        # Every definitively eligible location must have a normalized
        # ProviderLocation row (the storefront flag/name path reads ONLY
        # these rows): describe + upsert each resolved location, best effort
        # per row. A metadata write failure is reported but never touches
        # availability and never hides a valid product.
        try:
            metadata_warnings = await self._ensure_location_metadata(loc_repo, resolved)
        except Exception as exc:  # pragma: no cover - defensive; per-row handling above
            metadata_warnings = [f"location metadata pass failed: {type(exc).__name__}"]
        warnings.extend(metadata_warnings)
        return SyncResult(
            fetched,
            upserted,
            skipped,
            errors,
            account_locations={
                account_id: discovery.product_counts()
                for account_id, discovery in discoveries.items()
            },
            warnings=warnings,
            persistence_failures=persistence_failures,
            routes_persisted=routes_persisted,
            offers_persisted=upserted,
            offers_failed=sum(
                1 for failure in persistence_failures if failure.startswith("upsert ")
            ),
            availability_reconciled=availability_reconciled,
            marked_unavailable=marked_count,
            verified=frozenset(verified_pairs),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ensure_location_metadata(self, loc_repo: Any, locations: set[str]) -> list[str]:
        """Persist normalized metadata for definitively eligible locations.

        The storefront flag/name path reads ONLY ProviderLocation rows, so
        every location the sync proved eligible must have one with a usable
        country and city. Normalization lives in the adapter
        (``describe_location``: exact table, then city-prefix fallback).
        Best effort per row: failures are reported as warnings and never
        touch product availability.
        """
        warnings: list[str] = []
        for location in sorted(locations):
            try:
                described = self._provider.describe_location(location)
                await loc_repo.upsert(
                    LocationRecord(
                        provider_key=PROVIDER_KEY,
                        location_id=described.id,
                        name=described.name,
                        country_code=described.country_code or None,
                        city=described.city,
                    )
                )
            except Exception as exc:
                warnings.append(f"location metadata {location}: {type(exc).__name__}")
        return warnings

    def _discovered_extras(
        self, discoveries: Mapping[str, _AccountDiscovery], location: str
    ) -> tuple[str, ...]:
        extras: list[str] = []
        for discovery in discoveries.values():
            probe = discovery.probes.get(location)
            if probe is None:
                continue
            for extra in probe.discovered_locations:
                if extra not in extras:
                    extras.append(extra)
        return tuple(extras)

    def _route_observations(
        self, discoveries: Mapping[str, _AccountDiscovery]
    ) -> list[RouteObservation]:
        """Map this run's probes onto durable, per-account route observations."""
        observations: list[RouteObservation] = []
        for account_id, discovery in discoveries.items():
            for location, probe in discovery.probes.items():
                product_ids = tuple(product.id for product in probe.products)
                observations.append(
                    RouteObservation(
                        credential_account_id=account_id,
                        location_id=location,
                        state=_ROUTE_STATE.get(probe.eligibility, RouteState.TRANSIENT_UNKNOWN),
                        product_ids=product_ids
                        if probe.eligibility is LocationEligibility.ELIGIBLE_AVAILABLE
                        else (),
                        error_class=_error_class_for(probe),
                        succeeded=(probe.eligibility is LocationEligibility.ELIGIBLE_AVAILABLE),
                    )
                )
            if discovery.auth_failed:
                observations.append(
                    RouteObservation(
                        credential_account_id=account_id,
                        location_id="*",
                        state=RouteState.AUTH_FAILED,
                        error_class="AuthenticationError",
                    )
                )
        return observations

    async def _upsert_location_products(
        self,
        location: str,
        serving_accounts: list[str],
        discoveries: Mapping[str, _AccountDiscovery],
        offers_repo: SqlAlchemySellableOfferRepository,
        available: set[tuple[str, str]],
        current_available: set[tuple[str, str]] | None,
        errors: list[str],
        warnings: list[str],
        persistence_failures: list[str],
        verified: set[tuple[str, str]],
    ) -> int:
        """Upsert every product a serving account reports at one location.

        The location-scoped LIST response is the authority for catalog
        MEMBERSHIP: every product it reported is written (or kept) available,
        even when the optional per-product detail read failed. The detail read
        only enriches the row — contract-term price and full configuration —
        and its absence is recorded as a warning, never as a removal.

        One logical customer-facing offer per (product, location) even when two
        credentials can supply it: the FIRST serving account in deterministic
        order supplies the provider cost snapshot, the product detail AND the
        ``provider_account_id`` provenance, so the same configuration always
        produces the same catalog and the same fulfillment route.

        Returns the number of offers newly written as AVAILABLE (so the sync
        result reflects real work, not merely locations probed).
        """
        written = 0
        for account_id in serving_accounts:
            discovery = discoveries[account_id]
            probe = discovery.probes.get(location)
            if probe is None:
                continue
            for product in probe.products:
                pair = (product.id, location)
                if pair in available:
                    # Already supplied by a higher-priority account.
                    continue
                detail = discovery.details.get(pair)
                if detail is None:
                    detail = await self._optional_detail(
                        account_id, discovery, location, product, errors
                    )
                available.add(pair)
                self._report_cost_disagreement(location, product, discoveries, serving_accounts)
                # Baseline spec = the LIST row (authoritative for membership and
                # always present); a successful detail read upgrades the price
                # to the contract-term total and keeps the full configuration.
                offer_product = detail.product if detail is not None else product
                if (
                    detail is not None
                    and detail.available_locations
                    and location not in detail.available_locations
                ):
                    # The DETAIL endpoint says the product is no longer sold
                    # here: real provider evidence, so flag it unavailable.
                    await self._write_offer(
                        offers_repo,
                        provider_account_id=account_id,
                        product=product,
                        location=location,
                        update=OfferSpecUpdate(
                            name=product.name,
                            vcpu=product.vcpu,
                            ram_gb=product.ram_gb,
                            disk_gb=product.disk_gb,
                            traffic=product.traffic,
                            provider_cost_minor=product.monthly_price_minor,
                            provider_cost_currency=product.currency,
                            provider_account_id=account_id,
                            billing_parameters={
                                "contract_term": self._contract_term_of(account_id),
                                "billing_cycle": self._billing_cycle_of(account_id),
                                "monthly_price_source": "contractTerms",
                            },
                            technical_metadata=normalize_technical_spec(product).to_metadata(),
                            provider_available=False,
                        ),
                        errors=errors,
                        warnings=warnings,
                        persistence_failures=persistence_failures,
                    )
                    continue
                if await self._write_offer(
                    offers_repo,
                    provider_account_id=account_id,
                    product=product,
                    location=location,
                    update=OfferSpecUpdate(
                        name=offer_product.name,
                        vcpu=offer_product.vcpu,
                        ram_gb=offer_product.ram_gb,
                        disk_gb=offer_product.disk_gb,
                        traffic=offer_product.traffic,
                        provider_cost_minor=offer_product.monthly_price_minor,
                        provider_cost_currency=offer_product.currency,
                        provider_account_id=account_id,
                        billing_parameters={
                            "contract_term": self._contract_term_of(account_id),
                            "billing_cycle": self._billing_cycle_of(account_id),
                            "monthly_price_minor": offer_product.monthly_price_minor,
                            "monthly_price_source": (
                                "contractTerms" if detail is not None else "list"
                            ),
                            "available_locations": sorted(
                                detail.available_locations if detail is not None else ()
                            ),
                        },
                        technical_metadata=normalize_technical_spec(offer_product).to_metadata(),
                        provider_available=True,
                    ),
                    errors=errors,
                    warnings=warnings,
                    persistence_failures=persistence_failures,
                ):
                    written += 1
                    verified.add(pair)
        return written

    def _contract_term_of(self, account_id: str) -> str:
        return str(getattr(self._accounts[account_id], "_contract_term", "1_MONTH"))

    def _billing_cycle_of(self, account_id: str) -> str:
        return str(getattr(self._accounts[account_id], "_billing_cycle", "1_MONTH"))

    def _report_cost_disagreement(
        self,
        location: str,
        product: LeasewebProduct,
        discoveries: Mapping[str, _AccountDiscovery],
        serving_accounts: list[str],
    ) -> None:
        """Log (never hide) a provider-cost disagreement between accounts.

        Different Sales Organizations may price the same product differently.
        The chosen route's cost is the snapshot used for the durable order, but
        the operator must be able to SEE the discrepancy — without credentials.
        """
        costs: dict[str, int] = {}
        for account_id in serving_accounts:
            detail = discoveries[account_id].details.get((product.id, location))
            if detail is not None:
                costs[account_id] = detail.product.monthly_price_minor
        if len(set(costs.values())) > 1:
            logger.warning(
                "leaseweb provider cost for %s at %s differs across credential "
                "accounts %s (customer selling price is unaffected)",
                product.id,
                location,
                ", ".join(f"{account}={cost}" for account, cost in sorted(costs.items())),
            )

    @staticmethod
    async def _write_offer(
        offers_repo: SqlAlchemySellableOfferRepository,
        *,
        provider_account_id: str,
        product: LeasewebProduct,
        location: str,
        update: OfferSpecUpdate,
        errors: list[str],
        warnings: list[str] | None = None,
        persistence_failures: list[str] | None = None,
    ) -> bool:
        """Persist one provider observation; ``True`` when it succeeded.

        FAIL CLOSED on an unproven currency: Sales Organizations bill in
        different currencies (EUR, GBP, ...), so a response that omitted the
        currency must never overwrite a known-correct stored price. The
        observation is skipped as an advisory warning; the caller has already
        recorded the pair as available, so a skipped write cannot retire it.

        A DURABLE-WRITE failure (schema error, connection error) is recorded
        separately from provider-data advisories: it makes the whole run
        unusable and must be reported as a failed sync.
        """
        if not str(update.provider_cost_currency or "").strip():
            target = warnings if warnings is not None else errors
            target.append(
                f"currency not reported for {product.id}/{location}: keeping the "
                "last-known provider cost/currency (never inferred)"
            )
            return False
        try:
            await offers_repo.upsert_from_provider(
                provider_key=PROVIDER_KEY,
                product_id=product.id,
                location_id=location,
                provider_account_id=provider_account_id,
                update=update,
            )
        except Exception as exc:
            target_failures = persistence_failures if persistence_failures is not None else errors
            target_failures.append(f"upsert {product.id}/{location}: {exc}")
            return False
        return True

    async def _details_of(
        self,
        account_id: str,
        discovery: _AccountDiscovery,
        location: str,
        product: LeasewebProduct,
    ) -> LeasewebProductDetail:
        detail = await self._accounts[account_id].get_product(location, product.id)
        discovery.details[(product.id, location)] = detail
        return detail

    async def _discover_account(
        self,
        account_id: str,
        provider: LeaseWebOrderingProvider,
        loc_repo: Any,
        prior_states: Mapping[str, Mapping[str, LocationEligibility]],
        errors: list[str],
    ) -> _AccountDiscovery:
        """Run the READ-ONLY discovery pipeline for ONE credential account."""
        discovery = _AccountDiscovery(account_id=account_id)
        # Best-effort unscoped discovery (+ authentication liveness). Any
        # failure here only narrows the candidate set; per-location probes
        # below still decide everything.
        try:
            unscoped = await provider.list_products_unscoped()
        except LeasewebAuthenticationError:
            discovery.auth_failed = True
            discovery.note = "authentication failed"
            logger.error("leaseweb account %s authentication failed", account_id)
            errors.append(f"account {account_id}: authentication failed")
            return discovery
        except Exception as exc:
            logger.info("leaseweb account %s unscoped catalog unavailable: %s", account_id, exc)
            unscoped = []
        unscoped_locations = [product.location for product in unscoped if product.location]

        persisted: tuple[str, ...] = ()
        try:
            persisted = tuple(r.location_id for r in await loc_repo.list_for_provider(PROVIDER_KEY))
        except Exception:  # pragma: no cover - best effort
            persisted = ()

        queue = list(
            merge_candidates(
                tuple(provider.discovery_seeds),
                KNOWN_VPS_DATACENTERS,
                persisted,
                tuple(prior_states),
                unscoped_locations,
            )
        )
        probed: set[str] = set()
        while queue:
            location = queue.pop(0)
            if location in probed:
                continue
            probed.add(location)
            try:
                probe = await provider.probe_location(location)
            except LeasewebAuthenticationError:
                discovery.auth_failed = True
                discovery.note = "authentication failed"
                logger.error(
                    "leaseweb account %s authentication failed while probing %s",
                    account_id,
                    location,
                )
                errors.append(f"account {account_id}: authentication failed")
                return discovery
            except Exception as exc:
                logger.warning(
                    "leaseweb account %s probe of %s failed inconclusively: %s",
                    account_id,
                    location,
                    exc,
                )
                errors.append(f"probe {account_id}/{location}: {exc}")
                probe = LocationProbe(
                    location, LocationEligibility.TRANSIENT_UNKNOWN, (), (), "probe error"
                )
            discovery.probes[location] = probe
            if probe.eligibility is LocationEligibility.FATAL_AUTHENTICATION:
                discovery.auth_failed = True
                discovery.note = "authentication failed"
                errors.append(f"account {account_id}: authentication failed")
                return discovery
            # Persist the discovery itself (display metadata included), so a
            # newly seen location is probed again automatically next run.
            # Best effort: a location-row write failure must not stop products.
            try:
                described = provider.describe_location(location)
                from cloud_platform.modules.catalog.repository import (
                    SqlAlchemyLocationRepository,
                )

                await SqlAlchemyLocationRepository(self._session_factory).upsert(
                    LocationRecord(
                        provider_key=PROVIDER_KEY,
                        location_id=described.id,
                        name=described.name,
                        country_code=described.country_code or None,
                        city=described.city,
                    )
                )
            except Exception as exc:
                errors.append(f"location {location}: {exc}")
            for extra in probe.discovered_locations:
                if extra not in probed and extra not in queue:
                    queue.append(extra)
            if probe.eligibility is not LocationEligibility.ELIGIBLE_AVAILABLE:
                continue
            for product in probe.products:
                pair = (product.id, location)
                if await self._optional_detail(account_id, discovery, location, product, errors):
                    for extra in discovery.details[pair].available_locations:
                        if extra not in probed and extra not in queue:
                            queue.append(extra)
        return discovery

    async def _optional_detail(
        self,
        account_id: str,
        discovery: _AccountDiscovery,
        location: str,
        product: LeasewebProduct,
        errors: list[str],
    ) -> LeasewebProductDetail | None:
        """Best-effort DETAIL read for one product — never authoritative.

        ``GET /ordering/v1/products/vps?location=`` (the LIST endpoint) decides
        which products exist at which location for this credential. The
        per-product detail endpoint is OPTIONAL ENRICHMENT ONLY: Leaseweb
        returns HTTP 500 for it on locations whose list endpoint answers
        normally, so a failure here (403/404/500/timeout/anything) must never
        drop the product, fail the sync or hide the offer. It records a warning
        and the caller falls back to the list row.

        Returns the detail, or ``None`` when it could not be read.
        """
        pair = (product.id, location)
        if pair in discovery.detail_failures:
            return None
        try:
            detail = await self._accounts[account_id].get_product(location, product.id)
        except Exception as exc:
            discovery.detail_failures.add(pair)
            logger.warning(
                "leaseweb detail endpoint unavailable for %s at %s (%s); "
                "keeping the product from the location list response",
                product.id,
                location,
                type(exc).__name__,
            )
            errors.append(f"detail {account_id}/{product.id}/{location}: {type(exc).__name__}")
            return None
        discovery.details[pair] = detail
        return detail

    async def sync_all(self) -> dict[str, SyncResult]:
        locations = await self.sync_locations()
        products = await self.sync_products()
        return {"locations": locations, "products": products}


def _error_class_for(probe: LocationProbe) -> str | None:
    """A SAFE error class for a non-serving probe (never the note text)."""
    if probe.eligibility is LocationEligibility.ELIGIBLE_AVAILABLE:
        return None
    if probe.eligibility is LocationEligibility.INELIGIBLE_ACCOUNT:
        return "LeasewebForbiddenError"
    if probe.eligibility is LocationEligibility.TRANSIENT_THROTTLED:
        return "LeasewebRateLimitError"
    if probe.eligibility is LocationEligibility.FATAL_AUTHENTICATION:
        return "LeasewebAuthenticationError"
    if probe.eligibility is LocationEligibility.ELIGIBLE_EMPTY:
        return None
    return "LeasewebUnavailableError"


# Back-compat alias (same convention as the other Leaseweb syncers).
LeasewebOrderingCatalogSyncer = LeaseWebOrderingCatalogSyncer
