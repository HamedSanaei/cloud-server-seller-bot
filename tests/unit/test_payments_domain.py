"""Tests for the PaymentSession domain: unique, stateful gateway payments."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cloud_platform.modules.payments.domain import (
    DuplicateExternalIdError,
    InvalidPaymentSessionTransition,
    PaymentSession,
    PaymentSessionStatus,
)


def _session(**overrides: object) -> PaymentSession:
    defaults: dict[str, object] = {
        "user_id": uuid4(),
        "gateway_key": "zarinpal",
        "amount_minor": 50000,
        "currency": "EUR",
        "idempotency_key": "deposit-key-1",
    }
    defaults.update(overrides)
    return PaymentSession(**defaults)  # type: ignore[arg-type]


class TestConstruction:
    def test_defaults_to_pending_without_external_id(self) -> None:
        session = _session()
        assert session.status is PaymentSessionStatus.PENDING
        assert session.gateway_payment_id is None
        assert session.credited_at is None

    def test_zero_amount_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            _session(amount_minor=0)

    def test_negative_amount_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            _session(amount_minor=-100)

    def test_bad_currency_rejected(self) -> None:
        for bad in ("eur", "EURO", "EU", "12$"):
            with pytest.raises(ValueError, match="currency"):
                _session(currency=bad)

    def test_empty_gateway_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="gateway_key"):
            _session(gateway_key="  ")


class TestStateMachine:
    def test_mark_succeeded_binds_external_id(self) -> None:
        session = _session().mark_succeeded(gateway_payment_id="gw-123")
        assert session.status is PaymentSessionStatus.SUCCEEDED
        assert session.gateway_payment_id == "gw-123"

    def test_mark_failed_binds_external_id(self) -> None:
        session = _session().mark_failed(gateway_payment_id="gw-err")
        assert session.status is PaymentSessionStatus.FAILED
        assert session.gateway_payment_id == "gw-err"

    def test_terminal_sessions_cannot_transition_again(self) -> None:
        done = _session().mark_succeeded(gateway_payment_id="gw-1")
        with pytest.raises(InvalidPaymentSessionTransition):
            done.mark_succeeded(gateway_payment_id="gw-2")
        with pytest.raises(InvalidPaymentSessionTransition):
            done.mark_failed(gateway_payment_id="gw-3")

        failed = _session().mark_failed(gateway_payment_id="gw-x")
        with pytest.raises(InvalidPaymentSessionTransition):
            failed.mark_succeeded(gateway_payment_id="gw-y")

    def test_empty_external_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="gateway_payment_id"):
            _session().mark_succeeded(gateway_payment_id=" ")

    def test_original_aggregate_unchanged_after_transition(self) -> None:
        original = _session()
        original.mark_succeeded(gateway_payment_id="gw-1")
        assert original.status is PaymentSessionStatus.PENDING  # frozen value object


class TestCrediting:
    def test_only_succeeded_sessions_can_be_credited(self) -> None:
        at = datetime(2026, 8, 22, tzinfo=UTC)
        pending = _session(id=uuid4())
        with pytest.raises(InvalidPaymentSessionTransition):
            pending.mark_credited(at=at)

        credited = pending.mark_succeeded(gateway_payment_id="gw-1").mark_credited(at=at)
        assert credited.credited_at == at
        assert credited.status is PaymentSessionStatus.SUCCEEDED


class TestUniquenessContract:
    def test_duplicate_error_is_exported_for_repo_mapping(self) -> None:
        """The DB unique constraint on (gateway_key, gateway_payment_id)
        maps to DuplicateExternalIdError in the repository layer."""
        assert issubclass(DuplicateExternalIdError, Exception)


class TestProviderInvoiceBinding:
    def test_binds_total_without_mutating_original_or_credit_snapshot(self) -> None:
        original = PaymentSession(
            user_id=uuid4(),
            gateway_key="atlaspay",
            amount_minor=250000,
            currency="IRT",
            idempotency_key="atlas-domain-snapshot",
            credit_amount_minor=250,
            credit_currency="EUR",
            fx_source="fixture",
            fx_rate="100000",
            fx_path="EUR->IRT",
        )
        tracking_code = "5c23c12c9fa0c8b3"  # pragma: allowlist secret -- public fixture
        metadata = {
            "payment_url": "https://t.me/atlaspaybot/pay?startapp=real",
            "tracking_code": tracking_code,
        }
        bound = original.with_payment_intent("66", 259739, metadata)
        assert original.gateway_payment_id is None
        assert original.amount_minor == 250000
        assert original.payment_details is None
        assert bound.amount_minor == 259739
        assert bound.credit_amount_minor == 250
        assert bound.credit_currency == "EUR"
        assert bound.fx_rate == "100000"
        assert bound.payment_details == metadata
        metadata["tracking_code"] = "changed-at-caller"
        assert bound.payment_details["tracking_code"] == tracking_code

    def test_customer_metadata_survives_each_terminal_transition(self) -> None:
        original = PaymentSession(
            user_id=uuid4(),
            gateway_key="atlaspay",
            amount_minor=250000,
            currency="IRT",
            idempotency_key="atlas-domain-metadata",
        )
        bound = original.with_payment_intent("66", 259739, {"tracking_code": "random-safe-code"})
        succeeded = bound.mark_succeeded(gateway_payment_id="66")
        credited = succeeded.mark_credited(at=datetime(2026, 8, 22, tzinfo=UTC))
        assert credited.payment_details == bound.payment_details
        another = original.with_payment_intent("67", 259739, {"tracking_code": "other-safe-code"})
        assert (
            another.mark_failed(gateway_payment_id="67").payment_details == another.payment_details
        )
