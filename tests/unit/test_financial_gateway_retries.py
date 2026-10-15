"""Real gateway HTTP boundaries plus durable non-idempotent attempt semantics."""

from __future__ import annotations

import asyncio
import json
import traceback
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.db.base import PaymentSession as PaymentRow
from cloud_platform.modules.payments.recharge import RechargeError, WalletRechargeService
from cloud_platform.providers.errors import ProviderError
from cloud_platform.providers.tetraminator import TetraminatorGateway
from cloud_platform.providers.zarinpal import ZarinPalGateway
from tests.unit.test_financial_payment_invariants import SqlStore

SECRET = "financial-test-credential-must-not-leak"  # pragma: allowlist secret -- redaction sentinel
KEY = "financial-durable-attempt"


@pytest.fixture
def sql_store():
    store = SqlStore()
    try:
        yield store
    finally:
        store.engine.dispose()


async def make_gateway(kind, handler):
    if kind == "zarinpal":
        gateway = ZarinPalGateway(SECRET, callback_url="https://example.test/callback")
    else:
        gateway = TetraminatorGateway(SECRET, callback_url="https://example.test/callback")
    await gateway.close()
    gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return gateway


def created_payload(kind):
    if kind == "zarinpal":
        return {"data": {"code": 100, "authority": "issued-authority"}, "errors": {}}
    return {
        "status": True,
        "pay_id": "issued-authority",
        "payment_link": "https://t.me/tetraminator_bot?start=pay_issued-authority",
    }


def durable_rows(store):
    with store.engine.connect() as connection:
        return list(connection.execute(select(store.tables[PaymentRow])).mappings())


@pytest.mark.parametrize("kind", ["zarinpal", "tetraminator"])
@pytest.mark.parametrize(
    "failure", ["timeout", "protocol", "401", "403", "429", "500", "invalid-json"]
)
async def test_failed_creation_never_reposts_an_ambiguous_durable_attempt(sql_store, kind, failure):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        # The payment fact must precede any potentially chargeable network I/O.
        rows = durable_rows(sql_store)
        assert len(rows) == 1 and rows[0]["gateway_payment_id"] is None
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if failure == "protocol":
            raise httpx.RemoteProtocolError(SECRET, request=request)
        if failure == "invalid-json":
            return httpx.Response(200, text=SECRET)
        return httpx.Response(int(failure), json={"error": SECRET})

    gateway = await make_gateway(kind, handler)
    owner = SimpleNamespace(id=uuid4())
    service = WalletRechargeService(payments_repo=sql_store.payments, gateway=gateway)
    try:
        with pytest.raises(ProviderError) as failed:
            await service.start(
                user=owner, amount_minor=100_000, currency="IRT", idempotency_key=KEY
            )
        assert SECRET not in str(failed.value)
        assert SECRET not in "".join(traceback.format_exception(failed.value))
        with pytest.raises(RechargeError, match="outcome is unknown"):
            await service.start(
                user=owner, amount_minor=100_000, currency="IRT", idempotency_key=KEY
            )
        assert len(requests) == 1
        (row,) = durable_rows(sql_store)
        assert row["user_id"] == owner.id and row["credit_amount_minor"] == 100_000
        assert row["credit_currency"] == "IRT" and row["gateway_payment_id"] is None
        assert row["status"] == "pending"
    finally:
        await gateway.close()


@pytest.mark.parametrize("kind", ["zarinpal", "tetraminator"])
async def test_bound_replay_keeps_frozen_credit_and_never_posts_again(sql_store, kind):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=created_payload(kind))

    gateway = await make_gateway(kind, handler)
    owner = SimpleNamespace(id=uuid4())
    service = WalletRechargeService(payments_repo=sql_store.payments, gateway=gateway)
    try:
        first = await service.start(
            user=owner, amount_minor=100_000, currency="IRT", idempotency_key=KEY
        )
        second = await service.start(
            user=owner, amount_minor=100_000, currency="IRT", idempotency_key=KEY
        )
        assert second.replayed and second.session.id == first.session.id
        assert second.redirect_url == first.redirect_url
        assert second.session.effective_credit_amount == 100_000
        assert second.session.effective_credit_currency == "IRT"
        assert second.session.amount_minor == (1_000_000 if kind == "zarinpal" else 100_000)
        assert len(requests) == 1 and len(durable_rows(sql_store)) == 1
        for changes in (
            {"amount_minor": 100_001},
            {"currency": "USD"},
            {"user": SimpleNamespace(id=uuid4())},
        ):
            arguments = dict(user=owner, amount_minor=100_000, currency="IRT", idempotency_key=KEY)
            arguments.update(changes)
            with pytest.raises(RechargeError, match="original owner/credit"):
                await service.start(**arguments)
        assert len(requests) == 1
    finally:
        await gateway.close()


