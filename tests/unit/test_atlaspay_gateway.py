"""AtlasPay's documented JSON envelope and full-payment wallet contract."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.payments.atlaspay import reconcile_atlaspay_pending
from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus
from cloud_platform.modules.payments.recharge import WalletRechargeService
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.modules.users.domain import User
from cloud_platform.providers.atlaspay.client import AtlasPayGateway
from cloud_platform.providers.base import PaymentStatus
from cloud_platform.providers.errors import ProviderError

REF = "bot-recharge:user:500000:n-12345678"
LINK = "https://t.me/atlaspaybot/pay?startapp=order_66_opaque-token"


def order_create() -> dict[str, object]:
    return {
        "orderId": 66,
        "trackingCode": "5c23c12c9fa0c8b3",
        "totalAmountToman": 500739,
        "cardNumberMasked": "6037****3165",
        "cardNumber": "6037991812345678",
        "paymentDeadlineAt": "2026-08-05T21:58:56.329Z",
        "customerStartLink": LINK,
    }


def order_status(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": True,
        "id": 66,
        "trackingCode": "5c23c12c9fa0c8b3",
        "merchantOrderRef": REF,
        "status": "confirmed",
        "totalAmountToman": 500739,
        "actualReceivedAmountToman": 500739,
        "requiresManualDelivery": False,
        "createdAt": "2026-08-05T21:38:56.335Z",
    }
    payload.update(changes)
    return payload


class Api:
    def __init__(self) -> None:
        self.status = order_status()
        self.created = 0
        self.gets = 0

    def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        assert request.headers["X-API-Key"] == "test-only-key"
        if request.method == "POST":
            assert request.url.path == "/api/v1/orders"
            assert json.loads(request.content) == {
                "merchantOrderRef": REF,
                "baseAmountToman": 500000,
                "customerTelegramId": 123456789,
            }
            self.created += 1
            return httpx.Response(201, json=order_create())
        assert request.url.path == "/api/v1/orders/66"
        self.gets += 1
        return httpx.Response(200, json=self.status)


@pytest.fixture
async def gateway():
    api = Api()
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.respond)) as client:
        yield AtlasPayGateway(api_key="test-only-key", client=client), api


async def test_create_uses_telegram_identity_and_returns_final_invoice(gateway):
    adapter, api = gateway
    result = await adapter.create_payment(
        amount_minor=500000,
        currency="IRT",
        reference="123456789",
        idempotency_key=IdempotencyKey(REF),
    )
    assert api.created == 1
    assert result.amount_minor == 500739
    assert result.gateway_payment_id == "66"
    assert result.redirect_url == LINK
    assert result.metadata["tracking_code"] == "5c23c12c9fa0c8b3"
    assert "cardNumber" not in result.metadata
    with pytest.raises(ValueError):
        await adapter.create_payment(
            amount_minor=500000,
            currency="IRR",
            reference="123456789",
            idempotency_key=IdempotencyKey(REF),
        )
    with pytest.raises(ValueError):
        AtlasPayGateway(api_key="test-only-key", base_url="http://api.atlaspay.space/api/v1")


def test_enabled_config_requires_https_and_key_without_leaking_secret():
    from pydantic import ValidationError

    from cloud_platform.core.config import Settings, toml_to_settings

    parsed = toml_to_settings(
        {
            "payments": {
                "atlaspay": {
                    "enabled": True,
                    "api_key": "example-test-key",
                    "base_url": "https://api.atlaspay.space/api/v1",
                }
            }
        }
    )
    configured = Settings(**parsed)
    assert configured.atlaspay_enabled
    assert configured.atlaspay_api_key == "example-test-key"
    assert Settings(default_currency="IRT").low_balance_threshold_minor == 200000
    assert (
        Settings(
            default_currency="IRT", low_balance_threshold_minor=100
        ).low_balance_threshold_minor
        == 100
    )
    assert "example-test-key" not in repr(configured)
    for overrides in (
        {"atlaspay_api_key": ""},
        {"atlaspay_base_url": "http://api.atlaspay.space/api/v1"},
    ):
        with pytest.raises(ValidationError) as error:
            Settings(**{**parsed, **overrides})
        assert "example-test-key" not in str(error.value)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, PaymentStatus.SUCCEEDED),
        ({"status": "settled"}, PaymentStatus.SUCCEEDED),
        ({"status": "awaiting_payment", "actualReceivedAmountToman": None}, PaymentStatus.PENDING),
        (
            {
                "status": "confirmed",
                "requiresManualDelivery": True,
                "actualReceivedAmountToman": 200000,
            },
            PaymentStatus.PENDING,
        ),
        ({"status": "rejected"}, PaymentStatus.FAILED),
    ],
)
async def test_inquiry_requires_full_amount_and_no_manual_delivery(gateway, changes, expected):
    adapter, api = gateway
    api.status = order_status(**changes)
    result = await adapter.verify_payment("66")
    assert result.status is expected


@pytest.mark.parametrize(
    "changes",
    [
        {"id": 67},
        {"totalAmountToman": None},
        {"merchantOrderRef": None},
        {"requiresManualDelivery": None},
        {"success": False},
        {"actualReceivedAmountToman": 500000},
        {"actualReceivedAmountToman": None},
    ],
)
async def test_untrusted_inquiry_cannot_authorize_credit(gateway, changes):
    adapter, api = gateway
    api.status = order_status(**changes)
    with pytest.raises(ProviderError):
        await adapter.verify_payment("66")


class Repo:
    def __init__(self) -> None:
        self.sessions: dict[str, PaymentSession] = {}
        self.creates = 0

    async def create(self, session: PaymentSession) -> PaymentSession:
        self.creates += 1
        stored = replace(session, id=uuid4(), created_at=datetime.now(UTC) - timedelta(minutes=2))
        self.sessions[stored.gateway_payment_id or ""] = stored
        return stored

    async def get_by_idempotency_key(self, key: str, ref: str) -> PaymentSession | None:
        return next(
            (
                s
                for s in self.sessions.values()
                if s.gateway_key == key and s.idempotency_key == ref
            ),
            None,
        )

    async def get_by_external_id(self, key: str, external_id: str) -> PaymentSession | None:
        return self.sessions.get(external_id)

    async def save(self, session: PaymentSession) -> PaymentSession:
        for key, existing in list(self.sessions.items()):
            if existing.id == session.id:
                del self.sessions[key]
        self.sessions[session.gateway_payment_id or ""] = session
        return session

    async def list_pending_before(
        self, key: str, before: datetime, limit: int
    ) -> list[PaymentSession]:
        return [
            s
            for s in self.sessions.values()
            if s.gateway_key == key
            and s.status is PaymentSessionStatus.PENDING
            and s.created_at <= before
        ][:limit]


class Wallet:
    def __init__(self) -> None:
        self.balance = 0
        self.deposit_keys: set[str] = set()
        self.currency = "IRT"

    async def get(self, user_id):
        del user_id
        return self

    async def credit_deposit(self, user_id, amount, key, reference, expected_currency):
        assert expected_currency == self.currency
        del user_id, reference
        applied = key not in self.deposit_keys
        if applied:
            self.balance += amount
            self.deposit_keys.add(key)
        return self, applied


async def test_replay_and_repeated_poll_credit_only_base_toman(gateway):
    adapter, api = gateway
    repo = Repo()
    user = User(
        id=uuid4(), username="customer", email="customer@example.test", telegram_user_id=123456789
    )
    recharge = WalletRechargeService(payments_repo=repo, gateways={"atlaspay": adapter})
    first = await recharge.start(
        user=user, amount_minor=500000, currency="IRT", gateway_key="atlaspay", idempotency_key=REF
    )
    replay = await recharge.start(
        user=user, amount_minor=500000, currency="IRT", gateway_key="atlaspay", idempotency_key=REF
    )
    assert api.created == repo.creates == 1
    assert replay.replayed is True and replay.redirect_url == first.redirect_url == LINK
    assert first.payable_amount_minor == 500739 and first.tracking_code == "5c23c12c9fa0c8b3"
    assert first.session.amount_minor == 500739
    assert first.session.credit_amount_minor == 500000
    wallet = Wallet()
    webhook = PaymentWebhookService(repo, wallet, ledger_repo=None)
    report = await reconcile_atlaspay_pending(
        payments_repo=repo, webhook_service=webhook, gateway=adapter
    )
    again = await reconcile_atlaspay_pending(
        payments_repo=repo, webhook_service=webhook, gateway=adapter
    )
    assert report.credited == 1 and again.credited == 0
    assert wallet.balance == 500000
    assert len(wallet.deposit_keys) == 1
    assert api.gets == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"merchantOrderRef": "other-ref"},
        {"totalAmountToman": 500740},
        {"trackingCode": "wrong-tracking"},
    ],
)
async def test_mismatch_never_credits(gateway, changes):
    adapter, api = gateway
    repo = Repo()
    await repo.create(
        PaymentSession(
            user_id=uuid4(),
            gateway_key="atlaspay",
            amount_minor=500739,
            currency="IRT",
            idempotency_key=REF,
            gateway_payment_id="66",
            credit_amount_minor=500000,
            credit_currency="IRT",
            tracking_code="5c23c12c9fa0c8b3",
        )
    )
    api.status = order_status(**changes)
    wallet = Wallet()
    report = await reconcile_atlaspay_pending(
        payments_repo=repo,
        webhook_service=PaymentWebhookService(repo, wallet, ledger_repo=None),
        gateway=adapter,
    )
    assert report.credited == 0 and wallet.balance == 0


async def test_ambiguous_order_creation_is_never_sent_twice():
    created = 0

    def fail_create(request):
        nonlocal created
        created += 1
        return httpx.Response(503, json={"success": False})

    from cloud_platform.modules.payments.recharge import RechargeError

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_create)) as client:
        adapter = AtlasPayGateway(api_key="test-only-key", client=client)
        repo = Repo()
        service = WalletRechargeService(payments_repo=repo, gateways={"atlaspay": adapter})
        user = User(
            id=uuid4(),
            username="customer",
            email="customer@example.test",
            telegram_user_id=123456789,
        )
        for _ in range(2):
            with pytest.raises(RechargeError):
                await service.start(
                    user=user,
                    amount_minor=500000,
                    currency="IRT",
                    gateway_key="atlaspay",
                    idempotency_key=REF,
                )
        assert repo.creates == 1 and created == 1
        assert repo.sessions[""].gateway_payment_id is None


async def test_concurrent_duplicate_intent_replays_winner_without_remote_post(gateway):
    adapter, api = gateway
    from cloud_platform.modules.payments.domain import DuplicateExternalIdError

    owner = User(
        id=uuid4(), username="customer", email="customer@example.test", telegram_user_id=123456789
    )
    winner = PaymentSession(
        id=uuid4(),
        user_id=owner.id,
        gateway_key="atlaspay",
        amount_minor=500739,
        currency="IRT",
        idempotency_key=REF,
        gateway_payment_id="66",
        credit_amount_minor=500000,
        credit_currency="IRT",
        redirect_url=LINK,
        tracking_code="5c23c12c9fa0c8b3",
    )

    class RaceRepo:
        lookup_count = 0

        async def get_by_idempotency_key(self, key, ref):
            self.lookup_count += 1
            return None if self.lookup_count == 1 else winner

        async def create(self, session):
            raise DuplicateExternalIdError("unique order reference")

    service = WalletRechargeService(payments_repo=RaceRepo(), gateways={"atlaspay": adapter})
    replayed = await service.start(
        user=owner, amount_minor=500000, currency="IRT", idempotency_key=REF, gateway_key="atlaspay"
    )
    assert replayed.replayed and replayed.redirect_url == LINK
    assert api.created == 0


async def test_manual_delivery_requires_operator_not_wallet_credit(gateway):
    adapter, api = gateway
    repo = Repo()
    await repo.create(
        PaymentSession(
            user_id=uuid4(),
            gateway_key="atlaspay",
            amount_minor=500739,
            currency="IRT",
            idempotency_key=REF,
            gateway_payment_id="66",
            credit_amount_minor=500000,
            credit_currency="IRT",
            tracking_code="5c23c12c9fa0c8b3",
        )
    )
    api.status = order_status(requiresManualDelivery=True, actualReceivedAmountToman=200000)
    audit_events = []

    class AuditRepo:
        async def append(self, event):
            audit_events.append(event)

    wallet = Wallet()
    report = await reconcile_atlaspay_pending(
        payments_repo=repo,
        webhook_service=PaymentWebhookService(repo, wallet, ledger_repo=None),
        gateway=adapter,
        audit_repo=AuditRepo(),
    )
    assert report.marked_failed == 0 and wallet.balance == 0
    assert repo.sessions["66"].status is PaymentSessionStatus.MANUAL_REVIEW
    assert any(
        e.action == "payments.atlaspay.manual_review"
        and e.resource_id == str(repo.sessions["66"].id)
        for e in audit_events
    )
    again = await reconcile_atlaspay_pending(
        payments_repo=repo,
        webhook_service=PaymentWebhookService(repo, wallet, ledger_repo=None),
        gateway=adapter,
    )
    assert again.checked == 0 and wallet.balance == 0


async def test_wallet_currency_change_never_credits(gateway):
    adapter, _ = gateway
    repo = Repo()
    await repo.create(
        PaymentSession(
            user_id=uuid4(),
            gateway_key="atlaspay",
            amount_minor=500739,
            currency="IRT",
            idempotency_key=REF,
            gateway_payment_id="66",
            credit_amount_minor=500000,
            credit_currency="IRT",
            tracking_code="5c23c12c9fa0c8b3",
        )
    )
    wallet = Wallet()
    wallet.currency = "USD"
    report = await reconcile_atlaspay_pending(
        payments_repo=repo,
        webhook_service=PaymentWebhookService(repo, wallet, ledger_repo=None),
        gateway=adapter,
    )
    assert report.errors == 1 and wallet.balance == 0
    assert repo.sessions["66"].status is PaymentSessionStatus.PENDING


async def test_atlaspay_only_offered_to_toman_wallet(gateway):
    adapter, api = gateway
    service = WalletRechargeService(payments_repo=Repo(), gateways={"atlaspay": adapter})
    assert service.compatible_gateways("IRT", 500000) == ["atlaspay"]
    assert service.compatible_gateways("USD", 500000) == []
    assert await service.compatible_gateways_async("USD", 500000) == []
    assert api.created == 0
