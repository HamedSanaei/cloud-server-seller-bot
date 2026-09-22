"""Hetzner sellable-catalog sync as a provider-neutral coordinator source.

Adapts :class:`HetznerCatalogSyncer` to the
:class:`OfferCatalogSyncSource` port the automatic coordinator drives: the
native ``/locations`` sync (country/city/network-zone metadata) runs first,
then the location-scoped offer sync. A provider-level failure is reported
(``ok=False``) instead of raised, so one provider can never break another
provider's run.
"""

from __future__ import annotations

import logging

from cloud_platform.modules.offers.domain import CatalogSyncReport
from cloud_platform.providers.hetzner.sync import PROVIDER_KEY, HetznerCatalogSyncer

logger = logging.getLogger(__name__)


class HetznerCatalogSyncSource:
    """Coordinator source backed by the Hetzner catalog syncer."""

    def __init__(self, syncer: HetznerCatalogSyncer) -> None:
        self._syncer = syncer

    @property
    def provider_key(self) -> str:
        return PROVIDER_KEY

    async def sync_catalog(self) -> CatalogSyncReport:
        warnings: list[str] = []
        try:
            locations_result = await self._syncer.sync_locations()
        except Exception as exc:
            logger.warning("hetzner location sync failed: %s", exc)
            return CatalogSyncReport(
                provider_key=PROVIDER_KEY,
                ok=False,
                complete=False,
                errors=(f"locations: {type(exc).__name__}: {exc}",),
            )
        warnings.extend(f"locations: {error}" for error in locations_result.errors)
        try:
            offers_result = await self._syncer.sync_offers()
        except Exception as exc:
            logger.warning("hetzner offer sync failed: %s", exc)
            return CatalogSyncReport(
                provider_key=PROVIDER_KEY,
                ok=False,
                complete=False,
                warnings=tuple(warnings),
                errors=(f"offers: {type(exc).__name__}: {exc}",),
            )
        warnings.extend(offers_result.warnings)
        # Usable when every durable write succeeded and the provider gave a
        # readable view (locations with zero products and no errors are a
        # valid empty view, not a failure). Availability is fully reconciled
        # exactly when the syncer retired unseen inventory itself.
        location_failed = any(report.error for report in offers_result.locations)
        no_view = not offers_result.locations
        ok = not offers_result.persistence_failures and (
            offers_result.offers_written > 0 or (not location_failed and not no_view)
        )
        complete = bool(offers_result.locations) and not location_failed
        errors = [
            f"{report.location_id}: {report.error}"
            for report in offers_result.locations
            if report.error
        ]
        if no_view:
            errors.append("provider returned no readable locations")
        return CatalogSyncReport(
            provider_key=PROVIDER_KEY,
            ok=ok,
            complete=complete,
            discovered=offers_result.offers_written,
            persisted=offers_result.offers_written,
            retired=offers_result.marked_unavailable,
            persistence_failures=offers_result.persistence_failures,
            warnings=tuple(warnings),
            errors=tuple(errors),
            verified=offers_result.verified,
        )
