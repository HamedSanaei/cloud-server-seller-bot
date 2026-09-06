"""Tests for the payment reconciliation job (M09-007): stuck sessions rechecked safely."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus
from cloud_platform.modules.payments.reconcile import PaymentReconciliationService
from cloud_platform.providers.base import PaymentIntent, PaymentStatus


@dataclass
class FakePaymentsRepo:
    sessions: list[PaymentSession]

    async def list_stuck_pending(self, cutoff: datetime) -> list[PaymentSession]:
        return [s for s in self.sessions if s.status is PaymentSessionStatus.PENDING]


class FakeWebhook:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def process_callback(self, **kw: Any) -> Any:
        self.calls.append(kw)

        @dataclass(frozen=True, slots=True)
        class Outcome:
            action: Any

        @dataclass(frozen=True, slots=True)
        class Action:
            value: str = "credited"

        return Outcome(
            action=Action(value="credited" if kw["status"] == "succeeded" else "failed_recorded")
        )


class FakeGateway:
    gateway_key = "zarinpal"

    def __init__(self, status: PaymentStatus) -> None:
        self._status = status

    async def verify_with_amount(self, authority: str, amount_minor: int) -> PaymentIntent:
        return PaymentIntent(
            gateway_payment_id=authority,
            status=self._status,
            amount_minor=amount_minor,
            currency="IRR",
        )


def _pending(external_id: str | None = "A0001") -> PaymentSession:
    return PaymentSession(
        user_id=uuid4(),
        gateway_key="zarinpal",
        amount_minor=50000,
        currency="IRR",
        idempotency_key=f"reconcile-test-{external_id or 'none'}",
        gateway_payment_id=external_id,
    )


class TestReconciliation:
    async def test_succeeded_verify_credits_via_webhook(self) -> None:
        webhook = FakeWebhook()
        service = PaymentReconciliationService(
            payments_repo=FakePaymentsRepo([_pending()]),
            webhook_service=webhook,
            gateway=FakeGateway(PaymentStatus.SUCCEEDED),
        )
        report = await service.run(now=datetime.now(UTC))
        assert report.credited == 1
        assert webhook.calls[0]["status"] == "succeeded"

    async def test_failed_verify_marks_failed(self) -> None:
        webhook = FakeWebhook()
        service = PaymentReconciliationService(
            payments_repo=FakePaymentsRepo([_pending()]),
            webhook_service=webhook,
            gateway=FakeGateway(PaymentStatus.FAILED),
        )
        report = await service.run(now=datetime.now(UTC))
        assert report.marked_failed == 1

    async def test_session_without_external_id_skipped(self) -> None:
        service = PaymentReconciliationService(
            payments_repo=FakePaymentsRepo([_pending(None)]),
            webhook_service=FakeWebhook(),
            gateway=FakeGateway(PaymentStatus.SUCCEEDED),
        )
        report = await service.run(now=datetime.now(UTC))
        assert report.skipped == 1
        assert report.checked == 0

    async def test_gateway_error_counted_not_raised(self) -> None:
        class BoomGateway(FakeGateway):
            async def verify_with_amount(self, authority: str, amount_minor: int) -> PaymentIntent:
                raise RuntimeError("gateway down")

        service = PaymentReconciliationService(
            payments_repo=FakePaymentsRepo([_pending()]),
            webhook_service=FakeWebhook(),
            gateway=BoomGateway(PaymentStatus.SUCCEEDED),
        )
        report = await service.run(now=datetime.now(UTC))
        assert report.errors == 1
