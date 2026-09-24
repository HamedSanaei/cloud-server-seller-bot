"""Leaseweb sellable-catalog sync as a provider-neutral coordinator source.

Adapts :class:`LeaseWebOrderingCatalogSyncer` to the
:class:`OfferCatalogSyncSource` port the automatic coordinator drives: sync
outcome becomes a normalized :class:`CatalogSyncReport`, and a provider-level
failure is reported (``ok=False``) instead of raised, so one provider can
never break another provider's run.
"""

from __future__ import annotations

import logging

from cloud_platform.modules.offers.domain import CatalogSyncReport
from cloud_platform.providers.leaseweb.ordering_sync import LeaseWebOrderingCatalogSyncer

logger = logging.getLogger(__name__)

#: Provider key this source syncs (matches the syncer's own key).
PROVIDER_KEY = "leaseweb"


class LeasewebCatalogSyncSource:
    """Coordinator source backed by the multi-account ordering syncer."""

    def __init__(self, syncer: LeaseWebOrderingCatalogSyncer) -> None:
        self._syncer = syncer

    @property
    def provider_key(self) -> str:
        return PROVIDER_KEY

    async def sync_catalog(self) -> CatalogSyncReport:
        try:
            result = await self._syncer.sync_all()
        except Exception as exc:
            logger.warning("leaseweb catalog sync failed: %s", exc)
            return CatalogSyncReport(
                provider_key=PROVIDER_KEY,
                ok=False,
                complete=False,
                errors=(f"{type(exc).__name__}: {exc}",),
            )
        products = result.get("products")
        if products is None:  # pragma: no cover - defensive shape guard
            return CatalogSyncReport(
                provider_key=PROVIDER_KEY,
                ok=False,
                complete=False,
                errors=("leaseweb sync returned no products report",),
            )
        # A run that persisted everything it wrote is usable; one that wrote
        # nothing AND reported errors (e.g. every credential rejected) is
        # not — pricing and publishing both key off the verified set anyway,
        # which is empty in that case.
        ok = products.persistence_ok and (products.total_fetched > 0 or not products.errors)
        return CatalogSyncReport(
            provider_key=PROVIDER_KEY,
            ok=ok,
            complete=products.availability_reconciled,
            billing_model="prepaid_monthly_fixed",
            discovered=products.total_fetched,
            persisted=products.offers_persisted,
            retired=products.marked_unavailable,
            persistence_failures=tuple(products.persistence_failures),
            warnings=tuple(products.warnings),
            errors=tuple(products.errors),
            verified=products.verified,
            verified_accounts=products.verified_accounts,
            account_aware=True,
        )
