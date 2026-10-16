"""Hermetic contracts for exact operator-cost evidence and its API boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import localcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.modules.funding import (
    FundingAuthorizationError,
    FundingFactMismatchError,
    FundingGrantState,
    FundingIdempotencyConflictError,
    FundingPurpose,
    FundingService,
    FundingSource,
    FundingStateError,
    FundingValidationError,
    OperatorFundingCutover,
    OperatorFundingGrant,
    ProviderCostSnapshot,
    SqlAlchemyOperatorFundingRepository,
    canonical_amount,
    expense_operation_key,
)
from cloud_platform.modules.funding import repository as persistence
from cloud_platform.modules.users.domain import PermissionDeniedError


def grant(**changes: object) -> OperatorFundingGrant:
    owner = uuid4()
    facts = OperatorFundingGrant(
        id=uuid4(),
        operator_user_id=owner,
        owner_user_id=owner,
        operation_key="period:1",
        resource_key="server:1",
        purpose=FundingPurpose.HOURLY_USAGE,
        nominal_amount_minor=123,
        nominal_currency="IRT",
        provider_cost_snapshot=ProviderCostSnapshot(
            "0.1234567890123456789012345678901", "EUR", "quote"
        ),
        created_at=datetime.now(UTC),
    )
    return replace(facts, **changes)


def grant_row(facts: OperatorFundingGrant) -> SimpleNamespace:
    return SimpleNamespace(
        id=facts.id,
        operator_user_id=facts.operator_user_id,
        owner_user_id=facts.owner_user_id,
        operation_key=facts.operation_key,
        resource_key=facts.resource_key,
        purpose=facts.purpose.value,
        nominal_amount_minor=facts.nominal_amount_minor,
        nominal_currency=facts.nominal_currency,
        provider_cost_snapshot=facts.provider_cost_snapshot.to_dict(),
        created_at=facts.created_at,
        state=facts.state.value,
    )


def expense_row(facts: OperatorFundingGrant) -> SimpleNamespace:
    row = grant_row(facts)
    row.grant_id = facts.id
    row.id = uuid4()
    row.operation_key = expense_operation_key(facts.operation_key)
    row.occurred_at = datetime.now(UTC)
    return row


def repository(
    monkeypatch: pytest.MonkeyPatch, rows: list[object]
) -> tuple[SqlAlchemyOperatorFundingRepository, MagicMock, list[str]]:
    session = MagicMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    session.execute.side_effect = [
        MagicMock(scalar_one_or_none=MagicMock(return_value=row)) for row in rows
    ]
    order: list[str] = []

    async def policy(*args: object) -> None:
        order.append("policy")

    async def funding(*args: object) -> None:
        order.append("funding")

    monkeypatch.setattr(persistence, "authorize_superadmin_transaction", policy)
    monkeypatch.setattr(persistence, "_lock_funding_policy", funding)
    return (
        SqlAlchemyOperatorFundingRepository(lambda: session, configured_ids=[10101]),
        session,
        order,
    )


async def reserve(
    repo: SqlAlchemyOperatorFundingRepository,
    facts: OperatorFundingGrant,
    reason: str | None = None,
):
    return await repo.reserve(
        operator_id=facts.operator_user_id,
        owner_id=facts.owner_user_id,
        operation_key=facts.operation_key,
        resource_key=facts.resource_key,
        purpose=facts.purpose,
        nominal_amount_minor=facts.nominal_amount_minor,
        nominal_currency=facts.nominal_currency,
        provider_cost_snapshot=facts.provider_cost_snapshot,
        reason=reason,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00012.34000", "12.34"),
        ("-000.000", "0"),
        ("00000", "0"),
        ("1234567890123456789012345678901234567890", "1234567890123456789012345678901234567890"),
        (
            "0.123456789012345678901234567890123456789000",
            "0.123456789012345678901234567890123456789",
        ),
    ],
)
def test_exact_amount_is_independent_of_decimal_context(raw: str, expected: str) -> None:
    with localcontext() as context:
        context.prec = 2
        assert canonical_amount(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["NaN", "Infinity", "1e3", "+1", "-0.001", "1_000", "\u0661", ".1", "1.", "1" * 4097, 1.25],
)
def test_invalid_exact_amount_is_rejected(raw: object) -> None:
    with pytest.raises(FundingValidationError):
        canonical_amount(raw)


def test_frozen_nested_cutover_metadata_and_provider_snapshot() -> None:
    source = {"period": {"components": [{"name": "cpu"}]}}
    cutover = OperatorFundingCutover(
        uuid4(),
        "cutover:1",
        uuid4(),
        uuid4(),
        FundingSource.CUSTOMER_WALLET,
        datetime.now(UTC),
        "explicit conversion",
        source,
    )
    source["period"]["components"][0]["name"] = "rewritten"
    assert cutover.metadata["period"]["components"][0]["name"] == "cpu"
    with pytest.raises(TypeError):
        cutover.metadata["period"]["components"][0]["name"] = "rewritten"
    raw = {"amount_exact": "1.2300", "currency": "EUR", "provenance": "quote", "components": []}
    snapshot = ProviderCostSnapshot.from_dict(raw)
    raw["amount_exact"] = "999"
    exported = snapshot.to_dict()
    exported["amount_exact"] = "888"
    assert snapshot.amount_exact == "1.23"


async def test_reserve_fresh_policy_lock_precedes_funding_and_replay(monkeypatch) -> None:
    facts = grant()
    repo, session, order = repository(monkeypatch, [grant_row(facts)])
    assert await reserve(repo, facts) == facts
    assert order == ["policy", "funding"]
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


async def test_reserve_replay_cannot_change_immutable_facts(monkeypatch) -> None:
    facts = grant()
    repo, _, _ = repository(monkeypatch, [grant_row(facts)])
    with pytest.raises(FundingIdempotencyConflictError):
        await reserve(repo, replace(facts, nominal_amount_minor=124))


async def test_reserve_policy_denial_is_typed_and_never_takes_funding_lock(monkeypatch) -> None:
    facts = grant()
    repo, session, order = repository(monkeypatch, [])
    denial = PermissionDeniedError("revoked")
    monkeypatch.setattr(
        persistence, "authorize_superadmin_transaction", AsyncMock(side_effect=denial)
    )
    with pytest.raises(FundingAuthorizationError) as captured:
        await reserve(repo, facts)
    assert captured.value.__cause__ is denial
    assert order == []
    session.execute.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.parametrize("reason", ["", "  ", "r" * 1025])
async def test_reserve_rejects_invalid_explicit_authorization_reason(monkeypatch, reason) -> None:
    repo, session, order = repository(monkeypatch, [])
    with pytest.raises(FundingValidationError):
        await reserve(repo, grant(), reason=reason)
    assert order == []
    session.add.assert_not_called()


@pytest.mark.parametrize("reason", [None, "Authorize monthly server creation", "r" * 1024])
async def test_reserve_persists_authorization_reason_without_changing_replay(
    monkeypatch, reason
) -> None:
    facts = grant()
    repo, session, _ = repository(monkeypatch, [None, grant_row(facts)])
    await reserve(repo, facts, reason=reason)
    audit = session.add.call_args_list[1].args[0]
    assert audit.reason == (facts.provider_cost_snapshot.provenance if reason is None else reason)
    await reserve(repo, facts, reason="New retry reason must not rewrite accepted audit")
    assert session.add.call_count == 2


async def test_reserve_infrastructure_failure_is_not_policy_denial(monkeypatch) -> None:
    facts = grant()
    repo, session, order = repository(monkeypatch, [])
    monkeypatch.setattr(
        persistence,
        "authorize_superadmin_transaction",
        AsyncMock(side_effect=RuntimeError("policy database unavailable")),
    )
    with pytest.raises(RuntimeError, match="policy database unavailable"):
        await reserve(repo, facts)
    assert order == []
    session.add.assert_not_called()


async def test_failed_audit_integrity_is_never_successful_replay(monkeypatch) -> None:
    facts = grant()
    repo, session, _ = repository(monkeypatch, [None])
    failure = IntegrityError("INSERT audit_events", {}, Exception("audit constraint"))
    session.commit.side_effect = failure
    with pytest.raises(IntegrityError) as captured:
        await reserve(repo, facts)
    assert captured.value is failure
    assert session.execute.await_count == 1  # never converts failure into a second read


async def test_maximum_key_reserve_settle_read_and_replay(monkeypatch) -> None:
    facts = grant(operation_key="x" * 200)
    expense = expense_row(facts)
    stored = grant_row(facts)
    repo, session, order = repository(
        monkeypatch, [stored, stored, expense, expense, stored, expense]
    )
    assert await reserve(repo, facts) == facts
    result = await repo.settle(facts)
    assert result.operation_key == expense_operation_key(facts.operation_key)
    assert len(result.operation_key) <= 200
    assert result.operation_key != expense_operation_key("y" + "x" * 199)
    assert await repo.get_expense(result.operation_key) == result
    assert await repo.settle(facts) == result
    assert order == ["policy", "funding", "funding", "funding"]
    assert session.commit.await_count == 1


async def test_settled_replay_verifies_expense_against_grant(monkeypatch) -> None:
    facts = grant(state=FundingGrantState.SETTLED)
    bad_expense = expense_row(facts)
    bad_expense.nominal_amount_minor += 1
    repo, _, _ = repository(monkeypatch, [grant_row(facts), bad_expense])
    with pytest.raises(FundingFactMismatchError, match="expense evidence"):
        await repo.settle(facts)


async def test_settled_reservation_replay_requires_coherent_expense(monkeypatch) -> None:
    facts = grant(state=FundingGrantState.SETTLED)
    repo, _, _ = repository(monkeypatch, [grant_row(facts), None])
    with pytest.raises(FundingFactMismatchError, match="no expense evidence"):
        await reserve(repo, facts)


async def test_cancelled_grant_cannot_settle(monkeypatch) -> None:
    facts = grant(state=FundingGrantState.CANCELLED)
    repo, session, _ = repository(monkeypatch, [grant_row(facts)])
    with pytest.raises(FundingStateError):
        await repo.settle(facts)
    session.commit.assert_not_awaited()


async def test_eligibility_only_permission_denial_means_false(monkeypatch) -> None:
    repo, _, _ = repository(monkeypatch, [])
    monkeypatch.setattr(
        persistence,
        "authorize_superadmin_transaction",
        AsyncMock(side_effect=PermissionDeniedError("revoked")),
    )
    assert not await repo.eligible(uuid4())
    monkeypatch.setattr(
        persistence,
        "authorize_superadmin_transaction",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await repo.eligible(uuid4())


async def test_service_fresh_source_period_policy_and_historical_settlement() -> None:
    repo = MagicMock()
    repo.eligible = AsyncMock(side_effect=[True, False, False])
    repo.settle = AsyncMock(return_value=expense_row(grant()))
    service = FundingService(repo)
    owner = uuid4()
    assert await service.resolve_source(owner) is FundingSource.OPERATOR
    assert await service.resolve_source(owner) is FundingSource.CUSTOMER_WALLET
    with pytest.raises(FundingAuthorizationError):
        await service.authorize_new_period(owner_id=owner, operator_id=owner)
    with pytest.raises(FundingAuthorizationError):
        await service.authorize_new_period(owner_id=owner, operator_id=uuid4())
    historical = grant()
    await service.settle(historical)
    repo.settle.assert_awaited_once_with(historical)
    assert repo.eligible.await_count == 3
