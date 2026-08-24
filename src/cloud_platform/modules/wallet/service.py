"""Application services for privileged wallet operations.

Authorization is enforced here in the application layer (not only at the
UI/bot layer): every adjustment requires the ``wallet:adjust`` permission,
a non-empty human-readable reason, and leaves an append-only audit record
linking the action to its actor. All audit writes go through the
:class:`~cloud_platform.modules.audit.service.AuditTrail` facade, which
structurally rejects admin mutations without a reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.users.domain import Permission, PermissionChecker, User
from cloud_platform.modules.wallet.domain import (
    Hold,
    LedgerEntry,
    LedgerEntryType,
    LedgerRepository,
    Wallet,
    WalletRepository,
)
from cloud_platform.modules.wallet.repository import HoldService

logger = logging.getLogger(__name__)

#: Page size for the wallet/ledger history UI (M08-010).
HISTORY_DEFAULT_PAGE_SIZE = 20
HISTORY_MAX_PAGE_SIZE = 100


class WalletAdminService:
    """Administrative balance adjustments with a mandatory audit trail.

    Every adjustment is:
    - authorized in the application layer (``Permission.WALLET_ADJUST``),
    - recorded in the append-only ledger under an idempotency key,
    - audited with the acting admin's identity and a non-empty reason.
    """

    def __init__(
        self,
        wallet_repo: WalletRepository,
        ledger_repo: LedgerRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._wallet_repo = wallet_repo
        self._ledger_repo = ledger_repo
        self._audit = AuditTrail(audit_repo)

    async def adjust_balance(
        self,
        *,
        admin: User,
        user_id: UUID,
        amount: int,
        reason: str,
        idempotency_key: str,
    ) -> tuple[Wallet, LedgerEntry]:
        """Credit (positive) or debit (negative) a user's wallet.

        Returns the updated wallet and the posted ledger entry. Retrying
        with the same ``idempotency_key`` replays the original outcome
        without applying the balance change twice.

        Raises:
            PermissionDeniedError: If the actor lacks wallet:adjust.
            ValueError: On empty reason, zero amount, or missing wallet.
            InsufficientBalanceError: On debits beyond the balance.
        """
        checker = PermissionChecker(admin)
        checker.require(Permission.WALLET_ADJUST)

        if not reason or not reason.strip():
            raise ValueError("adjustment reason must not be empty")
        if amount == 0:
            raise ValueError("adjustment amount must not be zero")

        wallet = await self._wallet_repo.get(user_id)
        if wallet is None:
            raise ValueError(f"no wallet for user {user_id}")
        assert wallet.id is not None  # persisted wallets carry an id
        wallet_id: UUID = wallet.id

        # Idempotent replay: the same key returns the original outcome.
        existing = await self._ledger_repo.get_entry_by_idempotency(wallet_id, idempotency_key)
        if existing is not None:
            logger.info(
                "adjustment %s already applied for wallet %s; replaying",
                idempotency_key,
                wallet_id,
            )
            return wallet, existing

        if amount > 0:
            updated = await self._wallet_repo.add_funds(user_id, amount, idempotency_key)
        else:
            updated = await self._wallet_repo.debit(user_id, -amount, idempotency_key)

        entry = await self._ledger_repo.post_entry(
            wallet_id,
            abs(amount),
            updated.currency,
            LedgerEntryType.ADJUSTMENT,
            idempotency_key,
            reference_type="admin_adjustment",
            reference_id=str(admin.id) if admin.id is not None else "",
            description=reason,
        )

        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN,
            actor_id=admin.id,
            action="wallet.adjust",
            resource_type="wallet",
            resource_id=str(wallet_id),
            reason=reason,
            metadata={"amount": str(amount), "currency": updated.currency},
        )
        return updated, entry


class HoldAdminService:
    """Administrative forced hold releases with mandatory audit linkage.

    Releasing a hold returns reserved funds to the available balance — a
    financial mutation — so it requires ``Permission.WALLET_ADJUST``, a
    non-empty reason, and produces an audit event linking actor and hold.
    """

    def __init__(self, hold_service: HoldService, audit_repo: AuditRepository) -> None:
        self._hold_service = hold_service
        self._audit = AuditTrail(audit_repo)

    async def force_release(
        self,
        *,
        admin: User,
        wallet_id: UUID,
        hold_id: UUID,
        reason: str,
        idempotency_key: str,
    ) -> Hold:
        """Release a hold on behalf of an administrator.

        Raises:
            PermissionDeniedError: If the actor lacks wallet:adjust.
            ValueError: On empty reason.
            HoldNotFoundError / HoldStateConflictError: From the underlying
                release state machine.
        """
        checker = PermissionChecker(admin)
        checker.require(Permission.WALLET_ADJUST)

        if not reason or not reason.strip():
            raise ValueError("force-release reason must not be empty")

        released = await self._hold_service.release_hold(wallet_id, hold_id, idempotency_key)
        assert released.id is not None  # persisted holds carry an id

        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN,
            actor_id=admin.id,
            action="wallet.force_release",
            resource_type="wallet",
            resource_id=str(wallet_id),
            reason=reason,
            metadata={
                "hold_id": str(released.id),
                "amount": str(released.amount),
                "currency": released.currency,
            },
        )
        return released


# ---------------------------------------------------------------------------
# Wallet / ledger history UI (M08-010)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WalletBalanceView:
    """The wallet.balance screen: the user's available balance, readable."""

    has_wallet: bool
    balance_minor: int
    currency: str
    formatted: str  # e.g. "12.34 EUR" - integer-formatted, no floats


