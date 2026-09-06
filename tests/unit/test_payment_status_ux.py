"""Tests for payment status Telegram UX (M09-006): pending/success/failure clear."""

from datetime import UTC, datetime
from uuid import uuid4

from cloud_platform.bot.payments_ui import render_payment_created, render_payment_status
from cloud_platform.core.i18n import Locale, Translator
from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus


def _session(status: PaymentSessionStatus, credited: bool = False) -> PaymentSession:
    session = PaymentSession(
        user_id=uuid4(),
        gateway_key="zarinpal",
        amount_minor=50000,
        currency="IRR",
        idempotency_key="test-payment-ux-key-1",
        gateway_payment_id="A0001",
        status=status,
    )
    if credited:
        session = session.mark_succeeded(gateway_payment_id="A0001").mark_credited(
            at=datetime.now(UTC)
        )
        # mark_succeeded returns SUCCEEDED; emulate credited SUCCEEDED:
        assert session.status is PaymentSessionStatus.SUCCEEDED
    return session


class TestPaymentStatusUx:
    def test_pending_screen_has_check_button(self) -> None:
        screen = render_payment_status(
            PaymentSession(
                user_id=uuid4(),
                gateway_key="zarinpal",
                amount_minor=50000,
                currency="IRR",
                idempotency_key="test-payment-ux-key-2",
                gateway_payment_id="A0001",
            ),
            amount_text="50,000",
        )
        assert (
            "انتظار" in screen.text
            or "pending" in screen.text.lower()
            or "در انتظار" in screen.text
        )
        assert screen.keyboard is not None

    def test_success_screen_clear(self) -> None:
        session = PaymentSession(
            user_id=uuid4(),
            gateway_key="zarinpal",
            amount_minor=50000,
            currency="IRR",
            idempotency_key="test-payment-ux-key-3",
            gateway_payment_id="A0001",
            status=PaymentSessionStatus.SUCCEEDED,
            credited_at=datetime.now(UTC),
        )
        screen = render_payment_status(session, amount_text="50,000")
        assert "موفق" in screen.text
        assert screen.keyboard is None

    def test_failed_screen_clear(self) -> None:
        session = PaymentSession(
            user_id=uuid4(),
            gateway_key="zarinpal",
            amount_minor=50000,
            currency="IRR",
            idempotency_key="test-payment-ux-key-4",
            gateway_payment_id="A0001",
            status=PaymentSessionStatus.FAILED,
        )
        screen = render_payment_status(session, amount_text="50,000")
        assert "ناموفق" in screen.text

    def test_created_screen_has_pay_and_check_buttons(self) -> None:
        screen = render_payment_created("https://pay.example/A0001", amount_text="50,000")
        assert screen.keyboard is not None
        assert len(screen.keyboard.inline_keyboard) == 2

    def test_english_locale_available(self) -> None:
        t = Translator(Locale.EN)
        session = PaymentSession(
            user_id=uuid4(),
            gateway_key="zarinpal",
            amount_minor=50000,
            currency="IRR",
            idempotency_key="test-payment-ux-key-5",
            gateway_payment_id="A0001",
            status=PaymentSessionStatus.FAILED,
        )
        screen = render_payment_status(session, amount_text="50,000", translator=t)
        assert "failed" in screen.text.lower()
