from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from cloud_platform.core.money import Money


class LedgerEntryType(StrEnum):
    DEPOSIT = "deposit"
    HOLD = "hold"
    RELEASE = "release"
    CHARGE = "charge"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    id: UUID
    wallet_id: UUID
    entry_type: LedgerEntryType
    amount: Money
    reference_type: str
    reference_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.amount.amount == 0:
            raise ValueError("ledger entry amount cannot be zero")
