"""Tests for PaymentWebhookService: duplicate callback cannot duplicate deposit (M09-003)."""

from __future__ import annotations

import dataclasses
import types
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.payments.domain import (
    InvalidPaymentSessionTransition,
    PaymentSession,
    PaymentSessionStatus,
)
from cloud_platform.modules.payments.service import (
    PaymentWebhookService,
    WebhookAction,
)

USER_ID = uuid4()
EXT = "ext-1"
DEPOSIT_KEY = f"deposit-zarinpal-{EXT}"


def _with_id(session: PaymentSession) -> PaymentSession:
    return dataclasses.replace(session, id=uuid4())


def _pending() -> PaymentSession:
    return _with_id(
        PaymentSession(
            user_id=USER_ID,
            gateway_key="zarinpal",
            amount_minor=500,
            currency="EUR",
            idempotency_key="webhook-zarinpal-ext-1",
            gateway_payment_id=EXT,
        )
    )


def _succeeded(credited: bool = False) -> PaymentSession:
    session = _pending().mark_succeeded(gateway_payment_id=EXT)
    if credited:
        session = session.mark_credited(at=datetime(2026, 8, 22, tzinfo=UTC))
    return session


def _failed() -> PaymentSession:
    return _pending().mark_failed(gateway_payment_id=EXT)


def _repos() -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """AsyncMock repos with the atomic-deposit contract.

    ``credit_deposit`` returns ``(wallet, applied=True)`` and carries the
    deterministic deposit key; the wallet mock exposes a ``balance`` so the
    business-event payload can report it.
    """
    payments = AsyncMock()
    wallet = AsyncMock()
    ledger = AsyncMock()
    wallet.get = AsyncMock(return_value=types.SimpleNamespace(currency="EUR"))
    wallet.credit_deposit = AsyncMock(return_value=(types.SimpleNamespace(balance=500), True))
    payments.save = AsyncMock(side_effect=lambda s: s)
    return payments, wallet, ledger


def _service(payments: AsyncMock, wallet: AsyncMock, ledger: AsyncMock) -> PaymentWebhookService:
    return PaymentWebhookService(payments, wallet, ledger)


class TestFirstSuccess:
    async def test_creates_session_and_credits_wallet(self) -> None:
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=None)
        payments.create = AsyncMock(side_effect=_with_id)

        outcome = await _service(payments, wallet, ledger).process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="succeeded",
            user_id=USER_ID,
            amount_minor=500,
            currency="EUR",
        )

        assert outcome.action is WebhookAction.CREDITED
        assert outcome.session.status is PaymentSessionStatus.SUCCEEDED
        assert outcome.session.credited_at is not None
        wallet.credit_deposit.assert_awaited_once_with(
            USER_ID,
            500,
            DEPOSIT_KEY,
            reference=f"zarinpal/{EXT}",
            expected_currency="EUR",
            business_event_factory=None,
        )

        # The deposit key is deterministic from (gateway, external id).
        key = wallet.credit_deposit.await_args.args[2]
        assert key == DEPOSIT_KEY
        assert payments.save.await_count == 2  # success claim, then credit marker

    async def test_existing_pending_session_is_credited(self) -> None:
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=_pending())

        outcome = await _service(payments, wallet, ledger).process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="succeeded",
        )

        assert outcome.action is WebhookAction.CREDITED
        payments.create.assert_not_awaited()
        wallet.credit_deposit.assert_awaited_once_with(
            USER_ID,
            500,
            DEPOSIT_KEY,
            reference=f"zarinpal/{EXT}",
            expected_currency="EUR",
            business_event_factory=None,
        )


