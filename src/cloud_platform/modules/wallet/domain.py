from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
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
    description: str = ""

    def __post_init__(self) -> None:
        if self.amount.amount == 0:
            raise ValueError("ledger entry amount cannot be zero")


# ---------------------------------------------------------------------------
# Wallet aggregate & repository port
# ---------------------------------------------------------------------------


class WalletError(Exception):
    """Base error for wallet operations."""


class InsufficientBalanceError(WalletError):
    """Raised when a debit would overdraw."""


class WalletStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    CLOSED = "closed"


@dataclass(eq=False, slots=True)
class Wallet:
    """Wallet aggregate owning balance, currency and state.

    Balance is stored in minor currency units (integers).
    """

    user_id: UUID
    id: UUID | None = None
    balance: int = 0  # minor units
    currency: str = "EUR"
    status: WalletStatus = WalletStatus.ACTIVE
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.currency or len(self.currency) != 3:
            raise ValueError("currency must be a 3-letter ISO code")
        object.__setattr__(self, "currency", self.currency.upper())

    def to_money(self) -> Money:
        """Return this wallet's balance as a Money value."""
        return Money(Decimal(self.balance), self.currency)

    def add_funds(self, amount: int) -> int:
        """Credit the wallet. Returns new balance (minor units)."""
        if amount <= 0:
            raise ValueError("add_funds requires a positive amount")
        self.balance += amount
        self.updated_at = datetime.now(UTC)
        return self.balance

    def debit(self, amount: int) -> int:
        """Debit the wallet. Raises InsufficientBalanceError if overdrawn."""
        if amount <= 0:
            raise ValueError("debit requires a positive amount")
        if self.balance < amount:
            raise InsufficientBalanceError(f"balance {self.balance} < required {amount}")
        self.balance -= amount
        self.updated_at = datetime.now(UTC)
        return self.balance

    def freeze(self) -> None:
        """Freeze the wallet so no debits are possible."""
        if self.status is WalletStatus.CLOSED:
            raise WalletError("cannot freeze a closed wallet")
        self.status = WalletStatus.FROZEN
        self.updated_at = datetime.now(UTC)

    def unfreeze(self) -> None:
        """Unfreeze a frozen wallet."""
        if self.status is not WalletStatus.FROZEN:
            raise WalletError("wallet is not frozen")
        self.status = WalletStatus.ACTIVE
        self.updated_at = datetime.now(UTC)

    def close(self) -> None:
        """Close the wallet permanently (balance must be zero)."""
        if self.balance != 0:
            raise WalletError("cannot close wallet with non-zero balance")
        self.status = WalletStatus.CLOSED
        self.updated_at = datetime.now(UTC)


class WalletRepository(Protocol):
    """Port for wallet persistence."""

    async def get(self, user_id: UUID) -> Wallet | None:
        """Return the wallet for a user, or None."""
        ...

    async def list_all(self) -> list[Wallet]:
        """Every wallet (reconciliation/reporting)."""
        ...

    async def get_or_create(self, user_id: UUID, currency: str = "EUR") -> Wallet:
        """Return the wallet; create it if absent."""
        ...

    async def debit(self, user_id: UUID, amount: int, idempotency_key: str) -> Wallet:
        """Debit the wallet idempotently. Returns updated aggregate."""
        ...

    async def add_funds(self, user_id: UUID, amount: int, idempotency_key: str) -> Wallet:
        """Credit the wallet idempotently. Returns updated aggregate."""
        ...


# ---------------------------------------------------------------------------
# Hold aggregate & repository port
# ---------------------------------------------------------------------------


class HoldError(WalletError):
    """Base error for hold operations."""


class InsufficientHoldBalanceError(HoldError):
    """Raised when the wallet balance is insufficient for the hold."""


class HoldNotFoundError(HoldError):
    """Raised when a hold cannot be found by id or idempotency key."""


class HoldStateConflictError(HoldError):
    """Raised when a hold transition is invalid (e.g. releasing a captured hold)."""


