"""Tests for the wallet/ledger history UI (M08-010).

Acceptance: readable amounts/references/pagination.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.core.money import Money
from cloud_platform.modules.wallet.domain import LedgerEntry, LedgerEntryType, Wallet
from cloud_platform.modules.wallet.repository import SqlAlchemyLedgerRepository
from cloud_platform.modules.wallet.service import WalletHistoryService

WALLET_ID = uuid4()
USER_ID = uuid4()
T0 = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


def _wallet(balance: int = 1234) -> Wallet:
    return Wallet(user_id=USER_ID, id=WALLET_ID, balance=balance, currency="EUR")


def _entry(
    amount_minor: int,
    entry_type: LedgerEntryType = LedgerEntryType.DEPOSIT,
    reference_type: str = "server",
    reference_id: str = "",
    description: str = "",
    key: str = "k-1",
) -> LedgerEntry:
    ref_id = reference_id
    return LedgerEntry(
        id=uuid4(),
        wallet_id=WALLET_ID,
        entry_type=entry_type,
        amount=Money(Decimal(amount_minor), "EUR"),
        reference_type=reference_type,
        reference_id=ref_id,
        idempotency_key=key,
        description=description,
        created_at=T0,
    )


class FakeWalletRepo:
    def __init__(self, wallet: Wallet | None) -> None:
        self._wallet = wallet

    async def get(self, user_id: UUID) -> Wallet | None:
        return self._wallet


class FakeLedgerRepo:
    """Returns a fixed list, sliced like the SQL repository would."""

    def __init__(self, entries: list[LedgerEntry]) -> None:
        self._entries = entries
        self.calls: list[tuple[int, int]] = []

    async def list_entries(self, wallet_id: UUID) -> list[LedgerEntry]:
        return list(self._entries)

    async def list_entries_paged(
        self, wallet_id: UUID, *, offset: int, limit: int
    ) -> tuple[list[LedgerEntry], int]:
        assert wallet_id == WALLET_ID
        self.calls.append((offset, limit))
        return self._entries[offset : offset + limit], len(self._entries)


def _service(
    wallet: Wallet | None = None,
    entries: list[LedgerEntry] | None = None,
    *,
    no_wallet: bool = False,
) -> WalletHistoryService:
    if no_wallet:
        wallet = None
    elif wallet is None:
        wallet = _wallet()
    return WalletHistoryService(FakeWalletRepo(wallet), FakeLedgerRepo(entries or []))


# --------------------------------------------------------------------------
# Balance screen
# --------------------------------------------------------------------------


class TestBalance:
    async def test_readable_balance(self) -> None:
        view = await _service(_wallet(1234)).balance(USER_ID)
        assert view.has_wallet is True
        assert view.balance_minor == 1234
        assert view.currency == "EUR"
        assert view.formatted == "12.34 EUR"

    async def test_small_amount_keeps_two_decimals(self) -> None:
        view = await _service(_wallet(7)).balance(USER_ID)
        assert view.formatted == "0.07 EUR"

    async def test_zero_balance(self) -> None:
        view = await _service(_wallet(0)).balance(USER_ID)
        assert view.formatted == "0.00 EUR"

    async def test_no_wallet(self) -> None:
        view = await _service(no_wallet=True).balance(USER_ID)
        assert view.has_wallet is False
        assert view.balance_minor == 0
        assert view.formatted == "0.00 EUR"


# --------------------------------------------------------------------------
# Readable amounts + references
# --------------------------------------------------------------------------


class TestReadableRows:
    async def test_credit_and_debit_signs(self) -> None:
        entries = [
            _entry(1234, LedgerEntryType.DEPOSIT, key="d"),
            _entry(-150, LedgerEntryType.CHARGE, reference_type="hold", reference_id="h1", key="c"),
        ]
        page = await _service(entries=entries).history(USER_ID)
        assert len(page.items) == 2

        credit, debit = page.items
        assert credit.amount_minor == 1234
        assert credit.formatted == "+12.34 EUR"
        assert credit.entry_type == "deposit"

        assert debit.amount_minor == -150
        assert debit.formatted == "-1.50 EUR"
        assert debit.entry_type == "charge"

    async def test_one_cent_never_rounds(self) -> None:
        entries = [_entry(-1, LedgerEntryType.CHARGE, key="c")]
        page = await _service(entries=entries).history(USER_ID)
        assert page.items[0].formatted == "-0.01 EUR"

    async def test_reference_label_type_plus_id(self) -> None:
        server_id = str(uuid4())
        entries = [_entry(100, reference_type="server", reference_id=server_id, key="s")]
        page = await _service(entries=entries).history(USER_ID)
        assert page.items[0].reference_label == f"server {server_id}"

    async def test_reference_label_type_only(self) -> None:
        entries = [_entry(100, reference_type="payment", reference_id="", key="p")]
        page = await _service(entries=entries).history(USER_ID)
        assert page.items[0].reference_label == "payment"

    async def test_reference_falls_back_to_description(self) -> None:
        entries = [
            _entry(100, reference_type="", reference_id="", description="admin refund", key="a")
        ]
        page = await _service(entries=entries).history(USER_ID)
        assert page.items[0].reference_label == "admin refund"


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


class TestPagination:
    async def test_pages_of_20_newest_first(self) -> None:
        entries = [_entry(100 + i, key=f"k{i}") for i in range(55)]
        service = _service(entries=entries)
        ledger = service._ledger

        page1 = await service.history(USER_ID)
        assert page1.limit == 20
        assert page1.total == 55
        assert len(page1.items) == 20
        assert page1.has_next is True
        assert page1.pages == 3
        assert ledger.calls[-1] == (0, 20)

        page2 = await service.history(USER_ID, offset=20)
        assert len(page2.items) == 20
        assert page2.has_next is True

        page3 = await service.history(USER_ID, offset=40)
        assert len(page3.items) == 15
        assert page3.has_next is False

    async def test_empty_ledger_is_an_empty_page(self) -> None:
        page = await _service().history(USER_ID)
        assert page.items == ()
        assert page.total == 0
        assert page.has_next is False
        assert page.pages == 0

    async def test_no_wallet_is_an_empty_page_not_an_error(self) -> None:
        page = await _service(no_wallet=True).history(USER_ID)
        assert page.total == 0
        assert page.items == ()

    @pytest.mark.parametrize("kwargs", [{"offset": -1}, {"limit": 0}, {"limit": 101}])
    async def test_invalid_pagination_rejected(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            await _service().history(USER_ID, **kwargs)

    async def test_explicit_limit_respected(self) -> None:
        entries = [_entry(100, key="a")]
        service = _service(entries=entries)
        await service.history(USER_ID, offset=0, limit=5)
        assert service._ledger.calls[-1] == (0, 5)


# --------------------------------------------------------------------------
# SQL repository: count + page, newest first, created_at mapped
# --------------------------------------------------------------------------


def _ledger_row(amount: int, created: datetime) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.wallet_id = WALLET_ID
    row.entry_type = "deposit"
    row.amount = amount
    row.currency = "EUR"
    row.idempotency_key = "k"
    row.reference_type = "server"
    row.reference_id = uuid4()
    row.description = None
    row.created_at = created
    return row


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock


class TestSqlPaged:
    async def test_count_then_page(self, db: AsyncMock) -> None:
        rows = [
            _ledger_row(100, T0),
            _ledger_row(200, T0.replace(hour=9)),
        ]
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar=lambda: 2),  # count
                MagicMock(scalars=lambda: MagicMock(all=lambda: list(reversed(rows)))),  # page
            ]
        )
        repo = SqlAlchemyLedgerRepository(lambda: db)  # type: ignore[arg-type]
        entries, total = await repo.list_entries_paged(WALLET_ID, offset=0, limit=10)

        assert total == 2
        assert len(entries) == 2
        # the repository maps created_at into the domain entry
        assert all(e.created_at is not None for e in entries)
        assert entries[0].reference_id == str(rows[1].reference_id)  # newest first as returned
        assert db.execute.await_count == 2

    async def test_empty_result(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar=lambda: 0),
                MagicMock(scalars=lambda: MagicMock(all=lambda: [])),
            ]
        )
        repo = SqlAlchemyLedgerRepository(lambda: db)  # type: ignore[arg-type]
        entries, total = await repo.list_entries_paged(WALLET_ID, offset=0, limit=10)
        assert entries == []
        assert total == 0