class TestReplaySafety:
    async def test_duplicate_success_callback_does_not_credit_again(self) -> None:
        """The core acceptance: a duplicated callback cannot duplicate deposit."""
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=_succeeded(credited=True))

        outcome = await _service(payments, wallet, ledger).process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="succeeded",
        )

        assert outcome.action is WebhookAction.DUPLICATE_IGNORED
        wallet.credit_deposit.assert_not_awaited()
        payments.save.assert_not_awaited()

    async def test_late_callback_credits_uncredited_succeeded_session(self) -> None:
        """Crash between mark_succeeded and the deposit => late credit, once."""
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=_succeeded(credited=False))

        outcome = await _service(payments, wallet, ledger).process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="succeeded",
        )

        assert outcome.action is WebhookAction.LATE_CREDIT
        wallet.credit_deposit.assert_awaited_once()

    async def test_already_applied_deposit_is_not_credited_again(self) -> None:
        """A concurrent duplicate (applied=False) still marks the session credited."""
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=_pending())
        wallet.credit_deposit = AsyncMock(return_value=(types.SimpleNamespace(balance=500), False))

        outcome = await _service(payments, wallet, ledger).process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="succeeded",
        )

        assert outcome.action is WebhookAction.CREDITED
        assert outcome.session.credited_at is not None
        assert payments.save.await_count == 2

    async def test_concurrent_duplicate_deposit_is_tolerated(self) -> None:
        """Losing the atomic deposit race still settles the session, once."""
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=_pending())
        # The concurrent winner already applied this exact deposit.
        wallet.credit_deposit = AsyncMock(return_value=(types.SimpleNamespace(balance=500), False))

        outcome = await _service(payments, wallet, ledger).process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="succeeded",
        )

        assert outcome.action is WebhookAction.CREDITED
        assert outcome.session.credited_at is not None  # still marked credited
        wallet.credit_deposit.assert_awaited_once()


class TestFailurePath:
    async def test_failed_callback_records_failure_without_money(self) -> None:
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=None)
        payments.create = AsyncMock(side_effect=_with_id)

        outcome = await _service(payments, wallet, ledger).process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="failed",
            user_id=USER_ID,
            amount_minor=500,
            currency="EUR",
        )

        assert outcome.action is WebhookAction.FAILED_RECORDED
        assert outcome.session.status is PaymentSessionStatus.FAILED
        wallet.credit_deposit.assert_not_awaited()

    async def test_duplicate_failed_callback_ignored(self) -> None:
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=_failed())

        outcome = await _service(payments, wallet, ledger).process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="failed",
        )

        assert outcome.action is WebhookAction.DUPLICATE_IGNORED
        wallet.credit_deposit.assert_not_awaited()

    async def test_failure_after_success_raises_transition_error(self) -> None:
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=_succeeded(credited=True))

        with pytest.raises(InvalidPaymentSessionTransition):
            await _service(payments, wallet, ledger).process_callback(
                gateway_key="zarinpal",
                external_id=EXT,
                status="failed",
            )


class TestValidation:
    async def test_unknown_payment_requires_self_describing_fields(self) -> None:
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=None)

        with pytest.raises(ValueError, match="user_id"):
            await _service(payments, wallet, ledger).process_callback(
                gateway_key="zarinpal",
                external_id=EXT,
                status="succeeded",
            )

    async def test_empty_external_id_rejected(self) -> None:
        service = _service(*_repos())
        with pytest.raises(ValueError, match="external"):
            await service.process_callback(
                gateway_key="zarinpal", external_id="  ", status="succeeded"
            )

    async def test_unknown_status_rejected(self) -> None:
        service = _service(*_repos())
        with pytest.raises(ValueError, match="status"):
            await service.process_callback(
                gateway_key="zarinpal", external_id=EXT, status="refunded"
            )


class _RecordingSink:
    """Records enqueued business events (protocol-compatible sink double)."""

    def __init__(self, *, explode: bool = False) -> None:
        self.events: list[Any] = []
        self._explode = explode

    async def emit(self, event: Any) -> bool:
        if self._explode:
            raise RuntimeError("outbox is down")
        self.events.append(event)
        return True

    def types(self) -> list[Any]:
        return [e.event_type for e in self.events]


@pytest.fixture
def outbox_store():
    from tests.unit.test_financial_payment_invariants import SqlStore

    store = SqlStore()
    try:
        yield store
    finally:
        store.engine.dispose()


