"""Tests for the operation ledger domain (M07-002)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cloud_platform.modules.operations.domain import (
    InvalidOperationTransition,
    Operation,
    OperationStatus,
    OperationType,
)

SERVER_ID = uuid4()


def _op(status: OperationStatus = OperationStatus.PENDING, **kwargs: object) -> Operation:
    base: dict[str, object] = {
        "id": uuid4(),
        "operation_key": "server-create:abc",
        "operation_type": OperationType.SERVER_CREATE,
        "resource_type": "server",
        "resource_id": SERVER_ID,
        "provider_key": "hetzner",
    }
    base.update(kwargs)
    op = Operation(**base)  # type: ignore[arg-type]
    if status is not OperationStatus.PENDING:
        # drive the aggregate through legal moves
        if status is OperationStatus.IN_FLIGHT:
            op.mark_in_flight()
        elif status is OperationStatus.COMPLETED:
            op.mark_in_flight()
            op.complete({"provider_server_id": "prov-1"})
        elif status is OperationStatus.FAILED:
            op.mark_in_flight()
            op.fail("boom")
    assert op.status is status
    return op


class TestValidation:
    def test_empty_operation_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="operation_key"):
            Operation(
                id=uuid4(),
                operation_key="  ",
                operation_type=OperationType.SERVER_CREATE,
                resource_type="server",
                resource_id=SERVER_ID,
                provider_key="hetzner",
            )

    def test_empty_provider_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider_key"):
            Operation(
                id=uuid4(),
                operation_key="k",
                operation_type=OperationType.SERVER_CREATE,
                resource_type="server",
                resource_id=SERVER_ID,
                provider_key="",
            )

    def test_negative_attempts_rejected(self) -> None:
        with pytest.raises(ValueError, match="attempts"):
            Operation(
                id=uuid4(),
                operation_key="k",
                operation_type=OperationType.SERVER_CREATE,
                resource_type="server",
                resource_id=SERVER_ID,
                provider_key="hetzner",
                attempts=-1,
            )


class TestStateMachine:
    def test_claim_increments_attempts(self) -> None:
        op = _op()
        op.mark_in_flight()
        assert op.status is OperationStatus.IN_FLIGHT
        assert op.attempts == 1

    def test_requeue_back_to_pending_keeps_error(self) -> None:
        op = _op(OperationStatus.IN_FLIGHT)
        op.requeue("rate limited")
        assert op.status is OperationStatus.PENDING
        assert op.error == "rate limited"
        assert op.attempts == 1  # attempts count claims, not failures

    def test_complete_records_correlation_and_clears_error(self) -> None:
        op = _op(OperationStatus.IN_FLIGHT)
        op.error = "old"
        op.complete({"provider_server_id": "prov-9", "idempotency_key": "k"})
        assert op.status is OperationStatus.COMPLETED
        assert op.provider_response == {"provider_server_id": "prov-9", "idempotency_key": "k"}
        assert op.error is None
        assert op.is_terminal

    def test_fail_records_error(self) -> None:
        op = _op(OperationStatus.IN_FLIGHT)
        op.fail("bad credentials")
        assert op.status is OperationStatus.FAILED
        assert op.error == "bad credentials"
        assert op.is_terminal

    @pytest.mark.parametrize(
        ("start", "bad_move"),
        [
            (OperationStatus.PENDING, "complete"),
            (OperationStatus.PENDING, "fail"),
            (OperationStatus.PENDING, "requeue"),
            (OperationStatus.IN_FLIGHT, "mark_in_flight"),
            (OperationStatus.COMPLETED, "mark_in_flight"),
            (OperationStatus.COMPLETED, "fail"),
            (OperationStatus.FAILED, "mark_in_flight"),
            (OperationStatus.FAILED, "requeue"),
        ],
    )
    def test_illegal_transitions_raise(self, start: OperationStatus, bad_move: str) -> None:
        op = _op(start)
        with pytest.raises(InvalidOperationTransition):
            if bad_move == "mark_in_flight":
                op.mark_in_flight()
            elif bad_move == "complete":
                op.complete({"provider_server_id": "x"})
            elif bad_move == "fail":
                op.fail("e")
            else:
                op.requeue("e")


class TestManualVerifiedAbsent:
    def test_mark_verified_absent_returns_pending_with_same_key(self) -> None:
        op = _op(OperationStatus.IN_FLIGHT)
        op.mark_outcome_unknown("read timeout after transmission")
        op.mark_verified_absent()
        assert op.status is OperationStatus.PENDING
        assert op.error == "read timeout after transmission"  # error retained for context
        assert op.operation_key  # SAME identity, never regenerated

    @pytest.mark.parametrize(
        "start",
        [
            OperationStatus.PENDING,
            OperationStatus.IN_FLIGHT,
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
        ],
    )
    def test_mark_verified_absent_refused_outside_outcome_unknown(
        self, start: OperationStatus
    ) -> None:
        op = _op(start)
        with pytest.raises(InvalidOperationTransition):
            op.mark_verified_absent()
        assert op.status is start  # no state change

    def test_reopen_for_retry_is_failed_only(self) -> None:
        op = _op(OperationStatus.IN_FLIGHT)
        op.mark_outcome_unknown("read timeout")
        with pytest.raises(InvalidOperationTransition):
            op.reopen_for_retry()
        assert op.status is OperationStatus.OUTCOME_UNKNOWN
