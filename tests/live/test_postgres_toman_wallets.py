"""Isolated PostgreSQL proof for Toman signup and untouched historical wallets."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from cloud_platform.db import base as db
from cloud_platform.db.base import User as UserRow
from cloud_platform.db.base import Wallet as WalletRow
from cloud_platform.modules.users.repository import SqlAlchemyUserRepository
from cloud_platform.modules.wallet.domain import WalletCurrencyMigrationError
from cloud_platform.modules.wallet.toman_cutover import (
    SqlAlchemyEmptyTomanCutover,
    invoice_facts,
    invoice_fingerprint,
)
from tests.live.test_postgres_operator_cutover import TELEGRAM_ID, seed
from tests.live.test_postgres_operator_cutover import database as complete_database  # noqa: F401
from tests.live.test_postgres_superadmins import DB_URL
from tests.live.test_postgres_superadmins import database as database

pytestmark = pytest.mark.skipif(not DB_URL, reason="explicit scratch PostgreSQL required")


async def test_direct_database_insert_defaults_to_toman(database):
    identity = uuid4()
    async with database.begin() as session:
        session.add(
            UserRow(id=identity, username=identity.hex, email=f"{identity.hex}@example.test")
        )
    async with database.begin() as session:
        wallet = WalletRow(user_id=identity, balance=0)
        session.add(wallet)
        await session.flush()
        assert wallet.currency == "IRT" and wallet.balance == 0


async def test_trusted_signup_and_restart_keep_one_toman_wallet(database):
    users = SqlAlchemyUserRepository(database)
    first = await users.onboard_telegram(121212, "toman-customer", "toman@example.test")
    async with database() as session:
        wallet = (
            await session.execute(select(WalletRow).where(WalletRow.user_id == first.id))
        ).scalar_one()
        identity = wallet.id
        assert wallet.currency == "IRT" and wallet.balance == 0
    repeated = await users.onboard_telegram(121212, "changed-name", "other@example.test")
    async with database() as session:
        wallet = (
            await session.execute(select(WalletRow).where(WalletRow.user_id == first.id))
        ).scalar_one()
        assert repeated.id == first.id and wallet.id == identity
        assert wallet.currency == "IRT" and wallet.balance == 0


async def test_signup_cannot_relabel_a_historical_dollar_balance(database):
    users = SqlAlchemyUserRepository(database)
    identity = uuid4()
    async with database.begin() as session:
        session.add(
            UserRow(
                id=identity,
                username=identity.hex,
                email=f"{identity.hex}@example.test",
                telegram_user_id=343434,
            )
        )
    async with database.begin() as session:
        session.add(WalletRow(user_id=identity, balance=321, currency="USD"))
    await users.onboard_telegram(343434, "existing", "existing@example.test")
    async with database() as session:
        wallet = (
            await session.execute(select(WalletRow).where(WalletRow.user_id == identity))
        ).scalar_one()
        assert wallet.balance == 321 and wallet.currency == "USD"


@pytest.fixture
async def empty_cutover(complete_database):  # noqa: F811
    manifest = await seed(complete_database, count=1)
    wallet_id, invoice_id = uuid4(), uuid4()
    async with complete_database.begin() as session:
        session.add(
            db.Wallet(id=wallet_id, user_id=manifest.operator_id, balance=0, currency="USD")
        )
        invoice = db.PaymentSession(
            id=invoice_id,
            user_id=manifest.operator_id,
            gateway_key="fixture",
            gateway_payment_id="old-unpaid",
            amount_minor=100001,
            currency="IRT",
            credit_amount_minor=100,
            credit_currency="USD",
            idempotency_key="old-frozen-order",
            status="pending",
        )
        session.add(invoice)
        await session.flush()
        fingerprint = invoice_fingerprint(invoice)
        original = invoice_facts(invoice)
    service = SqlAlchemyEmptyTomanCutover(complete_database, configured_ids=(TELEGRAM_ID,))
    facts = dict(
        user_id=manifest.operator_id,
        wallet_id=wallet_id,
        invoice_id=invoice_id,
        actor_id=manifest.operator_id,
        expected_invoice_fingerprint=fingerprint,
        reason="Owner approved old invoice cancellation and Toman wallet",
        operation_key="reviewed-cutover-1",
    )
    return complete_database, service, facts, original


async def test_explicit_zero_cutover_cancels_once_and_preserves_original_invoice(empty_cutover):
    factory, service, facts, original = empty_cutover
    outcomes = await asyncio.gather(service.apply(**facts), service.apply(**facts))
    assert set(outcomes) == {True, False}
    async with factory() as session:
        wallet = await session.get(db.Wallet, facts["wallet_id"])
        invoice = await session.get(db.PaymentSession, facts["invoice_id"])
        assert wallet.currency == "IRT" and wallet.balance == 0
        assert invoice.status == "failed" and invoice.credited_at is None
        assert invoice_facts(invoice) == original
        assert await session.scalar(select(func.count()).select_from(db.LedgerEntry)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(db.AuditEvent)
                .where(db.AuditEvent.action == "wallet.empty_toman_cutover")
            )
            == 1
        )
    with pytest.raises(WalletCurrencyMigrationError, match="replay"):
        await service.apply(**{**facts, "reason": "changed approval"})
    async with factory.begin() as session:
        (await session.get(db.Wallet, facts["wallet_id"])).balance = 321
        session.add(
            db.LedgerEntry(
                wallet_id=facts["wallet_id"],
                amount=321,
                currency="IRT",
                entry_type="deposit",
                idempotency_key="later-toman-credit",
            )
        )
    assert await service.apply(**facts) is False
    async with factory() as session:
        assert (await session.get(db.Wallet, facts["wallet_id"])).balance == 321


@pytest.mark.parametrize("change", ["funded", "history", "paid", "other_pending", "changed_facts"])
async def test_zero_cutover_cannot_discard_money_or_obligations(empty_cutover, change):
    factory, service, facts, _ = empty_cutover
    async with factory.begin() as session:
        if change == "funded":
            (await session.get(db.Wallet, facts["wallet_id"])).balance = 1
        if change == "history":
            session.add(
                db.LedgerEntry(
                    wallet_id=facts["wallet_id"],
                    amount=1,
                    currency="USD",
                    entry_type="deposit",
                    idempotency_key="historical-credit",
                )
            )
        if change == "paid":
            (await session.get(db.PaymentSession, facts["invoice_id"])).status = "succeeded"
        if change == "other_pending":
            session.add(
                db.PaymentSession(
                    user_id=facts["user_id"],
                    gateway_key="fixture",
                    amount_minor=100,
                    currency="USD",
                    idempotency_key="other-obligation",
                    status="pending",
                )
            )
        if change == "changed_facts":
            facts = {**facts, "expected_invoice_fingerprint": "0" * 64}
    with pytest.raises(WalletCurrencyMigrationError):
        await service.apply(**facts)
    async with factory() as session:
        assert (await session.get(db.Wallet, facts["wallet_id"])).currency == "USD"


async def test_failed_cutover_audit_rolls_back_wallet_and_cancellation(empty_cutover):
    factory, service, facts, _ = empty_cutover
    async with factory.begin() as session:
        await session.execute(
            text(
                "CREATE FUNCTION reject_toman_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'test audit unavailable'; END $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER reject_toman_audit BEFORE INSERT ON audit_events FOR EACH ROW "
                "WHEN (NEW.action = 'wallet.empty_toman_cutover') "
                "EXECUTE FUNCTION reject_toman_audit()"
            )
        )
    with pytest.raises(DBAPIError):
        await service.apply(**facts)
    async with factory() as session:
        assert (await session.get(db.Wallet, facts["wallet_id"])).currency == "USD"
        assert (await session.get(db.PaymentSession, facts["invoice_id"])).status == "pending"


async def test_cancelled_old_callback_cannot_recreate_a_dollar_credit(empty_cutover):
    from cloud_platform.modules.payments.domain import InvalidPaymentSessionTransition
    from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
    from cloud_platform.modules.payments.service import PaymentWebhookService
    from cloud_platform.modules.wallet.repository import (
        SqlAlchemyLedgerRepository,
        SqlAlchemyWalletRepository,
    )

    factory, service, facts, _ = empty_cutover
    await service.apply(**facts)
    webhook = PaymentWebhookService(
        payments_repo=SqlAlchemyPaymentSessionRepository(factory),
        wallet_repo=SqlAlchemyWalletRepository(factory),
        ledger_repo=SqlAlchemyLedgerRepository(factory),
    )
    with pytest.raises(InvalidPaymentSessionTransition):
        await webhook.process_callback(
            gateway_key="fixture", external_id="old-unpaid", status="succeeded"
        )
    async with factory() as session:
        wallet = await session.get(db.Wallet, facts["wallet_id"])
        assert wallet.balance == 0 and wallet.currency == "IRT"


async def test_zero_cutover_rechecks_authorization(empty_cutover):
    from cloud_platform.modules.users.domain import PermissionDeniedError
    from cloud_platform.modules.users.superadmin_repository import SqlAlchemySuperadminRepository

    factory, service, facts, _ = empty_cutover
    await SqlAlchemySuperadminRepository(factory).reconcile([])
    with pytest.raises(PermissionDeniedError):
        await service.apply(**facts)


async def test_competing_payment_lock_cannot_deadlock_or_partially_cut_over(empty_cutover):
    factory, service, facts, _ = empty_cutover
    async with factory.begin() as competing:
        await competing.execute(
            select(db.PaymentSession)
            .where(db.PaymentSession.id == facts["invoice_id"])
            .with_for_update()
        )
        with pytest.raises(DBAPIError):
            await asyncio.wait_for(service.apply(**facts), timeout=5)
    async with factory() as session:
        assert (await session.get(db.Wallet, facts["wallet_id"])).currency == "USD"
        assert (await session.get(db.PaymentSession, facts["invoice_id"])).status == "pending"
