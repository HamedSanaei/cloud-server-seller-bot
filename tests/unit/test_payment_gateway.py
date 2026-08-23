"""Tests for the PaymentGateway port: create/verify/refund capability model (M09-001)."""

from __future__ import annotations

import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.base import (
    CapabilityGatedGateway,
    GatewayCapability,
    GatewayRefund,
    PaymentIntent,
    PaymentStatus,
    UnsupportedGatewayOperation,
    gateway_supports,
)
from cloud_platform.providers.errors import ProviderNotFound

KEY = IdempotencyKey("payment-test-key-1")


class FullGateway(CapabilityGatedGateway):
    """Fake gateway advertising and implementing all three operations."""

    key = "full"
    capabilities = frozenset(GatewayCapability)

    def __init__(self) -> None:
        self.payments: dict[str, PaymentIntent] = {}

    async def create_payment(
        self,
        *,
        amount_minor: int,
        currency: str,
        reference: str,
        idempotency_key: IdempotencyKey,
        redirect_url: str | None = None,
    ) -> PaymentIntent:
        self._require_capability(GatewayCapability.CREATE_PAYMENT)
        self._validate_amount(amount_minor, currency)
        intent = PaymentIntent(
            gateway_payment_id=f"pay-{idempotency_key.value}",
            status=PaymentStatus.PENDING,
            amount_minor=amount_minor,
            currency=currency,
            redirect_url=redirect_url,
            metadata={"reference": reference},
        )
        self.payments[intent.gateway_payment_id] = intent
        return intent

    async def verify_payment(self, gateway_payment_id: str) -> PaymentIntent:
        self._require_capability(GatewayCapability.VERIFY_PAYMENT)
        if gateway_payment_id not in self.payments:
            raise ProviderNotFound(f"unknown payment {gateway_payment_id}")
        return self.payments[gateway_payment_id]

    async def refund(
        self,
        *,
        gateway_payment_id: str,
        amount_minor: int,
        idempotency_key: IdempotencyKey,
        reason: str = "",
    ) -> object:
        self._require_capability(GatewayCapability.REFUND)
        if amount_minor <= 0:
            raise ValueError("refund amount must be positive")
        return GatewayRefund(
            refund_id=f"ref-{idempotency_key.value}",
            gateway_payment_id=gateway_payment_id,
            amount_minor=amount_minor,
            status=PaymentStatus.REFUNDED,
        )


class CreateOnlyGateway(CapabilityGatedGateway):
    """Fake gateway that advertises only create; verify/refund are not overridden."""

    key = "create-only"
    capabilities = frozenset({GatewayCapability.CREATE_PAYMENT})


class TestCapabilityModel:
    def test_gateway_supports_helper(self) -> None:
        full = FullGateway()
        limited = CreateOnlyGateway()
        assert gateway_supports(full, GatewayCapability.REFUND) is True
        assert gateway_supports(limited, GatewayCapability.CREATE_PAYMENT) is True
        assert gateway_supports(limited, GatewayCapability.VERIFY_PAYMENT) is False

    async def test_bare_mixin_rejects_all_operations(self) -> None:
        gateway = CapabilityGatedGateway()
        with pytest.raises(UnsupportedGatewayOperation):
            await gateway.create_payment(
                amount_minor=100,
                currency="EUR",
                reference="r",
                idempotency_key=KEY,
            )
        with pytest.raises(UnsupportedGatewayOperation):
            await gateway.verify_payment("pay-1")
        with pytest.raises(UnsupportedGatewayOperation):
            await gateway.refund(gateway_payment_id="pay-1", amount_minor=100, idempotency_key=KEY)

    async def test_unadvertised_operations_raise_before_io(self) -> None:
        limited = CreateOnlyGateway()
        with pytest.raises(UnsupportedGatewayOperation, match="verify_payment"):
            await limited.verify_payment("pay-1")
        with pytest.raises(UnsupportedGatewayOperation, match="refund"):
            await limited.refund(gateway_payment_id="pay-1", amount_minor=100, idempotency_key=KEY)


class TestCreatePayment:
    async def test_returns_pending_intent(self) -> None:
        gateway = FullGateway()
        intent = await gateway.create_payment(
            amount_minor=1999,
            currency="EUR",
            reference="order-42",
            idempotency_key=KEY,
            redirect_url="https://example.com/return",
        )
        assert intent.status is PaymentStatus.PENDING
        assert intent.amount_minor == 1999
        assert intent.currency == "EUR"
        assert intent.redirect_url == "https://example.com/return"
        assert intent.metadata == {"reference": "order-42"}

    async def test_idempotency_key_determines_gateway_id(self) -> None:
        gateway = FullGateway()
        first = await gateway.create_payment(
            amount_minor=100, currency="EUR", reference="r", idempotency_key=KEY
        )
        second = await gateway.create_payment(
            amount_minor=100, currency="EUR", reference="r", idempotency_key=KEY
        )
        assert first.gateway_payment_id == second.gateway_payment_id

    async def test_zero_or_negative_amount_rejected(self) -> None:
        gateway = FullGateway()
        with pytest.raises(ValueError, match="positive"):
            await gateway.create_payment(
                amount_minor=0, currency="EUR", reference="r", idempotency_key=KEY
            )
        with pytest.raises(ValueError, match="positive"):
            await gateway.create_payment(
                amount_minor=-5, currency="EUR", reference="r", idempotency_key=KEY
            )

    async def test_bad_currency_rejected(self) -> None:
        gateway = FullGateway()
        for bad in ("eur", "EURO", "EU", "123"):
            with pytest.raises(ValueError, match="currency"):
                await gateway.create_payment(
                    amount_minor=100, currency=bad, reference="r", idempotency_key=KEY
                )

    async def test_invalid_idempotency_key_rejected(self) -> None:
        gateway = FullGateway()
        with pytest.raises(ValueError, match="idempotency"):
            await gateway.create_payment(
                amount_minor=100,
                currency="EUR",
                reference="r",
                idempotency_key=IdempotencyKey("short"),
            )


class TestVerifyAndRefund:
    async def test_verify_returns_recorded_intent(self) -> None:
        gateway = FullGateway()
        created = await gateway.create_payment(
            amount_minor=500, currency="EUR", reference="r", idempotency_key=KEY
        )
        verified = await gateway.verify_payment(created.gateway_payment_id)
        assert verified is created

    async def test_verify_unknown_payment_raises_provider_not_found(self) -> None:
        gateway = FullGateway()
        with pytest.raises(ProviderNotFound):
            await gateway.verify_payment("missing")

    async def test_refund_returns_gateway_refund(self) -> None:
        gateway = FullGateway()
        result = await gateway.refund(
            gateway_payment_id="pay-1", amount_minor=300, idempotency_key=KEY, reason="goodwill"
        )
        assert isinstance(result, GatewayRefund)
        assert result.amount_minor == 300
        assert result.status is PaymentStatus.REFUNDED

    async def test_refund_rejects_non_positive_amount(self) -> None:
        gateway = FullGateway()
        with pytest.raises(ValueError, match="positive"):
            await gateway.refund(gateway_payment_id="pay-1", amount_minor=0, idempotency_key=KEY)
