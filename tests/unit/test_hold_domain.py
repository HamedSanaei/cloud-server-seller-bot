"""Tests for wallet hold domain: Hold aggregate, HoldStatus, HoldError."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldError,
    HoldStatus,
    InsufficientHoldBalanceError,
)


class TestHoldErrorHierarchy:
    def test_hold_error_is_wallet_error(self) -> None:
        assert isinstance(HoldError("fail"), HoldError)

    def test_insufficient_hold_balance_is_hold_error(self) -> None:
        exc = InsufficientHoldBalanceError("no funds")
        assert isinstance(exc, HoldError)
        assert str(exc) == "no funds"


class TestHoldConstruction:
    def test_creates_created_hold(self) -> None:
        wallet_id = uuid4()
        hold = Hold(
            wallet_id=wallet_id,
            amount=1000,
            currency="USD",
            idempotency_key="hold-1",
            id=uuid4(),
        )
        assert hold.status is HoldStatus.CREATED
        assert hold.currency == "USD"
        assert hold.amount == 1000

    def test_currency_uppercased(self) -> None:
        hold = Hold(
            wallet_id=uuid4(),
            amount=500,
            currency="eur",
            idempotency_key="k",
            id=uuid4(),
        )
        assert hold.currency == "EUR"

    def test_invalid_currency_rejected(self) -> None:
        with pytest.raises(ValueError, match="3-letter"):
            Hold(
                wallet_id=uuid4(),
                amount=100,
                currency="XX",
                idempotency_key="k",
                id=uuid4(),
            )

    def test_non_created_hold_needs_id(self) -> None:
        with pytest.raises(ValueError, match="persisted id"):
            Hold(
                wallet_id=uuid4(),
                amount=100,
                currency="USD",
                idempotency_key="k",
                status=HoldStatus.CAPTURED,
            )


class TestHoldCapture:
    def test_creates_hold_then_captures(self) -> None:
        wallet_id = uuid4()
        hold = Hold(wallet_id=wallet_id, amount=100, currency="USD", idempotency_key="h1")
        assert hold.status is HoldStatus.CREATED

        hold.capture()
        assert hold.status is HoldStatus.CAPTURED
        assert hold.captured_at is not None

    def test_captures_with_custom_datetime(self) -> None:
        from datetime import UTC, datetime

        wallet_id = uuid4()
        hold = Hold(wallet_id=wallet_id, amount=100, currency="USD", idempotency_key="h1")
        at = datetime(2026, 1, 1, tzinfo=UTC)

        hold.capture(at=at)
        assert hold.captured_at == at

    def test_cannot_capture_already_released(self) -> None:
        wallet_id = uuid4()
        hold = Hold(wallet_id=wallet_id, amount=100, currency="USD", idempotency_key="h1")
        hold.release()

        with pytest.raises(ValueError, match="released"):
            hold.capture()


class TestHoldRelease:
    def test_creates_hold_then_releases(self) -> None:
        wallet_id = uuid4()
        hold = Hold(wallet_id=wallet_id, amount=100, currency="USD", idempotency_key="h1")
        assert hold.status is HoldStatus.CREATED

        hold.release()
        assert hold.status is HoldStatus.RELEASED
        assert hold.released_at is not None

    def test_releases_with_custom_datetime(self) -> None:
        from datetime import UTC, datetime

        wallet_id = uuid4()
        hold = Hold(wallet_id=wallet_id, amount=100, currency="USD", idempotency_key="h1")
        at = datetime(2026, 1, 1, tzinfo=UTC)

        hold.release(at=at)
        assert hold.released_at == at

    def test_cannot_release_already_captured(self) -> None:
        wallet_id = uuid4()
        hold = Hold(wallet_id=wallet_id, amount=100, currency="USD", idempotency_key="h1")
        hold.capture()

        with pytest.raises(ValueError, match="captured"):
            hold.release()