class HoldStatus(StrEnum):
    CREATED = "created"
    CAPTURED = "captured"
    RELEASED = "released"


@dataclass(eq=False, slots=True)
class Hold:
    """A reservation of wallet funds that prevents concurrent overspend.

    A hold can transition to CAPTURED (becomes permanent) or
    RELEASED (funds return to available balance).
    """

    wallet_id: UUID
    amount: int  # minor currency units
    currency: str
    idempotency_key: str
    id: UUID | None = None
    status: HoldStatus = HoldStatus.CREATED
    created_at: datetime | None = None
    captured_at: datetime | None = None
    released_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.currency or len(self.currency) != 3:
            raise ValueError("currency must be a 3-letter ISO code")
        object.__setattr__(self, "currency", self.currency.upper())
        if self.status is not HoldStatus.CREATED and self.id is None:
            raise ValueError("non-created holds must have a persisted id")

    def capture(self, at: datetime | None = None) -> None:
        """Transition hold to captured (permanent charge)."""
        if self.status is not HoldStatus.CREATED:
            raise ValueError(f"cannot capture hold in status {self.status.value}")
        self.status = HoldStatus.CAPTURED
        self.captured_at = at or datetime.now(UTC)

    def release(self, at: datetime | None = None) -> None:
        """Transition hold to released (funds return)."""
        if self.status is not HoldStatus.CREATED:
            raise ValueError(f"cannot release hold in status {self.status.value}")
        self.status = HoldStatus.RELEASED
        self.released_at = at or datetime.now(UTC)


class HoldRepository(Protocol):
    """Port for hold persistence with idempotency and concurrency safety."""

    async def get(self, hold_id: UUID) -> Hold | None:
        """Return a hold by id, or None."""
        ...

    async def create_hold(
        self,
        wallet_id: UUID,
        amount: int,
        currency: str,
        idempotency_key: str,
    ) -> Hold:
        """Create a hold. Raises InsufficientHoldBalanceError if balance is too low.

        Idempotent: an existing created hold for the key is returned.
        """
        ...

    async def release_hold(self, hold_id: UUID) -> Hold | None:
        """Release a hold. Returns None if already in a terminal state."""
        ...

    async def capture_hold(self, hold_id: UUID) -> Hold | None:
        """Capture a hold: atomically debits the wallet and marks it captured.

        Returns None if already in a terminal state.
        """
        ...

    async def get_by_idempotency(self, wallet_id: UUID, idempotency_key: str) -> Hold | None:
        """Return an existing hold if present, else None."""
        ...

    async def active_hold_sum(self, wallet_id: UUID) -> int:
        """Return sum of all active holds for a wallet."""
        ...

    async def list_by_wallet(self, wallet_id: UUID) -> list[Hold]:
        """Every hold of a wallet, in all states (reconciliation/reporting)."""
        ...


# ---------------------------------------------------------------------------
# Ledger posting — append-only with idempotency guarantee
# ---------------------------------------------------------------------------


class DuplicateIdempotencyError(WalletError):
    """Raised when an idempotency key was already consumed for this wallet."""


class LedgerRepository(Protocol):
    """Port for append-only ledger posting.

    Each entry is immutable once committed. The idempotency_key is enforced
    at the database level so duplicate postings are prevented atomically.
    """

    async def post_entry(
        self,
        wallet_id: UUID,
        amount: int,
        currency: str,
        entry_type: LedgerEntryType,
        idempotency_key: str,
        *,
        reference_type: str = "",
        reference_id: str = "",
        description: str = "",
    ) -> LedgerEntry:
        """Post a ledger entry. Raises DuplicateIdempotencyError if the key exists."""
        ...

    async def get_entry_by_idempotency(
        self,
        wallet_id: UUID,
        idempotency_key: str,
    ) -> LedgerEntry | None:
        """Return an existing entry if it exists, else None."""
        ...

    async def list_entries(self, wallet_id: UUID) -> list[LedgerEntry]:
        """Every ledger entry of a wallet (reconciliation/reporting)."""
        ...
