"""Hourly cloud catalog sync (STOREFRONT-REWORK).

Turns Leaseweb Public Cloud regions and instance types into hourly
``SellableOffer`` rows through the official read-only APIs: regions are
authoritative for location membership, the region-scoped instance-type list
for plan membership at that region. A region that fails is isolated — its
offers are left untouched rather than retired — while every other region
still syncs. Availability is reconciled only when every discovered region
synced cleanly, so a partial view never mass-retires unknown inventory.

``enabled`` and ``selling_price_minor`` are NEVER written here: the
automatic pricing/publication policy owns them downstream, and an explicit
operator block is never touched. Nothing is ever deleted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.offers.domain import OfferSpecUpdate, TechnicalSpec
from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.leaseweb.cloud import (
    PROVIDER_KEY,
    CloudInstanceType,
    LeasewebHourlyCloudProvider,
)
from cloud_platform.providers.routing import (
    DEFAULT_CREDENTIAL_ACCOUNT,
    CredentialAccountState,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegionTypeReport:
    """How many hourly plans one region contributed (or why it did not)."""

    region_id: str
    products: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CloudSyncResult:
    """Outcome of one hourly-cloud sync run."""

    regions: tuple[RegionTypeReport, ...] = ()
    offers_written: int = 0
    marked_unavailable: int = 0
    warnings: tuple[str, ...] = ()
    verified: frozenset[tuple[str, str]] = frozenset()
    #: Winning credential per verified pair: (account_id, product_id,
    #: location_id). The coordinator prices the exact account-scoped row.
    verified_accounts: frozenset[tuple[str, str, str]] = frozenset()
    persistence_failures: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class OwnerChoice:
    """The account a (type, region) observation is published under.

    ``published`` is the decision the storefront sees: an offer may only be
    advertised as sellable when its OWNER proved, read-only, that it can serve
    the exact pair (region + instance type + usable images) AND the account is
    known to accept new orders.
    """

    account_id: str
    images_state: str
    item: CloudInstanceType
    published: bool
    reason: str | None = None


#: Image-read outcomes an account can report for one (type, region) pair.
#: ``ok``    - the provider listed usable images for THIS credential;
#: ``empty`` - the read succeeded but listed nothing usable (definitive);
#: ``rejected`` - the provider refused the read for this credential/region
#:                (definitive: this account does not serve the pair);
#: ``unknown``  - the read failed inconclusively (timeout/5xx/rate limit).
IMAGE_STATE_OK = "ok"
IMAGE_STATE_EMPTY = "empty"
IMAGE_STATE_REJECTED = "rejected"
IMAGE_STATE_UNKNOWN = "unknown"

#: Outcomes that must never be published: the provider answered definitively
#: that this credential cannot install anything here.
_UNPUBLISHABLE_IMAGE_STATES = frozenset({IMAGE_STATE_EMPTY, IMAGE_STATE_REJECTED})


def select_hourly_owner(
    ranked: list[tuple[str, str, CloudInstanceType]],
    *,
    current_owner_id: str | None,
    limit_reached: frozenset[str],
) -> OwnerChoice:
    """Choose the publishing account for one (type, region) pair, deterministically.

    ``ranked`` is in account order (``(priority, id)``) and each entry carries
    the account's image-read outcome (see :data:`IMAGE_STATE_OK`).

    Policy, in order:

    1. the CURRENT owner publishes when it proved the pair and may take new
       orders (no churn: a sync run is not a reason to move ownership);
    2. otherwise the first account that PROVED the pair read-only and can take
       new orders publishes it — this is how an offer legitimately moves to a
       healthy credential whose eligibility changed;
    3. otherwise the current owner keeps the row, publishing only while it is
       neither capacity-limited nor definitively unable to serve the pair;
       an INCONCLUSIVE image read therefore never costs a proven owner its
       offer, and never hands one to an account that never proved anything;
    4. otherwise the first account that is neither limited nor definitively
       unable publishes (an inconclusive probe on a sole supplier keeps the
       inventory visible rather than flapping it off the storefront);
    5. otherwise the pair stays known but UNPUBLISHED (nothing is retired).

    A capacity-limited account is never selected for a NEW order; it can still
    own the row it already owns, so provenance and reconciliation are untouched.
    """
    owner_entry = next((entry for entry in ranked if entry[0] == current_owner_id), None)

    def proven(entry: tuple[str, str, CloudInstanceType]) -> bool:
        return entry[0] not in limit_reached and entry[1] == IMAGE_STATE_OK

    if owner_entry is not None and proven(owner_entry):
        account_id, images_state, item = owner_entry
        return OwnerChoice(account_id, images_state, item, published=True)

    alternative = next((entry for entry in ranked if proven(entry)), None)
    if alternative is not None:
        account_id, images_state, item = alternative
        return OwnerChoice(account_id, images_state, item, published=True)

    if owner_entry is not None:
        account_id, images_state, item = owner_entry
        limited = account_id in limit_reached
        published = not limited and images_state not in _UNPUBLISHABLE_IMAGE_STATES
        if limited:
            reason = "capacity-limit"
        elif images_state == IMAGE_STATE_EMPTY:
            reason = "no-usable-images"
        elif images_state == IMAGE_STATE_REJECTED:
            reason = "account-cannot-serve"
        else:
            reason = None
        return OwnerChoice(account_id, images_state, item, published=published, reason=reason)

    candidate = next(
        (
            entry
            for entry in ranked
            if entry[0] not in limit_reached and entry[1] not in _UNPUBLISHABLE_IMAGE_STATES
        ),
        None,
    )
    if candidate is not None:
        account_id, images_state, item = candidate
        return OwnerChoice(account_id, images_state, item, published=True)

    account_id, images_state, item = ranked[0]
    if account_id in limit_reached:
        reason = "capacity-limit"
    elif images_state == IMAGE_STATE_EMPTY:
        reason = "no-usable-images"
    else:
        reason = "account-cannot-serve"
    return OwnerChoice(account_id, images_state, item, published=False, reason=reason)


def offer_spec_from_item(
    item: CloudInstanceType,
    region_id: str,
    *,
    publishable: bool,
    account_id: str,
) -> OfferSpecUpdate:
    """The ONE translation from a provider observation to a catalog write.

    Shared by the periodic catalog sync and the targeted capacity
    republication, so an offer that legitimately moved to another credential
    account is written with exactly the same facts (cost, currency, technical
    metadata, publication decision) a normal sync would have written — never a
    hand-rolled partial update.
    """
    billing_parameters: dict[str, object] = {
        "contract_type": "HOURLY",
        "monthly_estimate_source": "hourly_rate",
        "instance_type_id": item.id,
        "region": region_id,
        # Exact provider hourly rate (verbatim decimal text):
        # sub-cent precision the integer minor field cannot
        # hold stays auditable here instead of being rounded
        # away silently.
        "provider_hourly_rate": item.hourly_rate_exact,
    }
    if item.monthly_cost_minor is not None:
        billing_parameters["provider_monthly_cost_minor"] = item.monthly_cost_minor
    return OfferSpecUpdate(
        name=item.name,
        vcpu=item.vcpu,
        ram_gb=item.ram_gb,
        disk_gb=item.disk_gb,
        traffic=item.traffic,
        provider_cost_minor=item.hourly_cost_minor,
        provider_cost_currency=item.currency,
        billing_parameters=billing_parameters,
        technical_metadata=_technical_spec(item),
        billing_model="hourly",
        provider_available=publishable,
        provider_account_id=account_id,
    )


def _technical_spec(item: CloudInstanceType) -> dict[str, object]:
    # Exact provider facts that do not fit the coarse integer spec fields
    # ride along as namespaced metadata (strings/lists only, never float):
    # fractional memory, network speeds, the full storage-type list.
    # Unknown keys are ignored by TechnicalSpec.from_metadata, so the
    # storefront keeps working if it does not know them yet.
    extra: dict[str, object] = {
        "plan_family": item.family_key,
        "plan_family_name": item.family_name,
    }
    if item.memory_gb_exact is not None:
        extra["memory_gb_exact"] = item.memory_gb_exact
    if item.network_public is not None:
        extra["network_public"] = item.network_public
    if item.network_private is not None:
        extra["network_private"] = item.network_private
    if item.storage_types:
        extra["storage_types"] = list(item.storage_types)
    return (
        TechnicalSpec(
            architecture=item.architecture,
            cpu_type=item.cpu_type,
            storage_type=item.storage_type,
            ipv4=item.ipv4,
            ipv6=item.ipv6,
        ).to_metadata()
        | extra
    )


class LeasewebHourlyCloudSyncer:
    """Syncs hourly instance types of every region into the price book.

    Multi-account: every enabled credential account probes its own regions;
    the union is reconciled deterministically (lowest ``(priority,
    account_id)`` wins each region/type pair), and every hourly offer
    records the owning account in ``provider_account_id`` so creation later
    POSTs through exactly that credential. One account failing never blinds
    the others, and any failure suppresses global retirement.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        provider: LeasewebHourlyCloudProvider | None = None,
        *,
        accounts: dict[str, LeasewebHourlyCloudProvider] | None = None,
        account_priorities: dict[str, int] | None = None,
        account_states: dict[str, CredentialAccountState] | None = None,
        capacity: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        if accounts:
            self._accounts: dict[str, LeasewebHourlyCloudProvider] = dict(accounts)
        elif provider is not None:
            self._accounts = {DEFAULT_CREDENTIAL_ACCOUNT: provider}
        else:
            raise ValueError("either provider= or accounts= must be supplied")
        self._priorities = dict(account_priorities or {})
        self._states = dict(account_states or {})
        #: Durable per-account capacity knowledge (optional). An account whose
        #: provider limit was definitively refused must not be handed NEW
        #: orders — see :func:`select_hourly_owner`.
        self._capacity = capacity
        #: Back-compat accessor for single-account call sites.
        self._provider = provider or next(iter(self._accounts.values()))

    def _ordered_accounts(self) -> list[str]:
        """Account ids in deterministic ``(priority, id)`` order."""
        accounts = getattr(self, "_accounts", None)
        if not accounts:
            # Legacy test construction sets only ``_provider`` (via
            # ``__new__``): treat it as the single default account so the
            # sync still runs instead of raising AttributeError.
            provider = getattr(self, "_provider", None)
            if provider is not None:
                self._accounts = {DEFAULT_CREDENTIAL_ACCOUNT: provider}
                self._priorities = dict(getattr(self, "_priorities", {}) or {})
                self._states = dict(getattr(self, "_states", {}) or {})
                accounts = self._accounts
            else:
                return []
        return sorted(accounts, key=lambda aid: (self._priorities.get(aid, 100), aid))

    def _state_of(self, account_id: str) -> CredentialAccountState:
        states: dict[str, CredentialAccountState] = dict(getattr(self, "_states", {}) or {})
        return states.get(account_id, CredentialAccountState.ACTIVE)

    def _provider_for(self, account_id: str) -> LeasewebHourlyCloudProvider:
        """Adapter for one account (legacy ``_provider`` fallback for tests)."""
        accounts: dict[str, LeasewebHourlyCloudProvider] = getattr(self, "_accounts", None) or {}
        if account_id in accounts:
            provider: LeasewebHourlyCloudProvider = accounts[account_id]
            return provider
        legacy: LeasewebHourlyCloudProvider | None = getattr(self, "_provider", None)
        if legacy is not None:
            return legacy
        raise KeyError(account_id)

    async def _current_owners(self, offers_repo: Any) -> dict[tuple[str, str], str]:
        """Which credential account currently publishes each (type, region).

        Ownership stability is a correctness rule, not a preference: an
        inconclusive image probe must not move a region to an account that has
        never proved it, and an accepted order's fingerprint keeps pointing at
        the account it was pinned to. An unreadable owner map degrades to "no
        current owner known", which makes the sync conservative (it publishes
        only PROVEN pairs) instead of silently re-routing anything.
        """
        reader = getattr(offers_repo, "hourly_account_owners", None)
        if not callable(reader):
            return {}
        try:
            return dict(await reader(PROVIDER_KEY))
        except Exception as exc:
            logger.warning(
                "leaseweb hourly sync could not read current owners: %s",
                type(exc).__name__,
            )
            return {}

    async def _limit_reached_accounts(self, warnings: list[str]) -> frozenset[str]:
        """Accounts that must not be given NEW orders (durable capacity signal).

        A store failure is reported and treated as "no account is known to be
        limited": capacity knowledge is an ADDITIONAL gate, never a reason to
        blind the whole hourly catalog.
        """
        # ``getattr`` mirrors the legacy ``__new__`` construction path used by
        # older single-account call sites/tests: an absent store simply means
        # "no account is known to be limited".
        store = getattr(self, "_capacity", None)
        reader = getattr(store, "limit_reached_accounts", None)
        if not callable(reader):
            return frozenset()
        try:
            accounts = await reader(PROVIDER_KEY)
        except Exception as exc:
            warnings.append(f"capacity state unreadable ({type(exc).__name__})")
            return frozenset()
        excluded = frozenset(str(account) for account in accounts or ())
        if excluded:
            warnings.append(
                "accounts without capacity for new instances (not published): "
                + ", ".join(sorted(excluded))
            )
        return excluded

    async def sync_all(self) -> CloudSyncResult:
        """Regions per account, then per-region instance types, then reconcile.

        Ownership is image-aware: checkout mandates image selection through
        the offer's pinned account, so among the accounts exposing a
        region/type pair the winner is deterministically the first (in
        ``(priority, id)`` order) whose region also lists at least one
        usable image. Image reads never fail an account — an inconclusive
        read preserves last-known-good routing, while a conclusive empty
        read routes away (or marks the pair unavailable when no account can
        serve images for it).
        """
        from cloud_platform.modules.catalog.repository import SqlAlchemyLocationRepository

        offers_repo = SqlAlchemySellableOfferRepository(self._session_factory)
        locations_repo = SqlAlchemyLocationRepository(self._session_factory)
        warnings: list[str] = []
        errors: list[str] = []
        persistence_failures: list[str] = []
        per_region: dict[str, RegionTypeReport] = {}
        verified: set[tuple[str, str]] = set()
        verified_owner: set[tuple[str, str, str]] = set()
        available: set[tuple[str, str, str]] = set()
        seen_locations: set[str] = set()
        account_failed = False
        written = 0
        # Which account currently PUBLISHES each (type, region) pair, and which
        # accounts are known to be out of capacity for NEW instances. Both are
        # needed to keep ownership stable and to never route a new order
        # through a credential the provider just refused.
        owners = await self._current_owners(offers_repo)
        limit_reached = await self._limit_reached_accounts(warnings)
        # (type, region) -> [(account, images-state, item)] in account order.
        # images-state: "ok" (>=1 usable image), "empty" (read ok, none),
        # "unknown" (read failed; never penalized).
        candidates: dict[tuple[str, str], list[tuple[str, str, CloudInstanceType]]] = {}

        for account_id in self._ordered_accounts():
            if self._state_of(account_id) is CredentialAccountState.DISABLED:
                continue
            try:
                provider = self._provider_for(account_id)
            except KeyError:
                continue
            try:
                regions = await provider.list_regions()
            except ProviderAuthError as exc:
                errors.append(f"regions[{account_id}]: {type(exc).__name__}")
                account_failed = True
                continue
            except Exception as exc:
                errors.append(f"regions[{account_id}]: {type(exc).__name__}")
                account_failed = True
                continue
            if not regions:
                warnings.append(f"regions[{account_id}]: no readable regions")
                continue
            for region in regions:
                if region.id not in seen_locations:
                    seen_locations.add(region.id)
                    try:
                        await locations_repo.upsert(
                            LocationRecord(
                                provider_key=PROVIDER_KEY,
                                location_id=region.id,
                                name=region.name,
                                country_code=region.country_code,
                                city=region.city,
                            )
                        )
                    except Exception as exc:
                        warnings.append(f"location metadata {region.id}: {type(exc).__name__}")
                try:
                    types = await provider.list_instance_types(region.id)
                except ProviderError as exc:
                    per_region[region.id] = RegionTypeReport(
                        region_id=region.id, products=0, error=type(exc).__name__
                    )
                    warnings.append(f"region {region.id}[{account_id}]: {type(exc).__name__}")
                    account_failed = True
                    continue
                if not types:
                    continue
                # Image capability of THIS account for THIS region (read-only).
                # Checkout mandates image selection through the pinned
                # account, so ownership below prefers image-capable accounts.
                # A failed read is "unknown" (never penalized); only a
                # conclusive empty read routes away.
                #
                # The STRICT region-scoped probe is used on purpose: the
                # customer-facing read falls back to the provider's global
                # image catalog, which would make every credential look
                # capable for every region and silently move a region's owner
                # to the priority-first account (the offer identity is
                # (provider, product, location) and cannot express two
                # owners).
                try:
                    images = await provider.probe_region_images(region.id)
                    images_state = IMAGE_STATE_OK if images else IMAGE_STATE_EMPTY
                except (ProviderUnavailable, ProviderRateLimited) as exc:
                    # INCONCLUSIVE: a timeout, a 5xx or a throttle says nothing
                    # about whether this credential serves the pair, so it must
                    # never cost the current owner its offer.
                    logger.warning(
                        "leaseweb cloud images of account %s region %s inconclusive: %s",
                        account_id,
                        region.id,
                        type(exc).__name__,
                    )
                    images_state = IMAGE_STATE_UNKNOWN
                except ProviderError as exc:
                    # DEFINITIVE provider answer (validation/forbidden/not
                    # found/conflict): this credential does not serve images for
                    # the region. Never "unknown" — an account that cannot prove
                    # a pair must not be handed it (production moved a region
                    # to a credential that could not serve it exactly this way).
                    logger.warning(
                        "leaseweb cloud images of account %s region %s rejected: %s",
                        account_id,
                        region.id,
                        type(exc).__name__,
                    )
                    images_state = IMAGE_STATE_REJECTED
                except Exception as exc:
                    # NOT a provider answer: an adapter/internal error (e.g. an
                    # adapter that does not implement the strict probe). That
                    # proves nothing about the credential, so it is treated as
                    # inconclusive rather than as "this account cannot serve".
                    logger.warning(
                        "leaseweb cloud images of account %s region %s inconclusive: %s",
                        account_id,
                        region.id,
                        type(exc).__name__,
                    )
                    images_state = IMAGE_STATE_UNKNOWN
                for item in types:
                    candidates.setdefault((item.id, region.id), []).append(
                        (account_id, images_state, item)
                    )

        # -- Phase 2: image-aware ownership (deterministic) ----------------
        for pair in sorted(candidates):
            type_id, region_id = pair
            ranked = candidates[pair]
            choice = select_hourly_owner(
                ranked,
                current_owner_id=owners.get(pair),
                limit_reached=limit_reached,
            )
            account_id, images_state, item = (
                choice.account_id,
                choice.images_state,
                choice.item,
            )
            # A pair is sellable only when its owner PROVED it read-only and
            # the account can still take new orders. Capacity exhaustion is a
            # distinct, time-bounded case: the offer stays stored (and its
            # existing resources untouched) but is never advertised for NEW
            # orders through an account that just refused one.
            image_ready = images_state not in _UNPUBLISHABLE_IMAGE_STATES
            publishable = choice.published
            if choice.reason == "no-usable-images":
                warnings.append(
                    f"{region_id}: no account lists usable images for "
                    f"{type_id}; offer kept unavailable (nothing retired)"
                )
            elif choice.reason is not None:
                warnings.append(
                    f"{region_id}: {type_id} not published through account "
                    f"{account_id} ({choice.reason})"
                )
            update = offer_spec_from_item(
                item,
                region_id,
                publishable=publishable,
                account_id=account_id,
            )
            counted = 0
            region_failed = False
            try:
                await offers_repo.upsert_from_provider(
                    provider_key=PROVIDER_KEY,
                    product_id=item.id,
                    location_id=region_id,
                    update=update,
                    provider_account_id=account_id,
                )
            except Exception as exc:
                persistence_failures.append(f"upsert {item.id}/{region_id}: {exc}")
                warnings.append(
                    f"{region_id}: keeping last-known offers "
                    f"({type(exc).__name__}); nothing retired"
                )
                region_failed = True
            else:
                # Account-qualified: scoped rows retire unless their exact
                # (account, product, location) triple was observed. Pair-only
                # sets would retire every scoped row the sync just wrote.
                available.add((account_id, item.id, region_id))
                if image_ready and publishable:
                    verified.add(pair)
                    verified_owner.add((account_id, item.id, region_id))
                counted += 1
                written += 1
            previous = per_region.get(region_id)
            base = previous.products if previous is not None else 0
            if previous is not None and previous.error is not None and not region_failed:
                error: str | None = previous.error
            else:
                error = "persistence failure" if region_failed else None
            per_region[region_id] = RegionTypeReport(
                region_id=region_id,
                products=base + counted,
                error=error,
            )
            if region_failed:
                account_failed = True

        reports = tuple(per_region[region_id] for region_id in sorted(per_region))
        if not reports and not errors:
            # No readable regions from any account (empty catalog): keep the
            # legacy error signal so empty stays distinguishable from a
            # partial failure (which is warnings-only).
            errors.append("provider returned no readable regions")
        marked = 0
        if not account_failed and not persistence_failures and reports:
            try:
                marked = await offers_repo.mark_unavailable(
                    PROVIDER_KEY, available, billing_model="hourly"
                )
            except Exception as exc:
                persistence_failures.append(f"mark_unavailable: {exc}")
        else:
            warnings.append("skipped mark_unavailable: current availability unreadable")
        return CloudSyncResult(
            regions=reports,
            offers_written=written,
            marked_unavailable=marked,
            warnings=tuple(warnings),
            verified=frozenset(verified),
            verified_accounts=frozenset(verified_owner),
            persistence_failures=tuple(persistence_failures),
            errors=errors,
        )
