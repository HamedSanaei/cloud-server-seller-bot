"""Tests for PaymentWebhookService: duplicate callback cannot duplicate deposit (M09-003)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
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
from cloud_platform.modules.wallet.domain import DuplicateIdempotencyError, LedgerEntryType

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
    payments = AsyncMock()
    wallet = AsyncMock()
    ledger = AsyncMock()
    ledger.get_entry_by_idempotency = AsyncMock(return_value=None)
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
        wallet.add_funds.assert_awaited_once_with(USER_ID, 500, DEPOSIT_KEY)

        args, kwargs = ledger.post_entry.call_args
        assert args[3] is LedgerEntryType.DEPOSIT
        assert args[4] == DEPOSIT_KEY
        assert kwargs["reference_id"] == EXT
        payments.save.assert_awaited_once()

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
        wallet.add_funds.assert_awaited_once_with(USER_ID, 500, DEPOSIT_KEY)


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
        wallet.add_funds.assert_not_awaited()
        ledger.post_entry.assert_not_awaited()
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
        wallet.add_funds.assert_awaited_once()

    async def test_ledger_replay_skips_wallet_debit(self) -> None:
        """If the ledger entry already exists, the wallet must not be re-debited."""
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=_pending())
        ledger.get_entry_by_idempotency = AsyncMock(return_value=MagicMock())

        outcome = await _service(payments, wallet, ledger).process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="succeeded",
        )

        assert outcome.action is WebhookAction.CREDITED
        wallet.add_funds.assert_not_awaited()
        ledger.post_entry.assert_not_awaited()
        payments.save.assert_awaited_once()

    async def test_concurrent_duplicate_ledger_post_is_tolerated(self) -> None:
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=_pending())
        ledger.post_entry = AsyncMock(
            side_effect=DuplicateIdempotencyError("duplicate deposit key")
        )

        outcome = await _service(payments, wallet, ledger).process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="succeeded",
        )

        assert outcome.action is WebhookAction.CREDITED
        assert outcome.session.credited_at is not None  # still marked credited


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
        wallet.add_funds.assert_not_awaited()
        ledger.post_entry.assert_not_awaited()

    async def test_duplicate_failed_callback_ignored(self) -> None:
        payments, wallet, ledger = _repos()
        payments.get_by_external_id = AsyncMock(return_value=_failed())

        outcome = await _service(payments, wallet, ledger).process_callback(
            gateway_key="zarinpal",
            external_id=EXT,
            status="failed",
        )

        assert outcome.action is WebhookAction.DUPLICATE_IGNORED
        wallet.add_funds.assert_not_awaited()

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
