"""Tests for the Wallet domain aggregate."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cloud_platform.core.money import Money
from cloud_platform.modules.wallet.domain import (
    InsufficientBalanceError,
    LedgerEntry,
    LedgerEntryType,
    Wallet,
    WalletError,
    WalletStatus,
)


def _make_wallet(
    balance: int = 0,
    currency: str = "EUR",
    status: WalletStatus = WalletStatus.ACTIVE,
) -> Wallet:
    from uuid import uuid4

    return Wallet(
        user_id=uuid4(),
        balance=balance,
        currency=currency,
        status=status,
    )


class TestWalletConstruction:
    def test_defaults(self) -> None:
        w = _make_wallet()
        assert w.balance == 0
        assert w.currency == "EUR"
        assert w.status is WalletStatus.ACTIVE

    def test_currency_uppercased(self) -> None:
        w = _make_wallet(currency="eur")
        assert w.currency == "EUR"

    def test_invalid_currency_rejected(self) -> None:
        with pytest.raises(ValueError, match="3-letter"):
            Wallet(user_id=1, currency="E")

    def test_to_money(self) -> None:
        w = _make_wallet(balance=1234)
        assert w.to_money() == Money(Decimal(1234), "EUR")


class TestAddFunds:
    def test_increments_balance(self) -> None:
        w = _make_wallet(balance=100)
        new_bal = w.add_funds(50)
        assert new_bal == 150
        assert w.balance == 150

    def test_negative_amount_rejected(self) -> None:
        w = _make_wallet()
        with pytest.raises(ValueError, match="positive"):
            w.add_funds(-10)


class TestDebit:
    def test_decrements_balance(self) -> None:
        w = _make_wallet(balance=100)
        new_bal = w.debit(30)
        assert new_bal == 70
        assert w.balance == 70

    def test_overdraft_raises(self) -> None:
        w = _make_wallet(balance=10)
        with pytest.raises(InsufficientBalanceError):
            w.debit(20)

    def test_negative_amount_rejected(self) -> None:
        w = _make_wallet()
        with pytest.raises(ValueError, match="positive"):
            w.debit(-5)


class TestFreeze:
    def test_freeze_from_active(self) -> None:
        w = _make_wallet()
        w.freeze()
        assert w.status is WalletStatus.FROZEN

    def test_freeze_closed_rejected(self) -> None:
        w = _make_wallet(status=WalletStatus.CLOSED)
        with pytest.raises(WalletError):
            w.freeze()


class TestUnfreeze:
    def test_unfreeze_from_frozen(self) -> None:
        w = _make_wallet()
        w.freeze()
        w.unfreeze()
        assert w.status is WalletStatus.ACTIVE

    def test_unfreeze_non_frozen_rejected(self) -> None:
        w = _make_wallet()
        with pytest.raises(WalletError):
            w.unfreeze()


class TestClose:
    def test_close_zero_balance(self) -> None:
        w = _make_wallet(balance=0)
        w.close()
        assert w.status is WalletStatus.CLOSED

    def test_close_nonzero_balance_rejected(self) -> None:
        w = _make_wallet(balance=10)
        with pytest.raises(WalletError):
            w.close()


class TestLedgerEntry:
    def test_nonzero_amount_required(self) -> None:
        from uuid import uuid4

        with pytest.raises(ValueError, match="zero"):
            LedgerEntry(
                id=uuid4(),
                wallet_id=uuid4(),
                entry_type=LedgerEntryType.DEPOSIT,
                amount=Money(Decimal("0"), "EUR"),
                reference_type="test",
                reference_id="0",
                idempotency_key="abc",
            )

    def test_valid_entry(self) -> None:
        from uuid import uuid4

        entry = LedgerEntry(
            id=uuid4(),
            wallet_id=uuid4(),
            entry_type=LedgerEntryType.CHARGE,
            amount=Money(Decimal("42"), "EUR"),
            reference_type="server",
            reference_id="srv-1",
            idempotency_key="idempotent-key",
        )
        assert entry.amount.amount == Decimal("42")
