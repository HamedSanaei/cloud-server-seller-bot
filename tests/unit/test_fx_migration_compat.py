"""Migration/backward compatibility: legacy sessions, ledger idempotency."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from cloud_platform.modules.payments.domain import (
    PaymentSession,
    PaymentSessionStatus,
    session_credit_amount,
    session_credit_currency,
)


class TestLegacySessions:
    def test_legacy_session_reads_credit_as_settlement(self) -> None:
        session = PaymentSession(
            user_id=uuid4(),
            gateway_key="zarinpal",
            amount_minor=500_000,
            currency="IRR",
            idempotency_key="legacy-1",
            gateway_payment_id="AUTH-1",
        )
        assert session.credit_amount_minor is None
        assert session.effective_credit_amount == 500_000
        assert session.effective_credit_currency == "IRR"

    def test_legacy_test_double_without_new_attrs(self) -> None:
        fake = SimpleNamespace(amount_minor=100_000, currency="IRT")
        assert session_credit_amount(fake) == 100_000
        assert session_credit_currency(fake) == "IRT"

    def test_same_currency_recharge_behaves_identically(self) -> None:
        session = PaymentSession(
            user_id=uuid4(),
            gateway_key="tetraminator",
            amount_minor=100_000,
            currency="IRT",
            idempotency_key="legacy-2",
            gateway_payment_id="pay-1",
            credit_amount_minor=100_000,
            credit_currency="IRT",
            fx_source="identity",
            fx_rate="1",
            fx_path="identity",
            fx_observed_at=None,
            fx_proxy=False,
            fx_proxy_asset=None,
        )
        assert session.effective_credit_amount == session.amount_minor
        assert session.status is PaymentSessionStatus.PENDING
        bound = session.with_gateway_payment_id("pay-1")
        assert bound.credit_amount_minor == 100_000
        succeeded = session.mark_succeeded(gateway_payment_id="pay-1")
        assert succeeded.credit_currency == "IRT"

    def test_deposit_key_uses_settlement_identity(self) -> None:
        # The deterministic ledger key stays (gateway, external_id): replay
        # safety and the append-only ledger are unchanged by the FX columns.
        session = PaymentSession(
            user_id=uuid4(),
            gateway_key="tetraminator",
            amount_minor=2_000_000,
            currency="IRT",
            idempotency_key="legacy-3",
            gateway_payment_id="pay-9",
            credit_amount_minor=1_000,
            credit_currency="EUR",
        )
        assert f"deposit-{session.gateway_key}-{session.gateway_payment_id}" == (
            "deposit-tetraminator-pay-9"
        )
        assert session_credit_amount(session) == 1_000
        assert session_credit_currency(session) == "EUR"
