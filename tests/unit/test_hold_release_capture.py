"""Tests for hold release/capture lifecycle (M05-005).

Acceptance: capture/release idempotent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.wallet.domain import (
    DuplicateIdempotencyError,
    Hold,
    HoldError,
    HoldNotFoundError,
    HoldStateConflictError,
    HoldStatus,
    LedgerEntryType,
)
from cloud_platform.modules.wallet.repository import HoldService

WALLET_ID = uuid4()
HOLD_ID = uuid4()


def _hold(status: HoldStatus = HoldStatus.CREATED, amount: int = 500) -> Hold:
    return Hold(
        wallet_id=WALLET_ID,
        amount=amount,
        currency="EUR",
        idempotency_key="hold-key",
        id=HOLD_ID,
        status=status,
        captured_at=None if status is not HoldStatus.CAPTURED else _ts(),
        released_at=None if status is not HoldStatus.RELEASED else _ts(),
    )


def _ts() -> object:
    from datetime import UTC, datetime

    return datetime(2026, 1, 1, tzinfo=UTC)


def _service(hold_repo: AsyncMock, ledger_repo: AsyncMock) -> HoldService:
    return HoldService(
        wallet_repo=AsyncMock(),
        hold_repo=hold_repo,
        ledger_repo=ledger_repo,
    )


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


class TestReleaseHold:
    async def test_release_created_hold(self) -> None:
        hold = _hold(HoldStatus.CREATED)
        released = _hold(HoldStatus.RELEASED)

        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=hold)
        hold_repo.release_hold = AsyncMock(return_value=released)
        ledger_repo = AsyncMock()
        ledger_repo.post_entry = AsyncMock()

        service = _service(hold_repo, ledger_repo)
        result = await service.release_hold(WALLET_ID, HOLD_ID, "rel-1")

        assert result is released
        hold_repo.release_hold.assert_awaited_once_with(HOLD_ID)
        ledger_repo.post_entry.assert_awaited_once()
        args, kwargs = ledger_repo.post_entry.call_args
        assert args[1] == 500  # held amount, not zero
        assert args[3] is LedgerEntryType.RELEASE
        assert args[4] == "release-rel-1"
        assert kwargs["reference_type"] == "hold"

    async def test_release_already_released_is_idempotent(self) -> None:
        """Releasing a released hold is a no-op; ledger re-post is safe."""
        hold = _hold(HoldStatus.RELEASED)

        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=hold)
        hold_repo.release_hold = AsyncMock()
        ledger_repo = AsyncMock()
        ledger_repo.post_entry = AsyncMock()

        service = _service(hold_repo, ledger_repo)
        result = await service.release_hold(WALLET_ID, HOLD_ID, "rel-1")

        assert result is hold
        hold_repo.release_hold.assert_not_awaited()
        # ledger entry still posted idempotently (key dedupes at DB level)
        ledger_repo.post_entry.assert_awaited_once()

    async def test_release_captured_raises_conflict(self) -> None:
        hold = _hold(HoldStatus.CAPTURED)

        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=hold)
        ledger_repo = AsyncMock()
        ledger_repo.post_entry = AsyncMock()

        service = _service(hold_repo, ledger_repo)
        with pytest.raises(HoldStateConflictError):
            await service.release_hold(WALLET_ID, HOLD_ID, "rel-1")
        ledger_repo.post_entry.assert_not_awaited()

    async def test_release_not_found_raises(self) -> None:
        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=None)
        hold_repo.get_by_idempotency = AsyncMock(return_value=None)
        ledger_repo = AsyncMock()

        service = _service(hold_repo, ledger_repo)
        with pytest.raises(HoldNotFoundError):
            await service.release_hold(WALLET_ID, HOLD_ID, "rel-1")

    async def test_release_falls_back_to_idempotency_key(self) -> None:
        """When the hold id is unknown, the idempotency key resolves it."""
        hold = _hold(HoldStatus.CREATED)

        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=None)
        hold_repo.get_by_idempotency = AsyncMock(return_value=hold)
        hold_repo.release_hold = AsyncMock(return_value=_hold(HoldStatus.RELEASED))
        ledger_repo = AsyncMock()
        ledger_repo.post_entry = AsyncMock()

        service = _service(hold_repo, ledger_repo)
        await service.release_hold(WALLET_ID, uuid4(), "hold-key")

        hold_repo.get_by_idempotency.assert_awaited_once_with(WALLET_ID, "hold-key")
        hold_repo.release_hold.assert_awaited_once()

    async def test_release_ledger_duplicate_swallowed(self) -> None:
        """A duplicate ledger key from a prior attempt must not propagate."""
        hold = _hold(HoldStatus.RELEASED)

        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=hold)
        hold_repo.release_hold = AsyncMock()
        ledger_repo = AsyncMock()
        ledger_repo.post_entry = AsyncMock(side_effect=DuplicateIdempotencyError("dup"))

        service = _service(hold_repo, ledger_repo)
        result = await service.release_hold(WALLET_ID, HOLD_ID, "rel-1")
        assert result is hold


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


class TestCaptureHold:
    async def test_capture_created_hold(self) -> None:
        hold = _hold(HoldStatus.CREATED, amount=300)
        captured = _hold(HoldStatus.CAPTURED, amount=300)

        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=hold)
        hold_repo.capture_hold = AsyncMock(return_value=captured)
        ledger_repo = AsyncMock()
        ledger_repo.post_entry = AsyncMock()

        service = _service(hold_repo, ledger_repo)
        result = await service.capture_hold(WALLET_ID, HOLD_ID, "cap-1")

        assert result is captured
        hold_repo.capture_hold.assert_awaited_once_with(HOLD_ID)
        ledger_repo.post_entry.assert_awaited_once()
        args, kwargs = ledger_repo.post_entry.call_args
        assert args[1] == 300  # held amount debited as charge
        assert args[3] is LedgerEntryType.CHARGE
        assert args[4] == "capture-cap-1"
        assert kwargs["reference_type"] == "hold"

    async def test_capture_already_captured_is_idempotent(self) -> None:
        """Re-capturing is a no-op; ledger re-post is safe under its key."""
        hold = _hold(HoldStatus.CAPTURED)

        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=hold)
        hold_repo.capture_hold = AsyncMock()
        ledger_repo = AsyncMock()
        ledger_repo.post_entry = AsyncMock()

        service = _service(hold_repo, ledger_repo)
        result = await service.capture_hold(WALLET_ID, HOLD_ID, "cap-1")

        assert result is hold
        hold_repo.capture_hold.assert_not_awaited()
        ledger_repo.post_entry.assert_awaited_once()

    async def test_capture_released_raises_conflict(self) -> None:
        hold = _hold(HoldStatus.RELEASED)

        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=hold)
        ledger_repo = AsyncMock()
        ledger_repo.post_entry = AsyncMock()

        service = _service(hold_repo, ledger_repo)
        with pytest.raises(HoldStateConflictError):
            await service.capture_hold(WALLET_ID, HOLD_ID, "cap-1")
        ledger_repo.post_entry.assert_not_awaited()

    async def test_capture_not_found_raises(self) -> None:
        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=None)
        hold_repo.get_by_idempotency = AsyncMock(return_value=None)
        ledger_repo = AsyncMock()

        service = _service(hold_repo, ledger_repo)
        with pytest.raises(HoldNotFoundError):
            await service.capture_hold(WALLET_ID, HOLD_ID, "cap-1")

    async def test_capture_ledger_duplicate_swallowed(self) -> None:
        hold = _hold(HoldStatus.CAPTURED)

        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=hold)
        hold_repo.capture_hold = AsyncMock()
        ledger_repo = AsyncMock()
        ledger_repo.post_entry = AsyncMock(side_effect=DuplicateIdempotencyError("dup"))

        service = _service(hold_repo, ledger_repo)
        result = await service.capture_hold(WALLET_ID, HOLD_ID, "cap-1")
        assert result is hold

    async def test_capture_concurrent_winner_returns_fresh_state(self) -> None:
        """If a concurrent capture committed first, re-read and still post ledger."""
        hold = _hold(HoldStatus.CREATED)
        captured = _hold(HoldStatus.CAPTURED)

        hold_repo = AsyncMock()
        hold_repo.get = AsyncMock(return_value=hold)
        hold_repo.capture_hold = AsyncMock(return_value=None)  # lost the race
        hold_repo.get.side_effect = [hold, captured]
        ledger_repo = AsyncMock()
        ledger_repo.post_entry = AsyncMock()

        service = _service(hold_repo, ledger_repo)
        result = await service.capture_hold(WALLET_ID, HOLD_ID, "cap-1")

        assert result is captured
        assert result.status is HoldStatus.CAPTURED
        ledger_repo.post_entry.assert_awaited_once()


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestHoldErrors:
    def test_not_found_is_hold_error(self) -> None:
        assert isinstance(HoldNotFoundError("nf"), HoldError)

    def test_state_conflict_is_hold_error(self) -> None:
        assert isinstance(HoldStateConflictError("sc"), HoldError)
