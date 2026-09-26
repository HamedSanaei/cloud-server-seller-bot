"""Republish NEW-order Cloud offers the moment an account hits its limit.

The periodic catalog sync only learns about a refusal on its NEXT walk, so
production showed this window::

    06:19:58  catalog_auto_sync finished (publishing through sales-org-north)
    06:20:02  PC-2031 learned for sales-org-north
    ...       hourly offers keep advertising north until the 15-minute cycle

That window is exactly where 8fc2e573 was accepted: the offer was still
published under an account that had just refused an instance. This module
closes it by refreshing future-order publication for the affected account
immediately, using a bounded, read-only, pair-level proof instead of a full
3-5 minute catalog walk:

* pairs the limited account currently publishes are collected from the catalog
  (no provider call);
* for each pair, every OTHER enabled account is asked, read-only, whether it
  PROVES the exact pair (its region list contains the region, its instance-type
  list contains the type, and its region-scoped image read returns usable
  images). The SAME ranking helper the periodic sync uses decides, so an
  alternate account can only win the pair by proving it, exactly as before a
  checkout;
* a pair that another account proves is re-pinned to that account through the
  normal offer-observation write (pricing provenance and cost-change detection
  included);
* every remaining pair of the limited account is unpublished — the offer stays
  known (and any existing server keeps its own pinned account), it simply stops
  taking NEW orders until capacity recovery is proven.

Hard safety rules, mirroring the capacity model:

* never touch an accepted order, server, fingerprint or its pinned account;
* never re-send the refused provider POST and never call a mutating endpoint;
* never move a pair to an account that has not proven it read-only;
* a provider read failure leaves the pair UNPUBLISHED (fail closed), never
  published through an unproven or limited credential.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
from cloud_platform.modules.provider_capacity.domain import AccountCapacityRepository
from cloud_platform.providers.errors import ProviderRateLimited, ProviderUnavailable
from cloud_platform.providers.leaseweb.accounts import PROVIDER_KEY
from cloud_platform.providers.leaseweb.cloud_accounts import LeasewebCloudAccountRouter
from cloud_platform.providers.leaseweb.cloud_sync import (
    IMAGE_STATE_EMPTY,
    IMAGE_STATE_OK,
    IMAGE_STATE_REJECTED,
    IMAGE_STATE_UNKNOWN,
    offer_spec_from_item,
    select_hourly_owner,
)

logger = logging.getLogger(__name__)

__all__ = ["CapacityRepublishReport", "LeasewebCapacityRepublisher"]


@dataclass(frozen=True, slots=True)
class CapacityRepublishReport:
    """What one post-refusal publication refresh did."""

    provider_key: str
    credential_account_id: str
    pairs_considered: int = 0
    rerouted: tuple[tuple[str, str, str], ...] = ()
    """``(product_id, location_id, new_account)`` pairs moved to an alternate."""
    unpublished: int = 0
    """Offers of the limited account left without NEW-order publication."""
    unproven: tuple[tuple[str, str, str], ...] = ()
    """``(product_id, location_id, reason)`` pairs no alternate account proved."""
    errors: tuple[str, ...] = field(default=())

    def summary(self) -> str:
        return (
            f"account={self.credential_account_id} pairs={self.pairs_considered} "
            f"rerouted={len(self.rerouted)} unpublished={self.unpublished} "
            f"unproven={len(self.unproven)}"
        )

    def log_fields(self) -> dict[str, object]:
        return {
            "account": self.credential_account_id,
            "pairs": self.pairs_considered,
            "rerouted": [
                f"{product}/{location}->{account}" for product, location, account in self.rerouted
            ],
            "unpublished": self.unpublished,
            "unproven": self.unproven,
        }


class LeasewebCapacityRepublisher:
    """Implements ``CapacityChangeRepublisher`` for hourly Leaseweb Cloud."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        router: LeasewebCloudAccountRouter,
        capacity_repo: AccountCapacityRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._router = router
        self._capacity = capacity_repo

    async def after_capacity_refusal(
        self, *, provider_key: str, credential_account_id: str
    ) -> CapacityRepublishReport:
        """Refresh future-order publication for one newly limited account.

        Best effort by design: the caller (the hourly create worker) must fail
        the provider operation regardless of what happens here, and the periodic
        catalog sync is still the backstop. Every failure is recorded in the
        report and logged, never raised.
        """
        account = str(credential_account_id or "").strip()
        report = CapacityRepublishReport(provider_key=provider_key, credential_account_id=account)
        if provider_key != PROVIDER_KEY or not account:
            return report
        offers_repo = SqlAlchemySellableOfferRepository(self._session_factory)
        try:
            owners = await offers_repo.hourly_account_owners(PROVIDER_KEY)
        except Exception as exc:
            logger.warning(
                "capacity republication could not read current owners (%s)", type(exc).__name__
            )
            return report
        pairs = sorted(pair for pair, owner in owners.items() if owner == account)
        if not pairs:
            return report
        limit_reached = await self._limited_accounts(account)
        candidates = [
            definition.account_id
            for definition in self._router.accounts
            if definition.enabled
            and definition.account_id != account
            and definition.account_id not in limit_reached
        ]
        errors: list[str] = []
        rerouted: list[tuple[str, str, str]] = []
        unproven: list[tuple[str, str, str]] = []
        regions_by_account: dict[str, frozenset[str]] = {}
        for product_id, location_id in pairs:
            ranked: list[tuple[str, str, Any]] = []
            for candidate in candidates:
                probed = await self._prove_pair(
                    candidate,
                    product_id=product_id,
                    location_id=location_id,
                    regions_by_account=regions_by_account,
                    errors=errors,
                )
                if probed is not None:
                    ranked.append(probed)
            if not ranked:
                unproven.append((product_id, location_id, "no-other-account"))
                continue
            choice = select_hourly_owner(
                ranked,
                current_owner_id=account,
                limit_reached=limit_reached | {account},
            )
            if choice.account_id == account or not choice.published:
                reason = choice.reason or "unproven"
                unproven.append((product_id, location_id, reason))
                continue
            try:
                await offers_repo.upsert_from_provider(
                    provider_key=PROVIDER_KEY,
                    product_id=product_id,
                    location_id=location_id,
                    update=offer_spec_from_item(
                        choice.item,
                        location_id,
                        publishable=True,
                        account_id=choice.account_id,
                    ),
                    provider_account_id=choice.account_id,
                )
            except Exception as exc:
                errors.append(f"reassign {product_id}/{location_id}: {type(exc).__name__}")
                unproven.append((product_id, location_id, "persistence-failed"))
                continue
            rerouted.append((product_id, location_id, choice.account_id))
            logger.warning(
                "leaseweb capacity: republished %s/%s through proven account %s "
                "(previous owner %s is at its provider limit)",
                product_id,
                location_id,
                choice.account_id,
                account,
            )
        unpublished = 0
        try:
            unpublished = await offers_repo.mark_unavailable(
                PROVIDER_KEY,
                (),
                billing_model="hourly",
                provider_account_id=account,
            )
        except Exception as exc:
            errors.append(f"mark_unavailable: {type(exc).__name__}")
        report = CapacityRepublishReport(
            provider_key=provider_key,
            credential_account_id=account,
            pairs_considered=len(pairs),
            rerouted=tuple(rerouted),
            unpublished=unpublished,
            unproven=tuple(unproven),
            errors=tuple(errors),
        )
        logger.warning("leaseweb capacity publication refreshed: %s", report.summary())
        return report

    async def _limited_accounts(self, refused: str) -> frozenset[str]:
        """Every account known to be out of capacity for NEW orders."""
        if self._capacity is None:
            return frozenset({refused})
        try:
            return frozenset(await self._capacity.limit_reached_accounts(PROVIDER_KEY))
        except Exception as exc:
            logger.warning(
                "leaseweb capacity: capacity store unreadable during republication (%s); "
                "only the refused account is excluded",
                type(exc).__name__,
            )
            return frozenset({refused})

    async def _prove_pair(
        self,
        account_id: str,
        *,
        product_id: str,
        location_id: str,
        regions_by_account: dict[str, frozenset[str]],
        errors: list[str],
    ) -> tuple[str, str, Any] | None:
        """Read-only proof that ONE account can serve ONE pair, or None.

        Mirrors the periodic sync exactly: the region must be listed by the
        credential, the instance type must be listed for that region, and the
        region-scoped image read must return usable images. An inconclusive
        read is never proof.
        """
        try:
            provider = self._router.client_for(account_id)
        except Exception:
            return None
        regions = regions_by_account.get(account_id)
        if regions is None:
            try:
                regions = frozenset(region.id for region in await provider.list_regions())
            except (ProviderUnavailable, ProviderRateLimited) as exc:
                errors.append(f"regions[{account_id}]: {type(exc).__name__}")
                return None
            except Exception as exc:
                errors.append(f"regions[{account_id}]: {type(exc).__name__}")
                return None
            regions_by_account[account_id] = regions
        if location_id not in regions:
            return None
        try:
            types = await provider.list_instance_types(location_id)
        except (ProviderUnavailable, ProviderRateLimited) as exc:
            errors.append(f"types[{account_id}/{location_id}]: {type(exc).__name__}")
            return None
        except Exception as exc:
            errors.append(f"types[{account_id}/{location_id}]: {type(exc).__name__}")
            return None
        item = next((entry for entry in types if entry.id == product_id), None)
        if item is None:
            return None
        try:
            images = await provider.probe_region_images(location_id)
            images_state = IMAGE_STATE_OK if images else IMAGE_STATE_EMPTY
        except (ProviderUnavailable, ProviderRateLimited):
            # Inconclusive: proves nothing, so it can never win the pair.
            images_state = IMAGE_STATE_UNKNOWN
        except Exception:
            images_state = IMAGE_STATE_REJECTED
        return (account_id, images_state, item)