@dataclass(frozen=True, slots=True)
class LedgerEntryView:
    """One readable row of the ledger history."""

    entry_id: UUID
    entry_type: str
    amount_minor: int  # signed: positive = credit, negative = debit
    currency: str
    formatted: str  # e.g. "+12.34 EUR" / "-1.50 EUR"
    reference_label: str  # e.g. "server 3f2c..." / "hold 9a1b..." / "admin adjustment"
    description: str
    created_at: object | None  # datetime as stored; None when the row predates the column


@dataclass(frozen=True, slots=True)
class LedgerHistoryPage:
    """One page of the ledger history, newest first, plus pagination context."""

    items: tuple[LedgerEntryView, ...]
    total: int
    offset: int
    limit: int

    @property
    def has_next(self) -> bool:
        return self.offset + len(self.items) < self.total

    @property
    def pages(self) -> int:
        if self.limit <= 0:
            return 0
        return (self.total + self.limit - 1) // self.limit


def _format_minor(amount_minor: int, currency: str, *, signed: bool = False) -> str:
    """Integer-formatted money (no float arithmetic, per the money invariant)."""
    sign = ""
    if signed and amount_minor < 0:
        sign = "-"
    elif signed:
        sign = "+"
    major, minor = divmod(abs(amount_minor), 100)
    return f"{sign}{major}.{minor:02d} {currency}"


def _reference_label(entry: LedgerEntry) -> str:
    """A human-readable reference: type + id when present, else the description."""
    if entry.reference_type and entry.reference_id:
        return f"{entry.reference_type} {entry.reference_id}"
    if entry.reference_type:
        return entry.reference_type
    return entry.description or ""


class WalletHistoryService:
    """User-facing wallet balance + ledger history (M08-010).

    Acceptance: **readable amounts/references/pagination.**

    - Amounts are rendered from INTEGER minor units (``divmod``), never
      float arithmetic: "12.34 EUR", "+1.50 EUR", "-0.07 EUR".
    - References are readable labels built from the entry's reference
      type/id (e.g. "server <uuid>", "hold <uuid>", "admin adjustment"),
      falling back to the description.
    - History is paged (default 20, max 100, same bounds as the server
      list), newest first, with total/has_next/pages context.
    - Read-only and ownership-safe: it only reads the acting user's own
      wallet; a user without a wallet gets an empty page, never an error
      and never another user's data.
    """

    def __init__(self, wallet_repo: WalletRepository, ledger_repo: LedgerRepository) -> None:
        self._wallets = wallet_repo
        self._ledger = ledger_repo

    async def balance(self, user_id: UUID) -> WalletBalanceView:
        """The balance screen for the user."""
        wallet = await self._wallets.get(user_id)
        if wallet is None:
            return WalletBalanceView(
                has_wallet=False, balance_minor=0, currency="EUR", formatted="0.00 EUR"
            )
        return WalletBalanceView(
            has_wallet=True,
            balance_minor=wallet.balance,
            currency=wallet.currency,
            formatted=_format_minor(wallet.balance, wallet.currency),
        )

    async def history(
        self,
        user_id: UUID,
        *,
        offset: int = 0,
        limit: int = HISTORY_DEFAULT_PAGE_SIZE,
    ) -> LedgerHistoryPage:
        """One page of the user's ledger history, newest first."""
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if limit > HISTORY_MAX_PAGE_SIZE:
            raise ValueError(f"limit must be <= {HISTORY_MAX_PAGE_SIZE}")

        wallet = await self._wallets.get(user_id)
        if wallet is None or wallet.id is None:
            return LedgerHistoryPage(items=(), total=0, offset=offset, limit=limit)

        entries, total = await self._ledger.list_entries_paged(
            wallet.id, offset=offset, limit=limit
        )
        return LedgerHistoryPage(
            items=tuple(
                LedgerEntryView(
                    entry_id=e.id,
                    entry_type=e.entry_type.value,
                    amount_minor=int(e.amount.amount),
                    currency=e.amount.currency,
                    formatted=_format_minor(int(e.amount.amount), e.amount.currency, signed=True),
                    reference_label=_reference_label(e),
                    description=e.description,
                    created_at=e.created_at,
                )
                for e in entries
            ),
            total=total,
            offset=offset,
            limit=limit,
        )
