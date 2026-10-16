"""Application boundary for funding decisions and accepted cost evidence.

Source resolution is a preview for a new resource. A stored server's source
must be retained for later periods; only an explicit audited cutover may change
it. Every new operator period must call reserve, whose transaction performs the
authoritative fresh policy check. No wallet success is inferred by this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from cloud_platform.modules.funding.domain import (
    FundingAuthorizationError,
    FundingPurpose,
    FundingSource,
    OperatorExpense,
    OperatorFundingCutover,
    OperatorFundingGrant,
    OperatorFundingRepository,
    ProviderCostSnapshot,
    _require_uuid,
)


class FundingService:
    """Typed funding API shared by monthly and hourly lifecycle services."""

    def __init__(self, repository: OperatorFundingRepository) -> None:
        self._repository = repository

    async def eligible(self, owner_id: UUID) -> bool:
        _require_uuid(owner_id, "owner_id")
        return await self._repository.eligible(owner_id)

    async def resolve_source(self, owner_id: UUID) -> FundingSource:
        """Preview a new resource's source using fresh configured active policy."""
        return (
            FundingSource.OPERATOR
            if await self.eligible(owner_id)
            else FundingSource.CUSTOMER_WALLET
        )

    async def authorize_new_period(self, *, owner_id: UUID, operator_id: UUID) -> None:
        """Preflight a stored operator source; reserve still authorizes spending."""
        _require_uuid(owner_id, "owner_id")
        _require_uuid(operator_id, "operator_id")
        if owner_id != operator_id or not await self.eligible(owner_id):
            raise FundingAuthorizationError("configured active self-owned operator required")

    async def reserve(
        self,
        *,
        operator_id: UUID,
        owner_id: UUID,
        operation_key: str,
        resource_key: str,
        purpose: FundingPurpose,
        nominal_amount_minor: int,
        nominal_currency: str,
        provider_cost_snapshot: ProviderCostSnapshot | Mapping[str, Any],
        reason: str | None = None,
    ) -> OperatorFundingGrant:
        """Authorize a new create/period; new lifecycle callers must supply a reason.

        Omitted reasons use the explicit snapshot provenance only for legacy
        API compatibility. A replay retains its original immutable audit event.
        """
        return await self._repository.reserve(
            operator_id=operator_id,
            owner_id=owner_id,
            operation_key=operation_key,
            resource_key=resource_key,
            purpose=purpose,
            nominal_amount_minor=nominal_amount_minor,
            nominal_currency=nominal_currency,
            provider_cost_snapshot=provider_cost_snapshot,
            reason=reason,
        )

    async def get_grant(self, operation_key: str) -> OperatorFundingGrant | None:
        return await self._repository.get_grant(operation_key)

    async def get_cutover(self, server_id: UUID) -> OperatorFundingCutover | None:
        _require_uuid(server_id, "server_id")
        return await self._repository.get_cutover(server_id)

    async def get_expense(self, operation_key: str) -> OperatorExpense | None:
        """Look up by the public expense_operation_key(grant_key) identity."""
        return await self._repository.get_expense(operation_key)

    async def settle(self, grant: OperatorFundingGrant) -> OperatorExpense:
        """Record accepted historical work even after operator revocation."""
        return await self._repository.settle(grant)

    async def cancel(self, grant: OperatorFundingGrant, *, reason: str) -> OperatorFundingGrant:
        return await self._repository.cancel(grant, reason=reason)

    async def list_expenses_between(
        self, start: datetime, end: datetime, *, limit: int = 500, offset: int = 0
    ) -> list[OperatorExpense]:
        return await self._repository.list_expenses_between(start, end, limit=limit, offset=offset)
