"""Hourly cloud sync as a provider-neutral coordinator source.

Adapts :class:`LeasewebHourlyCloudSyncer` to the
:class:`OfferCatalogSyncSource` port: regions, hourly instance types and
their prices flow into the price book with hourly billing, and a
provider-level failure is reported (``ok=False``) instead of raised.
"""

from __future__ import annotations

import logging

from cloud_platform.modules.offers.domain import CatalogSyncReport
from cloud_platform.providers.leaseweb.cloud import PROVIDER_KEY
from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

logger = logging.getLogger(__name__)


class LeasewebHourlyCloudSyncSource:
    """Coordinator source backed by the hourly cloud syncer."""

    def __init__(self, syncer: LeasewebHourlyCloudSyncer) -> None:
        self._syncer = syncer

    @property
    def provider_key(self) -> str:
        return PROVIDER_KEY

    async def sync_catalog(self) -> CatalogSyncReport:
        try:
            result = await self._syncer.sync_all()
        except Exception as exc:
            logger.warning("leaseweb hourly cloud sync failed: %s", exc)
            return CatalogSyncReport(
                provider_key=PROVIDER_KEY,
                ok=False,
                complete=False,
                billing_model="hourly",
                errors=(f"{type(exc).__name__}: {exc}",),
            )
        ok = not result.persistence_failures and (result.offers_written > 0 or not result.errors)
        complete = bool(result.regions) and not any(report.error for report in result.regions)
        errors = list(result.errors)
        if not result.regions:
            errors.append("provider returned no readable regions")
        return CatalogSyncReport(
            provider_key=PROVIDER_KEY,
            ok=ok,
            complete=complete,
            billing_model="hourly",
            discovered=result.offers_written,
            persisted=result.offers_written,
            retired=result.marked_unavailable,
            persistence_failures=result.persistence_failures,
            warnings=result.warnings,
            errors=tuple(errors),
            verified=result.verified,
            verified_accounts=result.verified_accounts,
            account_aware=True,
        )
