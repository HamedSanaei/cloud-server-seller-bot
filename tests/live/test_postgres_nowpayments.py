"""Opt-in scratch PostgreSQL proof; NOWPayments HTTP is always MockTransport."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from test_financial_payment_outbox import (
    DB_URL,
    migrated_payment_database,  # noqa: F401 -- shared opt-in migration fixture
    payment_database,  # noqa: F401 -- shared isolated scratch fixture
    seed_wallet,
)

from cloud_platform.db.base import BusinessLogEvent, LedgerEntry
from cloud_platform.db.base import PaymentSession as PaymentRow
from cloud_platform.modules.payments.domain import DuplicateExternalIdError, PaymentSession
from cloud_platform.modules.payments.inquiry import AuthoritativePaymentService
from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository
from cloud_platform.providers.nowpayments import NowPaymentsGateway
from cloud_platform.providers.nowpayments.callbacks import NowPaymentsCallbackService

pytestmark = pytest.mark.skipif(not DB_URL, reason="requires opt-in scratch PostgreSQL URL")


async def test_concurrent_native_and_poll_settlement_pin_payment_and_credit_once(
    payment_database,  # noqa: F811 -- pytest fixture injection
):
    owner, wallet_id, session_id, event_key = await seed_wallet(payment_database)
    invoice_id = str(uuid4().int)
    payment_id = str(uuid4().int)
    key = f"np-scratch-{session_id}"
    order_ref = NowPaymentsGateway.provider_order_ref_for(key)
    payments = SqlAlchemyPaymentSessionRepository(payment_database.factory)
    session = await payments.create(
        PaymentSession(
            user_id=owner,
            gateway_key="nowpayments",
            gateway_payment_id=invoice_id,
            amount_minor=1050,
            currency="USD",
            idempotency_key=key,
            credit_amount_minor=10,
            credit_currency="IRT",
            created_at=datetime.now(UTC),
        )
    )
    event_key = f"recharge.succeeded:{session.id}"
    payment_database.event_keys.append(event_key)
    payment = {
        "payment_id": payment_id,
        "invoice_id": int(invoice_id),
        "order_id": order_ref,
        "payment_status": "finished",
        "price_amount": "10.50",
        "price_currency": "usd",
        "pay_amount": "0.0001",
        "actually_paid": "0.0001",
    }
    invoice_posts = []

    def respond(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "scratch-jwt"})
        if request.url.path.endswith("/payment/"):
            assert request.url.params["page"] == "0"
            return httpx.Response(200, json={"data": [payment], "pagesCount": 1})
        if request.url.path.endswith(f"/payment/{payment_id}"):
            return httpx.Response(200, json=payment)
        invoice_posts.append(request)
        return httpx.Response(500)

    gateway = NowPaymentsGateway(
        "scratch-fixture-key",
        callback_url="https://app.test/webhooks/payments/nowpayments",
        auth_email="scratch@example.test",
        auth_password="scratch-fixture-password",  # pragma: allowlist secret
        transport=httpx.MockTransport(respond),
    )
    webhook = PaymentWebhookService(
        payments,
        SqlAlchemyWalletRepository(payment_database.factory),
        None,
        event_sink=SimpleNamespace(policy=None),
    )
    authoritative = AuthoritativePaymentService(
        payments_repo=payments, webhook_service=webhook, gateways={"nowpayments": gateway}
    )
    callback = NowPaymentsCallbackService(
        payments_repo=payments, authoritative_service=authoritative
    )
    try:
        async with asyncio.timeout(30):
            await asyncio.gather(
                *[
                    callback.process_callback({"invoice_id": invoice_id, "order_id": order_ref})
                    for _ in range(8)
                ]
            )
        saved = await payments.get(session.id)
        assert saved is not None and saved.provider_payment_id == payment_id
        assert saved.gateway_payment_id == invoice_id and saved.credited_at is not None
        async with payment_database.factory() as db:
            from cloud_platform.db.base import Wallet

            balance = (
                await db.execute(select(Wallet.balance).where(Wallet.id == wallet_id))
            ).scalar_one()
            entries = (
                await db.execute(
                    select(func.count())
                    .select_from(LedgerEntry)
                    .where(LedgerEntry.wallet_id == wallet_id)
                )
            ).scalar_one()
            events = (
                await db.execute(
                    select(func.count())
                    .select_from(BusinessLogEvent)
                    .where(BusinessLogEvent.event_key == event_key)
                )
            ).scalar_one()
        assert balance == 17 and entries == events == 1
        assert not invoice_posts
        second = await payments.create(
            PaymentSession(
                user_id=owner,
                gateway_key="nowpayments",
                gateway_payment_id=str(uuid4().int),
                amount_minor=1050,
                currency="USD",
                idempotency_key=f"other-{uuid4()}",
            )
        )
        with pytest.raises(DuplicateExternalIdError):
            await payments.save(second.bind_provider_payment(payment_id))
        with pytest.raises(ValueError, match="immutable"):
            await payments.save(replace(saved, provider_payment_id=str(uuid4().int)))
    finally:
        await gateway.close()
        async with payment_database.factory() as db:
            await db.execute(delete(PaymentRow).where(PaymentRow.user_id == owner))
            await db.commit()
