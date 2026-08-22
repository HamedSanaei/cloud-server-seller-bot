"""Tests for wallet domain: ledger posting, duplicate idempotency."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from cloud_platform.core.money import Money
from cloud_platform.modules.wallet.domain import (
    DuplicateIdempotencyError,
    LedgerEntry,
    LedgerEntryType,
)
from cloud_platform.modules.wallet.repository import SqlAlchemyLedgerRepository


class TestDuplicateIdempotencyError:
    def test_is_wallet_error(self) -> None:
        from cloud_platform.modules.wallet.domain import WalletError

        exc = DuplicateIdempotencyError("dup")
        assert isinstance(exc, WalletError)


class TestLedgerEntryConstruction:
    def test_nonzero_amount_required(self) -> None:
        with pytest.raises(ValueError, match="zero"):
            LedgerEntry(
                id=uuid4(),
                wallet_id=uuid4(),
                entry_type=LedgerEntryType.DEPOSIT,
                amount=Money(Decimal("0"), "EUR"),
                reference_type="payment",
                reference_id="p-1",
                description="zero",
                idempotency_key="key",
            )

    def test_valid_entry(self) -> None:
        entry = LedgerEntry(
            id=uuid4(),
            wallet_id=uuid4(),
            entry_type=LedgerEntryType.CHARGE,
            amount=Money(Decimal("42"), "EUR"),
            reference_type="server",
            reference_id="srv-1",
            description="monthly charge",
            idempotency_key="idemp-key-1",
        )
        assert entry.amount.amount == Decimal("42")

    def test_defaults_to_empty_strings(self) -> None:
        entry = LedgerEntry(
            id=uuid4(),
            wallet_id=uuid4(),
            entry_type=LedgerEntryType.DEPOSIT,
            amount=Money(Decimal("100"), "EUR"),
            reference_type="",
            reference_id="",
            description="",
            idempotency_key="k",
        )
        assert entry.description == ""
        assert entry.reference_type == ""
        assert entry.reference_id == ""

    def test_all_entry_types(self) -> None:
        for et in LedgerEntryType:
            entry = LedgerEntry(
                id=uuid4(),
                wallet_id=uuid4(),
                entry_type=et,
                amount=Money(Decimal("10"), "USD"),
                reference_type="test",
                reference_id="t",
                idempotency_key=f"key-{et}",
            )
            assert entry.entry_type == et
            assert entry.amount.currency == "USD"


class TestDuplicateKeyPost:
    """Verify that DuplicateIdempotencyError is raised on unique violation."""

    async def test_duplicate_raises_error(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.exc import IntegrityError

        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        class FakeDbError(Exception):
            pass

        fake_db = FakeDbError("uq_ledger_wallet_idempotency")

        def on_add(entry: object) -> None:
            raise IntegrityError("duplicate key", {}, fake_db)

        session.add = MagicMock(side_effect=on_add)
        session.commit = AsyncMock()
        session.rollback = AsyncMock()

        repo = SqlAlchemyLedgerRepository(lambda: session)
        with pytest.raises(DuplicateIdempotencyError):
            await repo.post_entry(
                uuid4(),
                100,
                "EUR",
                LedgerEntryType.DEPOSIT,
                "same-key",
            )
        session.rollback.assert_awaited_once()
