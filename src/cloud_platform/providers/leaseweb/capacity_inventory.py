"""Read-only Leaseweb instance census for capacity recovery.

The recovery controller needs exactly ONE provider fact: how many instances a
credential account currently holds, and which ones. That is the baseline the
refusal was learned against, and a later LOWER count is local evidence that
capacity may have been freed.

This adapter answers that question with ONE strictly read-only call —
``GET /publicCloud/v1/instances`` WITHOUT a region filter (see
:class:`CloudInstancesRead`). It NEVER calls a create, a delete, a power action
or any other mutating endpoint.

Two provider facts shape the read:

* Leaseweb Public Cloud credentials are REGION-SCOPED. The ``region`` query
  parameter is validated against the regions the credential is entitled to, so
  a region-filtered read raises the provider's validation error for every other
  region the global catalog lists: a per-region walk can never be exhaustive,
  while the account-scoped unfiltered read is the entire census.
* ``list_regions`` / ``list_instanceTypes`` / ``list_images`` are NEVER treated
  as capacity evidence: they prove authentication and catalog access, not
  create quota.

Fail-closed rules:

* a census that could not be read at all yields ``None``;
* a census that could not be EXHAUSTED yields ``None`` as well: a partial count
  is a LOWER bound, and publishing it could look like "instances were freed"
  when a page was merely missing. An inconclusive census must never open a
  recovery window;
* a raw entry the parser cannot read is schema drift, not an absent instance:
  the raw count and the parsed count must agree, because the ids hash is what
  makes two censuses comparable;
* a disabled credential account is skipped entirely (it receives no orders, so
  it needs no recovery).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from cloud_platform.modules.provider_capacity.recovery import (
    AccountInventory,
    inventory_ids_hash,
)
from cloud_platform.providers.leaseweb.cloud_accounts import LeasewebCloudAccountRouter

logger = logging.getLogger(__name__)

__all__ = ["LeasewebCloudInventorySource"]


class LeasewebCloudInventorySource:
    """Implements ``CloudInstanceInventorySource`` for hourly Leaseweb Cloud."""

    def __init__(self, *, router: LeasewebCloudAccountRouter) -> None:
        self._router = router

    async def inventories(self) -> Mapping[str, AccountInventory | None]:
        """One census per ENABLED configured account (never raises)."""
        result: dict[str, AccountInventory | None] = {}
        for definition in self._router.accounts:
            if not definition.enabled:
                continue
            result[definition.account_id] = await self._census(definition.account_id)
        return result

    async def _census(self, account_id: str) -> AccountInventory | None:
        try:
            provider = self._router.client_for(account_id)
            read = await provider.read_all_instances()
        except Exception as exc:
            logger.warning(
                "capacity inventory: instances unreadable for account %s (%s)",
                account_id,
                type(exc).__name__,
            )
            return None
        if not read.complete:
            # Deliberately NOT "the instances I could see": a truncated list is
            # a lower bound, and a lower bound is indistinguishable from
            # "instances were freed". Unknown is the only honest answer.
            logger.warning(
                "capacity inventory: census incomplete for account %s (%d of %s read)",
                account_id,
                read.raw_items,
                "unknown" if read.total_count is None else read.total_count,
            )
            return None
        if read.raw_items != len(read.instances):
            logger.warning(
                "capacity inventory: %d unreadable entry/entries for account %s",
                read.raw_items - len(read.instances),
                account_id,
            )
            return None
        regions = {str(instance.region).strip() for instance in read.instances if instance.region}
        return AccountInventory(
            instance_count=len(read.instances),
            ids_hash=inventory_ids_hash(instance.id for instance in read.instances),
            regions_read=len(regions),
        )
