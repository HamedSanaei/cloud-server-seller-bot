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
from cloud_platform.providers.errors import ProviderAuthError, ProviderError
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
    persistence_failures: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)


def _technical_spec(item: CloudInstanceType) -> dict[str, object]:
    return TechnicalSpec(
        architecture=item.architecture,
        cpu_type=item.cpu_type,
        storage_type=item.storage_type,
        ipv4=item.ipv4,
        ipv6=item.ipv6,
    ).to_metadata() | {
        "plan_family": item.family_key,
        "plan_family_name": item.family_name,
    }


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

    async def sync_all(self) -> CloudSyncResult:
        """Regions per account, then per-region instance types, then reconcile."""
        from cloud_platform.modules.catalog.repository import SqlAlchemyLocationRepository

        offers_repo = SqlAlchemySellableOfferRepository(self._session_factory)
        locations_repo = SqlAlchemyLocationRepository(self._session_factory)
        warnings: list[str] = []
        errors: list[str] = []
        persistence_failures: list[str] = []
        per_region: dict[str, RegionTypeReport] = {}
        verified: set[tuple[str, str]] = set()
        available: set[tuple[str, str]] = set()
        seen_locations: set[str] = set()
        account_failed = False
        written = 0

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
                counted = 0
                region_failed = False
                for item in types:
                    pair = (item.id, region.id)
                    if pair in available:
                        # Already supplied by a higher-priority account.
                        continue
                    update = OfferSpecUpdate(
                        name=item.name,
                        vcpu=item.vcpu,
                        ram_gb=item.ram_gb,
                        disk_gb=item.disk_gb,
                        traffic=item.traffic,
                        provider_cost_minor=item.hourly_cost_minor,
                        provider_cost_currency=item.currency,
                        billing_parameters={
                            "contract_type": "HOURLY",
                            "monthly_estimate_source": "hourly_rate",
                            "instance_type_id": item.id,
                            "region": region.id,
                        },
                        technical_metadata=_technical_spec(item),
                        billing_model="hourly",
                        provider_available=True,
                        provider_account_id=account_id,
                    )
                    try:
                        await offers_repo.upsert_from_provider(
                            provider_key=PROVIDER_KEY,
                            product_id=item.id,
                            location_id=region.id,
                            update=update,
                            provider_account_id=account_id,
                        )
                    except Exception as exc:
                        persistence_failures.append(f"upsert {item.id}/{region.id}: {exc}")
                        warnings.append(
                            f"{region.id}: keeping last-known offers "
                            f"({type(exc).__name__}); nothing retired"
                        )
                        region_failed = True
                        continue
                    available.add(pair)
                    verified.add(pair)
                    counted += 1
                    written += 1
                previous = per_region.get(region.id)
                base = previous.products if previous is not None else 0
                if previous is not None and previous.error is not None and not region_failed:
                    error: str | None = previous.error
                else:
                    error = "persistence failure" if region_failed else None
                per_region[region.id] = RegionTypeReport(
                    region_id=region.id,
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
            persistence_failures=tuple(persistence_failures),
            errors=errors,
        )