async def test_zarinpal_durable_creation_honors_explicit_callback_override(sql_store):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=created_payload("zarinpal"))

    gateway = await make_gateway("zarinpal", handler)
    try:
        result = await WalletRechargeService(
            payments_repo=sql_store.payments, gateway=gateway
        ).start(
            user=SimpleNamespace(id=uuid4()),
            amount_minor=100_000,
            currency="IRT",
            idempotency_key=KEY,
            redirect_url="https://example.test/override",
        )
        assert not result.replayed
        body = json.loads(requests[0].content)
        assert body["callback_url"] == "https://example.test/override"
        assert body["amount"] == 1_000_000
        assert body["metadata"]["idempotency_key"] == KEY
    finally:
        await gateway.close()


async def test_concurrent_zarinpal_initial_reads_cannot_issue_two_authorities(sql_store):
    initial_reads = asyncio.Event()
    posted = asyncio.Event()
    release = asyncio.Event()
    requests = []
    real_lookup = sql_store.payments.get_by_idempotency_key
    lookup_count = 0

    async def synchronized_lookup(gateway_key, key):
        nonlocal lookup_count
        row = await real_lookup(gateway_key, key)
        lookup_count += 1
        if lookup_count <= 2:
            if lookup_count == 2:
                initial_reads.set()
            async with asyncio.timeout(10):
                await initial_reads.wait()
        return row

    async def handler(request):
        requests.append(request)
        posted.set()
        async with asyncio.timeout(10):
            await release.wait()
        return httpx.Response(200, json=created_payload("zarinpal"))

    gateway = await make_gateway("zarinpal", handler)
    sql_store.payments.get_by_idempotency_key = synchronized_lookup
    service = WalletRechargeService(payments_repo=sql_store.payments, gateway=gateway)
    owner = SimpleNamespace(id=uuid4())
    tasks = []
    try:
        for _ in range(2):
            tasks.append(
                asyncio.create_task(
                    service.start(
                        user=owner, amount_minor=100_000, currency="IRT", idempotency_key=KEY
                    )
                )
            )
        async with asyncio.timeout(10):
            await posted.wait()
            finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            (loser,) = finished
            with pytest.raises(RechargeError, match="creation already started"):
                await loser
            release.set()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
        assert len(requests) == 1 and len(durable_rows(sql_store)) == 1
        (row,) = durable_rows(sql_store)
        assert row["gateway_payment_id"] == "issued-authority"
        assert row["credit_amount_minor"] == 100_000 and row["amount_minor"] == 1_000_000
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await gateway.close()


@pytest.mark.parametrize("operation", ["create", "verify"])
@pytest.mark.parametrize(
    "data", [None, [], {"code": SECRET}, {"code": True}, {"code": -9, "message": SECRET}]
)
async def test_zarinpal_malformed_or_reflected_secret_response_is_safe(operation, data):
    gateway = await make_gateway(
        "zarinpal",
        lambda _: httpx.Response(200, json={"data": data, "errors": {"merchant_id": [SECRET]}}),
    )
    try:
        with pytest.raises(ProviderError) as failed:
            if operation == "create":
                await gateway.create_payment(
                    amount_minor=1000,
                    currency="IRR",
                    reference="owner",
                    idempotency_key=IdempotencyKey(KEY),
                )
            else:
                await gateway.verify_with_amount("issued-authority", 1000)
        assert SECRET not in str(failed.value)
        assert SECRET not in "".join(traceback.format_exception(failed.value))
    finally:
        await gateway.close()


@pytest.mark.parametrize("operation", ["create", "verify"])
@pytest.mark.parametrize("amount", [True, 1.5, 0, -1, 2**63])
async def test_zarinpal_invalid_amounts_never_reach_provider(operation, amount):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=created_payload("zarinpal"))

    gateway = await make_gateway("zarinpal", handler)
    try:
        with pytest.raises(ValueError, match="positive integer"):
            if operation == "create":
                await gateway.create_payment(
                    amount_minor=amount,
                    currency="IRR",
                    reference="owner",
                    idempotency_key=IdempotencyKey(KEY),
                )
            else:
                await gateway.verify_with_amount("issued-authority", amount)
        assert requests == []
    finally:
        await gateway.close()


async def test_legacy_zarinpal_bound_invoice_replays_real_link_without_post(sql_store):
    from tests.unit.test_financial_payment_invariants import invoice

    original = invoice(payment_details=None, redirect_url=None)
    sql_store.seed(original)
    requests = []

    def handler(request):
        requests.append(request)
        raise AssertionError("replaying an issued authority cannot create another invoice")

    gateway = await make_gateway("zarinpal", handler)
    try:
        result = await WalletRechargeService(
            payments_repo=sql_store.payments, gateway=gateway
        ).start(
            user=SimpleNamespace(id=original.user_id),
            amount_minor=10,
            currency="IRT",
            idempotency_key=original.idempotency_key,
        )
        assert result.replayed and result.session.id == original.id
        assert result.redirect_url == ("https://www.zarinpal.com/pg/StartPay/authority-one")
        assert result.session.amount_minor == 100
        assert result.session.effective_credit_amount == 10
        assert requests == [] and len(durable_rows(sql_store)) == 1
    finally:
        await gateway.close()
