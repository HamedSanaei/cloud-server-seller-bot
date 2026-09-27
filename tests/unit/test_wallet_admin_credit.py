"""Telegram superadmin credit exercises the real atomic wallet repository path."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.users.domain import PermissionDeniedError, User, UserNotFound
from cloud_platform.modules.wallet.admin import AdminWalletCreditResult, AdminWalletService
from cloud_platform.modules.wallet.domain import DuplicateIdempotencyError, LedgerEntryType
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

ADMIN = 85758085
TARGET = 224466


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Session:
    """Script SQLAlchemy calls while persisting the wallet/ledger state between requests."""

    def __init__(
        self, *, currency: str = "IRT", fail_commit: bool = False, wallet_present: bool = True
    ) -> None:
        self.wallet = SimpleNamespace(
            id=uuid4(), user_id=uuid4(), currency=currency, balance=100, status="active"
        )
        self.ledger: dict[str, Any] = {}
        self.pending: Any = None
        self.commits = 0
        self.fail_commit = fail_commit
        self.wallet_present = wallet_present
        self._balance_before = 100

    async def __aenter__(self) -> _Session:
        self._balance_before = self.wallet.balance
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> _Result:
        entity = stmt.column_descriptions[0].get("entity")
        if entity.__name__ == "Wallet":
            return _Result(self.wallet if self.wallet_present else None)
        if entity.__name__ == "LedgerEntry":
            # The service tests use one operation key per session. Inspecting
            # the persisted ledger is enough to simulate a same-key replay.
            return _Result(next(iter(self.ledger.values()), None))
        raise AssertionError(f"unexpected query entity: {entity}")

    async def get(self, _model: Any, _identity: Any) -> Any:
        return self.wallet

    async def refresh(self, _row: Any) -> None:
        return None

    def add(self, entry: Any) -> None:
        self.pending = entry

    async def commit(self) -> None:
        if self.fail_commit:
            raise IntegrityError("insert", {}, Exception("ledger_insert_failed"))
        assert self.pending is not None
        self.ledger[self.pending.idempotency_key] = self.pending
        self.pending = None
        self.commits += 1

    async def rollback(self) -> None:
        self.wallet.balance = self._balance_before
        self.pending = None


class _Users:
    def __init__(self, user_id: Any, *, found: bool = True) -> None:
        self.user = User(
            id=user_id, username="customer", email="customer@example.com", telegram_user_id=TARGET
        )
        self.found = found
        self.lookups = 0

    async def get_by_telegram_user_id(self, target: int) -> User | None:
        self.lookups += 1
        assert target == TARGET
        return self.user if self.found else None


def _service(db: _Session, users: _Users) -> AdminWalletService:
    return AdminWalletService(users, SqlAlchemyWalletRepository(lambda: db))  # type: ignore[arg-type]


async def test_only_superadmin_may_attempt_credit() -> None:
    db = _Session()
    users = _Users(db.wallet.user_id)
    service = _service(db, users)
    for actor in (123, True, "85758085"):
        with pytest.raises(PermissionDeniedError):
            await service.credit_admin_toman(actor, TARGET, 25, "admin-credit-1")  # type: ignore[arg-type]
    assert users.lookups == 0
    assert db.wallet.balance == 100
    assert db.ledger == {}


async def test_credit_posts_one_irt_fact_and_replay_preserves_balance() -> None:
    db = _Session()
    service = _service(db, _Users(db.wallet.user_id))
    first = await service.credit_admin_toman(ADMIN, TARGET, 25, "admin-credit-1")
    second = await service.credit_admin_toman(ADMIN, TARGET, 25, "admin-credit-1")
    assert first == AdminWalletCreditResult(TARGET, 25, 125, True)
    assert second == AdminWalletCreditResult(TARGET, 25, 125, False)
    assert db.commits == 1
    assert db.wallet.balance == 125
    assert len(db.ledger) == 1
    entry = db.ledger["admin-credit-1"]
    assert entry.currency == "IRT"
    assert entry.entry_type == LedgerEntryType.ADJUSTMENT
    assert entry.amount == 25
    assert entry.reference_type == "admin_telegram_credit"
    assert str(ADMIN) in entry.description and str(TARGET) in entry.description


async def test_key_reused_with_different_amount_or_ledger_facts_fails_closed() -> None:
    db = _Session()
    service = _service(db, _Users(db.wallet.user_id))
    await service.credit_admin_toman(ADMIN, TARGET, 25, "admin-credit-1")
    with pytest.raises(DuplicateIdempotencyError):
        await service.credit_admin_toman(ADMIN, TARGET, 26, "admin-credit-1")
    db.ledger["admin-credit-1"].description = "different actor or target"
    with pytest.raises(DuplicateIdempotencyError):
        await service.credit_admin_toman(ADMIN, TARGET, 25, "admin-credit-1")
    assert db.commits == 1
    assert db.wallet.balance == 125
    assert len(db.ledger) == 1


async def test_replay_with_historical_different_currency_fails_closed() -> None:
    db = _Session()
    service = _service(db, _Users(db.wallet.user_id))
    await service.credit_admin_toman(ADMIN, TARGET, 25, "admin-credit-1")
    db.ledger["admin-credit-1"].currency = "USD"
    with pytest.raises(DuplicateIdempotencyError):
        await service.credit_admin_toman(ADMIN, TARGET, 25, "admin-credit-1")
    assert db.commits == 1
    assert db.wallet.balance == 125


async def test_non_irt_or_missing_user_does_not_credit() -> None:
    usd = _Session(currency="USD")
    with pytest.raises(ValueError, match="wallet currency must be IRT"):
        await _service(usd, _Users(usd.wallet.user_id)).credit_admin_toman(
            ADMIN, TARGET, 25, "admin-credit-1"
        )
    assert usd.wallet.balance == 100 and not usd.ledger

    missing = _Session()
    with pytest.raises(UserNotFound):
        await _service(missing, _Users(missing.wallet.user_id, found=False)).credit_admin_toman(
            ADMIN, TARGET, 25, "admin-credit-1"
        )
    assert missing.wallet.balance == 100 and not missing.ledger

    no_wallet = _Session(wallet_present=False)
    with pytest.raises(ValueError, match="no wallet"):
        await _service(no_wallet, _Users(no_wallet.wallet.user_id)).credit_admin_toman(
            ADMIN, TARGET, 25, "admin-credit-1"
        )
    assert no_wallet.wallet.balance == 100 and not no_wallet.ledger


async def test_invalid_amount_and_key_never_credit() -> None:
    db = _Session()
    service = _service(db, _Users(db.wallet.user_id))
    for amount in (0, -1, True, 2.5):
        with pytest.raises(ValueError):
            await service.credit_admin_toman(ADMIN, TARGET, amount, "admin-credit-1")  # type: ignore[arg-type]
    for key in ("", "  ", " admin-credit-1"):
        with pytest.raises(ValueError):
            await service.credit_admin_toman(ADMIN, TARGET, 25, key)
    assert db.wallet.balance == 100 and not db.ledger


async def test_failed_ledger_insert_rolls_back_wallet_credit() -> None:
    db = _Session(fail_commit=True)
    with pytest.raises(IntegrityError):
        await _service(db, _Users(db.wallet.user_id)).credit_admin_toman(
            ADMIN, TARGET, 25, "admin-credit-1"
        )
    assert db.wallet.balance == 100
    assert db.ledger == {}
    assert db.commits == 0
