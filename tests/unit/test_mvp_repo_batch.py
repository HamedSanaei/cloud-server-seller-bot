"""Repository-layer tests (mocked sessions) for users, tokens, billing
accrual periods, and the wallet ledger + HoldService. Follows the
test_operations_repository.py pattern."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.billing.repository import (
    AccrualPeriodExistsError,
    PostgresAdvisoryAccrualLock,
    SqlAlchemyAccrualPeriodRepository,
)
from cloud_platform.modules.billing.service import AccrualPeriod
from cloud_platform.modules.tokens.domain import ApiToken
from cloud_platform.modules.tokens.repository import SqlAlchemyApiTokenRepository
from cloud_platform.modules.users.domain import UserStatus
from cloud_platform.modules.users.repository import (
    SqlAlchemyUserRepository,
    UserNotFound,
)
from cloud_platform.modules.wallet.domain import HoldStatus, LedgerEntryType
from cloud_platform.modules.wallet.repository import (
    HoldService,
    SqlAlchemyLedgerRepository,
)

WALLET_ID = uuid4()
USER_ID = uuid4()


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    mock.refresh = AsyncMock()
    return mock


def _scalar_result(row: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=row)
    return result


def _scalars_result(rows: list[object]) -> MagicMock:
    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=lambda: rows))
    return result


def _ledger_row(**overrides: object) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.wallet_id = WALLET_ID
    row.amount = 1000
    row.currency = "EUR"
    row.entry_type = "charge"
    row.idempotency_key = "k-1"
    row.reference_type = "order"
    row.reference_id = uuid4()
    row.description = "d"
    row.created_at = datetime.now(UTC)
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


def _wallet_row(**overrides: object) -> MagicMock:
    row = MagicMock()
    row.id = WALLET_ID
    row.user_id = USER_ID
    row.balance_minor = 5000
    row.currency = "EUR"
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


class TestWalletLedger:
    async def test_list_entries_paged_returns_page_and_total(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar=lambda: 3),
                _scalars_result(
                    [_ledger_row(idempotency_key="a"), _ledger_row(idempotency_key="b")]
                ),
            ]
        )
        repo = SqlAlchemyLedgerRepository(lambda: db)  # type: ignore[arg-type]
        entries, total = await repo.list_entries_paged(WALLET_ID, offset=0, limit=2)
        assert total == 3
        assert [e.idempotency_key for e in entries] == ["a", "b"]

    async def test_list_entries(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalars_result([_ledger_row(idempotency_key="x")]))
        repo = SqlAlchemyLedgerRepository(lambda: db)  # type: ignore[arg-type]
        entries = await repo.list_entries(WALLET_ID)
        assert entries[0].idempotency_key == "x"

    async def test_post_entry_duplicate_raises(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(None))
        exc = IntegrityError("stmt", {}, Exception("uq_ledger_wallet_idempotency"))
        db.commit = AsyncMock(side_effect=exc)
        repo = SqlAlchemyLedgerRepository(lambda: db)  # type: ignore[arg-type]
        from cloud_platform.modules.wallet.repository import DuplicateIdempotencyError

        with pytest.raises(DuplicateIdempotencyError):
            await repo.post_entry(WALLET_ID, 100, "EUR", LedgerEntryType.CHARGE, "dup-key")

    async def test_post_entry_unrelated_integrity_error_reraises(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(None))
        exc = IntegrityError("stmt", {}, Exception("fk_violation"))
        db.commit = AsyncMock(side_effect=exc)
        repo = SqlAlchemyLedgerRepository(lambda: db)  # type: ignore[arg-type]
        with pytest.raises(IntegrityError):
            await repo.post_entry(WALLET_ID, 100, "EUR", LedgerEntryType.CHARGE, "k")


class TestHoldService:
    def _hold_repo(self, hold: MagicMock) -> MagicMock:
        repo = MagicMock()
        repo.create_hold = AsyncMock(return_value=hold)
        repo.release_hold = AsyncMock(return_value=hold)
        repo.capture_hold = AsyncMock(return_value=hold)
        repo.get = AsyncMock(return_value=hold)
        repo.get_by_idempotency = AsyncMock(return_value=None)
        return repo

    def _ledger_repo(self) -> MagicMock:
        repo = MagicMock()
        repo.post_entry = AsyncMock()
        return repo

    async def test_create_hold_posts_ledger_entry(self) -> None:
        hold = MagicMock()
        hold.id = uuid4()
        hold.status = HoldStatus.CREATED
        hold_repo = self._hold_repo(hold)
        ledger = self._ledger_repo()
        wallet = MagicMock()
        service = HoldService(wallet, hold_repo, ledger)
        result = await service.create_hold(WALLET_ID, 1000, "EUR", "h-1")
        assert result is hold
        args, kwargs = ledger.post_entry.await_args
        assert args[1] == 1000
        assert args[3] is LedgerEntryType.HOLD
        assert kwargs["reference_id"] == str(hold.id)

    async def test_create_hold_duplicate_ledger_entry_is_silent(self) -> None:
        hold = MagicMock()
        hold.id = uuid4()
        hold_repo = self._hold_repo(hold)
        ledger = self._ledger_repo()
        from cloud_platform.modules.wallet.repository import DuplicateIdempotencyError

        ledger.post_entry = AsyncMock(side_effect=DuplicateIdempotencyError("dup"))
        service = HoldService(MagicMock(), hold_repo, ledger)
        result = await service.create_hold(WALLET_ID, 1000, "EUR", "h-2")
        assert result is hold

    async def test_capture_hold_posts_charge_entry(self) -> None:
        hold = MagicMock()
        hold.id = uuid4()
        hold.amount = 1000
        hold.currency = "EUR"
        hold.status = HoldStatus.CREATED
        hold_repo = self._hold_repo(hold)
        captured = MagicMock()
        captured.id = hold.id
        captured.amount = 1000
        captured.currency = "EUR"
        captured.status = HoldStatus.CAPTURED
        hold_repo.capture_hold = AsyncMock(return_value=captured)
        ledger = self._ledger_repo()
        service = HoldService(MagicMock(), hold_repo, ledger)
        result = await service.capture_hold(WALLET_ID, hold.id, "cap-1")
        assert result.status is HoldStatus.CAPTURED
        args, _ = ledger.post_entry.await_args
        assert args[3] is LedgerEntryType.CHARGE
        assert args[1] == 1000

    async def test_release_hold_posts_release_entry(self) -> None:
        hold = MagicMock()
        hold.id = uuid4()
        hold.amount = 1000
        hold.currency = "EUR"
        hold.status = HoldStatus.CREATED
        hold_repo = self._hold_repo(hold)
        released = MagicMock()
        released.id = hold.id
        released.amount = 1000
        released.currency = "EUR"
        released.status = HoldStatus.RELEASED
        hold_repo.release_hold = AsyncMock(return_value=released)
        ledger = self._ledger_repo()
        service = HoldService(MagicMock(), hold_repo, ledger)
        result = await service.release_hold(WALLET_ID, hold.id, "rel-1")
        assert result.status is HoldStatus.RELEASED
        args, _ = ledger.post_entry.await_args
        assert args[3] is LedgerEntryType.RELEASE


class TestUserRepositoryExtras:
    def _user_row(self, **overrides: object) -> MagicMock:
        row = MagicMock()
        row.id = USER_ID
        row.username = "alice"
        row.email = "alice@example.com"
        row.status = "active"
        row.role = "user"
        row.terms_version = 1
        row.terms_accepted_at = None
        row.telegram_user_id = 12345
        row.created_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)
        for k, v in overrides.items():
            setattr(row, k, v)
        return row

    async def test_search_returns_matches(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalars_result([self._user_row(username="alice")]))
        repo = SqlAlchemyUserRepository(lambda: db)  # type: ignore[arg-type]
        users = await repo.search("ali")
        assert users[0].username == "alice"

    async def test_get_by_telegram_user_id(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(self._user_row(telegram_user_id=999)))
        repo = SqlAlchemyUserRepository(lambda: db)  # type: ignore[arg-type]
        user = await repo.get_by_telegram_user_id(999)
        assert user is not None
        assert user.telegram_user_id == 999

    async def test_get_by_telegram_user_id_missing(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(None))
        repo = SqlAlchemyUserRepository(lambda: db)  # type: ignore[arg-type]
        assert await repo.get_by_telegram_user_id(1) is None

    async def test_update_status(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(self._user_row()))
        repo = SqlAlchemyUserRepository(lambda: db)  # type: ignore[arg-type]
        user = await repo.update_status(USER_ID, UserStatus.BANNED)
        assert user.status is UserStatus.BANNED
        db.commit.assert_awaited_once()
        db.refresh.assert_awaited_once()

    async def test_update_status_missing_raises(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(None))
        repo = SqlAlchemyUserRepository(lambda: db)  # type: ignore[arg-type]
        with pytest.raises(UserNotFound):
            await repo.update_status(USER_ID, UserStatus.ACTIVE)

    async def test_update_terms(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(self._user_row()))
        repo = SqlAlchemyUserRepository(lambda: db)  # type: ignore[arg-type]
        user = await repo.update_terms(USER_ID, 3)
        assert user.terms_version == 3

    async def test_update_telegram_user_id(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(self._user_row()))
        repo = SqlAlchemyUserRepository(lambda: db)  # type: ignore[arg-type]
        user = await repo.update_telegram_user_id(USER_ID, 555)
        assert user.telegram_user_id == 555

    async def test_update_telegram_user_id_missing_raises(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(None))
        repo = SqlAlchemyUserRepository(lambda: db)  # type: ignore[arg-type]
        with pytest.raises(UserNotFound):
            await repo.update_telegram_user_id(USER_ID, 555)


class TestApiTokenRepository:
    def _token_row(self, **overrides: object) -> MagicMock:
        row = MagicMock()
        row.id = uuid4()
        row.user_id = USER_ID
        row.name = "cli"
        row.token_hash = "abc123"
        row.scopes = []
        row.last_used_at = None
        row.expires_at = None
        row.created_at = datetime.now(UTC)
        for k, v in overrides.items():
            setattr(row, k, v)
        return row

    async def test_add_commits(self, db: AsyncMock) -> None:
        db.refresh = AsyncMock(side_effect=lambda row: setattr(row, "id", uuid4()))
        db.execute = AsyncMock(return_value=_scalar_result(self._token_row()))
        repo = SqlAlchemyApiTokenRepository(lambda: db)  # type: ignore[arg-type]
        token = ApiToken(user_id=USER_ID, name="cli", token_hash="abc123", prefix="ab")
        saved = await repo.add(token)
        assert saved.token_hash == "abc123"
        db.add.assert_called_once()

    async def test_get_by_hash(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(self._token_row(token_hash="xyz")))
        repo = SqlAlchemyApiTokenRepository(lambda: db)  # type: ignore[arg-type]
        token = await repo.get_by_hash("xyz")
        assert token is not None
        assert token.token_hash == "xyz"

    async def test_get_by_hash_missing(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(None))
        repo = SqlAlchemyApiTokenRepository(lambda: db)  # type: ignore[arg-type]
        assert await repo.get_by_hash("nope") is None

    async def test_list_for_user(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=_scalars_result([self._token_row(name="a"), self._token_row(name="b")])
        )
        repo = SqlAlchemyApiTokenRepository(lambda: db)  # type: ignore[arg-type]
        tokens = await repo.list_for_user(USER_ID)
        assert [t.name for t in tokens] == ["a", "b"]

    async def test_save_updates_row(self, db: AsyncMock) -> None:
        row = self._token_row()
        db.merge = AsyncMock(return_value=row)
        db.execute = AsyncMock(return_value=_scalar_result(row))
        repo = SqlAlchemyApiTokenRepository(lambda: db)  # type: ignore[arg-type]
        token = ApiToken(
            user_id=USER_ID,
            name="cli",
            token_hash="abc123",
            prefix="ab",
            last_used_at=datetime.now(UTC),
        )
        saved = await repo.save(token)
        assert saved.token_hash == "abc123"
        db.commit.assert_awaited_once()


class TestAccrualPeriodRepository:
    def _period_row(self, **overrides: object) -> MagicMock:
        row = MagicMock()
        row.id = uuid4()
        row.server_id = uuid4()
        row.wallet_id = WALLET_ID
        row.period_start = datetime(2026, 1, 1, tzinfo=UTC)
        row.period_end = datetime(2026, 1, 2, tzinfo=UTC)
        row.quanta = 24
        row.cost_minor = 100
        row.selling_minor = 150
        row.currency = "EUR"
        row.idempotency_key = "p-1"
        for k, v in overrides.items():
            setattr(row, k, v)
        return row

    def _period(self) -> AccrualPeriod:
        return AccrualPeriod(
            server_id=uuid4(),
            wallet_id=WALLET_ID,
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
            quanta=24,
            cost_minor=100,
            selling_minor=150,
            currency="EUR",
            idempotency_key="p-1",
        )

    async def test_get_by_key(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(self._period_row()))
        repo = SqlAlchemyAccrualPeriodRepository(lambda: db)  # type: ignore[arg-type]
        period = await repo.get_by_key("p-1")
        assert period is not None
        assert period.quanta == 24

    async def test_add_success(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            side_effect=[
                _scalar_result(None),
                _scalar_result(self._period_row()),
            ]
        )
        repo = SqlAlchemyAccrualPeriodRepository(lambda: db)  # type: ignore[arg-type]
        period = await repo.add(self._period())
        assert period.selling_minor == 150

    async def test_add_duplicate_raises(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_scalar_result(None))
        db.commit = AsyncMock(side_effect=Exception("duplicate key"))
        repo = SqlAlchemyAccrualPeriodRepository(lambda: db)  # type: ignore[arg-type]
        with pytest.raises(AccrualPeriodExistsError):
            await repo.add(self._period())
        db.rollback.assert_awaited_once()

    async def test_list_between(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=_scalars_result([self._period_row(), self._period_row()])
        )
        repo = SqlAlchemyAccrualPeriodRepository(lambda: db)  # type: ignore[arg-type]
        periods = await repo.list_between(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
        )
        assert len(periods) == 2

    async def test_month_total(self, db: AsyncMock) -> None:
        db.scalar = AsyncMock(return_value=450)
        repo = SqlAlchemyAccrualPeriodRepository(lambda: db)  # type: ignore[arg-type]
        total = await repo.month_total(WALLET_ID, datetime(2026, 1, 1, tzinfo=UTC))
        assert total == 450

    async def test_daily_cost_total_empty_servers_is_zero(self, db: AsyncMock) -> None:
        repo = SqlAlchemyAccrualPeriodRepository(lambda: db)  # type: ignore[arg-type]
        total = await repo.daily_cost_total(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC), frozenset()
        )
        assert total == 0

    async def test_daily_cost_total_with_servers(self, db: AsyncMock) -> None:
        db.scalar = AsyncMock(return_value=200)
        repo = SqlAlchemyAccrualPeriodRepository(lambda: db)  # type: ignore[arg-type]
        total = await repo.daily_cost_total(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            frozenset({uuid4()}),
        )
        assert total == 200

    async def test_advisory_lock_acquires_and_releases(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=MagicMock(scalar_one=lambda: True))
        lock = PostgresAdvisoryAccrualLock(lambda: db)  # type: ignore[arg-type]
        acquired = False
        async with lock.guard():
            acquired = True
        assert acquired
        assert db.execute.await_count >= 2  # acquire + unlock
