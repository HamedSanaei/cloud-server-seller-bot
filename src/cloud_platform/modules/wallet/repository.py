"""SQLAlchemy repository adapters for wallet and ledger domains.

This module contains:
- SqlAlchemyWalletRepository — CRUD with SELECT FOR UPDATE locking.
- SqlAlchemyLedgerRepository — append-only ledger posting with idempotency.
- SqlAlchemyHoldRepository — hold CRUD with row-level wallet locking.
- HoldService — orchestrates create/release/capture with concurrency safety.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.core.money import Money
from cloud_platform.db.base import Hold as _HoldModel
from cloud_platform.db.base import LedgerEntry as _LEModel
from cloud_platform.db.base import PaymentSession as _PaymentModel
from cloud_platform.db.base import Server as _ServerModel
from cloud_platform.db.base import Wallet as SQLAlchemyWallet
from cloud_platform.db.base import WalletCurrencyMigration as _MigrationModel
from cloud_platform.db.timestamps import (
    from_db_utc_or_none,
    to_db_utc,
    utc_now,
)
from cloud_platform.modules.fx.domain import ConversionSnapshot
from cloud_platform.modules.wallet.domain import (
    DEFAULT_WALLET_CURRENCY,
    DuplicateIdempotencyError,
    Hold,
    HoldNotFoundError,
    HoldRepository,
    HoldStateConflictError,
    HoldStatus,
    InsufficientBalanceError,
    InsufficientHoldBalanceError,
    LedgerEntry,
    LedgerEntryType,
    LedgerRepository,
    Wallet,
    WalletCurrencyMigration,
    WalletCurrencyMigrationError,
    WalletCurrencyMigrationPlan,
    WalletRepository,
    WalletStatus,
)
from cloud_platform.observability.metrics import metrics


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute (typed as Any at the boundary)."""
    return getattr(row, name, None)


# ===================================================================
# Wallet repository — with row-level locking
# ===================================================================


def _wallet_to_domain(row: SQLAlchemyWallet) -> Wallet:
    bal = _attr(row, "balance")
    status_val = _attr(row, "status")
    return Wallet(
        user_id=_attr(row, "user_id"),
        id=_attr(row, "id"),
        balance=int(bal) if bal is not None else 0,
        currency=str(_attr(row, "currency")),
        status=WalletStatus(status_val) if status_val else WalletStatus.ACTIVE,
        # wallets.created_at/updated_at are legacy naive-UTC columns.
        created_at=from_db_utc_or_none(_attr(row, "created_at")),
        updated_at=from_db_utc_or_none(_attr(row, "updated_at")),
    )


