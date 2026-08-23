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
