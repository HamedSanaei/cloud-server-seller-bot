"""A cutover is safe only with one auditable, chronological USD/IRT boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from cloud_platform.core.money import Money
from cloud_platform.modules.fx.domain import ConversionSnapshot, FxPurpose
from cloud_platform.modules.wallet.currency_migration import WalletCurrencyMigrationService
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldStatus,
    LedgerEntry,
    LedgerEntryType,
    Wallet,
    WalletCurrencyMigration,
    WalletCurrencyMigrationError,
    WalletCurrencyMigrationPlan,
)
from cloud_platform.modules.wallet.reconciliation import (
    BALANCE_LEDGER_MISMATCH,
    CURRENCY_MISMATCH_ENTRY,
    MIGRATION_BOUNDARY_INVALID,
    check_wallet,
)

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
USER_ID, WALLET_ID, OPERATOR_ID, MIGRATION_ID = (uuid4() for _ in range(4))


def _snapshot(amount: int = 200, target: int = 200_000) -> ConversionSnapshot:
    return ConversionSnapshot(
        source_amount_minor=amount,
        source_currency="USD",
        target_amount_minor=target,
        target_currency="IRT",
        rate=Decimal("100000"),
        purpose=FxPurpose.LIQUIDATION,
        source="abantether",
        path="USDTIRT.sell (proxy USDT for USD)",
        observed_at=NOW - timedelta(seconds=5),
        expires_at=NOW + timedelta(seconds=20),
        proxy=True,
        proxy_asset="USDT",
        stale=False,
    )


def _entry(
    kind: LedgerEntryType,
    amount: int,
    currency: str,
    at: datetime,
    *,
    key: str,
    reference_type: str = "",
    reference_id: str = "",
) -> LedgerEntry:
    return LedgerEntry(
        id=uuid4(),
        wallet_id=WALLET_ID,
        entry_type=kind,
        amount=Money(Decimal(amount), currency),
        reference_type=reference_type,
        reference_id=reference_id,
        idempotency_key=key,
        created_at=at,
    )


def _case() -> tuple[Wallet, list[LedgerEntry], WalletCurrencyMigration]:
    before = NOW - timedelta(seconds=3)
    close_at = NOW - timedelta(microseconds=1)
    opened_at = NOW + timedelta(microseconds=1)
    entries = [
        _entry(LedgerEntryType.DEPOSIT, 250, "USD", before, key="old-deposit"),
        _entry(LedgerEntryType.CHARGE, 50, "USD", before, key="old-charge"),
        _entry(
            LedgerEntryType.CURRENCY_CLOSE,
            200,
            "USD",
            close_at,
            key=f"currency-close-{MIGRATION_ID}",
            reference_type="currency_migration",
            reference_id=str(MIGRATION_ID),
        ),
        _entry(
            LedgerEntryType.CURRENCY_OPEN,
            200_000,
            "IRT",
            opened_at,
            key=f"currency-open-{MIGRATION_ID}",
            reference_type="currency_migration",
            reference_id=str(MIGRATION_ID),
        ),
        _entry(LedgerEntryType.CHARGE, 30_000, "IRT", NOW + timedelta(seconds=2), key="new-charge"),
    ]
    migration = WalletCurrencyMigration(
        id=MIGRATION_ID,
        wallet_id=WALLET_ID,
        user_id=USER_ID,
        source_balance_minor=200,
        snapshot=_snapshot(),
        operator_id=OPERATOR_ID,
        reason="Reviewed liquidation",
        close_entry_id=entries[2].id,
        open_entry_id=entries[3].id,
        created_at=NOW,
    )
    wallet = Wallet(user_id=USER_ID, id=WALLET_ID, currency="IRT", balance=170_000)
    return wallet, entries, migration


def _errors(
    wallet: Wallet,
    entries: list[LedgerEntry],
    migration: WalletCurrencyMigration | None,
    holds: list[Hold] | None = None,
) -> set[str]:
    return {
        finding.code
        for finding in check_wallet(wallet, holds or [], entries, [migration] if migration else [])
        if finding.severity.value == "error"
    }


def test_audited_mixed_epochs_reconcile_separately() -> None:
    wallet, entries, migration = _case()
    assert check_wallet(wallet, [], list(reversed(entries)), [migration]) == []


def test_mixed_currencies_without_cutover_are_not_reinterpreted() -> None:
    wallet, entries, _ = _case()
    errors = _errors(wallet, entries, None)
    assert CURRENCY_MISMATCH_ENTRY in errors
    assert MIGRATION_BOUNDARY_INVALID in errors


def test_boundary_requires_matching_cash_events_and_unique_audit_record() -> None:
    wallet, entries, migration = _case()
    assert MIGRATION_BOUNDARY_INVALID in _errors(wallet, entries[:3] + entries[4:], migration)
    altered = replace(entries[3], amount=Money(Decimal(199_999), "IRT"))
    assert MIGRATION_BOUNDARY_INVALID in _errors(
        wallet, [*entries[:3], altered, *entries[4:]], migration
    )
    assert MIGRATION_BOUNDARY_INVALID in {
        f.code for f in check_wallet(wallet, [], entries, [migration, migration])
    }
    expired = replace(
        migration, snapshot=replace(migration.snapshot, expires_at=NOW - timedelta(seconds=1))
    )
    assert MIGRATION_BOUNDARY_INVALID in _errors(wallet, entries, expired)


def test_boundary_rejects_overlapping_or_wrong_currency_ledger_events() -> None:
    wallet, entries, migration = _case()
    overlap = replace(entries[4], created_at=NOW)
    assert MIGRATION_BOUNDARY_INVALID in _errors(wallet, [*entries[:4], overlap], migration)
    wrong_epoch = replace(entries[4], amount=Money(Decimal(30_000), "USD"))
    assert CURRENCY_MISMATCH_ENTRY in _errors(wallet, [*entries[:4], wrong_epoch], migration)
    old_epoch_wrong = replace(entries[0], amount=Money(Decimal(250), "IRT"))
    assert CURRENCY_MISMATCH_ENTRY in _errors(wallet, [old_epoch_wrong, *entries[1:]], migration)


def test_balance_reconciles_both_currencies_not_only_final_irt() -> None:
    wallet, entries, migration = _case()
    old_wrong = replace(entries[0], amount=Money(Decimal(251), "USD"))
    errors = _errors(wallet, [old_wrong, *entries[1:]], migration)
    assert BALANCE_LEDGER_MISMATCH in errors
    assert wallet.balance == 170_000


def test_active_or_unproven_historical_holds_fail_closed() -> None:
    wallet, entries, migration = _case()
    hold = Hold(wallet_id=WALLET_ID, amount=10, currency="USD", idempotency_key="old", id=uuid4())
    assert MIGRATION_BOUNDARY_INVALID in _errors(wallet, entries, migration, [hold])
    hold.status = HoldStatus.RELEASED
    hold.created_at = NOW - timedelta(minutes=5)
    hold.released_at = NOW + timedelta(minutes=1)
    assert MIGRATION_BOUNDARY_INVALID in _errors(wallet, entries, migration, [hold])


def test_new_irt_hold_after_cutover_is_allowed_and_historical_usd_hold_is_immutable() -> None:
    wallet, entries, migration = _case()
    old = Hold(
        wallet_id=WALLET_ID,
        amount=10,
        currency="USD",
        idempotency_key="old",
        id=uuid4(),
        status=HoldStatus.RELEASED,
        created_at=NOW - timedelta(seconds=2),
        released_at=NOW - timedelta(seconds=1),
    )
    new = Hold(
        wallet_id=WALLET_ID,
        amount=1000,
        currency="IRT",
        idempotency_key="new",
        id=uuid4(),
        created_at=NOW + timedelta(minutes=1),
    )
    entries.extend(
        [
            _entry(
                LedgerEntryType.HOLD,
                10,
                "USD",
                NOW - timedelta(seconds=2),
                key="hold-old",
                reference_type="hold",
                reference_id=str(old.id),
            ),
            _entry(
                LedgerEntryType.RELEASE,
                10,
                "USD",
                NOW - timedelta(seconds=1),
                key="release-old",
                reference_type="hold",
                reference_id=str(old.id),
            ),
            _entry(
                LedgerEntryType.HOLD,
                1000,
                "IRT",
                NOW + timedelta(minutes=1),
                key="hold-new",
                reference_type="hold",
                reference_id=str(new.id),
            ),
        ]
    )
    assert check_wallet(wallet, [old, new], entries, [migration]) == []


def test_zero_balance_migration_has_audit_boundary_but_no_zero_amount_ledger_events() -> None:
    wallet = Wallet(user_id=USER_ID, id=WALLET_ID, currency="IRT", balance=0)
    migration = WalletCurrencyMigration(
        id=MIGRATION_ID,
        wallet_id=WALLET_ID,
        user_id=USER_ID,
        source_balance_minor=0,
        snapshot=_snapshot(0, 0),
        operator_id=OPERATOR_ID,
        reason="Zero USD cutover",
        close_entry_id=None,
        open_entry_id=None,
        created_at=NOW,
    )
    assert check_wallet(wallet, [], [], [migration]) == []
    ambiguous = _entry(LedgerEntryType.DEPOSIT, 1, "USD", NOW, key="ambiguous")
    assert MIGRATION_BOUNDARY_INVALID in _errors(wallet, [ambiguous], migration)


def test_plan_rejects_unaudited_rate_and_unreasoned_operator() -> None:
    snapshot = _snapshot()
    for invalid in (
        replace(snapshot, path="USD/IRT.sell"),
        replace(snapshot, purpose=FxPurpose.CHARGE),
        replace(snapshot, stale=True),
    ):
        with pytest.raises(WalletCurrencyMigrationError):
            WalletCurrencyMigrationPlan(
                id=MIGRATION_ID,
                wallet_id=WALLET_ID,
                user_id=USER_ID,
                source_balance_minor=200,
                snapshot=invalid,
                operator_id=OPERATOR_ID,
                reason="Reviewed",
            )
    with pytest.raises(WalletCurrencyMigrationError, match="reason"):
        WalletCurrencyMigrationPlan(
            id=MIGRATION_ID,
            wallet_id=WALLET_ID,
            user_id=USER_ID,
            source_balance_minor=200,
            snapshot=snapshot,
            operator_id=OPERATOR_ID,
            reason=" ",
        )


async def test_operator_plan_requires_existing_usd_wallet_before_fetching_rate() -> None:
    class Wallets:
        def __init__(self, wallet: Wallet) -> None:
            self.wallet = wallet

        async def get(self, user_id):
            return self.wallet

    class Fx:
        async def snapshot(self, amount_minor, source_currency, target_currency, purpose):
            raise AssertionError("must not fetch liquidation rate for non-USD wallet")

    service = WalletCurrencyMigrationService(
        Wallets(Wallet(user_id=USER_ID, id=WALLET_ID, currency="IRT")), Fx()
    )  # type: ignore[arg-type]
    with pytest.raises(WalletCurrencyMigrationError, match="existing USD"):
        await service.plan(
            user_id=USER_ID, operator_id=OPERATOR_ID, migration_id=MIGRATION_ID, reason="Reviewed"
        )