class TestBusinessLogEmission:
    """Success notification, balance and ledger share one database commit."""

    def _service(self, sink: object) -> PaymentWebhookService:
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=None)
        payments.create = AsyncMock(side_effect=_with_id)
        return PaymentWebhookService(
            payments,
            wallet,
            ledger,
            event_sink=sink,
            user_repo=None,  # type: ignore[arg-type]
        )

    def _persistent_service(self, store, *, balance=0, sink=None):
        store.seed(_pending(), balance=balance, wallet_currency="EUR")
        return PaymentWebhookService(
            store.payments,
            store.wallets,
            None,
            event_sink=sink if sink is not None else _RecordingSink(),
        )

    async def test_success_callback_enqueues_recharge_succeeded(self, outbox_store) -> None:
        service = self._persistent_service(outbox_store)
        outcome = await service.process_callback(
            gateway_key="zarinpal", external_id=EXT, status="succeeded"
        )
        assert outcome.action is WebhookAction.CREDITED
        (event,) = outbox_store.business_events("recharge.succeeded")
        assert event.event_type == "recharge.succeeded" and event.status == "PENDING"
        assert event.payload["amount"] == "€5.00"
        assert event.payload["gateway"] == "zarinpal"
        assert event.payload["gateway_reference"] == EXT
        assert event.event_key == f"recharge.succeeded:{outcome.session.id}"
        assert (await outbox_store.wallets.get(USER_ID)).balance == 500
        assert len(outbox_store.ledger()) == 1

    async def test_balance_after_is_reported_when_available(self, outbox_store) -> None:
        service = self._persistent_service(outbox_store, balance=734)
        await service.process_callback(gateway_key="zarinpal", external_id=EXT, status="succeeded")
        (event,) = outbox_store.business_events("recharge.succeeded")
        assert event.payload["balance_after"] == "€12.34"
        assert (await outbox_store.wallets.get(USER_ID)).balance == 1234

    async def test_duplicate_success_callback_emits_once(self, outbox_store) -> None:
        service = self._persistent_service(outbox_store)
        first = await service.process_callback(
            gateway_key="zarinpal", external_id=EXT, status="succeeded"
        )
        second = await service.process_callback(
            gateway_key="zarinpal", external_id=EXT, status="succeeded"
        )
        assert first.action is WebhookAction.CREDITED
        assert second.action is WebhookAction.DUPLICATE_IGNORED
        assert (
            len(outbox_store.business_events("recharge.succeeded"))
            == len(outbox_store.ledger())
            == 1
        )
        assert len(outbox_store.business_events("customer.notice")) == 1
        assert (await outbox_store.wallets.get(USER_ID)).balance == 500

    async def test_failed_callback_enqueues_recharge_failed_once(self) -> None:
        from cloud_platform.modules.businesslog.domain import BusinessEventType

        sink = _RecordingSink()
        service = self._service(sink)
        first = await service.process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="failed",
            user_id=USER_ID,
            amount_minor=500,
            currency="EUR",
        )
        assert first.action is WebhookAction.FAILED_RECORDED
        # Same (gateway, external) pair again -> no new event.
        service._payments.get_by_external_id = AsyncMock(return_value=first.session)
        await service.process_callback(gateway_key="zarinpal", external_id=EXT, status="failed")
        assert sink.types() == [BusinessEventType.RECHARGE_FAILED]

    async def test_delivery_sink_outage_does_not_block_transactional_deposit(
        self, outbox_store
    ) -> None:
        service = self._persistent_service(outbox_store, sink=_RecordingSink(explode=True))
        outcome = await service.process_callback(
            gateway_key="zarinpal", external_id=EXT, status="succeeded"
        )
        assert outcome.action is WebhookAction.CREDITED
        assert (await outbox_store.wallets.get(USER_ID)).balance == 500
        assert (
            len(outbox_store.business_events("recharge.succeeded"))
            == len(outbox_store.ledger())
            == 1
        )
        assert len(outbox_store.business_events("customer.notice")) == 1
