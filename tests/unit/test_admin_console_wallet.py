"""Frozen Toman adjustments through the real privileged wallet service."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.core.money import Money
from cloud_platform.core.session_store import InMemoryBotSessionStore, SessionStoreUnavailable
from cloud_platform.modules.admin_console.wallet import AdminWalletAdjustmentService
from cloud_platform.modules.fx.domain import ConversionSnapshot, FxUnavailableError
from cloud_platform.modules.users.domain import PermissionDeniedError, Role, User, UserStatus
from cloud_platform.modules.wallet.domain import (
    InsufficientBalanceError,
    LedgerEntry,
    LedgerEntryType,
    Wallet,
)
from cloud_platform.modules.wallet.service import WalletAdminService


@pytest.fixture
def case():
    admin = User(uuid4(), "admin", "admin@example.test", role=Role.ADMIN, telegram_user_id=101)
    customer = User(uuid4(), "customer", "customer@example.test", telegram_user_id=210)
    rows = {user.id: user for user in (admin, customer)}
    users = SimpleNamespace(
        get=AsyncMock(side_effect=lambda uid: rows.get(uid)),
        get_by_telegram_user_id=AsyncMock(
            side_effect=lambda tid: next(
                (u for u in rows.values() if u.telegram_user_id == tid), None
            )
        ),
    )

    async def onboard(tid, _username, _email):
        return await users.get_by_telegram_user_id(tid)

    users.onboard_telegram = AsyncMock(side_effect=onboard)
    wallet = Wallet(customer.id, id=uuid4(), balance=5000, currency="IRT")
    entries = {}
    lock = asyncio.Lock()

    async def adjust(uid, amount, key, **facts):
        async with lock:
            assert uid == customer.id
            assert facts["audit_actor_id"] == admin.id
            assert facts["admin_configured_ids"] == (101,)
            if wallet.currency != facts["expected_currency"]:
                raise ValueError("wallet currency changed")
            if key not in entries:
                wallet.add_funds(amount) if amount > 0 else wallet.debit(-amount)
                entries[key] = LedgerEntry(
                    id=uuid4(),
                    wallet_id=wallet.id,
                    entry_type=LedgerEntryType.ADJUSTMENT,
                    amount=Money(Decimal(amount), wallet.currency),
                    reference_type=facts["reference_type"],
                    reference_id=facts["reference_id"],
                    idempotency_key=key,
                    description=facts["description"],
                )
            return wallet, False

    wallets = SimpleNamespace(
        get=AsyncMock(return_value=wallet),
        adjust=adjust,
        get_entry_by_idempotency=AsyncMock(side_effect=lambda _wid, key: entries.get(key)),
    )
    wallet_admin = WalletAdminService(wallets, wallets, user_repo=users, configured_ids=(101,))
    store = InMemoryBotSessionStore()
    fx = SimpleNamespace(snapshot=AsyncMock())
    service = AdminWalletAdjustmentService(
        users, wallets, wallet_admin, store, fx=fx, configured_ids=(101,)
    )
    return SimpleNamespace(
        admin=admin,
        customer=customer,
        rows=rows,
        users=users,
        wallet=wallet,
        wallets=wallets,
        entries=entries,
        store=store,
        fx=fx,
        service=service,
        wallet_admin=wallet_admin,
    )


@pytest.mark.parametrize("amount", [1200, -1200])
async def test_signed_toman_replay_has_one_immutable_actor_and_fx_receipt(case, amount):
    confirmation = await case.service.prepare(case.admin, 210, amount, "Support correction")
    assert case.wallet.balance == 5000
    await asyncio.gather(
        case.service.apply(case.admin, confirmation.nonce),
        case.service.apply(case.admin, confirmation.nonce),
    )
    assert case.wallet.balance == 5000 + amount
    (entry,) = case.entries.values()
    assert entry.reference_id == str(case.admin.id)
    assert entry.amount.amount == amount
    assert (
        "Support correction" in entry.description
        and '"source_currency": "IRT"' in entry.description
    )
    case.fx.snapshot.assert_not_awaited()


async def test_no_overdraft_or_partial_ledger(case):
    confirmation = await case.service.prepare(case.admin, 210, -5001, "Debit correction")
    with pytest.raises(InsufficientBalanceError):
        await case.service.apply(case.admin, confirmation.nonce)
    assert case.wallet.balance == 5000 and not case.entries


async def test_foreign_conversion_is_frozen_and_never_refetched(case):
    case.wallet.currency = "USD"
    now = datetime.now(UTC)

    async def snapshot(amount, source, target, purpose):
        return ConversionSnapshot(
            amount,
            source,
            100,
            target,
            Decimal("0.00001"),
            purpose,
            "fixture",
            "IRT/USD",
            now,
            expires_at=now + timedelta(seconds=60),
        )

    case.fx.snapshot.side_effect = snapshot
    confirmation = await case.service.prepare(case.admin, 210, 100000, "Foreign correction")
    case.fx.snapshot.side_effect = FxUnavailableError("offline after preview")
    await case.service.apply(case.admin, confirmation.nonce)
    assert case.wallet.balance == 5100
    assert case.fx.snapshot.await_count == 1


@pytest.mark.parametrize("change", ["currency", "role", "revoked", "canceled", "different_actor"])
async def test_confirmation_revalidates_target_actor_currency_and_cancel(case, change):
    confirmation = await case.service.prepare(case.admin, 210, 1000, "Correction")
    actor = case.admin
    if change == "currency":
        case.wallet.currency = "USD"
    elif change == "role":
        case.customer.role = Role.ADMIN
    elif change == "revoked":
        case.rows[case.admin.id] = replace(case.admin, status=UserStatus.FROZEN)
    elif change == "canceled":
        await case.service.clear(case.admin)
    else:
        actor = case.customer
    with pytest.raises((ValueError, PermissionDeniedError)):
        await case.service.apply(actor, confirmation.nonce)
    assert case.wallet.balance == 5000 and not case.entries


@pytest.mark.parametrize(
    "amount,reason", [(0, "reason"), (True, "reason"), (2**63, "reason"), (1, ""), (1, "x" * 501)]
)
async def test_invalid_adjustment_never_creates_confirmation(case, amount, reason):
    with pytest.raises(ValueError):
        await case.service.prepare(case.admin, 210, amount, reason)
    assert await case.store.get("admin-adjust-active", str(case.admin.id)) is None
    assert not case.entries


async def test_session_outage_cannot_apply(case, monkeypatch):
    confirmation = await case.service.prepare(case.admin, 210, 10, "Correction")
    monkeypatch.setattr(
        case.store, "get", AsyncMock(side_effect=SessionStoreUnavailable("offline"))
    )
    with pytest.raises(SessionStoreUnavailable):
        await case.service.apply(case.admin, confirmation.nonce)
    assert case.wallet.balance == 5000 and not case.entries
