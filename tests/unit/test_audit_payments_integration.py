"""Financial regressions at the main/production contract integration boundary."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from cloud_platform.modules.payments.atlaspay import reconcile_atlaspay_pending
from cloud_platform.modules.payments.domain import (
    DuplicateExternalIdError,
    PaymentSession,
    PaymentSessionStatus,
)
from cloud_platform.modules.payments.recharge import WalletRechargeService
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.modules.users.domain import User
from cloud_platform.modules.wallet.domain import DuplicateIdempotencyError
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository
from cloud_platform.providers.atlaspay import AtlasPayGateway
from tests.unit.test_atlaspay_gateway import (
    CREATED,
    INQUIRY,
    LINK,
    MERCHANT_REF,
    VERIFY,
    MemoryPayments,
    MemoryWallet,
)


def invoice(user_id=None, **changes):
    return replace(
        PaymentSession(
            id=uuid4(),
            user_id=user_id or uuid4(),
            gateway_key="atlaspay",
            gateway_payment_id="66",
            amount_minor=259739,
            currency="IRT",
            idempotency_key=MERCHANT_REF,
            credit_amount_minor=250000,
            credit_currency="IRT",
            tracking_code=CREATED["trackingCode"],
            redirect_url=LINK,
            created_at=datetime(2026, 8, 5, tzinfo=UTC),
        ),
        **changes,
    )


@pytest.mark.parametrize("value", [True, 1.5, 2**63])
@pytest.mark.parametrize("field", ["amount_minor", "credit_amount_minor"])
def test_payment_session_rejects_non_bigint_money(field, value):
    with pytest.raises(ValueError):
        invoice(**{field: value})


async def test_legacy_bound_invoice_recovers_link_and_new_ui_projection_without_second_post():
    user = User(id=uuid4(), username="customer", email="customer@example.test")
    repo = MemoryPayments()
    original = invoice(user.id)
    repo.rows[original.id] = original
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json=INQUIRY)

    gateway = AtlasPayGateway("test-only-key", transport=httpx.MockTransport(respond))
    try:
        result = await WalletRechargeService(payments_repo=repo, gateway=gateway).start(
            user=user,
            amount_minor=250000,
            currency="IRT",
            idempotency_key=MERCHANT_REF,
        )
        assert result.replayed and result.redirect_url == LINK
        assert result.tracking_code == CREATED["trackingCode"]
        assert result.payable_amount_minor == 259739
        assert len(requests) == 1
    finally:
        await gateway.close()


async def test_concurrent_durable_insert_replays_bound_winner_without_second_post():
    user = User(id=uuid4(), username="customer", email="customer@example.test")
    winner = invoice(user.id)
    repo = SimpleNamespace(
        get_by_idempotency_key=AsyncMock(side_effect=[None, winner]),
        create=AsyncMock(side_effect=DuplicateExternalIdError("concurrent winner")),
        get=AsyncMock(return_value=winner),
    )

    def respond(request):
        assert request.method == "GET"
        return httpx.Response(200, json=INQUIRY)

    gateway = AtlasPayGateway("test-only-key", transport=httpx.MockTransport(respond))
    try:
        result = await WalletRechargeService(payments_repo=repo, gateway=gateway).start(
            user=user,
            amount_minor=250000,
            currency="IRT",
            idempotency_key=MERCHANT_REF,
        )
        assert result.replayed and result.session == winner and result.redirect_url == LINK
        repo.create.assert_awaited_once()
    finally:
        await gateway.close()


async def test_manual_underpayment_is_quarantined_and_audited_not_credited():
    repo, wallet = MemoryPayments(), MemoryWallet()
    original = invoice()
    repo.rows[original.id] = original
    audit = SimpleNamespace(append=AsyncMock())
    payload = {**VERIFY, "requiresManualDelivery": True, "actualReceivedAmountToman": 100000}
    gateway = AtlasPayGateway(
        "test-only-key",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    try:
        report = await reconcile_atlaspay_pending(
            payments_repo=repo,
            webhook_service=PaymentWebhookService(repo, wallet, None),
            gateway=gateway,
            audit_repo=audit,
        )
        assert report.still_pending == 1 and report.errors == 0 and wallet.balance == 0
        assert repo.rows[original.id].status is PaymentSessionStatus.MANUAL_REVIEW
        assert any(
            call.args[0].action == "payments.atlaspay.manual_review"
            for call in audit.append.await_args_list
        )
        again = await reconcile_atlaspay_pending(
            payments_repo=repo,
            webhook_service=PaymentWebhookService(repo, wallet, None),
            gateway=gateway,
        )
        assert again.checked == 0 and wallet.balance == 0
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"merchantOrderRef": "wrong-ref"},
        {"totalAmountToman": 259740},
        {"trackingCode": "anothertrackingcode"},
    ],
)
async def test_legacy_invoice_mismatch_does_not_credit_or_change_pending_state(changes):
    repo, wallet = MemoryPayments(), MemoryWallet()
    original = invoice()
    repo.rows[original.id] = original
    gateway = AtlasPayGateway(
        "test-only-key",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={**VERIFY, **changes}),
        ),
    )
    try:
        report = await reconcile_atlaspay_pending(
            payments_repo=repo,
            webhook_service=PaymentWebhookService(repo, wallet, None),
            gateway=gateway,
        )
        assert report.errors == 1 and wallet.balance == 0
        assert repo.rows[original.id] == original
    finally:
        await gateway.close()


@pytest.mark.parametrize("value", [True, 1.5, 0, -1, 2**63])
async def test_deposit_rejects_non_bigint_amount_before_opening_transaction(value):
    factory = AsyncMock()
    repo = SqlAlchemyWalletRepository(factory)
    with pytest.raises(ValueError):
        await repo.credit_deposit(uuid4(), value, "deposit-test")
    factory.assert_not_called()


def test_deposit_same_key_with_different_facts_fails_closed():
    entry = SimpleNamespace(
        amount=200, currency="IRT", entry_type="deposit", description="gateway deposit a"
    )
    with pytest.raises(DuplicateIdempotencyError):
        SqlAlchemyWalletRepository._check_deposit_replay(entry, 100, "IRT", "a", "key")
