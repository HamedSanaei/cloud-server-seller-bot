"""Explicit, reviewed server funding cutover; preview is the default operation.

This module never discovers targets by role, calls a provider mutation, creates
wallet credit without a proved personal debit, or edits historical billing rows.
All accepted coverage, corrective money, source changes and audit receipts share
one PostgreSQL transaction. Callers must explicitly provide every target anchor.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db import base as db
from cloud_platform.db.timestamps import to_db_utc
from cloud_platform.modules.funding.domain import (
    FundingAuthorizationError,
    FundingIdempotencyConflictError,
    FundingPurpose,
    FundingSource,
    FundingValidationError,
    ProviderCostSnapshot,
    expense_operation_key,
)
from cloud_platform.modules.funding.repository import _lock_funding_policy
from cloud_platform.modules.users.domain import PermissionDeniedError
from cloud_platform.modules.users.superadmin_repository import authorize_superadmin_transaction

MAX_MANIFEST_SERVERS = 20
MAX_REVIEW_ROWS = 10000


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _text(value: str, name: str, limit: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise FundingValidationError(f"{name} must be nonempty text of at most {limit} characters")
    return value.strip()


@dataclass(frozen=True, slots=True)
class CutoverEntry:
    server_id: UUID
    owner_id: UUID
    expected_state: str
    provider_key: str
    provider_server_id: str | None
    credential_account_id: str | None
    billing_model: str
    reason: str
    provider_proof: str
    classification_only: bool = True
    expected_source: FundingSource = FundingSource.CUSTOMER_WALLET
    expected_last_accrued_at: datetime | None = None
    expected_paid_until: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    nominal_amount_minor: int | None = None
    nominal_currency: str | None = None
    provider_cost_snapshot: ProviderCostSnapshot | None = None
    refund_charge_ids: tuple[UUID, ...] = ()
    expected_refund_minor: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, UUID) or not isinstance(self.owner_id, UUID):
            raise FundingValidationError("cutover server and owner must be UUIDs")
        for name in ("expected_state", "provider_key", "billing_model", "reason", "provider_proof"):
            _text(getattr(self, name), name)
        if type(self.classification_only) is not bool:
            raise FundingValidationError("classification_only must be a boolean")
        if self.expected_source is not FundingSource.CUSTOMER_WALLET:
            raise FundingValidationError(
                "cutover only accepts an explicitly customer-funded source"
            )
        for name in (
            "expected_last_accrued_at",
            "expected_paid_until",
            "period_start",
            "period_end",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
                raise FundingValidationError(f"{name} must carry an explicit timezone")
        if (
            type(self.expected_refund_minor) is not int
            or not 0 <= self.expected_refund_minor <= 2**63 - 1
        ):
            raise FundingValidationError("expected refund must be a nonnegative integer")
        if len(set(self.refund_charge_ids)) != len(self.refund_charge_ids) or any(
            not isinstance(item, UUID) for item in self.refund_charge_ids
        ):
            raise FundingValidationError("refund charge ids must be distinct UUIDs")
        if not self.refund_charge_ids and self.expected_refund_minor:
            raise FundingValidationError("refund requires explicit immutable personal charge ids")
        if self.classification_only:
            if self.expected_state not in {"error", "deleted"}:
                raise FundingValidationError(
                    "classification-only targets must be failed or deleted"
                )
            if any(
                value is not None
                for value in (
                    self.period_start,
                    self.period_end,
                    self.nominal_amount_minor,
                    self.nominal_currency,
                    self.provider_cost_snapshot,
                )
            ):
                raise FundingValidationError(
                    "classification cannot invent accepted coverage or cost"
                )
        else:
            if (
                self.expected_state not in {"running", "stopped"}
                or not self.provider_server_id
                or self.period_start is None
                or self.period_end is None
                or self.period_end <= self.period_start
                or type(self.nominal_amount_minor) is not int
                or self.nominal_amount_minor <= 0
                or not isinstance(self.provider_cost_snapshot, ProviderCostSnapshot)
                or not isinstance(self.nominal_currency, str)
                or len(self.nominal_currency) != 3
                or not self.nominal_currency.isupper()
            ):
                raise FundingValidationError(
                    "accepted cutover requires exact current coverage and cost"
                )


@dataclass(frozen=True, slots=True)
class CutoverManifest:
    operation_key: str
    operator_id: UUID
    effective_at: datetime
    entries: tuple[CutoverEntry, ...]

    def __post_init__(self) -> None:
        _text(self.operation_key, "operation_key", 150)
        if not isinstance(self.operator_id, UUID):
            raise FundingValidationError("operator_id must be a UUID")
        if not isinstance(self.effective_at, datetime) or self.effective_at.tzinfo is None:
            raise FundingValidationError("effective_at requires an explicit timezone")
        if not 0 < len(self.entries) <= MAX_MANIFEST_SERVERS:
            raise FundingValidationError("manifest requires between 1 and 20 explicit targets")
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, CutoverEntry) for entry in self.entries
        ):
            raise FundingValidationError(
                "manifest entries must be a tuple of typed explicit targets"
            )
        if len({entry.server_id for entry in self.entries}) != len(self.entries):
            raise FundingValidationError("manifest contains duplicate servers")
        if any(entry.owner_id != self.operator_id for entry in self.entries):
            raise FundingAuthorizationError("operator cutover is self-owned only")

    def fingerprint(self) -> str:
        def encode(value: Any) -> Any:
            if isinstance(value, (UUID, datetime, FundingSource)):
                return str(value)
            if isinstance(value, ProviderCostSnapshot):
                return value.to_dict()
            if isinstance(value, tuple):
                return [encode(item) for item in value]
            if hasattr(value, "__dataclass_fields__"):
                return {key: encode(getattr(value, key)) for key in value.__dataclass_fields__}
            return value

        raw = json.dumps(encode(self), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CutoverPreview:
    server_id: UUID
    status: str  # ready | replayed | needs_review
    reason: str
    refund_minor: int = 0
    receipt_id: UUID | None = None


@dataclass(slots=True)
class _Plan:
    entry: CutoverEntry
    server: Any
    order: Any = None
    renewal: Any = None
    wallet: Any = None
    refund_minor: int = 0
    funding_key: str | None = None
    purpose: FundingPurpose | None = None
    resource_key: str | None = None
    coverage: dict[str, Any] | None = None
    review: list[str] = field(default_factory=list)


class CutoverNeedsReview(FundingValidationError):
    """An explicit target could not be proved safe; no partial apply commits."""


class SqlAlchemyOperatorFundingCutover:
    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        configured_ids: Iterable[int],
        verified_provider_fact: Callable[[CutoverEntry], Awaitable[ProviderCostSnapshot]]
        | None = None,
        clock: Callable[[], datetime] | None = None,
        monthly_charge_guard: timedelta = timedelta(days=7),
    ) -> None:
        self._sessions = session_factory
        self._configured = tuple(configured_ids)
        self._verified_fact = verified_provider_fact
        self._now = clock or (lambda: datetime.now(UTC))
        self._monthly_guard = monthly_charge_guard

    async def preview(self, manifest: CutoverManifest) -> list[CutoverPreview]:
        """Only SELECTs and transaction locks; never add/flush/commit mutations."""
        return await self._execute(manifest, apply=False)

    async def apply(self, manifest: CutoverManifest) -> list[CutoverPreview]:
        """Explicit mutation entry point; all targets succeed or none commit."""
        return await self._execute(manifest, apply=True)

    async def _execute(self, manifest: CutoverManifest, *, apply: bool) -> list[CutoverPreview]:
        results: list[CutoverPreview] = []
        async with self._sessions() as session, session.begin():
            try:
                await authorize_superadmin_transaction(
                    session, manifest.operator_id, self._configured
                )
            except PermissionDeniedError as exc:
                raise FundingAuthorizationError(
                    "configured active self-owned operator required"
                ) from exc
            await _lock_funding_policy(session)
            # Customer charge code takes wallet before server. Keeping that order
            # fences stale decisions and avoids wallet/server deadlocks.
            wallet_query = select(db.Wallet).where(db.Wallet.user_id == manifest.operator_id)
            if apply:
                wallet_query = wallet_query.with_for_update()
            wallet = (await session.execute(wallet_query)).scalar_one_or_none()
            for entry in sorted(manifest.entries, key=lambda item: str(item.server_id)):
                receipt_key = f"{manifest.operation_key}:{entry.server_id}"
                prior = (
                    await session.execute(
                        select(db.OperatorFundingCutover).where(
                            db.OperatorFundingCutover.operation_key == receipt_key
                        )
                    )
                ).scalar_one_or_none()
                if prior is not None:
                    if (
                        prior.operator_user_id != manifest.operator_id
                        or prior.server_id != entry.server_id
                        or prior.cutover_metadata.get("manifest_fingerprint")
                        != manifest.fingerprint()
                    ):
                        raise FundingIdempotencyConflictError(
                            "cutover key reused with different facts"
                        )
                    results.append(
                        CutoverPreview(
                            entry.server_id,
                            "replayed",
                            "immutable receipt",
                            receipt_id=cast(UUID, prior.id),
                        )
                    )
                    continue
                plan = await self._plan(session, manifest, entry, wallet, lock=apply)
                if plan.review:
                    results.append(
                        CutoverPreview(entry.server_id, "needs_review", "; ".join(plan.review))
                    )
                    continue
                receipt_id = uuid4()
                if apply:
                    await self._write(session, manifest, plan, receipt_key, receipt_id)
                results.append(
                    CutoverPreview(
                        entry.server_id,
                        "ready",
                        "explicit anchors verified",
                        plan.refund_minor,
                        receipt_id if apply else None,
                    )
                )
            if apply and any(result.status == "needs_review" for result in results):
                raise CutoverNeedsReview(
                    "; ".join(
                        f"{r.server_id}: {r.reason}" for r in results if r.status == "needs_review"
                    )
                )
        return results

    async def _plan(
        self,
        session: AsyncSession,
        manifest: CutoverManifest,
        entry: CutoverEntry,
        wallet: Any,
        *,
        lock: bool,
    ) -> _Plan:
        query = select(db.Server).where(db.Server.id == entry.server_id)
        if lock:
            query = query.with_for_update()
        server = (await session.execute(query)).scalar_one_or_none()
        plan = _Plan(entry, server, wallet=wallet)
        if server is None:
            plan.review.append("server does not exist")
            return plan
        anchors = {
            "user_id": entry.owner_id,
            "state": entry.expected_state,
            "funding_source": entry.expected_source.value,
            "provider_server_id": entry.provider_server_id,
            "credential_account_id": entry.credential_account_id,
            "billing_model": entry.billing_model,
        }
        if any(getattr(server, name) != expected for name, expected in anchors.items()):
            plan.review.append(
                "persisted owner/source/state/provider identity differs from manifest"
            )
        provider = await session.get(db.Provider, server.provider_id)
        if provider is None or provider.name != entry.provider_key:
            plan.review.append("provider key differs from manifest")
        account = await session.get(db.ProviderAccount, server.provider_account_id)
        if (
            account is None
            or account.user_id != entry.owner_id
            or account.provider_id != server.provider_id
        ):
            plan.review.append("provider account owner or provider differs from server")
        if _aware(cast(datetime | None, server.last_accrued_at)) != _aware(
            entry.expected_last_accrued_at
        ) or _aware(cast(datetime | None, server.prepaid_paid_until)) != _aware(
            entry.expected_paid_until
        ):
            plan.review.append("billing watermark changed")
        plan.order = (
            await session.execute(
                select(db.ProviderOrder).where(db.ProviderOrder.server_id == entry.server_id)
            )
        ).scalar_one_or_none()
        plan.renewal = await session.get(db.RenewalRecord, entry.server_id)
        ids = [entry.server_id] + ([plan.order.id] if plan.order is not None else [])
        pending = (
            await session.execute(
                select(db.Operation.id)
                .where(
                    db.Operation.resource_id.in_(ids),
                    db.Operation.status.not_in(["completed", "failed"]),
                )
                .limit(1)
            )
        ).first()
        if pending:
            plan.review.append("provider dispatch or management operation is pending or ambiguous")
        holds = []
        if wallet is not None:
            keys = [
                f"leaseweb-order:{server.idempotency_key}",
                f"server-create:{server.idempotency_key}",
            ]
            holds = list(
                (
                    await session.execute(
                        select(db.Hold)
                        .where(
                            db.Hold.wallet_id == wallet.id,
                            or_(
                                db.Hold.idempotency_key.in_(keys),
                                db.Hold.idempotency_key.startswith(
                                    f"renewal-charge:{entry.server_id}:"
                                ),
                            ),
                        )
                        .limit(MAX_REVIEW_ROWS + 1)
                    )
                ).scalars()
            )
            if len(holds) > MAX_REVIEW_ROWS or any(hold.status == "created" for hold in holds):
                plan.review.append("unsettled or ambiguous personal reservation")
        pending_period = (
            await session.execute(
                select(db.PrepaidHourlyPeriod.id)
                .where(
                    db.PrepaidHourlyPeriod.server_id == entry.server_id,
                    db.PrepaidHourlyPeriod.status != "paid",
                )
                .limit(1)
            )
        ).first()
        if pending_period:
            plan.review.append("an hourly period is not durably settled")
        if entry.classification_only:
            if plan.order is not None or plan.renewal is not None:
                plan.review.append(
                    "historical classification with commercial records requires financial review"
                )
        else:
            await self._coverage(session, manifest, plan)
        if entry.refund_charge_ids:
            try:
                plan.refund_minor = await self._refund(session, plan, holds)
            except CutoverNeedsReview as exc:
                plan.review.append(str(exc))
        return plan

    async def _coverage(
        self, session: AsyncSession, manifest: CutoverManifest, plan: _Plan
    ) -> None:
        entry, server = plan.entry, plan.server
        if (
            entry.period_start is None
            or entry.period_end is None
            or entry.provider_cost_snapshot is None
        ):
            plan.review.append("accepted coverage facts are incomplete")
            return
        now = self._now()
        if not (entry.period_start <= manifest.effective_at <= now < entry.period_end):
            plan.review.append("accepted coverage must contain effective_at and current time")
            return
        stored: ProviderCostSnapshot | None = None
        if entry.billing_model == "hourly_prepaid_irt":
            price = (
                await session.execute(
                    select(db.ServerPriceSnapshot).where(
                        db.ServerPriceSnapshot.server_id == entry.server_id
                    )
                )
            ).scalar_one_or_none()
            period = (
                await session.execute(
                    select(db.PrepaidHourlyPeriod).where(
                        db.PrepaidHourlyPeriod.server_id == entry.server_id,
                        db.PrepaidHourlyPeriod.period_start == to_db_utc(entry.period_start),
                        db.PrepaidHourlyPeriod.period_end == to_db_utc(entry.period_end),
                        db.PrepaidHourlyPeriod.status == "paid",
                    )
                )
            ).scalar_one_or_none()
            if (
                price is None
                or not price.provider_rate_exact
                or period is None
                or _aware(server.prepaid_paid_until) != _aware(entry.period_end)
            ):
                plan.review.append(
                    "immutable exact hourly quote and current paid period are required"
                )
                return
            stored = ProviderCostSnapshot(
                str(price.provider_rate_exact),
                str(price.currency),
                f"accepted hourly price snapshot {server.id}",
            )
            if (
                price.selling_minor != entry.nominal_amount_minor
                or price.selling_currency != entry.nominal_currency
            ):
                plan.review.append("hourly nominal contract differs from immutable quote")
            if entry.period_end - now <= timedelta(minutes=2):
                plan.review.append("hourly renewal is in the competing charge window")
            plan.funding_key = (
                f"prepaid-hourly:{server.id}:{entry.period_start.astimezone(UTC).isoformat()}"
            )
            plan.purpose, plan.resource_key = FundingPurpose.HOURLY_USAGE, str(server.id)
        elif entry.billing_model == "prepaid_monthly_fixed":
            order, renewal = plan.order, plan.renewal
            if (
                order is None
                or renewal is None
                or order.status != "active"
                or order.settlement_status != "complete"
                or renewal.status != "active"
                or _aware(renewal.provider_renewal_at) != _aware(entry.period_end)
            ):
                plan.review.append(
                    "active settled order and exact current renewal anchor are required"
                )
                return
            if (
                renewal.customer_price_minor != entry.nominal_amount_minor
                or renewal.currency != entry.nominal_currency
                or order.selling_price_minor != entry.nominal_amount_minor
                or order.selling_currency != entry.nominal_currency
            ):
                plan.review.append("monthly nominal contract differs from immutable renewal")
            if order.provider_monthly_rate_exact and order.provider_cost_currency:
                stored = ProviderCostSnapshot(
                    str(order.provider_monthly_rate_exact),
                    str(order.provider_cost_currency),
                    entry.provider_cost_snapshot.provenance,
                )
            if entry.period_end - now <= self._monthly_guard:
                plan.review.append("monthly renewal is in the competing charge window")
            period_date = entry.period_start.astimezone(UTC).date().isoformat()
            plan.funding_key = f"renewal-charge:{server.id}:{period_date}"
            plan.purpose = FundingPurpose.MONTHLY_RENEW
            boundary = entry.period_start.astimezone(UTC).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            plan.resource_key = f"server:{server.id}:period:{boundary.isoformat()}"
        else:
            plan.review.append("billing model requires an explicit supported coverage adapter")
            return
        if stored is None and self._verified_fact is not None:
            stored = await self._verified_fact(entry)
        if stored != entry.provider_cost_snapshot:
            plan.review.append(
                "exact native snapshot is absent or differs from authoritative accepted evidence"
            )
        # Existing grants for this period would double-count provider cost or
        # conflict with another recovery; only the immutable cutover replays.
        exists = (
            await session.execute(
                select(db.OperatorFundingGrant.id).where(
                    db.OperatorFundingGrant.operation_key == plan.funding_key
                )
            )
        ).first()
        if exists:
            plan.review.append("current period already has separate corporate funding evidence")

    async def _refund(self, session: AsyncSession, plan: _Plan, holds: list[Any]) -> int:
        if plan.wallet is None:
            raise CutoverNeedsReview("refund has no original personal wallet")
        rows = list(
            (
                await session.execute(
                    select(db.LedgerEntry)
                    .where(db.LedgerEntry.wallet_id == plan.wallet.id)
                    .limit(MAX_REVIEW_ROWS + 1)
                )
            ).scalars()
        )
        if len(rows) > MAX_REVIEW_ROWS:
            raise CutoverNeedsReview("wallet correction history exceeds bounded review limit")
        period_ids = list(
            (
                await session.execute(
                    select(db.PrepaidHourlyPeriod.id)
                    .where(db.PrepaidHourlyPeriod.server_id == plan.entry.server_id)
                    .limit(MAX_REVIEW_ROWS + 1)
                )
            ).scalars()
        )
        accrual_ids = list(
            (
                await session.execute(
                    select(db.AccrualPeriod.id)
                    .where(db.AccrualPeriod.server_id == plan.entry.server_id)
                    .limit(MAX_REVIEW_ROWS + 1)
                )
            ).scalars()
        )
        if len(period_ids) > MAX_REVIEW_ROWS or len(accrual_ids) > MAX_REVIEW_ROWS:
            raise CutoverNeedsReview("service financial history exceeds bounded review limit")
        roots = {plan.entry.server_id, *(hold.id for hold in holds), *period_ids, *accrual_ids}
        if plan.order is not None:
            roots.add(plan.order.id)
        related = set(roots)
        for _ in range(len(rows) + 1):
            expanded = related | {row.id for row in rows if row.reference_id in related}
            if expanded == related:
                break
            related = expanded
        charges = [row for row in rows if row.id in related and row.entry_type == "charge"]
        if {row.id for row in charges} != set(plan.entry.refund_charge_ids):
            raise CutoverNeedsReview(
                "refund must enumerate all and only proved service personal debits"
            )
        if any(row.currency != plan.wallet.currency or row.amount <= 0 for row in charges):
            raise CutoverNeedsReview("original debit currency or amount is not provable")
        released = {hold.id for hold in holds if hold.status != "captured"}
        if any(row.reference_id in released for row in charges):
            raise CutoverNeedsReview("released or uncaptured holds are not refundable debits")
        credits = 0
        for row in rows:
            if row.entry_type not in {"refund", "adjustment"}:
                continue
            if row.reference_id is None:
                raise CutoverNeedsReview(
                    "unattributed wallet correction prevents safe refund attribution"
                )
            if row.id not in related:
                continue
            if row.currency != plan.wallet.currency or row.amount <= 0:
                raise CutoverNeedsReview("related correction has ambiguous amount or currency")
            if row.entry_type == "refund":
                credits += int(row.amount)
            else:
                audits = list(
                    (
                        await session.execute(
                            select(db.AuditEvent)
                            .where(
                                db.AuditEvent.action == "wallet.adjust",
                                db.AuditEvent.resource_id == str(plan.wallet.id),
                            )
                            .limit(MAX_REVIEW_ROWS + 1)
                        )
                    ).scalars()
                )
                if len(audits) > MAX_REVIEW_ROWS:
                    raise CutoverNeedsReview("wallet adjustment audit exceeds bounded review limit")
                matching = [
                    audit
                    for audit in audits
                    if isinstance(audit.event_metadata, Mapping)
                    and audit.event_metadata.get("idempotency_key") == row.idempotency_key
                ]
                if len(matching) != 1:
                    raise CutoverNeedsReview("adjustment direction has no unique immutable audit")
                try:
                    raw_delta = matching[0].event_metadata["amount"]
                    if not isinstance(raw_delta, str):
                        raise ValueError("adjustment amount must be canonical signed integer text")
                    delta = int(raw_delta)
                    if str(delta) != raw_delta:
                        raise ValueError("adjustment amount must be canonical signed integer text")
                except (KeyError, TypeError, ValueError) as exc:
                    raise CutoverNeedsReview(
                        "adjustment direction is not an exact integer"
                    ) from exc
                if abs(delta) != row.amount:
                    raise CutoverNeedsReview("adjustment audit differs from ledger magnitude")
                credits += delta
        original = sum(int(row.amount) for row in charges)
        net = min(original, max(0, original - credits))
        if net != plan.entry.expected_refund_minor:
            raise CutoverNeedsReview(
                "net prior corrections differ from the explicitly approved refund"
            )
        if plan.wallet.balance + net > 2**63 - 1:
            raise CutoverNeedsReview("refund would exceed the wallet int64 range")
        return net

    async def _write(
        self,
        session: AsyncSession,
        manifest: CutoverManifest,
        plan: _Plan,
        key: str,
        receipt_id: UUID,
    ) -> None:
        entry, server = plan.entry, plan.server
        coverage = None
        if not entry.classification_only:
            if (
                plan.purpose is None
                or plan.funding_key is None
                or plan.resource_key is None
                or entry.provider_cost_snapshot is None
                or entry.period_start is None
                or entry.period_end is None
            ):
                raise FundingValidationError("accepted corporate funding facts are incomplete")
            grant_id, expense_id = uuid4(), uuid4()
            facts = dict(
                owner_user_id=entry.owner_id,
                operator_user_id=entry.owner_id,
                resource_key=plan.resource_key,
                purpose=plan.purpose.value,
                nominal_amount_minor=entry.nominal_amount_minor,
                nominal_currency=entry.nominal_currency,
                provider_cost_snapshot=entry.provider_cost_snapshot.to_dict(),
            )
            session.add(
                db.OperatorFundingGrant(
                    id=grant_id,
                    operation_key=plan.funding_key,
                    state="authorized",
                    created_at=manifest.effective_at,
                    **facts,
                )
            )
            await session.flush()
            session.add(
                db.OperatorExpense(
                    id=expense_id,
                    grant_id=grant_id,
                    operation_key=expense_operation_key(plan.funding_key),
                    occurred_at=manifest.effective_at,
                    **facts,
                )
            )
            await session.flush()
            grant = await session.get(db.OperatorFundingGrant, grant_id)
            if grant is None:
                raise FundingValidationError("persisted corporate funding grant is missing")
            cast(Any, grant).state = "settled"
            coverage = dict(
                period_start=entry.period_start.astimezone(UTC).isoformat(),
                period_end=entry.period_end.astimezone(UTC).isoformat(),
                grant_operation_key=plan.funding_key,
                expense_operation_key=expense_operation_key(plan.funding_key),
                grant_id=str(grant_id),
                expense_id=str(expense_id),
            )
            if plan.order is not None:
                plan.order.funding_source = "operator"
                plan.order.funding_operator_id = entry.owner_id
                plan.order.funding_grant_operation_key = plan.funding_key
                plan.order.provider_cost_snapshot = entry.provider_cost_snapshot.to_dict()
            if plan.renewal is not None:
                plan.renewal.funding_source = "operator"
                plan.renewal.funding_operator_id = entry.owner_id
                plan.renewal.provider_cost_snapshot = entry.provider_cost_snapshot.to_dict()
        server.funding_source, server.funding_operator_id = "operator", entry.owner_id
        metadata = dict(
            manifest_fingerprint=manifest.fingerprint(),
            classification_only=entry.classification_only,
            provider_key=entry.provider_key,
            provider_server_id=entry.provider_server_id,
            credential_account_id=entry.credential_account_id,
            expected_state=entry.expected_state,
            provider_proof=entry.provider_proof,
            coverage=coverage,
            refund_minor=plan.refund_minor,
            refund_charge_ids=[str(item) for item in entry.refund_charge_ids],
            preserved_watermarks={
                "last_accrued_at": str(entry.expected_last_accrued_at),
                "prepaid_paid_until": str(entry.expected_paid_until),
            },
        )
        if plan.refund_minor:
            plan.wallet.balance += plan.refund_minor
            refund_key = "cutover-refund:" + hashlib.sha256(key.encode()).hexdigest()
            metadata["refund_operation_key"] = refund_key
            session.add(
                db.LedgerEntry(
                    id=uuid4(),
                    wallet_id=plan.wallet.id,
                    entry_type="refund",
                    amount=plan.refund_minor,
                    currency=plan.wallet.currency,
                    idempotency_key=refund_key,
                    reference_type="server",
                    reference_id=entry.server_id,
                    description=entry.reason,
                )
            )
        session.add(
            db.OperatorFundingCutover(
                id=receipt_id,
                operation_key=key,
                server_id=entry.server_id,
                operator_user_id=manifest.operator_id,
                prior_funding_source=entry.expected_source.value,
                effective_at=manifest.effective_at,
                reason=entry.reason,
                cutover_metadata=metadata,
            )
        )
        session.add(
            db.AuditEvent(
                id=uuid4(),
                actor_type="admin",
                actor_id=manifest.operator_id,
                action="funding.server_cutover",
                resource_type="server",
                resource_id=str(entry.server_id),
                reason=entry.reason,
                event_metadata=metadata,
                occurred_at=to_db_utc(self._now()),
            )
        )
