"""Source-aware hourly contracts shared by billing and provider lifecycles."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from cloud_platform.modules.funding.domain import FundingAuthorizationError, FundingSource


def operator_funded(server: Any) -> bool:
    """Read persisted source, validating its self-owned sponsorship invariant."""
    source = FundingSource(getattr(server, "funding_source", FundingSource.CUSTOMER_WALLET))
    if source is FundingSource.CUSTOMER_WALLET:
        return False
    if getattr(server, "funding_operator_id", None) != server.user_id:
        raise FundingAuthorizationError("operator hourly source is not self-owned")
    # Monthly contracts have a separate accepted-order/renewal receipt lifecycle.
    return not bool(getattr(server, "is_prepaid_monthly", False))


@dataclass(frozen=True, slots=True)
class OperatorHourlyCutoverCoverage:
    """Read-only corporate receipt for an already-paid personal hour at cutover."""

    server_id: UUID
    operator_id: UUID
    period_start: datetime
    period_end: datetime
    grant_operation_key: str
    expense_operation_key: str
    grant_id: UUID
    expense_id: UUID

    def __post_init__(self) -> None:
        if self.period_start.utcoffset() is None or self.period_end.utcoffset() is None:
            raise ValueError("cutover coverage boundaries must be timezone-aware")
        object.__setattr__(self, "period_start", self.period_start.astimezone(UTC))
        object.__setattr__(self, "period_end", self.period_end.astimezone(UTC))
        if self.period_end - self.period_start != timedelta(hours=1):
            raise ValueError("cutover coverage must preserve exactly one accepted hour")