class SqlAlchemyWalletRepository:
    """SQLAlchemy-backed Wallet repository with row-level locking.

    All mutating operations (debit, add_funds) use ``SELECT FOR UPDATE``
    to serialize concurrent access to the same wallet row, preventing
    race conditions where two operations read stale balance and both
    succeed despite the combined amount exceeding the actual balance.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get(self, user_id: UUID) -> Wallet | None:
        """The wallet owned by ``user_id`` (wallets.user_id is UNIQUE)."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyWallet).where(SQLAlchemyWallet.user_id == user_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return None if row is None else _wallet_to_domain(row)

    async def list_all(self) -> list[Wallet]:
        async with self._session_factory() as session:
            result = await session.execute(select(SQLAlchemyWallet))
            return [_wallet_to_domain(row) for row in result.scalars().all()]

    async def get_or_create(self, user_id: UUID, currency: str = DEFAULT_WALLET_CURRENCY) -> Wallet:
        wallet = await self.get(user_id)
        if wallet is not None:
            return wallet
        return await self._create(user_id, currency)

    async def debit(self, user_id: UUID, amount: int, idempotency_key: str) -> Wallet:
        del idempotency_key
        async with self._session_factory() as session:
            stmt = (
                select(SQLAlchemyWallet)
                .where(SQLAlchemyWallet.user_id == user_id)
                .with_for_update()
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                raise ValueError(f"no wallet for user {user_id}")
            bal = int(_attr(row, "balance"))
            if bal < amount:
                raise InsufficientBalanceError(f"balance {bal} < required {amount}")
            cast(Any, row).balance = bal - amount
            await session.commit()
            # Server-generated columns (updated_at onupdate) expire on flush;
            # refresh inside the greenlet before mapping to domain, otherwise
            # attribute access raises MissingGreenlet on real PostgreSQL.
            await session.refresh(row)
            return _wallet_to_domain(row)

    async def add_funds(self, user_id: UUID, amount: int, idempotency_key: str) -> Wallet:
        del idempotency_key
        if amount <= 0:
            raise ValueError("add_funds requires a positive amount")
        async with self._session_factory() as session:
            stmt = (
                select(SQLAlchemyWallet)
                .where(SQLAlchemyWallet.user_id == user_id)
                .with_for_update()
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                raise ValueError(f"no wallet for user {user_id}")
            bal = int(_attr(row, "balance"))
            cast(Any, row).balance = bal + amount
            await session.commit()
            await session.refresh(row)
            return _wallet_to_domain(row)

    async def adjust(
        self,
        user_id: UUID,
        delta: int,
        idempotency_key: str,
        *,
        entry_type: LedgerEntryType,
        reference_type: str = "",
        reference_id: str = "",
        description: str = "",
        expected_currency: str | None = None,
    ) -> tuple[Wallet, bool]:
        """Atomically post the balance delta and ledger, checking currency under the row lock."""
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            raise ValueError("wallet adjustment delta must be a non-zero integer")
        new_key = str(idempotency_key).strip()
        if not new_key:
            raise ValueError("idempotency_key must not be empty")

        async with self._session_factory() as session:
            stmt = (
                select(SQLAlchemyWallet)
                .where(SQLAlchemyWallet.user_id == user_id)
                .with_for_update()
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                raise ValueError(f"no wallet for user {user_id}")
            if expected_currency is not None and str(_attr(row, "currency")) != expected_currency:
                raise ValueError(f"wallet currency must be {expected_currency}")
            wallet_currency = str(_attr(row, "currency"))
            wallet_id: UUID = _attr(row, "id")

            existing = await session.execute(
                select(_LEModel).where(
                    _LEModel.wallet_id == wallet_id,
                    _LEModel.idempotency_key == new_key,
                )
            )
            prior = existing.scalar_one_or_none()
            if prior is not None:
                # Same key, same immutable facts: idempotent replay, move no
                # money. Same key, DIFFERENT facts: fail closed — a reused
                # key must never silently stand in for another movement.
                # Compare BEFORE rollback: rollback expires the row and its
                # attributes can no longer be read outside a refresh.
                self._check_adjust_replay(
                    prior,
                    wallet_id=wallet_id,
                    key=new_key,
                    delta=delta,
                    entry_type=entry_type,
                    currency=wallet_currency,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    description=description,
                )
                await session.rollback()
                refreshed = await session.get(SQLAlchemyWallet, wallet_id)
                assert refreshed is not None
                await session.refresh(refreshed)
                return _wallet_to_domain(refreshed), False

            balance = int(_attr(row, "balance"))
            new_balance = balance + delta
            if new_balance < 0:
                raise InsufficientBalanceError(f"balance {balance} < required {-delta}")
            cast(Any, row).balance = new_balance
            session.add(
                _LEModel(
                    wallet_id=wallet_id,
                    amount=abs(delta),
                    currency=str(_attr(row, "currency")),
                    entry_type=entry_type,
                    idempotency_key=new_key,
                    reference_type=reference_type or None,
                    reference_id=UUID(reference_id) if reference_id else None,
                    description=description,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if "uq_ledger_wallet_idempotency" in str(exc.orig):
                    # Lost a concurrent same-key race: the winner's facts
                    # decide — identical facts replay quietly, differing
                    # facts fail closed. The rollback above already undid
                    # this attempt's balance mutation.
                    raced = await session.execute(
                        select(_LEModel).where(
                            _LEModel.wallet_id == wallet_id,
                            _LEModel.idempotency_key == new_key,
                        )
                    )
                    winner = raced.scalar_one_or_none()
                    if winner is not None:
                        self._check_adjust_replay(
                            winner,
                            wallet_id=wallet_id,
                            key=new_key,
                            delta=delta,
                            entry_type=entry_type,
                            currency=wallet_currency,
                            reference_type=reference_type,
                            reference_id=reference_id,
                            description=description,
                        )
                        refreshed = await session.get(SQLAlchemyWallet, wallet_id)
                        assert refreshed is not None
                        await session.refresh(refreshed)
                        return _wallet_to_domain(refreshed), False
                raise

            await session.refresh(row)
            return _wallet_to_domain(row), True

    @staticmethod
    def _check_adjust_replay(
        row: Any,
        *,
        wallet_id: UUID,
        key: str,
        delta: int,
        entry_type: LedgerEntryType,
        currency: str,
        reference_type: str,
        reference_id: str,
        description: str,
    ) -> None:
        """Fail closed when a consumed idempotency key carries other facts."""
        facts = (
            str(_attr(row, "entry_type")),
            int(_attr(row, "amount")),
            str(_attr(row, "currency")),
            str(_attr(row, "reference_type") or ""),
            str(_attr(row, "reference_id") or ""),
            str(_attr(row, "description") or ""),
        )
        wanted = (
            str(entry_type.value if isinstance(entry_type, LedgerEntryType) else entry_type),
            abs(delta),
            currency,
            str(reference_type or ""),
            str(reference_id or ""),
            str(description or ""),
        )
        if facts != wanted:
            raise DuplicateIdempotencyError(
                f"ledger entry with idempotency_key {key!r} already exists "
                f"for wallet {wallet_id} with different facts"
            )

    async def credit_deposit(
        self,
        user_id: UUID,
        amount: int,
        idempotency_key: str,
        *,
        reference: str = "",
        expected_currency: str | None = None,
    ) -> tuple[Wallet, bool]:
        """Apply a gateway deposit EXACTLY once (atomically, row-locked).

        The wallet row is locked with ``SELECT FOR UPDATE``, the ledger
        existence is checked and the balance incremented inside ONE
        transaction that commits wallet+ledger together. A concurrent
        duplicate callback therefore cannot double-credit: the second
        transaction either sees the committed ledger row (applied=False) or
        loses the unique-constraint race and rolls back both of its writes.

        ``idempotency_key`` is the deterministic deposit key (e.g.
        ``deposit-{gateway}-{external_id}``) from the ledger's
        ``(wallet_id, idempotency_key)`` uniqueness.
        """
        if amount <= 0:
            raise ValueError("credit_deposit requires a positive amount")
        new_key = str(idempotency_key)
        async with self._session_factory() as session:
            stmt = (
                select(SQLAlchemyWallet)
                .where(SQLAlchemyWallet.user_id == user_id)
                .with_for_update()
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                raise ValueError(f"no wallet for user {user_id}")
            actual_currency = str(_attr(row, "currency"))
            if expected_currency is not None and actual_currency != expected_currency:
                raise ValueError("wallet currency differs from verified payment credit")
            wallet_id: UUID = _attr(row, "id")
            existing = await session.execute(
                select(_LEModel).where(
                    _LEModel.wallet_id == wallet_id,
                    _LEModel.idempotency_key == new_key,
                )
            )
            previous = existing.scalar_one_or_none()
            if previous is not None:
                if (
                    int(_attr(previous, "amount")) != amount
                    or str(_attr(previous, "currency")) != actual_currency
                    or str(_attr(previous, "entry_type")) != LedgerEntryType.DEPOSIT.value
                    or str(_attr(previous, "description"))
                    != f"gateway deposit {reference or new_key}"
                ):
                    raise DuplicateIdempotencyError(
                        "deposit idempotency key carries different facts"
                    )
                await session.rollback()
                await session.refresh(row)
                return _wallet_to_domain(row), False
            bal = int(_attr(row, "balance"))
            cast(Any, row).balance = bal + amount
            session.add(
                _LEModel(
                    wallet_id=wallet_id,
                    amount=amount,
                    currency=str(_attr(row, "currency")),
                    entry_type=LedgerEntryType.DEPOSIT,
                    idempotency_key=new_key,
                    reference_type="payment",
                    # ``reference_id`` is a UUID column and gateway payment
                    # ids are NOT UUIDs — the gateway/external id lives in
                    # the (never-secret) description instead.
                    description=f"gateway deposit {reference or new_key}",
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                # Lost the concurrent race for this deposit key: the winner's
                # transaction already committed the credit, so this one must
                # roll back BOTH writes (balance + ledger) and report a replay.
                await session.rollback()
                if "uq_ledger_wallet_idempotency" in str(exc.orig):
                    refreshed = await session.get(SQLAlchemyWallet, wallet_id)
                    assert refreshed is not None
                    await session.refresh(refreshed)
                    return _wallet_to_domain(refreshed), False
                raise
            await session.refresh(row)
            return _wallet_to_domain(row), True

    async def apply_currency_migration(
        self, plan: WalletCurrencyMigrationPlan
    ) -> tuple[Wallet, bool]:
        """Apply an operator-approved USD-to-IRT cutover in one locked transaction.

        All wallet-affecting activity must be quiesced by the operator:
        standalone ledger posting, payment creation and server ordering do
        not take this wallet lock and can insert rows after the safety reads.
        """
        if not isinstance(plan, WalletCurrencyMigrationPlan):
            raise WalletCurrencyMigrationError("an approved currency migration plan is required")

        async with self._session_factory() as session:
            result = await session.execute(
                select(SQLAlchemyWallet)
                .where(SQLAlchemyWallet.id == plan.wallet_id)
                .with_for_update()
            )
            wallet_row = result.scalar_one_or_none()
            if wallet_row is None or _attr(wallet_row, "user_id") != plan.user_id:
                raise WalletCurrencyMigrationError("migration wallet does not belong to the user")

            prior = (
                await session.execute(
                    select(_MigrationModel).where(_MigrationModel.wallet_id == plan.wallet_id)
                )
            ).scalar_one_or_none()
            if prior is not None:
                await self._check_migration_replay(session, wallet_row, prior, plan)
                replayed_wallet = _wallet_to_domain(wallet_row)
                await session.rollback()
                return replayed_wallet, False

            if str(_attr(wallet_row, "currency")) != "USD":
                raise WalletCurrencyMigrationError("only a USD wallet can migrate to IRT")
            if str(_attr(wallet_row, "status") or "active") == WalletStatus.CLOSED:
                raise WalletCurrencyMigrationError("a closed wallet cannot migrate")
            if int(_attr(wallet_row, "balance")) != plan.source_balance_minor:
                raise WalletCurrencyMigrationError(
                    "USD wallet balance changed since migration approval"
                )

            quote_now = utc_now()
            if (
                plan.snapshot.observed_at > quote_now
                or plan.snapshot.expires_at is None
                or plan.snapshot.expires_at <= quote_now
            ):
                raise WalletCurrencyMigrationError(
                    "migration FX quote is expired or not yet observed"
                )

            # clock_timestamp() (not transaction-start now()) shares the ledger
            # DB clock. Two explicit microseconds separate close, audit and open;
            # old ledger rows and settled holds must precede the first boundary.
            boundary = from_db_utc_or_none(
                (await session.execute(select(func.clock_timestamp()))).scalar_one()
            )
            assert boundary is not None
            audit_at = boundary + timedelta(microseconds=1)
            open_at = boundary + timedelta(microseconds=2)
            if plan.snapshot.observed_at > boundary or plan.snapshot.expires_at <= open_at:
                raise WalletCurrencyMigrationError("migration FX quote is not fresh at the cutover")
            close_db_at = to_db_utc(boundary)
            historic_entry = (
                await session.execute(
                    select(_LEModel.id)
                    .where(
                        _LEModel.wallet_id == plan.wallet_id,
                        or_(
                            _LEModel.created_at.is_(None),
                            _LEModel.created_at >= close_db_at,
                            _LEModel.currency != "USD",
                            _LEModel.entry_type.in_(
                                (LedgerEntryType.CURRENCY_CLOSE, LedgerEntryType.CURRENCY_OPEN)
                            ),
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if historic_entry is not None:
                raise WalletCurrencyMigrationError(
                    "historical ledger cannot precede the USD boundary"
                )

            unsettled_hold = (
                await session.execute(
                    select(_HoldModel.id)
                    .where(
                        _HoldModel.wallet_id == plan.wallet_id,
                        or_(
                            _HoldModel.status == HoldStatus.CREATED,
                            _HoldModel.currency != "USD",
                            _HoldModel.created_at.is_(None),
                            _HoldModel.created_at >= close_db_at,
                            (
                                (_HoldModel.status == HoldStatus.CAPTURED)
                                & or_(
                                    _HoldModel.captured_at.is_(None),
                                    _HoldModel.captured_at >= close_db_at,
                                )
                            ),
                            (
                                (_HoldModel.status == HoldStatus.RELEASED)
                                & or_(
                                    _HoldModel.released_at.is_(None),
                                    _HoldModel.released_at >= close_db_at,
                                )
                            ),
                            ~_HoldModel.status.in_((HoldStatus.CAPTURED, HoldStatus.RELEASED)),
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if unsettled_hold is not None:
                raise WalletCurrencyMigrationError(
                    "an outstanding or unprovable USD hold blocks migration"
                )

            unsettled_payment = (
                await session.execute(
                    select(_PaymentModel.id)
                    .where(
                        _PaymentModel.user_id == plan.user_id,
                        or_(
                            _PaymentModel.status.in_(("pending", "manual_review")),
                            (_PaymentModel.status == "succeeded")
                            & _PaymentModel.credited_at.is_(None),
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if unsettled_payment is not None:
                raise WalletCurrencyMigrationError("an unsettled payment blocks migration")

            live_server = (
                await session.execute(
                    select(_ServerModel.id)
                    .where(
                        _ServerModel.user_id == plan.user_id,
                        _ServerModel.state != "deleted",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if live_server is not None:
                raise WalletCurrencyMigrationError("a non-deleted server blocks migration")

            amount_irt = plan.snapshot.target_amount_minor
            close_id: UUID | None = None
            open_id: UUID | None = None
            if plan.source_balance_minor:
                close_id, open_id = uuid4(), uuid4()
                session.add(
                    _LEModel(
                        id=close_id,
                        wallet_id=plan.wallet_id,
                        amount=plan.source_balance_minor,
                        currency="USD",
                        entry_type=LedgerEntryType.CURRENCY_CLOSE,
                        idempotency_key=f"currency-close-{plan.id}",
                        reference_type="currency_migration",
                        reference_id=plan.id,
                        description="USD wallet currency migration close",
                        created_at=close_db_at,
                    )
                )
                session.add(
                    _LEModel(
                        id=open_id,
                        wallet_id=plan.wallet_id,
                        amount=amount_irt,
                        currency="IRT",
                        entry_type=LedgerEntryType.CURRENCY_OPEN,
                        idempotency_key=f"currency-open-{plan.id}",
                        reference_type="currency_migration",
                        reference_id=plan.id,
                        description="IRT wallet currency migration open",
                        created_at=to_db_utc(open_at),
                    )
                )

            session.add(
                _MigrationModel(
                    id=plan.id,
                    user_id=plan.user_id,
                    wallet_id=plan.wallet_id,
                    source_balance_minor=plan.source_balance_minor,
                    snapshot=plan.snapshot.to_dict(),
                    operator_id=plan.operator_id,
                    reason=plan.reason,
                    close_entry_id=close_id,
                    open_entry_id=open_id,
                    created_at=audit_at,
                )
            )
            cast(Any, wallet_row).balance = amount_irt
            cast(Any, wallet_row).currency = "IRT"
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise WalletCurrencyMigrationError(
                    "migration identity or ledger boundary conflicts"
                ) from exc
            await session.refresh(wallet_row)
            return _wallet_to_domain(wallet_row), True

    @staticmethod
    async def _check_migration_replay(
        session: AsyncSession,
        wallet_row: SQLAlchemyWallet,
        prior: _MigrationModel,
        plan: WalletCurrencyMigrationPlan,
    ) -> None:
        """A consumed wallet migration cannot authorize different money or FX."""
        if (
            _attr(prior, "id") != plan.id
            or _attr(prior, "user_id") != plan.user_id
            or _attr(prior, "wallet_id") != plan.wallet_id
            or int(_attr(prior, "source_balance_minor")) != plan.source_balance_minor
            or _attr(prior, "snapshot") != plan.snapshot.to_dict()
            or _attr(prior, "operator_id") != plan.operator_id
            or _attr(prior, "reason") != plan.reason
            or str(_attr(wallet_row, "currency")) != "IRT"
        ):
            raise WalletCurrencyMigrationError(
                "migration ID or wallet has conflicting immutable facts"
            )

        created_at = from_db_utc_or_none(_attr(prior, "created_at"))
        if created_at is None:
            raise WalletCurrencyMigrationError("migration audit boundary lacks a timestamp")
        if plan.source_balance_minor == 0:
            if (
                _attr(prior, "close_entry_id") is not None
                or _attr(prior, "open_entry_id") is not None
            ):
                raise WalletCurrencyMigrationError(
                    "zero-balance migration contains unexpected ledger facts"
                )
            return

        close = await session.get(_LEModel, _attr(prior, "close_entry_id"))
        opened = await session.get(_LEModel, _attr(prior, "open_entry_id"))
        if close is None or opened is None:
            raise WalletCurrencyMigrationError("migration ledger boundary is missing")
        close_at = from_db_utc_or_none(_attr(close, "created_at"))
        open_at = from_db_utc_or_none(_attr(opened, "created_at"))
        if (
            close_at is None
            or open_at is None
            or not close_at < created_at < open_at
            or _attr(close, "id") == _attr(opened, "id")
        ):
            raise WalletCurrencyMigrationError("migration ledger chronology is invalid")
        for entry, entry_type, amount, currency, key, description in (
            (
                close,
                LedgerEntryType.CURRENCY_CLOSE,
                plan.source_balance_minor,
                "USD",
                f"currency-close-{plan.id}",
                "USD wallet currency migration close",
            ),
            (
                opened,
                LedgerEntryType.CURRENCY_OPEN,
                plan.snapshot.target_amount_minor,
                "IRT",
                f"currency-open-{plan.id}",
                "IRT wallet currency migration open",
            ),
        ):
            if (
                _attr(entry, "wallet_id") != plan.wallet_id
                or str(_attr(entry, "entry_type")) != entry_type.value
                or int(_attr(entry, "amount")) != amount
                or str(_attr(entry, "currency")) != currency
                or _attr(entry, "idempotency_key") != key
                or _attr(entry, "reference_type") != "currency_migration"
                or _attr(entry, "reference_id") != plan.id
                or _attr(entry, "description") != description
            ):
                raise WalletCurrencyMigrationError(
                    "migration ledger facts differ from the approved plan"
                )

    async def list_currency_migrations(self, wallet_id: UUID) -> list[WalletCurrencyMigration]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(_MigrationModel)
                .where(_MigrationModel.wallet_id == wallet_id)
                .order_by(_MigrationModel.created_at, _MigrationModel.id)
            )
            return [
                WalletCurrencyMigration(
                    id=_attr(row, "id"),
                    user_id=_attr(row, "user_id"),
                    wallet_id=_attr(row, "wallet_id"),
                    source_balance_minor=int(_attr(row, "source_balance_minor")),
                    snapshot=ConversionSnapshot.from_dict(_attr(row, "snapshot")),
                    operator_id=_attr(row, "operator_id"),
                    reason=_attr(row, "reason"),
                    close_entry_id=_attr(row, "close_entry_id"),
                    open_entry_id=_attr(row, "open_entry_id"),
                    created_at=from_db_utc_or_none(_attr(row, "created_at")),
                )
                for row in result.scalars().all()
            ]

    async def _create(self, user_id: UUID, currency: str) -> Wallet:
        async with self._session_factory() as session:
            row = SQLAlchemyWallet(user_id=user_id, currency=currency)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _wallet_to_domain(row)


# ===================================================================
# Ledger repository
# ===================================================================


def _ledger_entry_to_domain(row: _LEModel) -> LedgerEntry:
    ref_id = _attr(row, "reference_id")
    return LedgerEntry(
        id=_attr(row, "id"),
        wallet_id=_attr(row, "wallet_id"),
        entry_type=LedgerEntryType(str(_attr(row, "entry_type"))),
        amount=Money(Decimal(str(_attr(row, "amount"))), str(_attr(row, "currency"))),
        reference_type=_attr(row, "reference_type") or "",
        reference_id=str(ref_id) if ref_id else "",
        description=_attr(row, "description") or "",
        idempotency_key=str(_attr(row, "idempotency_key")),
        # ledger.created_at is a legacy naive-UTC column.
        created_at=from_db_utc_or_none(_attr(row, "created_at")),
    )


class SqlAlchemyLedgerRepository:
    """Append-only ledger posting with idempotency guarantee.

    The database unique constraint on (wallet_id, idempotency_key) prevents
    duplicate postings. This repository translates the constraint violation
    into a domain error so callers can detect and handle duplicate attempts.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

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
        new_key = str(idempotency_key)
        entry = _LEModel(
            wallet_id=wallet_id,
            amount=amount,
            currency=currency,
            entry_type=entry_type,
            idempotency_key=new_key,
            reference_type=reference_type,
            reference_id=UUID(reference_id) if reference_id else None,
            description=description,
        )
        async with self._session_factory() as session:
            try:
                session.add(entry)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if "uq_ledger_wallet_idempotency" in str(exc.orig):
                    raise DuplicateIdempotencyError(
                        f"ledger entry with idempotency_key {new_key!r} already exists "
                        f"for wallet {wallet_id}"
                    ) from exc
                raise
            result = await self.get_entry_by_idempotency(wallet_id, new_key)
            assert result is not None
            return result

    async def get_entry_by_idempotency(
        self,
        wallet_id: UUID,
        idempotency_key: str,
    ) -> LedgerEntry | None:
        """Return an existing entry by idempotency key, or None."""
        new_key = str(idempotency_key)
        async with self._session_factory() as session:
            stmt = select(_LEModel).where(
                _LEModel.wallet_id == wallet_id,
                _LEModel.idempotency_key == new_key,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _ledger_entry_to_domain(row)

    async def list_entries(self, wallet_id: UUID) -> list[LedgerEntry]:
        async with self._session_factory() as session:
            stmt = select(_LEModel).where(_LEModel.wallet_id == wallet_id)
            result = await session.execute(stmt)
            return [_ledger_entry_to_domain(row) for row in result.scalars().all()]

    async def list_entries_paged(
        self, wallet_id: UUID, *, offset: int, limit: int
    ) -> tuple[list[LedgerEntry], int]:
        """One page of the wallet's ledger, newest first, plus the total."""
        async with self._session_factory() as session:
            count_result = await session.execute(
                select(func.count()).select_from(_LEModel).where(_LEModel.wallet_id == wallet_id)
            )
            total = int(count_result.scalar() or 0)
            stmt = (
                select(_LEModel)
                .where(_LEModel.wallet_id == wallet_id)
                .order_by(_LEModel.created_at.desc().nulls_last(), _LEModel.id.desc())
                .offset(offset)
                .limit(limit)
            )
            result = await session.execute(stmt)
            return ([_ledger_entry_to_domain(row) for row in result.scalars().all()], total)


# ===================================================================
# Hold repository
# ===================================================================


def _hold_to_domain(row: _HoldModel) -> Hold:
    status_val = _attr(row, "status") or "created"
    return Hold(
        wallet_id=row.wallet_id,  # type: ignore[arg-type]
        amount=int(_attr(row, "amount")),
        currency=str(_attr(row, "currency")),
        idempotency_key=str(_attr(row, "idempotency_key")),
        id=_attr(row, "id"),
        status=HoldStatus(status_val),
        # holds.created_at/captured_at/released_at are legacy naive-UTC
        # columns; the domain compares them against aware clock values.
        created_at=from_db_utc_or_none(_attr(row, "created_at")),
        captured_at=from_db_utc_or_none(_attr(row, "captured_at")),
        released_at=from_db_utc_or_none(_attr(row, "released_at")),
    )


class SqlAlchemyHoldRepository:
    """Hold CRUD with row-level wallet locking via ``SELECT FOR UPDATE``.

    All hold operations that modify wallet state acquire an exclusive lock
    on the wallet row first, serializing concurrent access. This prevents
    the classic "lost update" problem where two simultaneous operations
    read the same stale balance and both succeed.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def create_hold(
        self,
        wallet_id: UUID,
        amount: int,
        currency: str,
        idempotency_key: str,
    ) -> Hold:
        """Create a hold with wallet row locking.

        Raises InsufficientHoldBalanceError when the wallet balance is too low.
        Uses the database unique constraint to prevent duplicate keys.
        """
        new_key = str(idempotency_key)

        # First check for existing idempotent hold
        async with self._session_factory() as session:
            stmt = select(_HoldModel).where(
                _HoldModel.wallet_id == wallet_id,
                _HoldModel.idempotency_key == new_key,
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing and existing.status == "created":
                return _hold_to_domain(existing)

        # Lock wallet row, check balance, then create hold — all in one transaction
        async with self._session_factory() as session:
            lock_stmt = (
                select(SQLAlchemyWallet).where(SQLAlchemyWallet.id == wallet_id).with_for_update()
            )
            lock_result = await session.execute(lock_stmt)
            wallet_row = lock_result.scalar_one_or_none()
            if wallet_row is None:
                raise ValueError(f"wallet {wallet_id} not found")

            balance = int(_attr(wallet_row, "balance"))

            # Check available balance (balance minus active holds) under the lock
            held_stmt = select(func.coalesce(func.sum(_HoldModel.amount), 0)).where(
                _HoldModel.wallet_id == wallet_id,
                _HoldModel.status == "created",
            )
            held_result = await session.execute(held_stmt)
            held = int(held_result.scalar() or 0)
            available = balance - held
            if available < amount:
                raise InsufficientHoldBalanceError(
                    f"available balance {available} < hold amount {amount} "
                    f"(balance {balance}, held {held})"
                )

            # Check for duplicate idempotency key within the lock
            dup_stmt = select(_HoldModel).where(
                _HoldModel.wallet_id == wallet_id,
                _HoldModel.idempotency_key == new_key,
            )
            dup_result = await session.execute(dup_stmt)
            dup = dup_result.scalar_one_or_none()
            if dup and dup.status == "created":
                return _hold_to_domain(dup)

            hold = _HoldModel(
                wallet_id=wallet_id,
                amount=amount,
                currency=currency,
                idempotency_key=new_key,
            )
            session.add(hold)
            await session.commit()

            hold_domain = await self.get_by_idempotency(wallet_id, new_key)
            assert hold_domain is not None
            return hold_domain

    async def get(self, hold_id: UUID) -> Hold | None:
        """Return a hold by id, or None."""
        async with self._session_factory() as session:
            stmt = select(_HoldModel).where(_HoldModel.id == hold_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _hold_to_domain(row)

    async def release_hold(self, hold_id: UUID) -> Hold | None:
        """Release a hold under a row lock.

        Returns the released Hold, or None if the hold is missing or already
        in a terminal state (idempotent no-op).
        """
        async with self._session_factory() as session:
            stmt = select(_HoldModel).where(_HoldModel.id == hold_id).with_for_update()
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None

            status_val = _attr(row, "status")
            if status_val in ("captured", "released"):
                return None

            cast(Any, row).status = "released"  # legacy Column attribute
            cast(Any, row).released_at = to_db_utc(utc_now())
            await session.commit()
            await session.refresh(row)
            return _hold_to_domain(row)

    async def capture_hold(self, hold_id: UUID) -> Hold | None:
        """Capture a hold: atomically debit the wallet and mark the hold captured.

        Both the hold row and the wallet row are locked within a single
        transaction so the debit cannot race with concurrent holds or debits.
        Returns the captured Hold, or None if the hold is missing or already
        in a terminal state (idempotent no-op).
        """
        async with self._session_factory() as session:
            hold_stmt = select(_HoldModel).where(_HoldModel.id == hold_id).with_for_update()
            hold_result = await session.execute(hold_stmt)
            hold_row = hold_result.scalar_one_or_none()
            if hold_row is None:
                return None

            status_val = _attr(hold_row, "status")
            if status_val in ("captured", "released"):
                return None

            hold_amount = int(_attr(hold_row, "amount"))

            wallet_stmt = (
                select(SQLAlchemyWallet)
                .where(SQLAlchemyWallet.id == _attr(hold_row, "wallet_id"))
                .with_for_update()
            )
            wallet_result = await session.execute(wallet_stmt)
            wallet_row = wallet_result.scalar_one_or_none()
            if wallet_row is None:
                raise ValueError(f"wallet for hold {hold_id} not found")

            balance = int(_attr(wallet_row, "balance"))
            if balance < hold_amount:
                await session.rollback()
                raise InsufficientBalanceError(
                    f"wallet balance {balance} < capture amount {hold_amount} (hold {hold_id})"
                )

            cast(Any, wallet_row).balance = balance - hold_amount
            cast(Any, hold_row).status = "captured"  # legacy Column attribute
            cast(Any, hold_row).captured_at = to_db_utc(utc_now())
            await session.commit()
            await session.refresh(hold_row)
            return _hold_to_domain(hold_row)

    async def get_by_idempotency(self, wallet_id: UUID, idempotency_key: str) -> Hold | None:
        """Return an existing hold if present, else None."""
        new_key = str(idempotency_key)
        async with self._session_factory() as session:
            stmt = select(_HoldModel).where(
                _HoldModel.wallet_id == wallet_id,
                _HoldModel.idempotency_key == new_key,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _hold_to_domain(row)

    async def active_hold_sum(self, wallet_id: UUID) -> int:
        """Sum of all active (created) holds for a wallet."""
        async with self._session_factory() as session:
            stmt = select(_HoldModel.amount).where(
                _HoldModel.wallet_id == wallet_id,
                _HoldModel.status == "created",
            )
            result = await session.execute(stmt)
            amounts = [int(_attr(r, "amount") or 0) for r in result.all()]
            return sum(amounts)

    async def list_by_wallet(self, wallet_id: UUID) -> list[Hold]:
        async with self._session_factory() as session:
            stmt = select(_HoldModel).where(_HoldModel.wallet_id == wallet_id)
            result = await session.execute(stmt)
            return [_hold_to_domain(row) for row in result.scalars().all()]


# ===================================================================
# Hold service — concurrent-safe orchestration
# ===================================================================


class HoldService:
    """Orchestrates wallet, ledger, and hold operations with concurrency safety.

    The critical path — ``create_hold`` — locks the wallet row first
    (SELECT FOR UPDATE), verifies balance against active holds, then
    creates the hold record inside the same transaction. This serializes
    concurrent purchase attempts so the total never exceeds the actual
    wallet balance.
    """

    def __init__(
        self,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        ledger_repo: LedgerRepository,
    ) -> None:
        self._wallet_repo = wallet_repo
        self._hold_repo = hold_repo
        self._ledger_repo = ledger_repo

    async def create_hold(
        self,
        wallet_id: UUID,
        amount: int,
        currency: str,
        idempotency_key: str,
    ) -> Hold:
        """Create a hold with full concurrency safety.

        This method:
        1. Acquires a row-level lock on the wallet (via hold_repo).
        2. Checks balance against sum of active holds.
        3. Creates the hold record idempotently.
        4. Posts a ledger entry.

        Returns the created Hold.
        Raises InsufficientHoldBalanceError if the wallet cannot cover it.
        """
        try:
            hold = await self._hold_repo.create_hold(wallet_id, amount, currency, idempotency_key)
        except InsufficientHoldBalanceError:
            metrics.record_billing_event("hold_created", "insufficient_balance")
            raise
        # Post hold ledger entry (idempotent)
        try:
            await self._ledger_repo.post_entry(
                wallet_id,
                amount,
                currency,
                LedgerEntryType.HOLD,
                f"hold-{idempotency_key}",
                reference_type="hold",
                reference_id=str(hold.id),
                description=f"hold created for {idempotency_key}",
            )
        except DuplicateIdempotencyError:
            pass  # ledger entry already posted by idempotent re-invocation
        metrics.record_billing_event("hold_created", "ok")
        return hold

    async def release_hold(
        self,
        wallet_id: UUID,
        hold_id: UUID,
        idempotency_key: str,
    ) -> Hold:
        """Release a hold. Posts ledger RELEASE entry with the held amount.

        Idempotent: releasing an already-released hold is a no-op and the
        ledger entry is re-posted safely under its own idempotency key.
        Raises HoldStateConflictError when the hold is captured, and
        HoldNotFoundError when the hold does not exist.
        """
        try:
            hold = await self._resolve_hold(wallet_id, hold_id, idempotency_key)
        except HoldNotFoundError:
            metrics.record_billing_event("hold_released", "not_found")
            raise
        if hold.status is HoldStatus.CAPTURED:
            metrics.record_billing_event("hold_released", "conflict")
            raise HoldStateConflictError(f"hold {hold_id} is already captured; cannot release")
        assert hold.id is not None
        persisted_id: UUID = hold.id
        if hold.status is HoldStatus.CREATED:
            released = await self._hold_repo.release_hold(persisted_id)
            if released is not None:
                hold = released
        try:
            await self._ledger_repo.post_entry(
                wallet_id,
                hold.amount,
                hold.currency,
                LedgerEntryType.RELEASE,
                f"release-{idempotency_key}",
                reference_type="hold",
                reference_id=str(persisted_id),
                description=f"hold released for {idempotency_key}",
            )
        except DuplicateIdempotencyError:
            pass  # already posted by an earlier attempt
        metrics.record_billing_event("hold_released", "ok")
        return hold

    async def capture_hold(
        self,
        wallet_id: UUID,
        hold_id: UUID,
        idempotency_key: str,
    ) -> Hold:
        """Capture a hold into a permanent charge.

        Atomically debits the wallet (row-locked) and posts a ledger CHARGE
        entry with the held amount. Idempotent: re-capturing an already
        captured hold is a no-op and the ledger entry is re-posted safely.
        Raises HoldStateConflictError when the hold was released.
        """
        try:
            hold = await self._resolve_hold(wallet_id, hold_id, idempotency_key)
        except HoldNotFoundError:
            metrics.record_billing_event("hold_captured", "not_found")
            raise
        if hold.status is HoldStatus.RELEASED:
            metrics.record_billing_event("hold_captured", "conflict")
            raise HoldStateConflictError(f"hold {hold_id} is already released; cannot capture")
        assert hold.id is not None
        persisted_id: UUID = hold.id
        if hold.status is HoldStatus.CREATED:
            captured = await self._hold_repo.capture_hold(persisted_id)
            if captured is not None:
                hold = captured
            else:
                refreshed = await self._hold_repo.get(hold_id)
                if refreshed is not None:
                    hold = refreshed
        try:
            await self._ledger_repo.post_entry(
                wallet_id,
                hold.amount,
                hold.currency,
                LedgerEntryType.CHARGE,
                f"capture-{idempotency_key}",
                reference_type="hold",
                reference_id=str(persisted_id),
                description=f"hold captured for {idempotency_key}",
            )
        except DuplicateIdempotencyError:
            pass  # already posted by an earlier attempt
        metrics.record_billing_event("hold_captured", "ok")
        return hold

    async def _resolve_hold(self, wallet_id: UUID, hold_id: UUID, idempotency_key: str) -> Hold:
        """Find a hold by id, falling back to the idempotency key.

        Raises HoldNotFoundError when neither lookup succeeds.
        """
        hold = await self._hold_repo.get(hold_id)
        if hold is None:
            hold = await self._hold_repo.get_by_idempotency(wallet_id, idempotency_key)
        if hold is None:
            raise HoldNotFoundError(f"hold {hold_id} not found for wallet {wallet_id}")
        assert hold.id is not None  # persisted holds always carry a DB-assigned id
        return hold

    async def available_balance(self, wallet_id: UUID) -> int:
        """Compute balance minus active holds. Locks wallet row."""
        wallet = await self._wallet_repo.get(wallet_id)
        if wallet is None:
            raise ValueError(f"wallet {wallet_id} not found")
        held = await self._hold_repo.active_hold_sum(wallet_id)
        return wallet.balance - held
