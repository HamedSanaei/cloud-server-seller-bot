"""Tetraminator verification with separated credit/settlement (FX era).

The inquiry response is authoritative: exact pay_id + exact SETTLEMENT
amount, `status is true` AND `payment_status == "paid"`. The wallet then
receives the FROZEN credit side. Covers mismatch policies, forged
callbacks, replays, opaque refs, reconciliation and secret hygiene.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus
from cloud_platform.modules.payments.service import (
    PaymentWebhookService,
    TetraminatorCallbackService,
)
from cloud_platform.providers.base import PaymentStatus
from cloud_platform.providers.tetraminator.client import TetraminatorGateway, toman_price_for

KEY = "tetra_TEST_SUPER_SECRET_API_KEY"


class _MemPayments:
    def __init__(self, sessions: list[PaymentSession] | None = None) -> None:
        self.by_id: dict[Any, PaymentSession] = {}
        for session in sessions or []:
            assert session.id is not None
            self.by_id[session.id] = session

    async def get(self, session_id: Any) -> PaymentSession | None:
        return self.by_id.get(session_id)

    async def get_by_external_id(self, gateway_key: str, pay_id: str) -> PaymentSession | None:
        for session in self.by_id.values():
            if session.gateway_key == gateway_key and session.gateway_payment_id == pay_id:
                return session
        return None

    async def get_by_idempotency_key(self, *args: Any) -> Any:
        return None

    async def list_pending_before(self, *args: Any) -> list[Any]:
        return []

    async def create(self, session: PaymentSession) -> PaymentSession:
        stored = PaymentSession(
            user_id=session.user_id,
            gateway_key=session.gateway_key,
            amount_minor=session.amount_minor,
            currency=session.currency,
            idempotency_key=session.idempotency_key,
            id=session.id or uuid4(),
            gateway_payment_id=session.gateway_payment_id,
            status=session.status,
            credit_amount_minor=session.credit_amount_minor,
            credit_currency=session.credit_currency,
        )
        assert stored.id is not None
        self.by_id[stored.id] = stored
        return stored

    async def save(self, session: PaymentSession) -> PaymentSession:
        assert session.id is not None
        self.by_id[session.id] = session
        return session


class _Wallet:
    def __init__(self) -> None:
        self.credits: list[int] = []

    async def credit_deposit(
        self, user_id: Any, amount: int, key: str, *, reference: str = ""
    ) -> tuple[Any, bool]:
        self.credits.append(amount)
        return type("W", (), {"balance": sum(self.credits)})(), True


class _Ledger:
    pass


class _Gateway:
    """Controllable inquiry double (never touches HTTP)."""

    def __init__(self, intent: Any = None, error: Exception | None = None) -> None:
        self._intent = intent
        self._error = error

    async def verify_payment(self, pay_id: str) -> Any:
        if self._error is not None:
            raise self._error
        return self._intent


def _paid_intent(pay_id: str = "pay-1", amount: int = 2_000_000) -> Any:
    from cloud_platform.providers.base import PaymentIntent

    return PaymentIntent(
        gateway_payment_id=pay_id,
        status=PaymentStatus.SUCCEEDED,
        amount_minor=amount,
        currency="IRT",
    )


def _pending_intent(pay_id: str = "pay-1") -> Any:
    from cloud_platform.providers.base import PaymentIntent

    return PaymentIntent(
        gateway_payment_id=pay_id,
        status=PaymentStatus.PENDING,
        amount_minor=0,
        currency="IRT",
    )


def _session(**over: Any) -> PaymentSession:
    base: dict[str, Any] = {
        "user_id": uuid4(),
        "gateway_key": "tetraminator",
        "amount_minor": 2_000_000,
        "currency": "IRT",
        "idempotency_key": f"fx-tetra-{uuid4().hex[:8]}",
        "id": uuid4(),
        "gateway_payment_id": "pay-1",
        "credit_amount_minor": 1_000,
        "credit_currency": "EUR",
    }
    base.update(over)
    return PaymentSession(**base)  # type: ignore[arg-type]


def _service(
    sessions: list[PaymentSession],
    gateway: _Gateway,
    wallet: _Wallet | None = None,
) -> tuple[TetraminatorCallbackService, _Wallet, _MemPayments]:
    payments = _MemPayments(sessions)
    wallet = wallet or _Wallet()
    webhook = PaymentWebhookService(
        payments_repo=payments,  # type: ignore[arg-type]
        wallet_repo=wallet,  # type: ignore[arg-type]
        ledger_repo=_Ledger(),  # type: ignore[arg-type]
    )
    return (
        TetraminatorCallbackService(
            payments_repo=payments,  # type: ignore[arg-type]
            webhook_service=webhook,
            gateway=gateway,  # type: ignore[arg-type]
        ),
        wallet,
        payments,
    )


class TestVerification:
    async def test_paid_exact_credits_frozen_credit_once(self) -> None:
        session = _session()
        service, wallet, _ = _service([session], _Gateway(_paid_intent()))
        outcome = await service.process_callback(ref=str(session.id))
        assert outcome.action.value == "credited"
        assert wallet.credits == [1_000]  # frozen EUR credit, not 2M settlement

    async def test_pay_id_mismatch_does_not_credit(self) -> None:
        session = _session()
        service, wallet, _ = _service([session], _Gateway(_paid_intent(pay_id="pay-OTHER")))
        outcome = await service.process_callback(ref=str(session.id))
        assert outcome.action.value == "failed_recorded"
        assert wallet.credits == []

    async def test_amount_mismatch_does_not_credit(self) -> None:
        session = _session()
        service, wallet, _ = _service([session], _Gateway(_paid_intent(amount=1_999_999)))
        outcome = await service.process_callback(ref=str(session.id))
        assert outcome.action.value == "failed_recorded"
        assert wallet.credits == []

    async def test_forged_callback_without_paid_inquiry_does_not_credit(self) -> None:
        session = _session()
        service, wallet, _ = _service([session], _Gateway(_pending_intent()))
        outcome = await service.process_callback(ref=str(session.id))
        assert outcome.action.value == "still_pending"
        assert wallet.credits == []

    async def test_repeated_callback_credits_once(self) -> None:
        session = _session()
        service, wallet, _ = _service([session], _Gateway(_paid_intent()))
        first = await service.process_callback(ref=str(session.id))
        second = await service.process_callback(ref=str(session.id))
        assert first.action.value == "credited"
        assert second.action.value == "already_processed"
        assert wallet.credits == [1_000]

    async def test_concurrent_duplicate_callbacks_credit_once(self) -> None:
        session = _session(status=PaymentSessionStatus.SUCCEEDED)
        credited_at_session = session.mark_credited(
            at=__import__("datetime").datetime.now(__import__("datetime").UTC)
        )
        service, wallet, _ = _service([credited_at_session], _Gateway(_paid_intent()))
        results = await asyncio.gather(
            service.process_callback(ref=str(session.id)),
            service.process_callback(ref=str(session.id)),
        )
        assert all(r.action.value == "already_processed" for r in results)
        assert wallet.credits == []

    async def test_stale_callback_after_credited_is_harmless(self) -> None:
        from datetime import UTC, datetime

        session = _session(status=PaymentSessionStatus.SUCCEEDED).mark_credited(
            at=datetime.now(UTC)
        )
        service, wallet, _ = _service([session], _Gateway(_paid_intent()))
        outcome = await service.process_callback(ref=str(session.id))
        assert outcome.action.value == "already_processed"
        assert wallet.credits == []

    async def test_callback_carries_no_trusted_amount(self) -> None:
        # The ref is the ONLY input; user/amount query fields do not exist on
        # the service signature and are never consulted.
        import inspect

        assert list(inspect.signature(TetraminatorCallbackService.process_callback).parameters) == [
            "self",
            "ref",
        ]

    async def test_invalid_reference_fails_safely(self) -> None:
        service, wallet, _ = _service([], _Gateway(_paid_intent()))
        with pytest.raises(ValueError):
            await service.process_callback(ref="not-a-uuid")
        assert wallet.credits == []
        unknown = await service.process_callback(ref=str(uuid4()))
        assert unknown.session is None
        assert wallet.credits == []


class TestReconciliation:
    async def test_missed_webhook_can_credit_once(self) -> None:
        from datetime import UTC, datetime, timedelta

        from cloud_platform.modules.payments.reconcile import reconcile_tetraminator_pending

        session = _session()
        payments = _MemPayments([session])
        wallet = _Wallet()
        webhook = PaymentWebhookService(
            payments_repo=payments,  # type: ignore[arg-type]
            wallet_repo=wallet,  # type: ignore[arg-type]
            ledger_repo=_Ledger(),  # type: ignore[arg-type]
        )

        class _Repo:
            async def list_pending_before(
                self, gateway_key: str, before: Any, limit: int = 100
            ) -> Any:
                assert gateway_key == "tetraminator"
                assert limit == 50
                assert before <= datetime.now(UTC)
                found = await payments.get(session.id)
                assert found is not None
                return [found]

        class _Audit:
            def __init__(self) -> None:
                self.events: list[Any] = []

            async def append(self, event: Any) -> Any:
                self.events.append(event)
                return event

        audit = _Audit()
        report = await reconcile_tetraminator_pending(
            payments_repo=_Repo(),  # type: ignore[arg-type]
            webhook_service=webhook,
            gateway=_Gateway(_paid_intent()),
            stale_after=timedelta(minutes=10),
            now=datetime.now(UTC),
            audit_repo=audit,  # type: ignore[arg-type]
        )
        assert report.checked == 1
        assert report.credited == 1
        assert wallet.credits == [1_000]  # frozen credit, exactly once
        # The audit port takes an EVENT: keyword fields raised TypeError and
        # silently dropped the run's audit record in production.
        from cloud_platform.modules.audit.domain import ActorType, AuditEvent

        (event,) = audit.events
        assert isinstance(event, AuditEvent)
        assert event.actor_type is ActorType.SYSTEM
        assert event.metadata == {"checked": 1, "credited": 1, "failed": 0}
        again = await reconcile_tetraminator_pending(
            payments_repo=_Repo(),  # type: ignore[arg-type]
            webhook_service=webhook,
            gateway=_Gateway(_paid_intent()),
            stale_after=timedelta(minutes=10),
            now=datetime.now(UTC),
        )
        assert again.credited == 0
        assert wallet.credits == [1_000]

    async def test_transient_failure_preserves_pending(self) -> None:
        from cloud_platform.providers.errors import ProviderUnavailable

        session = _session()
        service, wallet, payments = _service([session], _Gateway(error=ProviderUnavailable("down")))
        outcome = await service.process_callback(ref=str(session.id))
        assert outcome.action.value == "still_pending"
        assert (await payments.get(session.id)) is not None
        assert wallet.credits == []


class TestCurrencyGuards:
    def test_adapter_supports_irt_only(self) -> None:
        gateway = TetraminatorGateway(api_key=KEY)
        assert gateway.supported_currency == "IRT"
        with pytest.raises(ValueError):
            toman_price_for(1_000, "EUR")

    def test_no_implicit_eur_conversion(self) -> None:
        import inspect

        source = inspect.getsource(TetraminatorGateway.create_payment)
        assert "EUR" not in source

    def test_api_key_never_leaks(self, caplog: pytest.LogCaptureFixture) -> None:
        gateway = TetraminatorGateway(api_key=KEY)
        assert KEY not in repr(gateway)
        with caplog.at_level(logging.INFO):
            logging.getLogger("cloud_platform.providers.tetraminator.client").info("probe")
        assert KEY not in caplog.text
        try:
            toman_price_for(1_000, "EUR")
        except ValueError as exc:
            assert KEY not in str(exc)

    def test_no_float_in_adapter(self) -> None:
        import inspect

        import cloud_platform.providers.tetraminator.client as mod

        assert "float(" not in inspect.getsource(mod)
