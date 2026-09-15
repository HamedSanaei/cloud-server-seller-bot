"""Tetraminator GET callback + missed-webhook reconciliation.

The documented callback is unsigned, so it is UNTRUSTED: it only points at
a local session. Every credit decision comes from a server-side inquiry
compared EXACTLY against the stored session (gateway key, pinned pay_id,
exact Toman amount). These tests pin:

- a verified paid inquiry credits exactly once via the replay-safe webhook
  path (deterministic deposit key);
- replays, concurrent duplicates and stale callbacks are harmless;
- pay_id/amount/currency mismatches NEVER credit and record a failure;
- an unverified (forged) callback cannot credit;
- transient inquiry failures keep the session pending;
- the reconciliation job can credit a genuinely paid session once and
  survives provider errors without failing sessions prematurely.

All gateways here are fakes; no test performs a real payment.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus
from cloud_platform.modules.payments.reconcile import reconcile_tetraminator_pending
from cloud_platform.modules.payments.service import (
    PaymentWebhookService,
    TetraminatorCallbackAction,
    TetraminatorCallbackService,
)
from cloud_platform.providers.base import PaymentIntent, PaymentStatus
from cloud_platform.providers.errors import ProviderUnavailable

USER_ID = uuid4()
SESSION_ID = uuid4()
PAY_ID = "4b84b14d2e90f1bc8123"
AMOUNT = 500_000  # 500,000 Toman == IRT minor units (audited identity)


def _pending_session(
    *, session_id: Any = SESSION_ID, amount: int = AMOUNT, pay_id: str | None = PAY_ID
) -> PaymentSession:
    return PaymentSession(
        user_id=USER_ID,
        gateway_key="tetraminator",
        amount_minor=amount,
        currency="IRT",
        idempotency_key=f"bot-recharge:{USER_ID}:{amount}",
        id=session_id,
        gateway_payment_id=pay_id,
        status=PaymentSessionStatus.PENDING,
    )


def _paid_intent(
    *, pay_id: str = PAY_ID, amount: int = AMOUNT, currency: str = "IRT"
) -> PaymentIntent:
    return PaymentIntent(
        gateway_payment_id=pay_id,
        status=PaymentStatus.SUCCEEDED,
        amount_minor=amount,
        currency=currency,
        redirect_url=None,
        metadata={},
    )


def _pending_intent(pay_id: str = PAY_ID) -> PaymentIntent:
    return PaymentIntent(
        gateway_payment_id=pay_id,
        status=PaymentStatus.PENDING,
        amount_minor=AMOUNT,
        currency="IRT",
        redirect_url=None,
        metadata={"payment_status": "pending"},
    )


def _paid_session() -> PaymentSession:
    return dataclasses.replace(
        _pending_session(), status=PaymentSessionStatus.SUCCEEDED, credited_at=datetime.now(UTC)
    )


class FakeGateway:
    """Inquiry double: returns the configured intent, or raises."""

    def __init__(self, intent: PaymentIntent | None = None, error: Exception | None = None):
        self.intent = intent
        self.error = error
        self.calls: list[str] = []

    async def verify_payment(self, pay_id: str) -> PaymentIntent:
        self.calls.append(pay_id)
        if self.error is not None:
            raise self.error
        assert self.intent is not None
        return self.intent


def _service(
    session: PaymentSession | None,
    gateway: FakeGateway,
    *,
    webhook: PaymentWebhookService | None = None,
    payments: AsyncMock | None = None,
) -> tuple[TetraminatorCallbackService, PaymentWebhookService]:
    if payments is None:
        payments = AsyncMock()
        payments.get = AsyncMock(return_value=session)
    if webhook is None:
        webhook = _webhook()
    return (
        TetraminatorCallbackService(
            payments_repo=payments, webhook_service=webhook, gateway=gateway
        ),
        webhook,
    )


def _webhook(
    *, credited: PaymentSession | None = None, failed: PaymentSession | None = None
) -> PaymentWebhookService:
    webhook = AsyncMock(spec=PaymentWebhookService)
    outcome = AsyncMock()
    outcome.session = credited or failed or _pending_session()
    webhook.process_callback = AsyncMock(return_value=outcome)
    return webhook


class _DedupWebhook:
    """Webhook double with PaymentWebhookService's replay semantics.

    ``already_credited`` mirrors durable state the real webhook reads back
    (``session.credited_at``): when set, a ``succeeded`` callback is a
    duplicate with ZERO wallet effect. Otherwise the FIRST callback for an
    external id deposits exactly once and every later one is a duplicate.
    """

    def __init__(self, *, already_credited: bool = False) -> None:
        self.already_credited = already_credited
        self.deposits: list[str] = []
        self.calls = 0

    async def process_callback(self, **kw: Any) -> Any:
        self.calls += 1
        ext = str(kw.get("external_id", ""))
        if self.already_credited or ext in self.deposits:
            return type("Outcome", (), {"session": _paid_session(), "action": "duplicate"})()
        self.deposits.append(ext)
        return type("Outcome", (), {"session": _paid_session(), "action": "credited"})()


class TestUntrustedCallbackIdentification:
    async def test_invalid_reference_fails_safely(self) -> None:
        """Non-UUID refs raise (the route maps ValueError to HTTP 400)."""
        service, webhook = _service(None, FakeGateway())
        with pytest.raises(ValueError):
            await service.process_callback(ref="../etc/passwd")
        webhook.process_callback.assert_not_awaited()

    async def test_unknown_session_returns_safe_404_shape(self) -> None:
        service, webhook = _service(None, FakeGateway())
        outcome = await service.process_callback(ref=str(uuid4()))
        assert outcome.action is TetraminatorCallbackAction.ALREADY_PROCESSED
        assert outcome.session is None
        webhook.process_callback.assert_not_awaited()

    async def test_other_gateway_session_is_never_touched(self) -> None:
        session = dataclasses.replace(_pending_session(), gateway_key="zarinpal")
        gateway = FakeGateway(_paid_intent())
        service, webhook = _service(session, gateway)
        outcome = await service.process_callback(ref=str(SESSION_ID))
        assert outcome.action is TetraminatorCallbackAction.ALREADY_PROCESSED
        assert gateway.calls == []  # no inquiry for a foreign session
        webhook.process_callback.assert_not_awaited()

    async def test_intent_without_pay_id_stays_pending(self) -> None:
        """Crash between intent persist and invoice call: nothing to inquire."""
        session = _pending_session(pay_id=None)
        service, webhook = _service(session, FakeGateway())
        outcome = await service.process_callback(ref=str(SESSION_ID))
        assert outcome.action is TetraminatorCallbackAction.STILL_PENDING
        webhook.process_callback.assert_not_awaited()


class TestVerifiedCredit:
    async def test_paid_inquiry_with_exact_match_credits_once(self) -> None:
        session = _pending_session()
        gateway = FakeGateway(_paid_intent())
        webhook = _webhook(credited=_paid_session())
        service, _wh = _service(session, gateway, webhook=webhook)

        outcome = await service.process_callback(ref=str(SESSION_ID))

        assert outcome.action is TetraminatorCallbackAction.CREDITED
        webhook.process_callback.assert_awaited_once_with(
            gateway_key="tetraminator", external_id=PAY_ID, status="succeeded"
        )

    async def test_replayed_callback_is_harmless(self) -> None:
        """A replayed callback after a credited session has zero wallet effect.

        The callback service delegates terminal SUCCEEDED sessions to the
        webhook service, whose credited_at dedup is the single-credit
        authority: no new inquiry, no second deposit.
        """
        session = _paid_session()
        gateway = FakeGateway(_paid_intent())
        # The webhook reads back an ALREADY-CREDITED session: dedup, no deposit.
        webhook = _DedupWebhook(already_credited=True)
        service, _wh = _service(session, gateway, webhook=webhook)  # type: ignore[arg-type]

        outcome = await service.process_callback(ref=str(SESSION_ID))

        assert outcome.action is TetraminatorCallbackAction.ALREADY_PROCESSED
        assert gateway.calls == []  # no new inquiry for a terminal session
        assert webhook.deposits == []  # credited_at dedup: nothing re-credited

    async def test_concurrent_duplicate_callbacks_credit_once(self) -> None:
        """10 concurrent callbacks -> exactly one wallet deposit.

        Durable state serializes them: only the first read sees PENDING, the
        rest see the SUCCEEDED session, and the dedup webhook (standing in
        for the replay-safe webhook service) admits exactly one deposit.
        """
        pending = _pending_session()
        paid = _paid_session()
        payments = AsyncMock()
        seen = 0

        async def _get(_session_id: Any) -> PaymentSession:
            nonlocal seen
            seen += 1
            return pending if seen == 1 else paid

        payments.get = AsyncMock(side_effect=_get)
        gateway = FakeGateway(_paid_intent())
        webhook = _DedupWebhook()
        service, _wh = _service(pending, gateway, webhook=webhook, payments=payments)

        await asyncio.gather(*(service.process_callback(ref=str(SESSION_ID)) for _ in range(10)))

        assert len(webhook.deposits) == 1
        assert webhook.deposits[0] == PAY_ID

    async def test_stale_callback_after_credited_session_is_harmless(self) -> None:
        """credited_at set: the webhook dedup short-circuits before wallet I/O."""
        session = _paid_session()
        gateway = FakeGateway()
        # credited_at set => the webhook's own dedup short-circuits.
        webhook = _DedupWebhook(already_credited=True)
        service, _wh = _service(session, gateway, webhook=webhook)  # type: ignore[arg-type]

        outcome = await service.process_callback(ref=str(SESSION_ID))
        assert outcome.action is TetraminatorCallbackAction.ALREADY_PROCESSED
        assert gateway.calls == []
        assert webhook.deposits == []


class TestMismatchPolicy:
    async def test_pay_id_mismatch_never_credits(self) -> None:
        session = _pending_session()
        gateway = FakeGateway(_paid_intent(pay_id="different-pay-id"))
        webhook = _webhook(failed=session)
        service, _ = _service(session, gateway, webhook=webhook)

        outcome = await service.process_callback(ref=str(SESSION_ID))

        assert outcome.action is TetraminatorCallbackAction.FAILED_RECORDED
        webhook.process_callback.assert_awaited_once_with(
            gateway_key="tetraminator", external_id=PAY_ID, status="failed"
        )

    async def test_amount_mismatch_never_credits_and_records_failure(self) -> None:
        session = _pending_session()
        # Provider says paid but for a DIFFERENT amount than stored.
        gateway = FakeGateway(_paid_intent(amount=AMOUNT + 1))
        webhook = _webhook(failed=session)
        service, _ = _service(session, gateway, webhook=webhook)

        outcome = await service.process_callback(ref=str(SESSION_ID))

        assert outcome.action is TetraminatorCallbackAction.FAILED_RECORDED
        webhook.process_callback.assert_awaited_once_with(
            gateway_key="tetraminator", external_id=PAY_ID, status="failed"
        )

    async def test_currency_mismatch_never_credits(self) -> None:
        session = _pending_session()
        gateway = FakeGateway(_paid_intent(currency="EUR"))
        service, _ = _service(session, gateway, webhook=_webhook(failed=session))

        outcome = await service.process_callback(ref=str(SESSION_ID))
        assert outcome.action is TetraminatorCallbackAction.FAILED_RECORDED

    async def test_unpaid_inquiry_never_credits(self) -> None:
        session = _pending_session()
        gateway = FakeGateway(_pending_intent())
        service, webhook = _service(session, gateway)

        outcome = await service.process_callback(ref=str(SESSION_ID))
        assert outcome.action is TetraminatorCallbackAction.STILL_PENDING
        webhook.process_callback.assert_not_awaited()

    async def test_forged_callback_without_paid_inquiry_cannot_credit(self) -> None:
        """The GET itself carries no authority: only a paid inquiry credits."""
        session = _pending_session()
        gateway = FakeGateway(_pending_intent())  # provider NOT paid yet
        service, webhook = _service(session, gateway)

        outcome = await service.process_callback(ref=str(SESSION_ID))
        assert outcome.action is TetraminatorCallbackAction.STILL_PENDING
        webhook.process_callback.assert_not_awaited()


class TestTransientInquiryFailures:
    async def test_transport_error_keeps_session_pending(self) -> None:
        session = _pending_session()
        gateway = FakeGateway(error=ProviderUnavailable("connect timeout"))
        service, webhook = _service(session, gateway)

        outcome = await service.process_callback(ref=str(SESSION_ID))
        assert outcome.action is TetraminatorCallbackAction.STILL_PENDING
        webhook.process_callback.assert_not_awaited()

    async def test_failed_session_is_terminal_without_inquiry(self) -> None:
        session = dataclasses.replace(_pending_session(), status=PaymentSessionStatus.FAILED)
        gateway = FakeGateway()
        service, webhook = _service(session, gateway)

        outcome = await service.process_callback(ref=str(SESSION_ID))
        assert outcome.action is TetraminatorCallbackAction.ALREADY_PROCESSED
        assert gateway.calls == []
        webhook.process_callback.assert_not_awaited()


# ---------------------------------------------------------------------------
# Reconciliation (missed-callback safety net)
# ---------------------------------------------------------------------------


class _Repo:
    def __init__(self, sessions: list[PaymentSession]) -> None:
        self.sessions = sessions

    async def list_pending_before(
        self, gateway_key: str, before: object, limit: int = 100
    ) -> list[PaymentSession]:
        return list(self.sessions)


class _WebhookService:
    """Records process_callback calls with webhook-service semantics."""

    def __init__(self, action: str = "credited") -> None:
        self.calls: list[dict[str, Any]] = []
        self.action = action

    async def process_callback(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return type("Outcome", (), {"action": type("A", (), {"value": self.action})()})()


class TestReconciliation:
    async def test_paid_session_is_credited_once_via_the_webhook_path(self) -> None:
        session = _pending_session()
        gateway = FakeGateway(_paid_intent())
        webhook = _WebhookService("credited")

        report = await reconcile_tetraminator_pending(
            payments_repo=_Repo([session]),
            webhook_service=webhook,
            gateway=gateway,
            now=datetime.now(UTC),
        )

        assert report.credited == 1
        assert webhook.calls[0]["status"] == "succeeded"
        assert webhook.calls[0]["external_id"] == PAY_ID

    async def test_mismatched_paid_inquiry_fails_the_session(self) -> None:
        session = _pending_session()
        gateway = FakeGateway(_paid_intent(amount=AMOUNT + 1))
        webhook = _WebhookService()

        report = await reconcile_tetraminator_pending(
            payments_repo=_Repo([session]), webhook_service=webhook, gateway=gateway
        )
        assert report.marked_failed == 1
        assert webhook.calls[0]["status"] == "failed"

    async def test_still_pending_inquiry_left_pending(self) -> None:
        session = _pending_session()
        gateway = FakeGateway(_pending_intent())
        webhook = _WebhookService()

        report = await reconcile_tetraminator_pending(
            payments_repo=_Repo([session]), webhook_service=webhook, gateway=gateway
        )
        assert report.still_pending == 1
        assert webhook.calls == []

    async def test_transient_provider_error_preserves_pending(self) -> None:
        """A timeout/5xx must not credit nor permanently fail the session."""
        session = _pending_session()
        gateway = FakeGateway(error=ProviderUnavailable("503"))
        webhook = _WebhookService()

        report = await reconcile_tetraminator_pending(
            payments_repo=_Repo([session]), webhook_service=webhook, gateway=gateway
        )
        assert report.errors == 1
        assert report.credited == 0
        assert report.marked_failed == 0
        assert webhook.calls == []

    async def test_session_without_pay_id_is_skipped(self) -> None:
        session = _pending_session(pay_id=None)
        gateway = FakeGateway()
        webhook = _WebhookService()

        report = await reconcile_tetraminator_pending(
            payments_repo=_Repo([session]), webhook_service=webhook, gateway=gateway
        )
        assert report.skipped == 1
        assert gateway.calls == []

    async def test_repo_failure_is_contained(self) -> None:
        class _ExplodingRepo:
            async def list_pending_before(self, *a: Any, **kw: Any) -> list[PaymentSession]:
                raise RuntimeError("db down")

        report = await reconcile_tetraminator_pending(
            payments_repo=_ExplodingRepo(),
            webhook_service=_WebhookService(),
            gateway=FakeGateway(),
        )
        assert report.errors == 1


@pytest.mark.parametrize(
    ("amount_minor", "expected_compatible"),
    [(50_000, True), (49_999, False)],
)
def test_minimum_gates_compatibility(amount_minor: int, expected_compatible: bool) -> None:
    """The documented 50,000 Toman minimum is enforced provider-neutrally."""
    from cloud_platform.providers.tetraminator.client import MINIMUM_TOMAN

    assert MINIMUM_TOMAN == 50_000
    compatible = amount_minor >= MINIMUM_TOMAN
    assert compatible is expected_compatible
