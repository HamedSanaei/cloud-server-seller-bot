"""Official-shaped AtlasPay fixtures: no external requests or real orders."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.payments.domain import (
    DuplicateExternalIdError,
    PaymentSession,
    PaymentSessionStatus,
)
from cloud_platform.modules.payments.inquiry import PaymentInquiryService, verify_payment_session
from cloud_platform.modules.payments.recharge import (
    RechargeDisabledError,
    RechargeError,
    WalletRechargeService,
)
from cloud_platform.modules.payments.reconcile import PaymentReconciliationService
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.modules.users.domain import PermissionDeniedError, User
from cloud_platform.providers.atlaspay import AtlasPayGateway
from cloud_platform.providers.base import PaymentStatus
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)

MERCHANT_REF = "wallet-atlaspay-attempt-001"
LINK = "https://t.me/atlaspaybot/pay?startapp=order_66_5c23c12c9fa0c8b3d93367517c77a5af"
CREATED = {
    "orderId": 66,
    "trackingCode": "5c23c12c9fa0c8b3",  # pragma: allowlist secret -- public fixture
    "totalAmountToman": 259739,
    "cardNumberMasked": "6037****3165",
    "paymentDeadlineAt": "2026-08-05T21:58:56.329Z",
    "customerStartLink": LINK,
    # Optional direct-card data must not leak into persisted metadata.
    "cardNumber": "6037991812345678",
    "cardHolderName": "Test Holder",
    "bankName": "Test Bank",
}
INQUIRY = {
    "success": True,
    "id": 66,
    "trackingCode": "5c23c12c9fa0c8b3",  # pragma: allowlist secret -- public fixture
    "merchantOrderRef": MERCHANT_REF,
    "status": "awaiting_payment",
    "totalAmountToman": 259739,
    "actualReceivedAmountToman": None,
    "requiresManualDelivery": False,
    "createdAt": "2026-08-05T21:38:56.329Z",
}
VERIFY = {**INQUIRY, "status": "confirmed", "paid": True}


def adapter(payload: dict[str, Any], requests: list[httpx.Request]) -> AtlasPayGateway:
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    return AtlasPayGateway("fixture-only-key", transport=httpx.MockTransport(respond))


async def test_create_uses_official_top_level_payload_and_exact_customer_total() -> None:
    requests: list[httpx.Request] = []
    gateway = adapter(CREATED, requests)
    try:
        intent = await gateway.create_payment(
            amount_minor=250000,
            currency="IRT",
            reference=str(uuid4()),
            idempotency_key=IdempotencyKey(MERCHANT_REF),
            customer_telegram_id=123456789,
        )
    finally:
        await gateway.close()
    request = requests[0]
    assert request.url == "https://api.atlaspay.space/api/v1/orders"
    assert request.headers["X-API-Key"] == "fixture-only-key"
    assert json.loads(request.content) == {
        "merchantOrderRef": MERCHANT_REF,
        "baseAmountToman": 250000,
        "customerTelegramId": 123456789,
    }
    assert intent.amount_minor == 259739
    assert intent.currency == "IRT"
    assert intent.redirect_url == LINK
    assert intent.metadata["tracking_code"] == CREATED["trackingCode"]
    assert intent.metadata["payment_deadline_at"] == CREATED["paymentDeadlineAt"]
    assert "orderId" not in intent.metadata
    assert "cardNumber" not in intent.metadata
    assert "cardHolderName" not in intent.metadata


@pytest.mark.parametrize(
    ("state", "paid", "expected"),
    [
        ("awaiting_payment", False, PaymentStatus.PENDING),
        ("admin_review", False, PaymentStatus.PENDING),
        ("underpaid_review", False, PaymentStatus.PENDING),
        ("underpaid_awaiting_remainder", False, PaymentStatus.PENDING),
        ("confirmed", True, PaymentStatus.SUCCEEDED),
        ("settled", True, PaymentStatus.SUCCEEDED),
        ("confirmed", False, PaymentStatus.PENDING),
        ("rejected", False, PaymentStatus.FAILED),
        ("expired", False, PaymentStatus.FAILED),
        ("cancelled", False, PaymentStatus.FAILED),
    ],
)
async def test_all_documented_status_classes(
    state: str, paid: bool, expected: PaymentStatus
) -> None:
    requests: list[httpx.Request] = []
    gateway = adapter({**VERIFY, "status": state, "paid": paid}, requests)
    try:
        intent = await gateway.verify_with_reference("66", 259739, MERCHANT_REF)
    finally:
        await gateway.close()
    assert intent.status is expected
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v1/orders/66/verify"


@pytest.mark.parametrize("state", ["confirmed", "settled"])
async def test_accepted_underpayment_never_automatically_settles(state: str) -> None:
    gateway = adapter(
        {
            **VERIFY,
            "status": state,
            "requiresManualDelivery": True,
            "actualReceivedAmountToman": 200000,
        },
        [],
    )
    try:
        intent = await gateway.verify_with_reference("66", 259739, MERCHANT_REF)
    finally:
        await gateway.close()
    assert intent.status is PaymentStatus.PENDING
    assert intent.metadata["requires_manual_delivery"] == "true"


@pytest.mark.parametrize(
    "changes",
    [
        {"id": 67},
        {"totalAmountToman": 250000},
        {"merchantOrderRef": "somebody-else"},
        {"success": False},
        {"status": "invented"},
        {"paid": None},
        {"requiresManualDelivery": None},
        {"actualReceivedAmountToman": 200000},
        {"actualReceivedAmountToman": -1},
        {"totalAmountToman": 259739.0},
    ],
)
async def test_verify_rejects_mismatched_or_untrusted_evidence(changes: dict[str, Any]) -> None:
    gateway = adapter({**VERIFY, **changes}, [])
    try:
        with pytest.raises(ProviderError):
            await gateway.verify_with_reference("66", 259739, MERCHANT_REF)
    finally:
        await gateway.close()


@pytest.mark.parametrize("field", ["trackingCode", "paymentDeadlineAt", "customerStartLink"])
async def test_create_requires_safe_complete_customer_metadata(field: str) -> None:
    payload = dict(CREATED)
    payload.pop(field)
    gateway = adapter(payload, [])
    try:
        with pytest.raises(ProviderError):
            await gateway.create_payment(
                amount_minor=250000,
                currency="IRT",
                reference="customer",
                idempotency_key=IdempotencyKey(MERCHANT_REF),
            )
    finally:
        await gateway.close()


@pytest.mark.parametrize("amount,currency", [(250000.0, "IRT"), (True, "IRT"), (250000, "IRR")])
async def test_rejects_noninteger_or_wrong_currency_before_request(
    amount: Any, currency: str
) -> None:
    requests: list[httpx.Request] = []
    gateway = adapter(CREATED, requests)
    try:
        with pytest.raises(ValueError):
            await gateway.create_payment(
                amount_minor=amount,
                currency=currency,
                reference="customer",
                idempotency_key=IdempotencyKey(MERCHANT_REF),
            )
    finally:
        await gateway.close()
    assert requests == []


@pytest.mark.parametrize(
    "code,error",
    [
        (401, ProviderAuthError),
        (404, ProviderNotFound),
        (429, ProviderRateLimited),
        (400, ProviderError),
        (503, ProviderUnavailable),
    ],
)
async def test_provider_http_errors_are_safe(code: int, error: type[Exception]) -> None:
    gateway = AtlasPayGateway(
        "fixture-only-key",
        transport=httpx.MockTransport(lambda request: httpx.Response(code, text="secret body")),
    )
    try:
        with pytest.raises(error) as raised:
            await gateway.verify_with_reference("66", 259739, MERCHANT_REF)
        assert "secret body" not in str(raised.value)
        assert "fixture-only-key" not in str(raised.value)
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"api_key": " \t"},
        {
            "api_key": "fixture-only-key",  # pragma: allowlist secret -- rejected URL fixture
            "base_url": "http://api.atlaspay.space/api/v1",
        },
    ],
)
def test_gateway_configuration_cannot_start_with_blank_key_or_plaintext_transport(arguments):
    with pytest.raises(ValueError):
        AtlasPayGateway(**arguments)


@pytest.mark.parametrize(
    "changes",
    [
        {"merchantOrderRef": "a-different-attempt"},
        {"totalAmountToman": 249999},
        {"trackingCode": "unsafe#code"},
        {"customerStartLink": "http://t.me/atlaspaybot/pay"},
        {"paymentDeadlineAt": "not-a-date"},
        {"paymentDeadlineAt": "2026-10-04T12:00:00"},
    ],
)
async def test_untrusted_creation_stays_unbound_and_replay_never_posts_again(changes):
    requests: list[httpx.Request] = []
    gateway = adapter({**CREATED, **changes}, requests)
    repo = MemoryPayments()
    user = User(username="fixture-customer", email="fixture@example.test", id=uuid4())
    recharge = WalletRechargeService(payments_repo=repo, gateway=gateway)
    try:
        with pytest.raises(ProviderError):
            await recharge.start(
                user=user,
                amount_minor=250000,
                currency="IRT",
                idempotency_key=MERCHANT_REF,
            )
        with pytest.raises(RechargeError):
            await recharge.start(
                user=user,
                amount_minor=250000,
                currency="IRT",
                idempotency_key=MERCHANT_REF,
            )
        assert len(repo.rows) == 1
        saved = next(iter(repo.rows.values()))
        assert saved.gateway_payment_id is None
        assert saved.status is PaymentSessionStatus.PENDING
        assert saved.credit_amount_minor == 250000
        assert saved.credit_currency == "IRT"
        assert [(r.method, r.url.path) for r in requests] == [("POST", "/api/v1/orders")]
    finally:
        await gateway.close()


async def test_non_json_verification_never_credits_or_consumes_pending_attempt():
    repo, wallet = MemoryPayments(), MemoryWallet()
    user = User(username="fixture-customer", email="fixture@example.test", id=uuid4())

    def respond(request):
        if request.url.path.endswith("/verify"):
            return httpx.Response(200, text="09123456789 private non-json response")
        return httpx.Response(200, json=CREATED)

    gateway = AtlasPayGateway("fixture-only-key", transport=httpx.MockTransport(respond))
    recharge = WalletRechargeService(payments_repo=repo, gateway=gateway)
    try:
        started = await recharge.start(
            user=user,
            amount_minor=250000,
            currency="IRT",
            idempotency_key=MERCHANT_REF,
        )
        inquiry = PaymentInquiryService(
            payments_repo=repo,
            webhook_service=PaymentWebhookService(repo, wallet, None),
            gateways={gateway.key: gateway},
        )
        with pytest.raises(ProviderError) as error:
            await inquiry.check_status(user, started.session.id)
        assert "09123456789" not in str(error.value)
        saved = await repo.get(started.session.id)
        assert saved.status is PaymentSessionStatus.PENDING
        assert saved.gateway_payment_id == "66"
        assert saved.credited_at is None
        assert wallet.balance == 0
        assert wallet.deposits == {}
    finally:
        await gateway.close()


async def test_malformed_bound_order_id_never_verifies_another_invoice_or_credits():
    requests: list[httpx.Request] = []
    gateway = adapter(CREATED, requests)
    repo, wallet = MemoryPayments(), MemoryWallet()
    user = User(username="fixture-customer", email="fixture@example.test", id=uuid4())
    recharge = WalletRechargeService(payments_repo=repo, gateway=gateway)
    try:
        started = await recharge.start(
            user=user,
            amount_minor=250000,
            currency="IRT",
            idempotency_key=MERCHANT_REF,
        )
        repo.rows[started.session.id] = replace(
            started.session,
            gateway_payment_id="not-an-order",
        )
        requests.clear()
        inquiry = PaymentInquiryService(
            payments_repo=repo,
            webhook_service=PaymentWebhookService(repo, wallet, None),
            gateways={gateway.key: gateway},
        )
        with pytest.raises(ValueError):
            await inquiry.check_status(user, started.session.id)
        saved = await repo.get(started.session.id)
        assert saved.status is PaymentSessionStatus.PENDING
        assert saved.credited_at is None
        assert wallet.balance == 0
        assert wallet.deposits == {}
        assert requests == []
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_minor": 49999},
        {"amount_minor": 2000001},
        {"reference": " "},
        {"customer_telegram_id": True},
    ],
)
async def test_invalid_native_invoice_inputs_cannot_send_an_order(changes):
    requests: list[httpx.Request] = []
    gateway = adapter(CREATED, requests)
    arguments = dict(
        amount_minor=250000,
        currency="IRT",
        reference="customer",
        idempotency_key=IdempotencyKey(MERCHANT_REF),
    )
    arguments.update(changes)
    try:
        with pytest.raises(ValueError):
            await gateway.create_payment(**arguments)
        assert requests == []
    finally:
        await gateway.close()


class MemoryPayments:
    def __init__(self) -> None:
        self.rows: dict[UUID, PaymentSession] = {}

    async def create(self, session: PaymentSession) -> PaymentSession:
        if await self.get_by_idempotency_key(session.gateway_key, session.idempotency_key):
            raise DuplicateExternalIdError("unique merchant attempt")
        row = replace(session, id=uuid4(), created_at=datetime(2026, 8, 5, tzinfo=UTC))
        self.rows[row.id] = row
        return row

    async def save(self, session: PaymentSession) -> PaymentSession:
        self.rows[session.id] = session
        return session

    async def get(self, session_id: UUID) -> PaymentSession | None:
        return self.rows.get(session_id)

    async def get_by_idempotency_key(self, gateway: str, key: str) -> PaymentSession | None:
        return next(
            (
                s
                for s in self.rows.values()
                if s.gateway_key == gateway and s.idempotency_key == key
            ),
            None,
        )

    async def get_by_external_id(self, gateway: str, external_id: str) -> PaymentSession | None:
        return next(
            (
                s
                for s in self.rows.values()
                if s.gateway_key == gateway and s.gateway_payment_id == external_id
            ),
            None,
        )

    async def list_pending_before(
        self, key: str, before: datetime, limit: int = 100
    ) -> list[PaymentSession]:
        return [
            s
            for s in self.rows.values()
            if s.gateway_key == key
            and s.status is PaymentSessionStatus.PENDING
            and s.gateway_payment_id is not None
            and s.created_at is not None
            and s.created_at <= before
        ][:limit]


class MemoryWallet:
    def __init__(self) -> None:
        self.balance = 0
        self.deposits: dict[str, int] = {}

    async def credit_deposit(self, user_id: UUID, amount: int, key: str, **kwargs: Any) -> Any:
        applied = key not in self.deposits
        if applied:
            self.deposits[key] = amount
            self.balance += amount
        return SimpleNamespace(balance=self.balance), applied


class FrozenFx:
    def __init__(self) -> None:
        self.rate = Decimal("100000")
        self.resolves = 0
        self.available = True
        self.compatibility_checks = 0

    async def can_convert(self, *args: Any) -> bool:
        self.compatibility_checks += 1
        return self.available

    async def resolve(self, *args: Any) -> Any:
        self.resolves += 1
        return SimpleNamespace(
            target_amount_minor=250000,
            source="fixture-fx",
            rate=self.rate,
            path="EUR->IRT",
            observed_at=datetime(2026, 8, 5, tzinfo=UTC),
            proxy=False,
            proxy_asset=None,
        )


async def test_bound_replay_recovers_real_api_link_and_preserves_fx_then_credits_once() -> None:
    requests: list[httpx.Request] = []
    repo, wallet, fx = MemoryPayments(), MemoryWallet(), FrozenFx()
    user = User(
        username="fixture-customer",
        email="fixture@example.test",
        id=uuid4(),
        telegram_user_id=123456789,
    )
    recovered_link = LINK + "-api-recovered"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/verify"):
            return httpx.Response(200, json=VERIFY)
        if request.method == "GET":
            return httpx.Response(200, json={**INQUIRY, "customerStartLink": recovered_link})
        # Persist-before-POST is exercised, not asserted against source text.
        assert len(repo.rows) == 1
        assert next(iter(repo.rows.values())).gateway_payment_id is None
        return httpx.Response(200, json=CREATED)

    gateway = AtlasPayGateway("fixture-only-key", transport=httpx.MockTransport(respond))
    recharge = WalletRechargeService(payments_repo=repo, gateway=gateway, fx_resolver=fx)
    try:
        first = await recharge.start(
            user=user, amount_minor=250, currency="EUR", idempotency_key=MERCHANT_REF
        )
        frozen = (
            first.session.fx_rate,
            first.session.fx_observed_at,
            first.session.credit_amount_minor,
        )
        fx.rate = Decimal("999999")
        fx.available = False
        replay = await recharge.start(
            user=user, amount_minor=250, currency="EUR", idempotency_key=MERCHANT_REF
        )
        assert replay.session.id == first.session.id
        assert replay.replayed is True
        assert replay.redirect_url == recovered_link
        assert replay.payment_details.amount_minor == 259739
        assert replay.payment_details.tracking_code == CREATED["trackingCode"]
        assert replay.payment_details.payment_deadline_at == CREATED["paymentDeadlineAt"]
        assert replay.session.amount_minor == 259739
        assert frozen == (
            replay.session.fx_rate,
            replay.session.fx_observed_at,
            replay.session.credit_amount_minor,
        )
        assert fx.resolves == 1
        assert fx.compatibility_checks == 1
        assert sum(r.method == "POST" and r.url.path.endswith("/orders") for r in requests) == 1
        webhook = PaymentWebhookService(repo, wallet, None)
        inquiry = PaymentInquiryService(
            payments_repo=repo, webhook_service=webhook, gateways={gateway.key: gateway}
        )
        credited = await inquiry.check_status(user, first.session.id)
        await inquiry.check_status(user, first.session.id)
        assert credited.status is PaymentSessionStatus.SUCCEEDED
        assert wallet.balance == 250
        assert len(wallet.deposits) == 1
        assert credited.payment_details["tracking_code"] == CREATED["trackingCode"]
    finally:
        await gateway.close()


async def test_documented_inquiry_without_link_replays_saved_genuine_creation_metadata() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=INQUIRY if request.method == "GET" else CREATED)

    gateway = AtlasPayGateway("fixture-only-key", transport=httpx.MockTransport(respond))
    user, repo = (
        User(username="fixture-customer", email="fixture@example.test", id=uuid4()),
        MemoryPayments(),
    )
    recharge = WalletRechargeService(payments_repo=repo, gateway=gateway)
    try:
        await recharge.start(
            user=user, amount_minor=250000, currency="IRT", idempotency_key=MERCHANT_REF
        )
        replay = await recharge.start(
            user=user, amount_minor=250000, currency="IRT", idempotency_key=MERCHANT_REF
        )
        assert replay.redirect_url == CREATED["customerStartLink"]
    finally:
        await gateway.close()


async def test_ambiguous_creation_is_durable_and_never_reposts_on_replay() -> None:
    requests: list[httpx.Request] = []

    def timeout(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("fixture timeout", request=request)

    gateway = AtlasPayGateway("fixture-only-key", transport=httpx.MockTransport(timeout))
    repo, user = (
        MemoryPayments(),
        User(username="fixture-customer", email="fixture@example.test", id=uuid4()),
    )
    recharge = WalletRechargeService(payments_repo=repo, gateway=gateway)
    try:
        with pytest.raises(ProviderUnavailable):
            await recharge.start(
                user=user, amount_minor=250000, currency="IRT", idempotency_key=MERCHANT_REF
            )
        with pytest.raises(RechargeError, match="outcome is unknown"):
            await recharge.start(
                user=user, amount_minor=250000, currency="IRT", idempotency_key=MERCHANT_REF
            )
        assert len(requests) == 1
        assert len(repo.rows) == 1
    finally:
        await gateway.close()


async def test_untrusted_status_check_cannot_credit_and_checks_ownership() -> None:
    repo, wallet, user = (
        MemoryPayments(),
        MemoryWallet(),
        User(username="fixture-customer", email="fixture@example.test", id=uuid4()),
    )
    session = await repo.create(
        PaymentSession(
            user_id=user.id,
            gateway_key="atlaspay",
            gateway_payment_id="66",
            amount_minor=259739,
            currency="IRT",
            idempotency_key=MERCHANT_REF,
            credit_amount_minor=250000,
            credit_currency="IRT",
        )
    )
    requests: list[httpx.Request] = []
    gateway = adapter({**VERIFY, "merchantOrderRef": "attacker-order"}, requests)
    service = PaymentInquiryService(
        payments_repo=repo,
        webhook_service=PaymentWebhookService(repo, wallet, None),
        gateways={gateway.key: gateway},
    )
    try:
        with pytest.raises(PermissionDeniedError):
            await service.check_status(
                User(username="fixture-customer", email="fixture@example.test", id=uuid4()),
                session.id,
            )
        assert requests == []
        with pytest.raises(ProviderError):
            await service.check_status(user, session.id)
        assert wallet.balance == 0
        assert session.status is PaymentSessionStatus.PENDING
    finally:
        await gateway.close()


async def test_polling_reconciliation_settles_only_registered_gateway() -> None:
    repo, wallet = MemoryPayments(), MemoryWallet()
    atlas = await repo.create(
        PaymentSession(
            user_id=uuid4(),
            gateway_key="atlaspay",
            gateway_payment_id="66",
            amount_minor=259739,
            currency="IRT",
            idempotency_key=MERCHANT_REF,
            credit_amount_minor=250000,
            credit_currency="IRT",
        )
    )
    other = await repo.create(
        PaymentSession(
            user_id=uuid4(),
            gateway_key="zarinpal",
            gateway_payment_id="other-authority",
            amount_minor=50000,
            currency="IRR",
            idempotency_key="different-attempt",
        )
    )
    gateway = adapter(VERIFY, [])
    try:
        reconciler = PaymentReconciliationService(
            payments_repo=repo,
            webhook_service=PaymentWebhookService(repo, wallet, None),
            gateway=gateway,
            stale_after=timedelta(seconds=0),
        )
        report = await reconciler.run(now=datetime(2026, 8, 6, tzinfo=UTC))
        assert report.checked == report.credited == 1
        assert report.errors == 0
        assert repo.rows[atlas.id].status is PaymentSessionStatus.SUCCEEDED
        assert repo.rows[other.id].status is PaymentSessionStatus.PENDING
        assert wallet.balance == 250000
    finally:
        await gateway.close()


async def test_tracking_code_mismatch_rejected_before_settlement() -> None:
    session = PaymentSession(
        user_id=uuid4(),
        gateway_key="atlaspay",
        gateway_payment_id="66",
        amount_minor=259739,
        currency="IRT",
        idempotency_key=MERCHANT_REF,
        payment_details={"tracking_code": "differenttracking"},
    )
    gateway = adapter(VERIFY, [])
    try:
        with pytest.raises(ProviderError, match="tracking code"):
            await verify_payment_session(gateway, session)
    finally:
        await gateway.close()


async def test_concurrent_double_click_cannot_create_two_provider_orders() -> None:
    import asyncio

    repo, user = (
        MemoryPayments(),
        User(username="fixture-customer", email="fixture@example.test", id=uuid4()),
    )
    posted = asyncio.Event()
    release = asyncio.Event()
    mutations = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal mutations
        mutations += 1
        posted.set()
        await release.wait()
        return httpx.Response(200, json=CREATED)

    gateway = AtlasPayGateway("fixture-only-key", transport=httpx.MockTransport(respond))
    recharge = WalletRechargeService(payments_repo=repo, gateway=gateway)
    first = asyncio.create_task(
        recharge.start(
            user=user,
            amount_minor=250000,
            currency="IRT",
            idempotency_key=MERCHANT_REF,
        )
    )
    try:
        await posted.wait()
        with pytest.raises(RechargeError, match="outcome is unknown"):
            await recharge.start(
                user=user,
                amount_minor=250000,
                currency="IRT",
                idempotency_key=MERCHANT_REF,
            )
        release.set()
        await first
        assert mutations == 1
        assert len(repo.rows) == 1
    finally:
        release.set()
        await first
        await gateway.close()


@pytest.mark.parametrize("bound", [False, True], ids=["ambiguous-attempt", "issued-invoice"])
@pytest.mark.parametrize("altered_fact", ["owner", "credit_amount", "credit_currency"])
async def test_altered_retry_cannot_replace_durable_payment_facts(
    bound: bool, altered_fact: str
) -> None:
    repo, fx = MemoryPayments(), FrozenFx()
    user = User(username="fixture-customer", email="fixture@example.test", id=uuid4())
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if not bound:
            raise httpx.ReadTimeout("ambiguous fixture creation", request=request)
        return httpx.Response(200, json=CREATED)

    gateway = AtlasPayGateway("fixture-only-key", transport=httpx.MockTransport(respond))
    recharge = WalletRechargeService(payments_repo=repo, gateway=gateway, fx_resolver=fx)
    try:
        if bound:
            await recharge.start(
                user=user, amount_minor=250, currency="EUR", idempotency_key=MERCHANT_REF
            )
        else:
            with pytest.raises(ProviderUnavailable):
                await recharge.start(
                    user=user, amount_minor=250, currency="EUR", idempotency_key=MERCHANT_REF
                )
        original = deepcopy(next(iter(repo.rows.values())))
        retry_user = replace(user, id=uuid4()) if altered_fact == "owner" else user
        retry_amount = 251 if altered_fact == "credit_amount" else 250
        retry_currency = "USD" if altered_fact == "credit_currency" else "EUR"
        with pytest.raises(RechargeError):
            await recharge.start(
                user=retry_user,
                amount_minor=retry_amount,
                currency=retry_currency,
                idempotency_key=MERCHANT_REF,
            )
        assert repo.rows == {original.id: original}
        assert len(requests) == 1
        assert requests[0].method == "POST"
        assert fx.resolves == 1
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    ("changes", "invalid"),
    [
        pytest.param({"id": 67}, True, id="foreign-order"),
        pytest.param(
            {"merchantOrderRef": "foreign-merchant-attempt"}, True, id="foreign-reference"
        ),
        pytest.param({"merchantOrderRef": None}, True, id="missing-reference"),
        pytest.param({"totalAmountToman": 259740}, True, id="changed-invoice-total"),
        pytest.param({"paid": None}, True, id="ambiguous-paid"),
        pytest.param({"paid": "true"}, True, id="nonboolean-paid"),
        pytest.param({"requiresManualDelivery": None}, True, id="ambiguous-manual-delivery"),
        pytest.param({"actualReceivedAmountToman": 200000}, True, id="underpaid-conflict"),
        pytest.param({"paid": False}, False, id="unpaid-confirmation"),
        pytest.param(
            {"requiresManualDelivery": True, "actualReceivedAmountToman": 200000},
            False,
            id="manual-underpayment-acceptance",
        ),
    ],
)
@pytest.mark.parametrize("entrypoint", ["inquiry", "reconciliation"])
async def test_untrusted_settlement_evidence_preserves_pending_money(
    changes: dict[str, Any], invalid: bool, entrypoint: str
) -> None:
    repo, wallet = MemoryPayments(), MemoryWallet()
    user = User(username="fixture-customer", email="fixture@example.test", id=uuid4())
    session = await repo.create(
        PaymentSession(
            user_id=user.id,
            gateway_key="atlaspay",
            gateway_payment_id="66",
            amount_minor=259739,
            currency="IRT",
            idempotency_key=MERCHANT_REF,
            credit_amount_minor=250,
            credit_currency="EUR",
            fx_source="fixture-fx",
            fx_rate="100000",
            fx_path="EUR->IRT",
            fx_observed_at=datetime(2026, 8, 5, tzinfo=UTC),
            payment_details={
                "payment_url": LINK,
                "tracking_code": CREATED["trackingCode"],
                "payment_deadline_at": CREATED["paymentDeadlineAt"],
            },
        )
    )
    original = deepcopy(session)
    requests: list[httpx.Request] = []
    gateway = adapter({**VERIFY, **changes}, requests)
    webhook = PaymentWebhookService(repo, wallet, None)
    try:
        if entrypoint == "inquiry":
            inquiry = PaymentInquiryService(
                payments_repo=repo, webhook_service=webhook, gateways={gateway.key: gateway}
            )
            if invalid:
                with pytest.raises(ProviderError):
                    await inquiry.check_status(user, session.id)
            else:
                result = await inquiry.check_status(user, session.id)
                assert result == original
        else:
            report = await PaymentReconciliationService(
                payments_repo=repo,
                webhook_service=webhook,
                gateway=gateway,
                stale_after=timedelta(seconds=0),
            ).run(now=datetime(2026, 8, 6, tzinfo=UTC))
            assert report.checked == 1
            assert report.credited == report.marked_failed == 0
            assert report.errors == int(invalid)
            assert report.still_pending == int(not invalid)
        assert len(requests) == 1
        assert wallet.balance == 0
        assert wallet.deposits == {}
        assert repo.rows == {original.id: original}
    finally:
        await gateway.close()


@pytest.mark.parametrize("condition", ["foreign-owner", "missing-gateway", "still-pending"])
async def test_inquiry_preserves_entire_pending_invoice(condition: str) -> None:
    repo, wallet, fx = MemoryPayments(), MemoryWallet(), FrozenFx()
    user = User(username="fixture-customer", email="fixture@example.test", id=uuid4())
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={**INQUIRY, "paid": False} if request.url.path.endswith("/verify") else CREATED,
        )

    gateway = AtlasPayGateway("fixture-only-key", transport=httpx.MockTransport(respond))
    try:
        started = await WalletRechargeService(
            payments_repo=repo, gateway=gateway, fx_resolver=fx
        ).start(user=user, amount_minor=250, currency="EUR", idempotency_key=MERCHANT_REF)
        original = deepcopy(started.session)
        requests.clear()
        inquiry = PaymentInquiryService(
            payments_repo=repo,
            webhook_service=PaymentWebhookService(repo, wallet, None),
            gateways={} if condition == "missing-gateway" else {gateway.key: gateway},
        )
        if condition == "foreign-owner":
            with pytest.raises(PermissionDeniedError):
                await inquiry.check_status(replace(user, id=uuid4()), original.id)
        elif condition == "missing-gateway":
            with pytest.raises(ProviderError):
                await inquiry.check_status(user, original.id)
        else:
            assert await inquiry.check_status(user, original.id) == original
        assert len(requests) == int(condition == "still-pending")
        assert repo.rows == {original.id: original}
        assert wallet.balance == 0
        assert wallet.deposits == {}
        assert fx.resolves == 1
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"totalAmountToman": 259740}, id="changed-total"),
        pytest.param({"merchantOrderRef": "foreign-attempt"}, id="foreign-reference"),
        pytest.param({"trackingCode": "differenttracking"}, id="foreign-tracking"),
    ],
)
async def test_bound_invoice_recovery_rejects_changed_provider_facts(
    changes: dict[str, Any],
) -> None:
    requests: list[httpx.Request] = []
    repo, user = (
        MemoryPayments(),
        User(username="fixture-customer", email="fixture@example.test", id=uuid4()),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={**INQUIRY, **changes} if request.method == "GET" else CREATED
        )

    gateway = AtlasPayGateway("fixture-only-key", transport=httpx.MockTransport(respond))
    recharge = WalletRechargeService(payments_repo=repo, gateway=gateway)
    try:
        started = await recharge.start(
            user=user, amount_minor=250000, currency="IRT", idempotency_key=MERCHANT_REF
        )
        original = deepcopy(started.session)
        with pytest.raises(RechargeError):
            await recharge.start(
                user=user, amount_minor=250000, currency="IRT", idempotency_key=MERCHANT_REF
            )
        assert repo.rows == {original.id: original}
        assert [request.method for request in requests] == ["POST", "GET"]
    finally:
        await gateway.close()


async def test_disabled_new_invoices_remain_recoverable_and_periodically_settle_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cloud_platform.worker import settings as worker

    repo, wallet, fx = MemoryPayments(), MemoryWallet(), FrozenFx()
    user = User(username="fixture-customer", email="fixture@example.test", id=uuid4())
    requests: list[httpx.Request] = []

    class NewInvoicePolicy:
        enabled = True

        async def get(self, key: str) -> bool:
            return self.enabled

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/verify"):
            return httpx.Response(200, json=VERIFY)
        return httpx.Response(200, json=INQUIRY if request.method == "GET" else CREATED)

    transport = httpx.MockTransport(respond)
    gateway = AtlasPayGateway("fixture-only-key", transport=transport)
    policy = NewInvoicePolicy()
    recharge = WalletRechargeService(
        payments_repo=repo, gateway=gateway, fx_resolver=fx, gateway_settings=policy
    )

    class DomainContainer:
        session_factory = None

        def wallet_repository(self) -> MemoryWallet:
            return wallet

        def ledger_repository(self) -> None:
            return None

        def business_event_sink(self) -> None:
            return None

        def user_repository(self) -> None:
            return None

        def audit_repository(self) -> None:
            return None

        async def close(self) -> None:
            pass

    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            atlaspay_enabled=True,
            atlaspay_api_key="fixture-only-key",  # pragma: allowlist secret -- mock transport
            atlaspay_base_url="https://api.atlaspay.space/api/v1",
            atlaspay_timeout_seconds=1,
        ),
    )
    monkeypatch.setattr("cloud_platform.core.container.create_container", DomainContainer)
    monkeypatch.setattr(
        "cloud_platform.modules.payments.repository.SqlAlchemyPaymentSessionRepository",
        lambda factory: repo,
    )
    monkeypatch.setattr(
        "cloud_platform.providers.atlaspay.client.AtlasPayGateway",
        lambda **kwargs: AtlasPayGateway(**kwargs, transport=transport),
    )
    try:
        started = await recharge.start(
            user=user, amount_minor=250, currency="EUR", idempotency_key=MERCHANT_REF
        )
        original = deepcopy(started.session)
        policy.enabled = False
        fx.available = False
        fx.rate = Decimal("999999")
        assert await recharge.compatible_gateways_async("EUR", 250) == []
        for explicit_key in (None, gateway.key):
            with pytest.raises(RechargeDisabledError):
                await recharge.start(
                    user=user,
                    amount_minor=250,
                    currency="EUR",
                    idempotency_key="disabled-new-attempt",
                    gateway_key=explicit_key,
                )
        replay = await recharge.start(
            user=user, amount_minor=250, currency="EUR", idempotency_key=MERCHANT_REF
        )
        assert replay.session == original
        assert replay.redirect_url == LINK
        await worker.reconcile_atlaspay_payments({})
        await worker.reconcile_atlaspay_payments({})
        settled = repo.rows[original.id]
        assert settled.status is PaymentSessionStatus.SUCCEEDED
        assert settled.credited_at is not None
        assert replace(settled, status=original.status, credited_at=None) == original
        assert wallet.balance == 250
        assert wallet.deposits == {"deposit-atlaspay-66": 250}
        assert sum(r.method == "POST" and r.url.path.endswith("/orders") for r in requests) == 1
        assert sum(r.url.path.endswith("/verify") for r in requests) == 1
        assert fx.resolves == 1
    finally:
        await gateway.close()
