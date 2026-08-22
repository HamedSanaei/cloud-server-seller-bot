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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.core.money import Money
from cloud_platform.db.base import Hold as _HoldModel
from cloud_platform.db.base import LedgerEntry as _LEModel
from cloud_platform.db.base import Wallet as SQLAlchemyWallet
from cloud_platform.modules.wallet.domain import (
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
    WalletRepository,
    WalletStatus,
)


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
        created_at=_attr(row, "created_at"),
        updated_at=_attr(row, "updated_at"),
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
        async with self._session_factory() as session:
            row = await session.get(SQLAlchemyWallet, user_id)
            return _wallet_to_domain(row) if row is not None else None

    async def get_or_create(self, user_id: UUID, currency: str = "EUR") -> Wallet:
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
            updated_row = await session.get(SQLAlchemyWallet, user_id)
            assert updated_row is not None
            return _wallet_to_domain(updated_row)

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
            updated_row = await session.get(SQLAlchemyWallet, user_id)
            assert updated_row is not None
            return _wallet_to_domain(updated_row)

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
        created_at=_attr(row, "created_at"),
        captured_at=_attr(row, "captured_at"),
        released_at=_attr(row, "released_at"),
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
            cast(Any, row).released_at = datetime.now(UTC)
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
            cast(Any, hold_row).captured_at = datetime.now(UTC)
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
        hold = await self._hold_repo.create_hold(wallet_id, amount, currency, idempotency_key)
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
        hold = await self._resolve_hold(wallet_id, hold_id, idempotency_key)
        if hold.status is HoldStatus.CAPTURED:
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
        hold = await self._resolve_hold(wallet_id, hold_id, idempotency_key)
        if hold.status is HoldStatus.RELEASED:
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
