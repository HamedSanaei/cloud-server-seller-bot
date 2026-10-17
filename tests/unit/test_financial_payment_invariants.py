"""Execute payment recovery, immutable facts and wallet transactions against SQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import JSON, DefaultClause, MetaData, UniqueConstraint, create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cloud_platform.db.base import BusinessLogEvent as BusinessLogRow
from cloud_platform.db.base import LedgerEntry as LedgerRow
from cloud_platform.db.base import PaymentSession as PaymentRow
from cloud_platform.db.base import User as UserRow
from cloud_platform.db.base import Wallet as WalletRow
from cloud_platform.modules.payments.domain import (
    DuplicateExternalIdError,
    InvalidPaymentSessionTransition,
    PaymentSession,
    PaymentSessionStatus,
)
from cloud_platform.modules.payments.inquiry import PaymentInquiryService, verify_payment_session
from cloud_platform.modules.payments.reconcile import PaymentReconciliationService
from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
from cloud_platform.modules.payments.service import PaymentWebhookService, WebhookAction
from cloud_platform.modules.users.domain import PermissionDeniedError
from cloud_platform.modules.wallet.domain import DuplicateIdempotencyError
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository
from cloud_platform.providers.base import PaymentIntent, PaymentStatus
from cloud_platform.providers.errors import ProviderError

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def invoice(**changes):
    return replace(
        PaymentSession(
            id=uuid4(),
            user_id=uuid4(),
            gateway_key="zarinpal",
            gateway_payment_id="authority-one",
            amount_minor=100,
            currency="IRR",
            idempotency_key="financial-invoice-one",
            credit_amount_minor=10,
            credit_currency="IRT",
            fx_source="exact",
            fx_rate="10",
            fx_path="IRT->IRR",
            fx_observed_at=NOW,
        ),
        **changes,
    )


class SqlStore:
    """Adapt synchronous SQLite I/O, not the production financial algorithms.

    PostgreSQL-only JSON/default DDL is adapted on copies. SQLite does not
    prove PostgreSQL locking; stale aggregate tests execute the real guards,
    while the statement capture verifies that the production lock is requested.
    """

    def __init__(self):
        self.engine = create_engine("sqlite://")
        metadata = MetaData()
        self.tables = {}
        for model in (UserRow, WalletRow, LedgerRow, PaymentRow, BusinessLogRow):
            table = model.__table__.to_metadata(metadata)
            table.c.id.server_default = None
            for column in table.columns:
                default = column.server_default
                if default is not None and str(default.arg).strip().upper() == "CURRENT_TIMESTAMP":
                    column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
            if model is PaymentRow:
                table.c.payment_details.type = JSON()
            if model is BusinessLogRow:
                table.c.payload.type = JSON()
                table.c.payload.server_default = None
            if model is LedgerRow:
                table.append_constraint(
                    UniqueConstraint(
                        "wallet_id", "idempotency_key", name="uq_ledger_wallet_idempotency"
                    )
                )
            self.tables[model] = table
        metadata.create_all(self.engine)
        self.statements = []
        self.fail_next_commit = False
        self.payments = SqlAlchemyPaymentSessionRepository(self.session)
        self.wallets = SqlAlchemyWalletRepository(self.session)

    def session(self):
        store = self

        class SqlSession:
            def __init__(self):
                self.db = Session(store.engine, expire_on_commit=False)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                self.db.close()

            async def execute(self, statement):
                store.statements.append(statement)
                return self.db.execute(statement)

            def add(self, row):
                if row.id is None:
                    row.id = uuid4()
                self.db.add(row)

            async def get(self, model, key):
                return self.db.get(model, key)

            async def commit(self):
                if store.fail_next_commit:
                    store.fail_next_commit = False
                    raise IntegrityError(
                        "injected transaction failure", {}, Exception("disk failure")
                    )
                self.db.commit()

            async def rollback(self):
                self.db.rollback()

            async def refresh(self, row):
                self.db.refresh(row)

        return SqlSession()

    def seed(self, payment, *, balance=0, wallet_currency="IRT"):
        wallet_id = uuid4()
        values = {
            "id": payment.id,
            "user_id": payment.user_id,
            "gateway_key": payment.gateway_key,
            "gateway_payment_id": payment.gateway_payment_id,
            "amount_minor": payment.amount_minor,
            "currency": payment.currency,
            "idempotency_key": payment.idempotency_key,
            "status": payment.status.value,
            "credited_at": payment.credited_at.replace(tzinfo=None)
            if payment.credited_at
            else None,
            "created_at": (NOW - timedelta(hours=1)).replace(tzinfo=None),
            "updated_at": NOW.replace(tzinfo=None),
            "credit_amount_minor": payment.credit_amount_minor,
            "credit_currency": payment.credit_currency,
            "fx_source": payment.fx_source,
            "fx_rate": payment.fx_rate,
            "fx_path": payment.fx_path,
            "fx_observed_at": payment.fx_observed_at,
            "fx_proxy": payment.fx_proxy,
            "fx_proxy_asset": payment.fx_proxy_asset,
            "payment_details": payment.payment_details,
            "redirect_url": payment.redirect_url,
            "tracking_code": payment.tracking_code,
        }
        with self.engine.begin() as connection:
            connection.execute(self.tables[PaymentRow].insert(), values)
            connection.execute(
                self.tables[WalletRow].insert(),
                {
                    "id": wallet_id,
                    "user_id": payment.user_id,
                    "balance": balance,
                    "currency": wallet_currency,
                },
            )
        return wallet_id

    def ledger(self):
        with Session(self.engine) as db:
            return list(db.execute(select(LedgerRow)).scalars())

    def business_events(self, event_type=None):
        statement = select(BusinessLogRow)
        if event_type is not None:
            statement = statement.where(BusinessLogRow.event_type == event_type)
        with Session(self.engine) as db:
            return list(db.execute(statement).scalars())


@pytest.fixture
def sql_store():
    store = SqlStore()
    try:
        yield store
    finally:
        store.engine.dispose()


@pytest.mark.parametrize("method", ["with_gateway_payment_id", "mark_succeeded", "mark_failed"])
def test_external_binding_never_mutates_the_input(method):
    original = invoice(gateway_payment_id=None)
    transition = getattr(original, method)
    result = (
        transition("new-authority")
        if method == "with_gateway_payment_id"
        else transition(gateway_payment_id="new-authority")
    )
    assert original.gateway_payment_id is None
    assert original.status is PaymentSessionStatus.PENDING
    assert result.gateway_payment_id == "new-authority"
    assert result.effective_credit_amount == original.effective_credit_amount


@pytest.mark.parametrize("method", ["with_gateway_payment_id", "mark_succeeded", "mark_failed"])
def test_bound_invoice_cannot_be_reassigned(method):
    original = invoice()
    with pytest.raises(InvalidPaymentSessionTransition, match="already bound"):
        if method == "with_gateway_payment_id":
            original.with_gateway_payment_id("another-authority")
        else:
            getattr(original, method)(gateway_payment_id="another-authority")
    assert original.gateway_payment_id == "authority-one"


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": uuid4()},
        {"gateway_key": "atlaspay"},
        {"currency": "IRT"},
        {"idempotency_key": "another-merchant-reference"},
        {"credit_amount_minor": 11},
        {"credit_currency": "USD"},
        {"fx_source": "fresh-rate-source"},
        {"fx_rate": "11"},
        {"fx_path": "USD->IRR"},
        {"fx_observed_at": NOW + timedelta(seconds=1)},
        {"fx_proxy": True},
        {"fx_proxy_asset": "USDT"},
        {"gateway_payment_id": "another-authority"},
        {"amount_minor": 101},
        {"payment_details": {"tracking_code": "substituted-invoice"}},
        {"redirect_url": "https://example.test/another-invoice"},
        {"tracking_code": "substituted-tracking"},
    ],
)
async def test_repository_rejects_changed_financial_facts_without_a_write(sql_store, changes):
    original = invoice()
    sql_store.seed(original)
    saved = await sql_store.payments.get(original.id)
    with pytest.raises(ValueError, match="immutable"):
        await sql_store.payments.save(replace(saved, **changes))
    assert await sql_store.payments.get(original.id) == saved
    assert sql_store.ledger() == []
    assert (await sql_store.wallets.get(original.user_id)).balance == 0


@pytest.mark.parametrize(
    "winner_status", [PaymentSessionStatus.SUCCEEDED, PaymentSessionStatus.FAILED]
)
async def test_stale_competing_terminal_save_cannot_overwrite_winner(sql_store, winner_status):
    original = invoice()
    sql_store.seed(original)
    stale_success, stale_failure = await asyncio.gather(
        sql_store.payments.get(original.id), sql_store.payments.get(original.id)
    )
    success = stale_success.mark_succeeded(gateway_payment_id=original.gateway_payment_id)
    failure = stale_failure.mark_failed(gateway_payment_id=original.gateway_payment_id)
    winner, loser = (
        (success, failure)
        if winner_status is PaymentSessionStatus.SUCCEEDED
        else (failure, success)
    )
    await sql_store.payments.save(winner)
    with pytest.raises(InvalidPaymentSessionTransition, match="already terminal"):
        await sql_store.payments.save(loser)
    assert (await sql_store.payments.get(original.id)).status is winner_status
    assert any(statement._for_update_arg is not None for statement in sql_store.statements)


async def test_stale_success_save_never_clears_or_replaces_first_credit_marker(sql_store):
    original = invoice()
    sql_store.seed(original)
    pending = await sql_store.payments.get(original.id)
    success = pending.mark_succeeded(gateway_payment_id=original.gateway_payment_id)
    first = await sql_store.payments.save(success.mark_credited(at=NOW))
    assert await sql_store.payments.save(success) == first
    assert (
        await sql_store.payments.save(success.mark_credited(at=NOW + timedelta(hours=1))) == first
    )
    with pytest.raises(InvalidPaymentSessionTransition):
        await sql_store.payments.save(pending)


@pytest.mark.parametrize("repair", ["reconciliation", "owner_inquiry"])
async def test_crash_after_success_claim_recovers_frozen_credit_exactly_once(sql_store, repair):
    original = invoice()
    sql_store.seed(original)
    real_credit = sql_store.wallets.credit_deposit
    sql_store.wallets.credit_deposit = AsyncMock(side_effect=RuntimeError("crash before deposit"))
    webhook = PaymentWebhookService(sql_store.payments, sql_store.wallets, None)
    with pytest.raises(RuntimeError, match="before deposit"):
        await webhook.process_callback(
            gateway_key=original.gateway_key,
            external_id=original.gateway_payment_id,
            status="succeeded",
        )
    interrupted = await sql_store.payments.get(original.id)
    assert interrupted.status is PaymentSessionStatus.SUCCEEDED
    assert interrupted.credited_at is None
    assert sql_store.ledger() == []
    sql_store.wallets.credit_deposit = real_credit
    gateway = SimpleNamespace(
        key="zarinpal",
        verify_with_amount=AsyncMock(
            return_value=PaymentIntent("authority-one", PaymentStatus.SUCCEEDED, 100, "IRR")
        ),
    )
    if repair == "reconciliation":
        report = await PaymentReconciliationService(sql_store.payments, webhook, gateway).run(
            now=NOW
        )
        assert report.credited == 1 and report.errors == 0
        gateway.verify_with_amount.assert_awaited_once_with("authority-one", 100)
        assert await sql_store.payments.list_pending_before("zarinpal", NOW) == []
    else:
        inquiry = PaymentInquiryService(
            payments_repo=sql_store.payments, webhook_service=webhook, gateways={}
        )
        repaired = await inquiry.check_status(SimpleNamespace(id=original.user_id), original.id)
        assert repaired.credited_at is not None
        gateway.verify_with_amount.assert_not_awaited()
    replay = await webhook.process_callback(
        gateway_key=original.gateway_key,
        external_id=original.gateway_payment_id,
        status="succeeded",
    )
    assert replay.action is WebhookAction.DUPLICATE_IGNORED
    assert (await sql_store.wallets.get(original.user_id)).balance == 10
    entries = sql_store.ledger()
    assert len(entries) == 1 and entries[0].amount == 10 and entries[0].currency == "IRT"


async def test_success_failure_interleave_before_claim_moves_no_money(sql_store):
    original = invoice()
    sql_store.seed(original)
    webhook = PaymentWebhookService(sql_store.payments, sql_store.wallets, None)
    read_wallet = sql_store.wallets.get

    async def lose_success_race(owner):
        wallet = await read_wallet(owner)
        current = await sql_store.payments.get(original.id)
        await sql_store.payments.save(
            current.mark_failed(gateway_payment_id=original.gateway_payment_id)
        )
        return wallet

    sql_store.wallets.get = lose_success_race
    with pytest.raises(InvalidPaymentSessionTransition):
        await webhook.process_callback(
            gateway_key=original.gateway_key,
            external_id=original.gateway_payment_id,
            status="succeeded",
        )
    assert (await sql_store.payments.get(original.id)).status is PaymentSessionStatus.FAILED
    assert (await read_wallet(original.user_id)).balance == 0
    assert sql_store.ledger() == []


@pytest.mark.parametrize("credited", [False, True])
@pytest.mark.parametrize(
    "facts",
    [{"user_id": uuid4()}, {"amount_minor": 101}, {"amount_minor": True}, {"currency": "IRT"}],
)
async def test_conflicting_callback_replay_fails_before_wallet_io(sql_store, credited, facts):
    original = invoice()
    if credited:
        original = original.mark_succeeded(
            gateway_payment_id=original.gateway_payment_id
        ).mark_credited(at=NOW)
    sql_store.seed(original)
    wallet = SimpleNamespace(get=AsyncMock(), credit_deposit=AsyncMock())
    webhook = PaymentWebhookService(sql_store.payments, wallet, None)
    with pytest.raises(ValueError, match="callback facts"):
        await webhook.process_callback(
            gateway_key=original.gateway_key,
            external_id=original.gateway_payment_id,
            status="succeeded",
            **facts,
        )
    wallet.get.assert_not_awaited()
    wallet.credit_deposit.assert_not_awaited()
    assert sql_store.ledger() == []


async def test_concurrent_unknown_callback_create_reuses_only_exact_winner():
    winner = invoice()
    repo = SimpleNamespace(
        get_by_external_id=AsyncMock(side_effect=[None, winner]),
        create=AsyncMock(side_effect=DuplicateExternalIdError("concurrent winner")),
        save=AsyncMock(side_effect=lambda session: session),
    )
    wallet = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(currency="IRT")),
        credit_deposit=AsyncMock(return_value=(SimpleNamespace(balance=10), True)),
    )
    result = await PaymentWebhookService(repo, wallet, None).process_callback(
        gateway_key=winner.gateway_key,
        external_id=winner.gateway_payment_id,
        status="succeeded",
        user_id=winner.user_id,
        amount_minor=100,
        currency="IRR",
    )
    assert result.session.user_id == winner.user_id
    wallet.credit_deposit.assert_awaited_once_with(
        winner.user_id,
        10,
        "deposit-zarinpal-authority-one",
        reference="zarinpal/authority-one",
        expected_currency="IRT",
        business_event_factory=None,
    )


async def test_wallet_transaction_failure_rolls_back_balance_and_ledger(sql_store):
    original = invoice()
    sql_store.seed(original, balance=7)
    sql_store.fail_next_commit = True
    with pytest.raises(IntegrityError):
        await sql_store.wallets.credit_deposit(
            original.user_id, 10, "atomic-deposit", expected_currency="IRT"
        )
    assert (await sql_store.wallets.get(original.user_id)).balance == 7
    assert sql_store.ledger() == []
    wallet, applied = await sql_store.wallets.credit_deposit(
        original.user_id, 10, "atomic-deposit", expected_currency="IRT"
    )
    assert applied and wallet.balance == 17
    replayed, applied = await sql_store.wallets.credit_deposit(
        original.user_id, 10, "atomic-deposit", expected_currency="IRT"
    )
    assert not applied and replayed.balance == 17 and len(sql_store.ledger()) == 1


@pytest.mark.parametrize(
    "amount,currency,reference",
    [(11, "IRT", "invoice-one"), (10, "USD", "invoice-one"), (10, "IRT", "invoice-two")],
)
async def test_wallet_replay_changed_facts_leave_balance_and_original_ledger(
    sql_store, amount, currency, reference
):
    original = invoice()
    sql_store.seed(original)
    await sql_store.wallets.credit_deposit(
        original.user_id, 10, "immutable-deposit", reference="invoice-one", expected_currency="IRT"
    )
    with pytest.raises((ValueError, DuplicateIdempotencyError)):
        await sql_store.wallets.credit_deposit(
            original.user_id,
            amount,
            "immutable-deposit",
            reference=reference,
            expected_currency=currency,
        )
    assert (await sql_store.wallets.get(original.user_id)).balance == 10
    (entry,) = sql_store.ledger()
    assert (
        entry.amount == 10
        and entry.currency == "IRT"
        and entry.description == "gateway deposit invoice-one"
    )


@pytest.mark.parametrize(
    "balance,amount,accepted", [(2**63 - 11, 10, True), (2**63 - 10, 10, False)]
)
async def test_wallet_int64_boundary_is_atomic(sql_store, balance, amount, accepted):
    original = invoice()
    sql_store.seed(original, balance=balance)
    if accepted:
        updated, applied = await sql_store.wallets.credit_deposit(
            original.user_id, amount, "boundary-deposit"
        )
        assert applied and updated.balance == 2**63 - 1
        assert len(sql_store.ledger()) == 1
    else:
        with pytest.raises(ValueError, match="signed int64"):
            await sql_store.wallets.credit_deposit(original.user_id, amount, "boundary-deposit")
        assert (await sql_store.wallets.get(original.user_id)).balance == balance
        assert sql_store.ledger() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_minor": True},
        {"amount_minor": 100.0},
        {"currency": "IRT"},
        {"gateway_payment_id": "another-authority"},
        {
            "metadata": {
                "merchant_order_ref": "another-reference",
                "tracking_code": "pinned-tracking-code",
            }
        },
        {"metadata": {}},
    ],
)
async def test_inquiry_rejects_wrong_or_missing_pinned_financial_identity(changes):
    original = invoice(
        amount_minor=1 if changes.get("amount_minor") is True else 100,
        tracking_code="pinned-tracking-code",
    )
    verified = replace(
        PaymentIntent(
            original.gateway_payment_id,
            PaymentStatus.SUCCEEDED,
            original.amount_minor,
            original.currency,
            metadata={
                "tracking_code": "pinned-tracking-code",
                "merchant_order_ref": original.idempotency_key,
            },
        ),
        **changes,
    )
    gateway = SimpleNamespace(key="zarinpal", verify_with_amount=AsyncMock(return_value=verified))
    with pytest.raises(ProviderError):
        await verify_payment_session(gateway, original)
    assert original.status is PaymentSessionStatus.PENDING


async def test_nonowner_cannot_trigger_recovery_or_provider_inquiry(sql_store):
    original = invoice().mark_succeeded(gateway_payment_id="authority-one")
    sql_store.seed(original)
    webhook = SimpleNamespace(process_callback=AsyncMock())
    inquiry = PaymentInquiryService(
        payments_repo=sql_store.payments, webhook_service=webhook, gateways={}
    )
    with pytest.raises(PermissionDeniedError):
        await inquiry.check_status(SimpleNamespace(id=uuid4()), original.id)
    webhook.process_callback.assert_not_awaited()
    assert sql_store.ledger() == []


async def test_deposit_balance_ledger_and_outbox_roll_back_and_retry_together(sql_store):
    from cloud_platform.modules.businesslog.events import recharge_succeeded_event

    original = invoice()
    sql_store.seed(original, balance=7)
    factories = []

    def event_factory(wallet):
        factories.append(wallet.balance)
        return recharge_succeeded_event(
            user=None,
            payment_session_id=original.id,
            amount_minor=10,
            currency="IRT",
            gateway="zarinpal",
            gateway_reference="authority-one",
            balance_after_minor=wallet.balance,
            at=NOW,
        )

    sql_store.fail_next_commit = True
    with pytest.raises(IntegrityError):
        await sql_store.wallets.credit_deposit(
            original.user_id, 10, "outbox-deposit", business_event_factory=event_factory
        )
    assert (await sql_store.wallets.get(original.user_id)).balance == 7
    assert sql_store.ledger() == sql_store.business_events() == []
    updated, applied = await sql_store.wallets.credit_deposit(
        original.user_id, 10, "outbox-deposit", business_event_factory=event_factory
    )
    assert applied and updated.balance == 17
    (event,) = sql_store.business_events("recharge.succeeded")
    assert event.event_key == f"recharge.succeeded:{original.id}"
    assert event.payload["balance_after"] == "17 تومان"
    assert len(sql_store.ledger()) == 1
    replayed, applied = await sql_store.wallets.credit_deposit(
        original.user_id, 10, "outbox-deposit", business_event_factory=event_factory
    )
    assert not applied and replayed.balance == 17
    assert factories == [17, 17]  # A replay never constructs another event.
    assert len(sql_store.business_events("recharge.succeeded")) == len(sql_store.ledger()) == 1
    (notice,) = sql_store.business_events("customer.notice")
    assert notice.payload["user_id"] == str(original.user_id)


async def test_event_factory_failure_cannot_commit_money_without_its_event(sql_store):
    original = invoice()
    sql_store.seed(original, balance=7)

    def broken_factory(wallet):
        assert wallet.balance == 17
        raise RuntimeError("event factory failed")

    with pytest.raises(RuntimeError, match="event factory"):
        await sql_store.wallets.credit_deposit(
            original.user_id, 10, "broken-outbox", business_event_factory=broken_factory
        )
    assert (await sql_store.wallets.get(original.user_id)).balance == 7
    assert sql_store.ledger() == sql_store.business_events() == []


async def test_concurrent_callbacks_share_one_committed_deposit_and_event(sql_store):
    original = invoice()
    sql_store.seed(original)
    delivery_sink = SimpleNamespace(emit=AsyncMock())
    service = PaymentWebhookService(
        sql_store.payments, sql_store.wallets, None, event_sink=delivery_sink
    )
    read_wallet = sql_store.wallets.get
    both_read = asyncio.Event()
    reads = 0

    async def synchronized_wallet_read(owner):
        nonlocal reads
        wallet = await read_wallet(owner)
        reads += 1
        if reads <= 2:
            if reads == 2:
                both_read.set()
            async with asyncio.timeout(10):
                await both_read.wait()
        return wallet

    sql_store.wallets.get = synchronized_wallet_read
    async with asyncio.timeout(10):
        outcomes = await asyncio.gather(
            *(
                service.process_callback(
                    gateway_key="zarinpal", external_id="authority-one", status="succeeded"
                )
                for _ in range(2)
            )
        )
    assert all(outcome.session.credited_at is not None for outcome in outcomes)
    assert (await read_wallet(original.user_id)).balance == 10
    assert len(sql_store.ledger()) == len(sql_store.business_events("recharge.succeeded")) == 1
    assert len(sql_store.business_events("customer.notice")) == 1
    delivery_sink.emit.assert_not_awaited()


async def test_credit_marker_failure_cannot_lose_or_duplicate_committed_outbox(sql_store):
    original = invoice()
    sql_store.seed(original)
    delivery_sink = SimpleNamespace(emit=AsyncMock())
    service = PaymentWebhookService(
        sql_store.payments, sql_store.wallets, None, event_sink=delivery_sink
    )
    real_save = sql_store.payments.save
    saves = 0

    async def fail_credit_marker(session):
        nonlocal saves
        saves += 1
        if saves == 2:
            raise RuntimeError("crash before payment credit marker")
        return await real_save(session)

    sql_store.payments.save = fail_credit_marker
    with pytest.raises(RuntimeError, match="credit marker"):
        await service.process_callback(
            gateway_key="zarinpal", external_id="authority-one", status="succeeded"
        )
    assert len(sql_store.ledger()) == len(sql_store.business_events("recharge.succeeded")) == 1
    assert len(sql_store.business_events("customer.notice")) == 1
    assert (await sql_store.wallets.get(original.user_id)).balance == 10
    assert (await sql_store.payments.get(original.id)).credited_at is None
    sql_store.payments.save = real_save
    recovered = await PaymentInquiryService(
        payments_repo=sql_store.payments, webhook_service=service, gateways={}
    ).check_status(SimpleNamespace(id=original.user_id), original.id)
    assert recovered.credited_at is not None
    assert len(sql_store.ledger()) == len(sql_store.business_events("recharge.succeeded")) == 1
    assert len(sql_store.business_events("customer.notice")) == 1
    assert (await sql_store.wallets.get(original.user_id)).balance == 10
    delivery_sink.emit.assert_not_awaited()


@pytest.mark.parametrize(
    "sink_kind,expected_events",
    [
        ("null", 0),
        ("disabled-policy", 0),
        ("no-channel", 0),
        ("recharges-disabled", 0),
        ("enabled-policy", 1),
        ("recording", 1),
    ],
)
async def test_atomic_recharge_outbox_respects_sink_category_policy(
    sql_store, sink_kind, expected_events
):
    from cloud_platform.modules.businesslog.domain import (
        BusinessLogPolicy,
        NullBusinessEventSink,
        OutboxBusinessEventSink,
    )

    original = invoice()
    sql_store.seed(original)
    separate_enqueue = AsyncMock()
    if sink_kind == "null":
        sink = NullBusinessEventSink()
    elif sink_kind == "recording":
        sink = SimpleNamespace(emit=AsyncMock())
    else:
        policy = BusinessLogPolicy(
            enabled=sink_kind != "disabled-policy",
            chat_id=0 if sink_kind == "no-channel" else -100123,
            log_recharges=sink_kind != "recharges-disabled",
        )
        sink = OutboxBusinessEventSink(SimpleNamespace(enqueue=separate_enqueue), policy)
    service = PaymentWebhookService(sql_store.payments, sql_store.wallets, None, event_sink=sink)
    outcome = await service.process_callback(
        gateway_key="zarinpal", external_id="authority-one", status="succeeded"
    )
    assert outcome.session.credited_at is not None
    assert (await sql_store.wallets.get(original.user_id)).balance == 10
    assert len(sql_store.ledger()) == 1
    assert len(sql_store.business_events("recharge.succeeded")) == expected_events
    (notice,) = sql_store.business_events("customer.notice")
    assert notice.payload["user_id"] == str(original.user_id)
    separate_enqueue.assert_not_awaited()
    replay = await service.process_callback(
        gateway_key="zarinpal", external_id="authority-one", status="succeeded"
    )
    assert replay.action is WebhookAction.DUPLICATE_IGNORED
    assert len(sql_store.ledger()) == 1
    assert len(sql_store.business_events("recharge.succeeded")) == expected_events
    assert len(sql_store.business_events("customer.notice")) == 1
    if sink_kind == "recording":
        sink.emit.assert_not_awaited()
