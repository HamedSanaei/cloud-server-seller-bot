"""Ledger reconciliation report (M05-008).

Detects impossible or corrupt balance states by re-deriving what the ledger
and hold records *imply* and comparing them with the stored wallet balance.
This is a read-only audit: it never mutates state (findings are reported,
never auto-fixed).

Model recap (what is "impossible"):
- ``Wallet.balance`` is available cash in minor units; active holds
  (status ``created``) reserve a slice of it, so
  ``balance - sum(active holds) >= 0`` must hold.
- Captures debit the balance and must pair 1:1 with a CHARGE ledger entry;
  releases pair with a RELEASE entry; every hold pairs with a HOLD entry.
- The balance must equal the ledger's unambiguous net
  (deposits + refunds - charges) up to the total of admin adjustments,
  whose signed direction is intentionally not encoded on the entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldStatus,
    LedgerEntry,
    LedgerEntryType,
    Wallet,
    WalletStatus,
)

__all__ = [
    "FindingSeverity",
    "LedgerReconciliationService",
    "ReconciliationFinding",
    "ReconciliationReport",
    "check_wallet",
]


class FindingSeverity(StrEnum):
    ERROR = "error"  # an impossible/corrupt state
    WARNING = "warning"  # suspicious but explainable


#: Finding codes (stable identifiers for dashboards/alerts).
NEGATIVE_BALANCE = "NEGATIVE_BALANCE"
OVERRESERVED = "OVERRESERVED"
CLOSED_NONZERO_BALANCE = "CLOSED_NONZERO_BALANCE"
CURRENCY_MISMATCH_HOLD = "CURRENCY_MISMATCH_HOLD"
CURRENCY_MISMATCH_ENTRY = "CURRENCY_MISMATCH_ENTRY"
ZERO_AMOUNT_ENTRY = "ZERO_AMOUNT_ENTRY"
DUPLICATE_IDEMPOTENCY_KEY = "DUPLICATE_IDEMPOTENCY_KEY"
CAPTURED_HOLD_MISSING_CHARGE = "CAPTURED_HOLD_MISSING_CHARGE"
RELEASED_HOLD_MISSING_RELEASE = "RELEASED_HOLD_MISSING_RELEASE"
CHARGE_WITHOUT_HOLD = "CHARGE_WITHOUT_HOLD"
RELEASE_WITHOUT_HOLD = "RELEASE_WITHOUT_HOLD"
HOLD_WITHOUT_ENTRY = "HOLD_WITHOUT_ENTRY"
BALANCE_LEDGER_MISMATCH = "BALANCE_LEDGER_MISMATCH"


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    """One detected anomaly in one wallet."""

    wallet_id: UUID
    code: str
    severity: FindingSeverity
    message: str
    details: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """The outcome of a full reconciliation pass."""

    wallets_checked: int
    findings: tuple[ReconciliationFinding, ...]

    @property
    def errors(self) -> tuple[ReconciliationFinding, ...]:
        return tuple(f for f in self.findings if f.severity is FindingSeverity.ERROR)

    @property
    def warnings(self) -> tuple[ReconciliationFinding, ...]:
        return tuple(f for f in self.findings if f.severity is FindingSeverity.WARNING)

    @property
    def is_clean(self) -> bool:
        """True when no impossible/corrupt state was detected."""
        return not self.errors

    def render(self) -> str:
        """A log-safe plain-text rendering (ASCII only)."""
        lines = [
            f"ledger reconciliation: {self.wallets_checked} wallet(s) checked, "
            f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        ]
        for finding in self.findings:
            lines.append(
                f"  [{finding.severity.value.upper()}] {finding.wallet_id} "
                f"{finding.code}: {finding.message}"
            )
        if not self.findings:
            lines.append("  all wallet states consistent")
        return "\n".join(lines)


def _entry_minor(entry: LedgerEntry) -> int:
    """A ledger entry amount in minor units (the ledger stores minor units)."""
    return int(entry.amount.amount)


def _resolve_hold(entry: LedgerEntry, holds: list[Hold]) -> Hold | None:
    """Find the hold an entry references (by hold id, else idempotency key)."""
    for hold in holds:
        if hold.id is not None and str(hold.id) == entry.reference_id:
            return hold
    return next((h for h in holds if h.idempotency_key == entry.reference_id), None)


def check_wallet(
    wallet: Wallet, holds: list[Hold], entries: list[LedgerEntry]
) -> list[ReconciliationFinding]:
    """Pure per-wallet consistency checks (no I/O)."""
    assert wallet.id is not None
    wallet_id = wallet.id
    findings: list[ReconciliationFinding] = []

    def add(
        code: str,
        severity: FindingSeverity,
        message: str,
        **details: str,
    ) -> None:
        findings.append(ReconciliationFinding(wallet_id, code, severity, message, dict(details)))

    # -- balance sanity -----------------------------------------------------
    if wallet.balance < 0:
        add(NEGATIVE_BALANCE, FindingSeverity.ERROR, f"balance is negative: {wallet.balance}")
    active_held = sum(h.amount for h in holds if h.status is HoldStatus.CREATED)
    if wallet.balance - active_held < 0:
        add(
            OVERRESERVED,
            FindingSeverity.ERROR,
            f"active holds exceed the balance (balance {wallet.balance} < held {active_held})",
        )
    if wallet.status is WalletStatus.CLOSED and wallet.balance != 0:
        add(
            CLOSED_NONZERO_BALANCE,
            FindingSeverity.ERROR,
            f"closed wallet has non-zero balance: {wallet.balance}",
        )

    # -- currency consistency ------------------------------------------------
    for hold in holds:
        if hold.currency != wallet.currency:
            add(
                CURRENCY_MISMATCH_HOLD,
                FindingSeverity.ERROR,
                f"hold {hold.idempotency_key} currency {hold.currency} != wallet {wallet.currency}",
                hold_id=str(hold.id) if hold.id else "",
            )
    for entry in entries:
        if entry.amount.currency != wallet.currency:
            add(
                CURRENCY_MISMATCH_ENTRY,
                FindingSeverity.ERROR,
                f"entry {entry.idempotency_key} currency {entry.amount.currency} != wallet "
                f"{wallet.currency}",
                entry_type=entry.entry_type.value,
            )

    # -- entry sanity ---------------------------------------------------------
    seen_keys: set[str] = set()
    for entry in entries:
        if _entry_minor(entry) == 0:
            add(
                ZERO_AMOUNT_ENTRY,
                FindingSeverity.ERROR,
                f"entry {entry.idempotency_key} has a zero amount",
                entry_type=entry.entry_type.value,
            )
        if entry.idempotency_key in seen_keys:
            add(
                DUPLICATE_IDEMPOTENCY_KEY,
                FindingSeverity.ERROR,
                f"idempotency key {entry.idempotency_key} used by multiple entries",
            )
        seen_keys.add(entry.idempotency_key)

    # -- hold <-> ledger pairing ---------------------------------------------
    for hold in holds:
        key = hold.idempotency_key
        hold_entry = f"hold-{key}"
        if hold_entry not in seen_keys:
            add(
                HOLD_WITHOUT_ENTRY,
                FindingSeverity.WARNING,
                f"hold {key} has no HOLD ledger entry",
                hold_id=str(hold.id) if hold.id else "",
            )
        if hold.status is HoldStatus.CAPTURED and f"capture-{key}" not in seen_keys:
            add(
                CAPTURED_HOLD_MISSING_CHARGE,
                FindingSeverity.ERROR,
                f"captured hold {key} has no CHARGE ledger entry",
                hold_id=str(hold.id) if hold.id else "",
            )
        if hold.status is HoldStatus.RELEASED and f"release-{key}" not in seen_keys:
            add(
                RELEASED_HOLD_MISSING_RELEASE,
                FindingSeverity.ERROR,
                f"released hold {key} has no RELEASE ledger entry",
                hold_id=str(hold.id) if hold.id else "",
            )

    for entry in entries:
        if entry.reference_type != "hold":
            continue
        referenced = _resolve_hold(entry, holds)
        if entry.entry_type is LedgerEntryType.CHARGE:
            if referenced is None:
                add(
                    CHARGE_WITHOUT_HOLD,
                    FindingSeverity.ERROR,
                    f"CHARGE entry {entry.idempotency_key} references no hold "
                    f"({entry.reference_id!r})",
                )
            elif referenced.status is not HoldStatus.CAPTURED:
                add(
                    CHARGE_WITHOUT_HOLD,
                    FindingSeverity.ERROR,
                    f"CHARGE entry {entry.idempotency_key} references hold "
                    f"{referenced.idempotency_key} in state {referenced.status.value}",
                )
        if entry.entry_type is LedgerEntryType.RELEASE:
            if referenced is None:
                add(
                    RELEASE_WITHOUT_HOLD,
                    FindingSeverity.ERROR,
                    f"RELEASE entry {entry.idempotency_key} references no hold "
                    f"({entry.reference_id!r})",
                )
            elif referenced.status is not HoldStatus.RELEASED:
                add(
                    RELEASE_WITHOUT_HOLD,
                    FindingSeverity.ERROR,
                    f"RELEASE entry {entry.idempotency_key} references hold "
                    f"{referenced.idempotency_key} in state {referenced.status.value}",
                )

    # -- balance vs ledger derivation ------------------------------------------
    # HOLD/RELEASE entries move no cash (reservations only); captures are the
    # CHARGE entries. Admin adjustments carry unsigned amounts, so they explain
    # a residual of at most their total.
    expected = 0
    adjustment_slack = 0
    for entry in entries:
        amount = _entry_minor(entry)
        if entry.entry_type is LedgerEntryType.DEPOSIT:
            expected += amount
        elif entry.entry_type is LedgerEntryType.REFUND:
            expected += amount
        elif entry.entry_type is LedgerEntryType.CHARGE:
            expected -= amount
        elif entry.entry_type is LedgerEntryType.ADJUSTMENT:
            adjustment_slack += amount
    residual = wallet.balance - expected
    if residual != 0:
        if abs(residual) <= adjustment_slack:
            add(
                BALANCE_LEDGER_MISMATCH,
                FindingSeverity.WARNING,
                f"balance differs from ledger net by {residual}; within the "
                f"admin-adjustment total ({adjustment_slack})",
                residual=str(residual),
            )
        else:
            add(
                BALANCE_LEDGER_MISMATCH,
                FindingSeverity.ERROR,
                f"balance differs from ledger net by {residual} and no admin "
                f"adjustment total ({adjustment_slack}) explains it",
                residual=str(residual),
                expected=str(expected),
            )

    return findings


class LedgerReconciliationService:
    """Runs the reconciliation pass over every wallet (read-only)."""

    def __init__(
        self,
        wallet_repo: object,
        hold_repo: object,
        ledger_repo: object,
    ) -> None:
        self._wallets = wallet_repo
        self._holds = hold_repo
        self._ledger = ledger_repo

    async def generate_report(self) -> ReconciliationReport:
        wallets: list[Wallet] = await self._wallets.list_all()  # type: ignore[attr-defined]
        findings: list[ReconciliationFinding] = []
        for wallet in wallets:
            if wallet.id is None:
                continue
            holds: list[Hold] = await self._holds.list_by_wallet(wallet.id)  # type: ignore[attr-defined]
            entries: list[LedgerEntry] = await self._ledger.list_entries(wallet.id)  # type: ignore[attr-defined]
            findings.extend(check_wallet(wallet, holds, entries))
        return ReconciliationReport(
            wallets_checked=len(wallets),
            findings=tuple(findings),
        )
