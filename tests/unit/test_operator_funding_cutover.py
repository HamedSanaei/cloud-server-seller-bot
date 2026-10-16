"""Explicit cutover boundaries, financial attribution and atomicity."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.db import base as db
from cloud_platform.modules.funding import cutover
from cloud_platform.modules.funding.cli import manifest_from_dict, run_manifest
from cloud_platform.modules.funding.cutover import (
    CutoverEntry,
    CutoverManifest,
    CutoverNeedsReview,
    SqlAlchemyOperatorFundingCutover,
)
from cloud_platform.modules.funding.domain import (
    FundingAuthorizationError,
    FundingIdempotencyConflictError,
    FundingValidationError,
    ProviderCostSnapshot,
)
from cloud_platform.modules.users.domain import PermissionDeniedError

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


class Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalar_one_or_none(self) -> Any:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def scalars(self) -> Result:
        return self

    def __iter__(self):
        return iter(self.rows)

    def first(self) -> Any:
        return self.rows[0] if self.rows else None


class MemoryDatabase:
    def __init__(self) -> None:
        self.rows: dict[Any, list[Any]] = {}
        self.adds = 0
        self.reject_audit = False

    def put(self, model: Any, **facts: Any) -> Any:
        row = SimpleNamespace(**facts)
        self.rows.setdefault(model, []).append(row)
        return row

    @asynccontextmanager
    async def session(self):
        database = self

        class Session:
            @asynccontextmanager
            async def begin(self):
                before = copy.deepcopy(database.rows)
                try:
                    yield self
                    if database.reject_audit and database.rows.get(db.AuditEvent):
                        raise RuntimeError("audit persistence failed")
                except BaseException:
                    database.rows = before
                    raise

            async def execute(self, statement):
                model = statement.column_descriptions[0]["entity"]
                rows = list(database.rows.get(model, []))
                params = statement.compile().params
                for name in (
                    "id",
                    "user_id",
                    "server_id",
                    "operation_key",
                    "wallet_id",
                    "resource_id",
                ):
                    value = params.get(f"{name}_1")
                    if value is not None:
                        rows = (
                            [row for row in rows if getattr(row, name, None) in value]
                            if isinstance(value, list)
                            else [row for row in rows if getattr(row, name, None) == value]
                        )
                if "status !=" in str(statement):
                    rows = [row for row in rows if row.status != params["status_1"]]
                if statement.column_descriptions[0]["name"] == "id":
                    rows = [row.id for row in rows]
                return Result(rows)

            async def get(self, model, identity):
                return next(
                    (
                        row
                        for row in database.rows.get(model, [])
                        if getattr(row, "id", getattr(row, "server_id", None)) == identity
                    ),
                    None,
                )

            def add(self, row):
                database.adds += 1
                database.rows.setdefault(type(row), []).append(row)

            async def flush(self):
                pass

        yield Session()


def fixture(monkeypatch, *, count: int = 1):
    monkeypatch.setattr(cutover, "authorize_superadmin_transaction", AsyncMock())
    monkeypatch.setattr(cutover, "_lock_funding_policy", AsyncMock())
    memory = MemoryDatabase()
    owner, provider_id, account_id = uuid4(), uuid4(), uuid4()
    memory.put(db.Provider, id=provider_id, name="provider-a")
    memory.put(db.ProviderAccount, id=account_id, user_id=owner, provider_id=provider_id)
    entries = []
    for index in range(count):
        server_id = uuid4()
        state, native_id = ("deleted", "historical-native-id") if index == 0 else ("error", None)
        memory.put(
            db.Server,
            id=server_id,
            user_id=owner,
            provider_id=provider_id,
            provider_account_id=account_id,
            state=state,
            provider_server_id=native_id,
            credential_account_id="account-a",
            billing_model="hourly",
            funding_source="customer_wallet",
            funding_operator_id=None,
            last_accrued_at=NOW - timedelta(days=90),
            prepaid_paid_until=None,
            idempotency_key=f"historic:{server_id}",
            deleted_at=NOW - timedelta(days=60),
        )
        entries.append(
            CutoverEntry(
                server_id,
                owner,
                state,
                "provider-a",
                native_id,
                "account-a",
                "hourly",
                "Explicitly classify the retained operator service",
                "Read-only verified inventory archived by operator",
                expected_last_accrued_at=NOW - timedelta(days=90),
            )
        )
    manifest = CutoverManifest("explicit-cutover:v1", owner, NOW, tuple(entries))
    service = SqlAlchemyOperatorFundingCutover(
        memory.session, configured_ids=[123], clock=lambda: NOW
    )
    return service, manifest, memory


def payload(manifest):
    entry = asdict(manifest.entries[0])
    for key, value in list(entry.items()):
        if isinstance(value, datetime):
            entry[key] = value.isoformat()
        elif key in {"server_id", "owner_id"}:
            entry[key] = str(value)
    entry["refund_charge_ids"] = []
    return dict(
        operation_key=manifest.operation_key,
        operator_id=str(manifest.operator_id),
        effective_at=manifest.effective_at.isoformat(),
        entries=[entry],
    )


async def test_preview_default_does_not_change_sources_or_create_financial_records(monkeypatch):
    service, manifest, memory = fixture(monkeypatch, count=5)
    result = await service.preview(manifest)
    assert all(row.status == "ready" and row.refund_minor == 0 for row in result)
    assert memory.adds == 0
    assert all(row.funding_source == "customer_wallet" for row in memory.rows[db.Server])


async def test_five_historical_records_classify_once_without_resurrection_or_invented_cost(
    monkeypatch,
):
    service, manifest, memory = fixture(monkeypatch, count=5)
    before = [dict(vars(server)) for server in memory.rows[db.Server]]
    first = await service.apply(manifest)
    again = await service.apply(manifest)
    assert all(result.status == "ready" for result in first)
    assert all(result.status == "replayed" for result in again)
    assert len(memory.rows[db.OperatorFundingCutover]) == 5
    assert len(memory.rows[db.AuditEvent]) == 5
    assert not memory.rows.get(db.OperatorFundingGrant)
    assert not memory.rows.get(db.OperatorExpense)
    assert not memory.rows.get(db.LedgerEntry)
    for old, server in zip(before, memory.rows[db.Server], strict=True):
        assert (
            server.funding_source == "operator"
            and server.funding_operator_id == manifest.operator_id
        )
        assert {
            key: value
            for key, value in vars(server).items()
            if key not in {"funding_source", "funding_operator_id"}
        } == {
            key: value
            for key, value in old.items()
            if key not in {"funding_source", "funding_operator_id"}
        }


async def test_manifest_anchor_mismatch_blocks_the_whole_transaction(monkeypatch):
    service, manifest, memory = fixture(monkeypatch, count=2)
    bad = replace(manifest.entries[1], provider_server_id="not-the-persisted-resource")
    manifest = replace(manifest, entries=(manifest.entries[0], bad))
    assert (
        next(
            row for row in await service.preview(manifest) if row.server_id == bad.server_id
        ).status
        == "needs_review"
    )
    with pytest.raises(CutoverNeedsReview):
        await service.apply(manifest)
    assert all(row.funding_source == "customer_wallet" for row in memory.rows[db.Server])
    assert not memory.rows.get(db.OperatorFundingCutover)


async def test_audit_failure_rolls_back_source_changes_and_receipts(monkeypatch):
    service, manifest, memory = fixture(monkeypatch)
    memory.reject_audit = True
    with pytest.raises(RuntimeError, match="audit persistence"):
        await service.apply(manifest)
    assert memory.rows[db.Server][0].funding_source == "customer_wallet"
    assert not memory.rows.get(db.OperatorFundingCutover)


async def test_reused_manifest_key_with_other_reason_is_not_a_replay(monkeypatch):
    service, manifest, _ = fixture(monkeypatch)
    await service.apply(manifest)
    changed = replace(
        manifest, entries=(replace(manifest.entries[0], reason="A different reviewed request"),)
    )
    with pytest.raises(FundingIdempotencyConflictError):
        await service.apply(changed)


async def test_revocation_denies_even_a_previously_previewed_manifest(monkeypatch):
    service, manifest, memory = fixture(monkeypatch)
    await service.preview(manifest)
    monkeypatch.setattr(
        cutover,
        "authorize_superadmin_transaction",
        AsyncMock(side_effect=PermissionDeniedError("revoked")),
    )
    with pytest.raises(FundingAuthorizationError):
        await service.apply(manifest)
    assert memory.adds == 0


async def test_pending_operation_is_needs_review_without_partial_apply(monkeypatch):
    service, manifest, memory = fixture(monkeypatch)
    memory.put(
        db.Operation,
        id=uuid4(),
        resource_id=manifest.entries[0].server_id,
        status="outcome_unknown",
    )
    assert (await service.preview(manifest))[0].status == "needs_review"
    with pytest.raises(CutoverNeedsReview):
        await service.apply(manifest)


async def test_cli_defaults_to_preview_and_rejects_unknown_or_implicit_targets(monkeypatch):
    service, manifest, memory = fixture(monkeypatch)
    raw = payload(manifest)
    assert (await run_manifest(service, raw))["mode"] == "preview"
    assert memory.adds == 0
    with pytest.raises(FundingValidationError):
        manifest_from_dict({**raw, "select_all_admin_servers": True})
    with pytest.raises(FundingValidationError):
        manifest_from_dict({**raw, "entries": []})


def test_manifest_rejects_cross_owner_bulk_duplicate_or_fake_coverage(monkeypatch):
    _, manifest, _ = fixture(monkeypatch)
    with pytest.raises(FundingAuthorizationError):
        replace(manifest, operator_id=uuid4())
    with pytest.raises(FundingValidationError):
        replace(manifest, entries=manifest.entries * 21)
    with pytest.raises(FundingValidationError):
        replace(manifest, entries=manifest.entries * 2)
    with pytest.raises(FundingValidationError):
        replace(
            manifest.entries[0], provider_cost_snapshot=ProviderCostSnapshot("0", "USD", "invented")
        )


async def test_refund_needs_actual_immutable_debit_and_accounts_for_all_corrections(monkeypatch):
    service, manifest, memory = fixture(monkeypatch)
    server_id, wallet_id, charge_id = manifest.entries[0].server_id, uuid4(), uuid4()
    memory.put(db.Wallet, id=wallet_id, user_id=manifest.operator_id, balance=10, currency="IRT")
    memory.put(
        db.LedgerEntry,
        id=charge_id,
        wallet_id=wallet_id,
        entry_type="charge",
        amount=100,
        currency="IRT",
        reference_id=server_id,
        idempotency_key="actual-personal-charge",
    )
    memory.put(
        db.LedgerEntry,
        id=uuid4(),
        wallet_id=wallet_id,
        entry_type="refund",
        amount=30,
        currency="IRT",
        reference_id=charge_id,
        idempotency_key="earlier-refund",
    )
    memory.put(
        db.LedgerEntry,
        id=uuid4(),
        wallet_id=wallet_id,
        entry_type="adjustment",
        amount=20,
        currency="IRT",
        reference_id=charge_id,
        idempotency_key="earlier-correction",
    )
    memory.put(
        db.AuditEvent,
        id=uuid4(),
        action="wallet.adjust",
        resource_id=str(wallet_id),
        event_metadata={"idempotency_key": "earlier-correction", "amount": "20"},
    )
    entry = replace(manifest.entries[0], refund_charge_ids=(charge_id,), expected_refund_minor=50)
    manifest = replace(manifest, entries=(entry,))
    assert (await service.preview(manifest))[0].refund_minor == 50
    await service.apply(manifest)
    assert memory.rows[db.Wallet][0].balance == 60
    await service.apply(manifest)
    assert memory.rows[db.Wallet][0].balance == 60
    assert (
        sum(row.amount for row in memory.rows[db.LedgerEntry] if row.entry_type == "refund") == 80
    )


async def test_unattributed_correction_refuses_refund_instead_of_guessing(monkeypatch):
    service, manifest, memory = fixture(monkeypatch)
    charge_id, wallet_id = uuid4(), uuid4()
    memory.put(db.Wallet, id=wallet_id, user_id=manifest.operator_id, balance=0, currency="IRT")
    memory.put(
        db.LedgerEntry,
        id=charge_id,
        wallet_id=wallet_id,
        entry_type="charge",
        amount=100,
        currency="IRT",
        reference_id=manifest.entries[0].server_id,
        idempotency_key="charge",
    )
    memory.put(
        db.LedgerEntry,
        id=uuid4(),
        wallet_id=wallet_id,
        entry_type="adjustment",
        amount=100,
        currency="IRT",
        reference_id=None,
        idempotency_key="unknown-correction",
    )
    entry = replace(manifest.entries[0], refund_charge_ids=(charge_id,), expected_refund_minor=100)
    assert (await service.preview(replace(manifest, entries=(entry,))))[0].status == "needs_review"


def accepted_hourly_fixture(monkeypatch):
    service, manifest, memory = fixture(monkeypatch)
    server = memory.rows[db.Server][0]
    start, end = NOW - timedelta(minutes=30), NOW + timedelta(minutes=30)
    server.state, server.provider_server_id = "running", "accepted-active-native"
    server.billing_model, server.prepaid_paid_until = "hourly_prepaid_irt", end.replace(tzinfo=None)
    price = memory.put(
        db.ServerPriceSnapshot,
        id=uuid4(),
        server_id=server.id,
        provider_rate_exact="0.0142",
        currency="EUR",
        selling_minor=5,
        selling_currency="USD",
    )
    period = memory.put(
        db.PrepaidHourlyPeriod,
        id=uuid4(),
        server_id=server.id,
        period_start=start.replace(tzinfo=None),
        period_end=end.replace(tzinfo=None),
        status="paid",
        funding_source="customer_wallet",
        wallet_id=uuid4(),
        usd_minor=5,
        irt_minor=500,
        fx_snapshot={"immutable-personal-fx-evidence": True},
    )
    entry = replace(
        manifest.entries[0],
        expected_state="running",
        provider_server_id=server.provider_server_id,
        billing_model=server.billing_model,
        classification_only=False,
        expected_paid_until=end,
        period_start=start,
        period_end=end,
        nominal_amount_minor=5,
        nominal_currency="USD",
        provider_cost_snapshot=ProviderCostSnapshot(
            "0.0142", "EUR", f"accepted hourly price snapshot {server.id}"
        ),
    )
    return service, replace(manifest, entries=(entry,)), memory, price, period


async def test_accepted_hourly_cutover_records_current_expense_without_rewriting_personal_history(
    monkeypatch,
):
    service, manifest, memory, price, period = accepted_hourly_fixture(monkeypatch)
    original_period, original_price = copy.deepcopy(vars(period)), copy.deepcopy(vars(price))
    await service.apply(manifest)
    assert vars(period) == original_period and vars(price) == original_price
    assert len(memory.rows[db.OperatorFundingGrant]) == len(memory.rows[db.OperatorExpense]) == 1
    grant, expense = memory.rows[db.OperatorFundingGrant][0], memory.rows[db.OperatorExpense][0]
    assert grant.state == "settled" and grant.id == expense.grant_id
    entry = manifest.entries[0]
    expected_key = f"prepaid-hourly:{entry.server_id}:{entry.period_start.isoformat()}"
    assert grant.operation_key == expected_key
    assert grant.provider_cost_snapshot["amount_exact"] == "0.0142"
    assert not memory.rows.get(db.Wallet) and not memory.rows.get(db.LedgerEntry)
    coverage = memory.rows[db.OperatorFundingCutover][0].cutover_metadata["coverage"]
    assert coverage["expense_id"] == str(expense.id)
    assert (await service.apply(manifest))[0].status == "replayed"
    assert len(memory.rows[db.OperatorExpense]) == 1


async def test_accepted_cutover_never_reconstructs_missing_exact_native_quote(monkeypatch):
    service, manifest, memory, price, _ = accepted_hourly_fixture(monkeypatch)
    price.provider_rate_exact = None
    assert (await service.preview(manifest))[0].status == "needs_review"
    with pytest.raises(CutoverNeedsReview):
        await service.apply(manifest)
    assert not memory.rows.get(db.OperatorExpense)


async def test_accepted_cutover_blocks_competing_charge_window(monkeypatch):
    service, manifest, _, _, _ = accepted_hourly_fixture(monkeypatch)
    service._now = lambda: manifest.entries[0].period_end - timedelta(seconds=30)
    assert (await service.preview(manifest))[0].status == "needs_review"


async def monthly_bridge_fixture():
    import test_operator_monthly_funding as monthly

    from cloud_platform.modules.funding.domain import (
        FundingPurpose,
        OperatorFundingCutover,
        OperatorFundingGrant,
        expense_operation_key,
    )
    from cloud_platform.modules.funding.service import FundingService
    from cloud_platform.modules.orders.service import OrderSettlementService
    from cloud_platform.modules.renewals.domain import RenewalRecord

    server, order, _, ledger = monthly.operator_order()
    server.provider_server_id = "accepted-cutover-native"
    order.provider_order_id = "accepted-cutover-order"
    start, end = NOW - timedelta(days=10), NOW + timedelta(days=20)
    boundary = start.replace(hour=0, minute=0, second=0, microsecond=0)
    key = f"renewal-charge:{server.id}:{boundary.date().isoformat()}"
    grant = OperatorFundingGrant(
        uuid4(),
        server.user_id,
        server.user_id,
        key,
        f"server:{server.id}:period:{boundary.isoformat()}",
        FundingPurpose.MONTHLY_RENEW,
        order.selling_price_minor,
        order.selling_currency,
        ProviderCostSnapshot.from_dict(order.provider_cost_snapshot),
        NOW,
    )
    ledger.grants[key] = grant
    expense = await ledger.settle(grant)
    order.funding_grant_operation_key = key
    record = RenewalRecord(
        server.id,
        start,
        order.selling_price_minor,
        order.selling_currency,
        provider_order_ref=order.provider_order_id,
        provider_renewal_at=end,
        funding_source=order.funding_source,
        funding_operator_id=server.user_id,
        provider_cost_snapshot=order.provider_cost_snapshot,
    )
    receipt = OperatorFundingCutover(
        uuid4(),
        "monthly-cutover",
        server.id,
        server.user_id,
        cutover.FundingSource.CUSTOMER_WALLET,
        NOW,
        "Reviewed current monthly coverage",
        {
            "classification_only": False,
            "provider_key": server.provider_key,
            "provider_server_id": server.provider_server_id,
            "credential_account_id": server.credential_account_id,
            "provider_proof": "Verified accepted current provider contract",
            "coverage": {
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "grant_operation_key": key,
                "expense_operation_key": expense_operation_key(key),
                "grant_id": str(grant.id),
                "expense_id": str(expense.id),
            },
        },
    )
    ledger.get_cutover = AsyncMock(return_value=receipt)
    service = OrderSettlementService(
        wallet_repo=monthly.ForbiddenWallet(),
        hold_repo=monthly.ForbiddenWallet(),
        hold_service=monthly.ForbiddenWallet(),
        ledger_repo=monthly.ForbiddenWallet(),
        orders_repo=monthly.orders.FakeOrdersRepo(order),
        operation_repo=monthly.orders.FakeOperationRepo(),
        audit_repo=monthly.orders.FakeAuditRepo(),
        funding_service=FundingService(ledger),
        renewal_repo=SimpleNamespace(get=AsyncMock(return_value=record)),
        clock=lambda: NOW,
    )
    return service, server, order, record, ledger, receipt


async def test_monthly_receipt_bridge_uses_existing_cutover_expense_without_new_charge():
    from cloud_platform.modules.orders.service import SettlementVerdict

    service, server, order, _, ledger, _ = await monthly_bridge_fixture()
    ledger.enabled = False  # Historical accepted receipt may recover after revocation.
    assert await service.ensure_order_payment_settled(order, server) is SettlementVerdict.SETTLED
    assert len(ledger.expenses) == 1


@pytest.mark.parametrize(
    "defect", ["missing_expense", "wrong_period", "classification", "wrong_provider"]
)
async def test_monthly_cutover_bridge_rejects_fabricated_or_stale_complete_receipt(defect):
    from cloud_platform.modules.orders.domain import SettlementStatus
    from cloud_platform.modules.orders.service import SettlementVerdict

    service, server, order, record, ledger, receipt = await monthly_bridge_fixture()
    order.settlement_status = SettlementStatus.COMPLETE
    metadata = {key: value for key, value in receipt.metadata.items()}
    if defect == "missing_expense":
        ledger.expenses.clear()
    elif defect == "wrong_period":
        record.provider_renewal_at += timedelta(days=30)
    elif defect == "classification":
        metadata["classification_only"], metadata["coverage"] = True, None
    else:
        metadata["provider_server_id"] = "different-provider-resource"
    ledger.get_cutover = AsyncMock(return_value=replace(receipt, metadata=metadata))
    assert (
        await service.ensure_order_payment_settled(order, server) is SettlementVerdict.NEEDS_REVIEW
    )
    assert len(ledger.expenses) == (0 if defect == "missing_expense" else 1)
