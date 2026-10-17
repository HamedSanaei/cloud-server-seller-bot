"""Tests for the wallet repository (mocked session, no live DB)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.wallet.domain import (
    DuplicateIdempotencyError,
)
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository


def _orm_wallet(
    user_id: int | None = None,
    balance: int = 100,
    currency: str = "EUR",
    status: str = "active",
) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.user_id = user_id or uuid4()
    row.balance = balance
    row.currency = currency
    row.status = status
    return row


def _result(value: Any) -> MagicMock:
    return MagicMock(scalar_one_or_none=lambda: value)


@pytest.fixture
def session() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    return mock


@pytest.fixture
def repo(session: AsyncMock) -> SqlAlchemyWalletRepository:
    return SqlAlchemyWalletRepository(lambda: session)


class TestGet:
    async def test_returns_none_when_absent(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        # get() is BY OWNER (user_id): wallets.user_id is UNIQUE, while the
        # wallet PK is a server-generated UUID.
        session.execute = AsyncMock(return_value=_result(None))
        assert await repo.get(uuid4()) is None

    async def test_maps_row_to_aggregate(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(user_id=1, balance=42)
        session.execute = AsyncMock(return_value=_result(orm))
        wallet = await repo.get(uuid4())
        assert wallet is not None
        assert wallet.balance == 42

    async def test_queries_by_user_id_not_pk(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(balance=42)
        captured: dict[str, Any] = {}

        def _capture(stmt: Any) -> _result:
            captured["stmt"] = str(stmt)
            return _result(orm)

        session.execute = AsyncMock(side_effect=_capture)
        owner = uuid4()
        await repo.get(owner)
        # The WHERE clause targets the OWNER column (wallets.user_id is
        # UNIQUE; the PK is a server-generated UUID).
        assert "wallets.user_id" in captured["stmt"]


class TestGetOrCreate:
    async def test_returns_existing_when_found(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(user_id=1, balance=42)
        session.execute = AsyncMock(return_value=_result(orm))
        wallet = await repo.get_or_create(uuid4())
        assert wallet is not None
        assert wallet.balance == 42

    async def test_creates_when_missing(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        session.execute = AsyncMock(return_value=_result(None))
        session.add = MagicMock()
        session.refresh = AsyncMock(side_effect=lambda o: setattr(o, "id", uuid4()))
        wallet = await repo.get_or_create(uuid4())
        assert wallet is not None


class TestCreditDeposit:
    """Gateway deposits apply exactly once, atomically with their ledger entry."""

    async def test_first_call_credits_balance_and_posts_ledger(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(balance=100)
        session.execute = AsyncMock(
            side_effect=[_result(orm), _result(None)]  # lock row, ledger check
        )
        session.get = AsyncMock(return_value=orm)

        wallet, applied = await repo.credit_deposit(orm.user_id, 500, "deposit-tetraminator-pay-1")
        assert applied is True
        assert wallet.balance == 600
        assert session.add.call_count == 2  # ledger + atomic customer outbox
        notice = session.add.call_args_list[0].args[0]
        assert notice.event_type == "customer.notice"
        assert notice.payload["user_id"] == str(orm.user_id)
        entry = session.add.call_args_list[1].args[0]
        assert entry.idempotency_key == "deposit-tetraminator-pay-1"
        assert entry.amount == 500
        assert entry.entry_type.value == "deposit"
        assert entry.wallet_id == orm.id

    async def test_replayed_key_credits_nothing(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(balance=100)
        session.execute = AsyncMock(
            side_effect=[
                _result(orm),
                _result(
                    MagicMock(
                        amount=500,
                        currency="EUR",
                        entry_type="deposit",
                        description="gateway deposit deposit-tetraminator-pay-1",
                    )
                ),
            ]
        )

        wallet, applied = await repo.credit_deposit(orm.user_id, 500, "deposit-tetraminator-pay-1")
        assert applied is False
        assert wallet.balance == 100  # unchanged
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    async def test_unique_constraint_race_rolls_back_both_writes(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        """The loser of a concurrent duplicate rolls back balance AND ledger."""
        orm = _orm_wallet(balance=100)
        refreshed = _orm_wallet(balance=600, user_id=orm.user_id)
        refreshed.id = orm.id
        winner = MagicMock(
            amount=500,
            currency="EUR",
            entry_type="deposit",
            description="gateway deposit deposit-k",
        )
        session.execute = AsyncMock(side_effect=[_result(orm), _result(None), _result(winner)])
        integrity_error = IntegrityError(
            "stmt", {}, Exception('duplicate key ... "uq_ledger_wallet_idempotency"')
        )
        integrity_error.orig = Exception('duplicate key ... "uq_ledger_wallet_idempotency"')
        session.commit = AsyncMock(side_effect=integrity_error)
        session.get = AsyncMock(return_value=refreshed)

        wallet, applied = await repo.credit_deposit(orm.user_id, 500, "deposit-k")
        assert applied is False
        assert wallet.balance == 600  # winner's committed balance
        session.rollback.assert_awaited()

    @pytest.mark.parametrize(
        "changed",
        [
            {"amount": 501},
            {"currency": "IRT"},
            {"entry_type": "refund"},
            {"description": "gateway deposit another-payment"},
        ],
    )
    async def test_unique_race_does_not_replay_different_immutable_facts(
        self,
        repo: SqlAlchemyWalletRepository,
        session: AsyncMock,
        changed: dict[str, Any],
    ) -> None:
        orm = _orm_wallet(balance=100)
        facts = dict(
            amount=500,
            currency="EUR",
            entry_type="deposit",
            description="gateway deposit deposit-k",
        )
        facts.update(changed)
        winner = MagicMock(**facts)
        session.execute = AsyncMock(side_effect=[_result(orm), _result(None), _result(winner)])
        session.commit = AsyncMock(
            side_effect=IntegrityError(
                "stmt",
                {},
                Exception('duplicate key "uq_ledger_wallet_idempotency"'),
            )
        )
        session.rollback = AsyncMock(side_effect=lambda: setattr(orm, "balance", 100))
        with pytest.raises(DuplicateIdempotencyError):
            await repo.credit_deposit(orm.user_id, 500, "deposit-k")
        assert orm.balance == 100
        session.rollback.assert_awaited_once()
        session.get.assert_not_awaited()

    async def test_deposit_overflow_changes_no_balance_or_ledger(
        self,
        repo: SqlAlchemyWalletRepository,
        session: AsyncMock,
    ) -> None:
        orm = _orm_wallet(balance=2**63 - 10)
        session.execute = AsyncMock(side_effect=[_result(orm), _result(None)])
        with pytest.raises(ValueError, match="signed int64"):
            await repo.credit_deposit(orm.user_id, 10, "deposit-k")
        assert orm.balance == 2**63 - 10
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    async def test_other_integrity_errors_propagate(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(balance=100)
        session.execute = AsyncMock(side_effect=[_result(orm), _result(None)])
        integrity_error = IntegrityError("stmt", {}, Exception("some other constraint"))
        integrity_error.orig = Exception("some other constraint")
        session.commit = AsyncMock(side_effect=integrity_error)

        with pytest.raises(IntegrityError):
            await repo.credit_deposit(orm.user_id, 500, "deposit-k")

    async def test_non_positive_amount_rejected(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="positive"):
            await repo.credit_deposit(uuid4(), 0, "deposit-k")

    async def test_no_wallet_raises(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        session.execute = AsyncMock(return_value=_result(None))
        with pytest.raises(ValueError, match="no wallet"):
            await repo.credit_deposit(uuid4(), 500, "deposit-k")

    async def test_reference_falls_back_to_deposit_key(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(balance=0)
        session.execute = AsyncMock(side_effect=[_result(orm), _result(None)])
        session.get = AsyncMock(return_value=orm)

        await repo.credit_deposit(orm.user_id, 100, "deposit-zarinpal-ext-1")
        entry = session.add.call_args.args[0]
        assert "deposit-zarinpal-ext-1" in entry.description

    async def test_returned_wallet_carries_owner_identity(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(balance=0)
        session.execute = AsyncMock(side_effect=[_result(orm), _result(None)])
        session.get = AsyncMock(return_value=orm)

        wallet, _applied = await repo.credit_deposit(
            orm.user_id, 100, "deposit-k", reference="tetraminator/pay-9"
        )
        assert isinstance(wallet.user_id, UUID)
