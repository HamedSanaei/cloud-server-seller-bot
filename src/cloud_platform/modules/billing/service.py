"""Periodic usage accrual job (M06-005).

While a server is RUNNING, its usage is settled in quantum-sized periods
anchored at the server's creation instant. Every run settles all COMPLETE
periods that have elapsed since the server's accrual watermark
(``CloudServer.last_accrued_at``); the trailing partial quantum is never
settled here - it is billed as the final segment when the server is deleted
(M06-006), so a customer is never charged twice for the same slice.

Idempotency ("no duplicate charges on retry"):

- Every settled period moves money under a DETERMINISTIC idempotency key:
  ``accrual:{server_id}:{period_start_epoch}`` - or, for the very first
  period, by CAPTURING the creation-time hold (ledger entry key
  ``capture-server-create:{idempotency_key}``), which was reserved for
  exactly this charge.
- Before moving money the job looks the key up in the append-only ledger; a
  hit means the period was already settled and is replayed as a no-op that
  only advances the watermark.
- The ledger's unique idempotency-key constraint and the unique key on
  ``accrual_periods`` are the durable backstops; the job itself is
  serialized across processes by an advisory lock (same pattern as the
  catalog sync).

The first period is settled by capturing the creation hold instead of a
fresh debit: the hold reserved the first quantum's funds at creation time,
so capturing consumes exactly what was reserved (no balance race). If the
hold is gone (released by the failed-create path) or missing, the period is
charged like any later period and may hit insufficient balance - the job
then stops for that server and reports it, leaving the period unsettled for
the next run (the low-balance policy, M06-007, acts on the report).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
    ServerRepository,
)
from cloud_platform.modules.fx.domain import (
    SUPPORTED_CURRENCIES,
    FxPurpose,
    currency_exponent,
    major_to_minor,
    minor_to_major,
)
from cloud_platform.modules.pricing.domain import ServerPriceSnapshot, ServerPriceSnapshotRepository
from cloud_platform.modules.wallet.domain import (
    HoldRepository,
    HoldStatus,
    InsufficientBalanceError,
    LedgerEntryType,
    LedgerRepository,
    Wallet,
    WalletRepository,
)
from cloud_platform.modules.wallet.repository import HoldService

logger = logging.getLogger(__name__)


def _rule_key(rule: Any) -> str:
    return "|".join(str(part).strip() for part in (rule.provider, rule.plan, rule.location))


class AccrualPeriodExistsError(Exception):
    """Raised when an accrual-period record already exists for the charge key."""


@dataclass(frozen=True, slots=True)
class AccrualPeriod:
    """One settled quantum of usage (business record for the margin report)."""

    server_id: UUID
    wallet_id: UUID
    period_start: datetime
    period_end: datetime
    selling_minor: int
    idempotency_key: str
    quanta: int = 1
    cost_minor: int = 0
    currency: str = "USD"
    id: UUID | None = None
    #: Explicit money-side currencies.  Both are optional at the end of the
    #: constructor so legacy callers continue to work; None means the old
    #: ``currency`` value.  ``cost_minor`` is never converted or relabelled.
    cost_currency: str | None = None
    selling_currency: str | None = None
    #: Exact provider-native major-unit cost for this settled window.
    cost_amount: Decimal | None = None
    rule_key: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.selling_minor, bool) or not isinstance(self.selling_minor, int):
            raise ValueError("selling_minor must be an integer")
        if self.selling_minor <= 0 or self.selling_minor > 9_223_372_036_854_775_807:
            raise ValueError("selling_minor is outside signed int64 bounds")
        if isinstance(self.cost_minor, bool) or not isinstance(self.cost_minor, int):
            raise ValueError("cost_minor must be an integer")
        if self.cost_minor < 0 or self.cost_minor > 9_223_372_036_854_775_807:
            raise ValueError("cost_minor is outside signed int64 bounds")
        if (
            isinstance(self.quanta, bool)
            or not isinstance(self.quanta, int)
            or self.quanta <= 0
            or self.quanta > 9_223_372_036_854_775_807
        ):
            raise ValueError("quanta is outside signed int64 bounds")
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        code = str(self.currency or "").strip().upper()
        if code not in SUPPORTED_CURRENCIES:
            raise ValueError("currency must be an audited ISO code")
        object.__setattr__(self, "currency", code)
        for name, value in (
            ("cost_currency", self.cost_currency),
            ("selling_currency", self.selling_currency),
        ):
            if value is not None and (
                not isinstance(value, str) or value.strip().upper() not in SUPPORTED_CURRENCIES
            ):
                raise ValueError(f"{name} must be an audited ISO code")
        object.__setattr__(self, "cost_currency", (self.cost_currency or code).strip().upper())
        object.__setattr__(
            self, "selling_currency", (self.selling_currency or code).strip().upper()
        )
        if self.cost_amount is not None and (
            not isinstance(self.cost_amount, Decimal)
            or not self.cost_amount.is_finite()
            or self.cost_amount < 0
        ):
            raise ValueError("cost_amount must be a non-negative finite Decimal")
        if self.rule_key is not None and (
            not isinstance(self.rule_key, str) or not self.rule_key.strip()
        ):
            raise ValueError("rule_key must be a non-empty string when supplied")
        if self.rule_key is not None:
            object.__setattr__(self, "rule_key", self.rule_key.strip())


class AccrualPeriodRepository(Protocol):
    """Port for the accrual-period business records."""

    async def get_by_key(self, idempotency_key: str) -> AccrualPeriod | None: ...

    async def add(self, period: AccrualPeriod) -> AccrualPeriod:
        """Insert the record. Raises AccrualPeriodExistsError on key collision."""
        ...

    async def list_between(self, start: datetime, end: datetime) -> list[AccrualPeriod]:
        """All periods with period_start in [start, end) (margin reporting)."""
        ...

    async def daily_cost_totals(
        self, day_start: datetime, day_end: datetime, server_ids: frozenset[UUID]
    ) -> dict[str, int]: ...

    async def month_total(
        self,
        wallet_id: UUID,
        month_start: datetime,
        currency: str | None = None,
        rule_key: str | None = None,
    ) -> int:
        """Sum of selling_minor billed to the wallet at/after month_start (caps).

        When supplied, the currency is mandatory: a wallet's cap cannot mix
        minor units from another currency or silently absorb corrupt rows.
        """
        ...

    async def daily_cost_total(
        self,
        day_start: datetime,
        day_end: datetime,
        server_ids: frozenset[UUID],
        currency: str | None = None,
    ) -> int:
        """Legacy rounded provider-spend total for compatibility only."""
        ...

    async def daily_cost_exact_total(
        self,
        day_start: datetime,
        day_end: datetime,
        server_ids: frozenset[UUID],
        currency: str,
    ) -> Decimal:
        """Exact Decimal provider-spend total in one native currency."""
        ...


class JobLock(Protocol):
    """Port for a lock that serializes accrual runs across processes."""

    def guard(self) -> AbstractAsyncContextManager[bool]:
        """Enter the run critical section; yields whether the lock was won."""
        ...


@dataclass(slots=True)
class AccrualRunReport:
    """Summary of one accrual run (accumulated in place)."""

    servers_checked: int = 0
    periods_posted: int = 0
    periods_replayed: int = 0
    insufficient_balance: int = 0
    capped_periods: int = 0
    errors: int = 0
    # ``charged_minor``/``currency`` are retained as a compatibility projection
    # only when the run contains exactly one currency. Mixed-currency runs are
    # represented by ``charged_by_currency`` alone; summing amounts across
    # currencies would be financially meaningless.
    charged_minor: int = 0
    currency: str = ""
    charged_by_currency: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        charged = self.charged_by_currency
        if not charged and self.currency:
            charged = {self.currency: self.charged_minor}
        compat = (
            f" charged={self.charged_minor}{self.currency}"
            if self.currency and len(charged) == 1
            else ""
        )
        return (
            "accrual run: "
            f"servers={self.servers_checked} posted={self.periods_posted} "
            f"replayed={self.periods_replayed} insufficient={self.insufficient_balance} "
            f"capped={self.capped_periods} errors={self.errors} "
            f"charged_by_currency={charged}{compat}"
        )


def _aware(dt: datetime | None, name: str) -> datetime:
    """Normalize to an aware UTC datetime (DB timestamps are UTC)."""
    if dt is None:
        raise ValueError(f"{name} is required")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _elapsed_seconds(start: datetime, end: datetime) -> Decimal:
    """Return elapsed seconds without passing through a binary float."""
    delta = _aware(end, "end") - _aware(start, "start")
    whole_seconds = delta.days * 86_400 + delta.seconds
    with localcontext() as context:
        context.prec = 50
        return Decimal(whole_seconds) + Decimal(delta.microseconds) / Decimal(1_000_000)


def _exact_cost_amount(snapshot: ServerPriceSnapshot, quanta: int) -> Decimal:
    """Native provider cost without reconstructing exact rates from cents."""
    if quanta <= 0:
        raise ValueError("quanta must be positive")
    raw = snapshot.offer.provider_rate_exact
    if raw is not None:
        with localcontext() as context:
            context.prec = max(50, len(str(raw)) + len(str(quanta)) + 20)
            return Decimal(raw) * Decimal(quanta)
    cost_currency, selling_currency = _snapshot_currencies(snapshot)
    if cost_currency == selling_currency:
        # Legacy same-currency snapshots predate exact provider-rate capture.
        # Reconstructing their already-billed native minor cost is safe because
        # no cross-currency conversion is being inferred. Cross-currency rows
        # still fail closed below.
        return minor_to_major(snapshot.offer.cost_minor, cost_currency) * Decimal(quanta)
    raise ValueError(
        "exact provider rate is required for cross-currency financial accrual; refusing "
        "to reconstruct it from rounded minor units"
    )


def _snapshot_currencies(snapshot: Any) -> tuple[str, str]:
    """Return ``(cost_currency, selling_currency)`` without conversion."""
    offer = snapshot.offer
    cost_currency = str(offer.currency).strip()
    raw_selling = getattr(snapshot, "selling_currency", None)
    selling_currency = (
        raw_selling.strip() if isinstance(raw_selling, str) and raw_selling.strip() else ""
    )
    selling_currency = selling_currency or cost_currency
    if not cost_currency or not selling_currency:
        raise ValueError("price snapshot must carry both cost and selling currencies")
    return cost_currency, selling_currency


def _require_wallet_currency(wallet: Wallet, currency: str) -> None:
    """Fail closed before charging a wallet in the wrong currency."""
    if wallet.currency != currency:
        raise ValueError(
            f"wallet currency {wallet.currency} does not match selling currency {currency}"
        )


def _minor_product(quanta: int, unit_minor: int) -> int:
    """Multiply integer minor units without introducing floating point."""
    with localcontext() as context:
        context.prec = max(50, len(str(quanta)) + len(str(unit_minor)) + 20)
        result = int(Decimal(quanta) * Decimal(unit_minor))
    if not -(2**63) <= result <= 2**63 - 1:
        raise ValueError("minor-unit product is outside signed int64 bounds")
    return result


def _accrual_matches(actual: AccrualPeriod, expected: AccrualPeriod) -> bool:
    """Compare every immutable billing fact, not merely the key."""
    return (
        actual.server_id == expected.server_id
        and actual.wallet_id == expected.wallet_id
        and _aware(actual.period_start, "period_start")
        == _aware(expected.period_start, "period_start")
        and _aware(actual.period_end, "period_end") == _aware(expected.period_end, "period_end")
        and actual.selling_minor == expected.selling_minor
        and actual.quanta == expected.quanta
        and actual.cost_minor == expected.cost_minor
        and actual.currency == expected.currency
        and actual.cost_currency == expected.cost_currency
        and actual.selling_currency == expected.selling_currency
        and (actual.rule_key or "") == (expected.rule_key or "")
        and actual.cost_amount == expected.cost_amount
    )


async def _persist_accrual_record(
    repository: AccrualPeriodRepository, period: AccrualPeriod
) -> AccrualPeriod:
    """Insert or validate an existing period; mismatched replays fail closed."""
    getter = getattr(repository, "get_by_key", None)
    if callable(getter):
        existing = await getter(period.idempotency_key)
        if existing is not None:
            if not _accrual_matches(existing, period):
                raise ValueError(
                    f"accrual idempotency key {period.idempotency_key!r} was reused "
                    "with different immutable facts"
                )
            return cast(AccrualPeriod, existing)
    try:
        return await repository.add(period)
    except AccrualPeriodExistsError:
        if not callable(getter):
            raise ValueError(
                "cannot validate a duplicate accrual record without get_by_key"
            ) from None
        existing = await getter(period.idempotency_key)
        if existing is None or not _accrual_matches(existing, period):
            raise ValueError(
                f"accrual idempotency key {period.idempotency_key!r} has conflicting facts"
            ) from None
        return cast(AccrualPeriod, existing)


def accrual_charge_key(server_id: UUID, period_start: datetime) -> str:
    """Deterministic ledger idempotency key for one settlement period."""
    epoch = int(_aware(period_start, "period_start").timestamp())
    return f"accrual:{server_id}:{epoch}"


def month_start_utc(at: datetime) -> datetime:
    """The first instant of the UTC calendar month containing ``at``."""
    moment = _aware(at, "at")
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class MissingSnapshotError(Exception):
    """Raised when a server has no pinned price snapshot to price a period."""


class AccrualJob:
    """Settles complete usage periods for RUNNING servers."""

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        hold_service: HoldService,
        ledger_repo: LedgerRepository,
        accrual_repo: AccrualPeriodRepository,
        snapshot_repo: ServerPriceSnapshotRepository,
        audit_repo: AuditRepository,
        lock: JobLock | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._servers = server_repo
        self._wallets = wallet_repo
        self._hold_repo = hold_repo
        self._holds = hold_service
        self._ledger = ledger_repo
        self._accruals = accrual_repo
        self._snapshots = snapshot_repo
        self._audit = AuditTrail(audit_repo)
        self._lock = lock
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _assert_capture_ledger(
        self,
        *,
        wallet_id: UUID,
        hold: Any,
        capture_key: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
    ) -> None:
        """Require the atomic capture's complete CHARGE audit fact."""
        if hold is None or getattr(hold, "id", None) is None:
            raise ValueError("hold capture returned no persisted hold")
        entry = await self._ledger.get_entry_by_idempotency(wallet_id, capture_key)
        if entry is None:
            raise ValueError(f"hold capture {capture_key!r} has no CHARGE ledger fact")
        if (
            entry.amount.amount != Decimal(amount_minor)
            or entry.amount.currency.upper() != currency.upper()
            or entry.entry_type is not LedgerEntryType.CHARGE
            or entry.reference_type != "hold"
            or entry.reference_id != str(hold.id)
            or entry.description != f"hold captured for {idempotency_key}"
        ):
            raise ValueError(f"hold capture {capture_key!r} has different CHARGE facts")

    async def run(self, now: datetime | None = None) -> AccrualRunReport:
        """One accrual pass over all RUNNING servers.

        Per-server problems never break the run: they are counted in the
        report so one bad server cannot stop billing for the rest.
        """
        moment = _aware(now or self._clock(), "now")
        if self._lock is not None:
            async with self._lock.guard() as acquired:
                if not acquired:
                    logger.info("accrual run skipped: lock held by another run")
                    return AccrualRunReport()
                return await self._run_servers(moment)
        return await self._run_servers(moment)

    async def _run_servers(self, now: datetime) -> AccrualRunReport:
        report = AccrualRunReport()
        servers = [s for s in await self._servers.list_running() if not s.is_prepaid_monthly]
        report.servers_checked = len(servers)
        for server in servers:
            try:
                report.periods_posted += await self._accrue_server(server, now, report)
            except InsufficientBalanceError:
                report.insufficient_balance += 1
                logger.warning("server %s: insufficient balance for accrual", server.id)
            except Exception:
                report.errors += 1
                logger.exception("accrual failed for server %s", server.id)
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="billing.accrual",
            resource_type="billing",
            resource_id="accrual_run",
            reason=report.render(),
            metadata={
                "servers_checked": str(report.servers_checked),
                "periods_posted": str(report.periods_posted),
                "periods_replayed": str(report.periods_replayed),
                "insufficient_balance": str(report.insufficient_balance),
                "capped_periods": str(report.capped_periods),
                "errors": str(report.errors),
                "charged_by_currency": {
                    key: str(value) for key, value in report.charged_by_currency.items()
                },
            },
        )
        return report

    async def _accrue_server(
        self, server: CloudServer, now: datetime, report: AccrualRunReport
    ) -> int:
        """Settle all complete periods for one server; return periods posted."""
        start = _aware(server.created_at, "server.created_at")
        quantum_seconds = server.quantum_seconds
        last = _aware(server.last_accrued_at or start, "watermark")
        if last < start:
            last = start  # corrupted/legacy watermark: rebase to the billing start

        elapsed = _elapsed_seconds(last, now)
        quantum = Decimal(quantum_seconds)
        if elapsed < quantum:
            return 0  # no complete period has elapsed yet

        completed = int(elapsed // quantum)
        posted = 0
        for k in range(completed):
            period_start = last + timedelta(seconds=quantum_seconds * k)
            period_end = period_start + timedelta(seconds=quantum_seconds)
            did_post, stop = await self._settle_period(server, period_start, period_end, report)
            posted += did_post
            if stop:
                break
        return posted

    async def _settle_period(
        self,
        server: CloudServer,
        period_start: datetime,
        period_end: datetime,
        report: AccrualRunReport,
    ) -> tuple[int, bool]:
        """Settle one period. Returns (posted: 0/1, stop: bool)."""
        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.id is None:
            report.errors += 1
            logger.error("server %s: no wallet for user %s", server.id, server.user_id)
            return 0, True

        snapshot = await self._snapshots.get(server.id)
        if snapshot is None:
            raise MissingSnapshotError(f"server {server.id} has no price snapshot")
        cost_currency, selling_currency = _snapshot_currencies(snapshot)
        # A wallet has one balance unit.  Never debit it and then label the
        # resulting ledger entry with a different selling currency.
        _require_wallet_currency(wallet, selling_currency)
        selling_minor = int(Decimal(snapshot.selling_minor))
        cost_minor = int(Decimal(snapshot.offer.cost_minor))
        # Validate the exact native cost before any hold capture, wallet
        # debit, or ledger mutation. Legacy snapshots without exact text are
        # quarantined by this failure; they are never rounded into a charge.
        exact_cost = _exact_cost_amount(snapshot, 1)

        # First period: the creation hold was reserved for exactly this charge.
        hold = None
        captured_this_period = False
        is_first_period = period_start == _aware(server.created_at, "server.created_at")
        if is_first_period and server.idempotency_key:
            hold = await self._hold_repo.get_by_idempotency(
                wallet.id, f"server-create:{server.idempotency_key}"
            )

        # The charge key must be deterministic independent of the hold's
        # current status: period 0 may have been settled by capturing the
        # hold (entry key "capture-server-create:{ik}") OR, when the hold was
        # released by the failed-create path, by a plain debit under the
        # period key. A replay is a hit under EITHER key.
        candidate_keys: list[str] = []
        if is_first_period and server.idempotency_key:
            candidate_keys.append(f"capture-server-create:{server.idempotency_key}")
        candidate_keys.append(accrual_charge_key(server.id, period_start))

        # Idempotent replay: the ledger is the source of truth for money, while
        # the accrual row is a repairable audit record. A crash can leave one
        # without the other; never advance the watermark until the missing
        # record is persisted and every immutable fact agrees.
        replay_matches: list[tuple[str, Any]] = []
        for key in candidate_keys:
            replay_entry = await self._ledger.get_entry_by_idempotency(wallet.id, key)
            if replay_entry is None:
                continue
            if (
                replay_entry.amount.amount != Decimal(selling_minor)
                or replay_entry.amount.currency.upper() != selling_currency.upper()
                or replay_entry.entry_type is not LedgerEntryType.CHARGE
                or (
                    key.startswith("capture-")
                    and (
                        hold is None
                        or hold.id is None
                        or hold.status is not HoldStatus.CAPTURED
                        or replay_entry.reference_type != "hold"
                        or replay_entry.reference_id != str(hold.id)
                        or replay_entry.description
                        != f"hold captured for server-create:{server.idempotency_key}"
                    )
                )
                or (
                    not key.startswith("capture-")
                    and (
                        replay_entry.reference_type != "server"
                        or replay_entry.reference_id != str(server.id)
                        or replay_entry.description
                        != (
                            f"usage {period_start.strftime('%Y-%m-%d %H:%M')} to "
                            f"{period_end.strftime('%H:%M')} UTC"
                        )
                    )
                )
            ):
                raise ValueError(f"ledger idempotency key {key!r} has different settlement facts")
            replay_matches.append((key, replay_entry))
        if len(replay_matches) > 1:
            raise ValueError(
                "first billing period has both capture and usage settlement keys; "
                "financial reconciliation requires operator review"
            )
        if replay_matches:
            record_key, _replay_entry = replay_matches[0]
            replay_period = AccrualPeriod(
                server_id=server.id,
                wallet_id=wallet.id,
                period_start=period_start,
                period_end=period_end,
                selling_minor=selling_minor,
                cost_minor=cost_minor,
                currency=selling_currency,
                cost_currency=cost_currency,
                selling_currency=selling_currency,
                cost_amount=exact_cost,
                rule_key=_rule_key(snapshot.rule),
                idempotency_key=record_key,
            )
            await _persist_accrual_record(self._accruals, replay_period)
            report.periods_replayed += 1
            server = await self._advance(server, period_end)
            return 0, False

        # Optional monthly cap. This check deliberately happens after replay
        # reconciliation: a crash after a captured hold/CHARGE must repair the
        # missing audit record even when the cap would otherwise skip the
        # period. The period's own start defines its calendar month, including
        # a period ending exactly at the next UTC midnight.
        cap = snapshot.rule.monthly_cap_minor
        if cap is not None:
            month_start = month_start_utc(period_start)
            billed = await self._accruals.month_total(
                wallet.id, month_start, currency=selling_currency, rule_key=_rule_key(snapshot.rule)
            )
            if billed + selling_minor > cap:
                # A CAPTURED hold is already money spent. Repair its missing
                # CHARGE fact and the accrual row before advancing the cap
                # watermark; a CREATED hold is merely reserved and is left
                # for the normal capture/release path.
                if (
                    is_first_period
                    and hold is not None
                    and hold.status is HoldStatus.CAPTURED
                    and server.idempotency_key
                ):
                    assert hold.id is not None
                    capture_key = f"capture-server-create:{server.idempotency_key}"
                    repaired = await self._holds.capture_hold(
                        wallet.id, hold.id, f"server-create:{server.idempotency_key}"
                    )
                    if repaired is None or repaired.status is not HoldStatus.CAPTURED:
                        raise ValueError("captured hold could not be repaired")
                    hold = repaired
                    await self._assert_capture_ledger(
                        wallet_id=wallet.id,
                        hold=hold,
                        capture_key=capture_key,
                        amount_minor=selling_minor,
                        currency=selling_currency,
                        idempotency_key=f"server-create:{server.idempotency_key}",
                    )
                    await _persist_accrual_record(
                        self._accruals,
                        AccrualPeriod(
                            server_id=server.id,
                            wallet_id=wallet.id,
                            period_start=period_start,
                            period_end=period_end,
                            selling_minor=selling_minor,
                            cost_minor=cost_minor,
                            currency=selling_currency,
                            cost_currency=cost_currency,
                            selling_currency=selling_currency,
                            cost_amount=exact_cost,
                            rule_key=_rule_key(snapshot.rule),
                            idempotency_key=candidate_keys[0],
                        ),
                    )
                    report.periods_replayed += 1
                    server = await self._advance(server, period_end)
                    return 0, False
                if is_first_period and hold is not None and hold.status is HoldStatus.CREATED:
                    assert hold.id is not None
                    hold_key = f"server-create:{server.idempotency_key}"
                    released_hold = await self._holds.release_hold(wallet.id, hold.id, hold_key)
                    if released_hold is None or released_hold.status is not HoldStatus.RELEASED:
                        raise ValueError("capped creation hold release was not durably confirmed")
                report.capped_periods += 1
                logger.warning(
                    "server %s: monthly cap %d reached (billed %d + %d), period "
                    "%s settled as capped (never billed)",
                    server.id,
                    cap,
                    billed,
                    selling_minor,
                    period_start,
                )
                await self._audit.record_mutation(
                    actor_type=ActorType.SYSTEM,
                    actor_id=None,
                    action="billing.cap_reached",
                    resource_type="server",
                    resource_id=str(server.id),
                    reason=(
                        f"monthly cap {cap} reached: billed {billed}, "
                        f"period charge {selling_minor} settled as capped"
                    ),
                    metadata={
                        "server_id": str(server.id),
                        "cap": str(cap),
                        "billed": str(billed),
                        "skipped_charge": str(selling_minor),
                        "month_start": month_start.isoformat(),
                    },
                )
                server = await self._advance(server, period_end)
                return 0, False

        if hold is not None and hold.status in (HoldStatus.CREATED, HoldStatus.CAPTURED):
            # A hold reserves funds in its own currency. Capturing a hold from
            # a different currency would silently charge the wrong unit. A
            # captured hold is also passed through the idempotent service so a
            # missing CHARGE row is repaired without a second debit.
            hold_currency = getattr(hold, "currency", selling_currency)
            hold_amount = getattr(hold, "amount", selling_minor)
            if hold_currency != selling_currency or hold_amount != selling_minor:
                raise ValueError(
                    "creation hold amount/currency does not match the first-period charge"
                )
            assert hold.id is not None  # persisted holds carry a DB-assigned id
            hold_id: UUID = hold.id
            captured_hold = await self._holds.capture_hold(
                wallet.id, hold_id, f"server-create:{server.idempotency_key}"
            )
            if captured_hold is None or captured_hold.status is not HoldStatus.CAPTURED:
                raise ValueError("hold capture returned no persisted CAPTURED hold")
            hold = captured_hold
            await self._assert_capture_ledger(
                wallet_id=wallet.id,
                hold=hold,
                capture_key=f"capture-server-create:{server.idempotency_key}",
                amount_minor=selling_minor,
                currency=selling_currency,
                idempotency_key=f"server-create:{server.idempotency_key}",
            )
            captured_this_period = True
        else:
            charge_key = accrual_charge_key(server.id, period_start)
            description = (
                f"usage {period_start.strftime('%Y-%m-%d %H:%M')} to "
                f"{period_end.strftime('%H:%M')} UTC"
            )
            atomic_adjust = getattr(self._wallets, "adjust", None)
            if not callable(atomic_adjust):
                raise RuntimeError("atomic wallet/ledger adjustment capability is required")
            await atomic_adjust(
                server.user_id,
                -selling_minor,
                charge_key,
                entry_type=LedgerEntryType.CHARGE,
                reference_type="server",
                reference_id=str(server.id),
                description=description,
            )

        # Business record for the margin report. The ledger entry above is
        # authoritative for the money; a duplicate record (crash + retry)
        # is harmless because the key is unique.
        if captured_this_period or (hold is not None and hold.status is HoldStatus.CAPTURED):
            record_key = f"capture-server-create:{server.idempotency_key}"
        else:
            record_key = accrual_charge_key(server.id, period_start)
        period = AccrualPeriod(
            server_id=server.id,
            wallet_id=wallet.id,
            period_start=period_start,
            period_end=period_end,
            selling_minor=selling_minor,
            cost_minor=cost_minor,
            currency=selling_currency,
            cost_currency=cost_currency,
            selling_currency=selling_currency,
            cost_amount=exact_cost,
            rule_key=_rule_key(snapshot.rule),
            idempotency_key=record_key,
            quanta=1,
        )
        await _persist_accrual_record(self._accruals, period)

        report.charged_by_currency[selling_currency] = (
            report.charged_by_currency.get(selling_currency, 0) + selling_minor
        )
        if len(report.charged_by_currency) == 1:
            only_currency, only_total = next(iter(report.charged_by_currency.items()))
            report.charged_minor = only_total
            report.currency = only_currency
        else:
            # Never expose a sum whose unit is ambiguous.
            report.charged_minor = 0
            report.currency = ""
        await self._advance(server, period_end)
        return 1, False

    async def _advance(self, server: CloudServer, period_end: datetime) -> CloudServer:
        """Move the server's accrual watermark and return the refreshed row."""
        server.last_accrued_at = period_end
        refreshed = await self._servers.save(server)
        # Keep the loop's object on the same optimistic version.  SQLAlchemy
        # repositories return a fresh domain row rather than mutating the input;
        # failing to copy its version makes a second backlog period CAS-fail
        # after the first period's money is already committed.
        server.updated_at = refreshed.updated_at
        return refreshed


# ---------------------------------------------------------------------------
# Final deletion charge (M06-006)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinalChargeResult:
    """Outcome of settling a server's final usage segment."""

    charged_minor: int
    captured_hold: bool
    posted_entry_key: str | None
    replayed: bool
    capped: bool

    @property
    def charged(self) -> int:
        return self.charged_minor


class FinalChargeService:
    """Posts the final usage segment once, after confirmed deletion.

    When a server is deleted, the trailing PARTIAL quantum between its last
    accrual watermark and the deletion instant has not been settled by the
    periodic job (it only settles complete periods). This service bills it,
    and only it:

    - The window is ``[last_accrued_at or created_at, deleted_at]``. A
      zero-length window (deleted exactly on a grid boundary) charges
      nothing.
    - If the creation hold is still CREATED and the window covers the first
      quantum, the hold is CAPTURED first: it reserved exactly that quantum's
      funds, and the capture carries its own deterministic key
      (``capture-server-create:{ik}``). Any remaining window is then billed
      flat.
    - The flat remainder is one debit plus one CHARGE entry under the
      deterministic key ``final:{server_id}`` (a partial quantum bills as a
      full one, per the billing policy).
    - Every money movement is idempotent under its key: a full replay
      (crash before persistence) re-derives both legs and moves nothing;
      a partial replay (crash between the two legs) settles exactly the
      missing leg. The final segment is therefore posted exactly once.

    Called by the deletion flow (M07-007) when deletion is confirmed; it
    requires the server to be in DELETED state with a deletion timestamp.
    """

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        hold_service: HoldService,
        ledger_repo: LedgerRepository,
        accrual_repo: AccrualPeriodRepository,
        snapshot_repo: ServerPriceSnapshotRepository,
        audit_repo: AuditRepository,
        lock: JobLock | None = None,
    ) -> None:
        self._servers = server_repo
        self._wallets = wallet_repo
        self._hold_repo = hold_repo
        self._holds = hold_service
        self._ledger = ledger_repo
        self._accruals = accrual_repo
        self._snapshots = snapshot_repo
        self._audit = AuditTrail(audit_repo)
        self._lock = lock

    @staticmethod
    def final_charge_key(server_id: UUID) -> str:
        return f"final:{server_id}"

    async def charge_final(self, server: CloudServer, deleted_at: datetime) -> FinalChargeResult:
        """Serialize cap-sensitive final charging with periodic accrual."""
        if self._lock is None:
            return await self._charge_final_locked(server, deleted_at)
        async with self._lock.guard() as acquired:
            if not acquired:
                raise RuntimeError("billing cap lock is busy; retry final charge")
            return await self._charge_final_locked(server, deleted_at)

    async def _charge_final_locked(
        self, server: CloudServer, deleted_at: datetime
    ) -> FinalChargeResult:
        """Settle the final usage segment of a confirmed deletion."""
        if server.state is not ServerLifecycleState.DELETED:
            raise ValueError(f"final charge requires a DELETED server (state={server.state.value})")
        deleted = _aware(deleted_at, "deleted_at")
        created = _aware(server.created_at, "server.created_at")
        if deleted < created:
            raise ValueError("deleted_at cannot be before the server was created")

        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.id is None:
            raise ValueError(f"server {server.id}: no wallet for user {server.user_id}")
        wallet_id: UUID = wallet.id

        snapshot = await self._snapshots.get(server.id)
        if snapshot is None:
            raise MissingSnapshotError(f"server {server.id} has no price snapshot")
        cost_currency, selling_currency = _snapshot_currencies(snapshot)
        # A wallet has one balance unit.  Never debit it and then label the
        # resulting ledger entry with a different selling currency.
        _require_wallet_currency(wallet, selling_currency)
        selling_minor = int(Decimal(snapshot.selling_minor))
        cost_minor = int(Decimal(snapshot.offer.cost_minor))
        exact_base_cost = _exact_cost_amount(snapshot, 1)

        window_start = _aware(server.last_accrued_at or created, "window start")
        if window_start < created:
            window_start = created

        charged = 0
        charged_by_month: dict[datetime, int] = {}
        remainder_periods: list[tuple[datetime, datetime, int, str]] = []
        charged_quanta = 0
        first_leg_charged = False
        remainder_charged = False
        captured = False
        capped = False
        posted_key: str | None = None
        replayed = False
        first_leg_key = (
            f"capture-server-create:{server.idempotency_key}" if server.idempotency_key else None
        )
        if first_leg_key is not None and window_start == created:
            getter = getattr(self._accruals, "get_by_key", None)
            if callable(getter):
                first_record = await getter(first_leg_key)
                if first_record is not None:
                    hold = await self._hold_repo.get_by_idempotency(
                        wallet.id, f"server-create:{server.idempotency_key}"
                    )
                if first_record is not None:
                    if first_record.quanta != 1 or first_record.selling_minor != selling_minor:
                        raise ValueError("first-period accrual facts do not match the final charge")
                    capture_entry = await self._ledger.get_entry_by_idempotency(
                        wallet.id, first_leg_key
                    )
                    if (
                        capture_entry is None
                        or capture_entry.amount.amount != Decimal(selling_minor)
                        or capture_entry.amount.currency.upper() != selling_currency.upper()
                        or capture_entry.entry_type is not LedgerEntryType.CHARGE
                        or hold is None
                        or hold.id is None
                        or hold.status is not HoldStatus.CAPTURED
                        or capture_entry.reference_type != "hold"
                        or capture_entry.reference_id != str(hold.id)
                        or capture_entry.description
                        != f"hold captured for server-create:{server.idempotency_key}"
                    ):
                        raise ValueError(
                            "first-period accrual exists without its CHARGE ledger fact"
                        )
                    first_leg_charged = True
                    charged_quanta += 1
                    # The accrual and its CHARGE were created by an earlier
                    # periodic pass. This final-charge replay must not debit
                    # again, but it must still advance the durable watermark.
                    replayed = True
                    posted_key = first_leg_key
        start = window_start

        # Optional monthly cap.  Each leg is settled in the UTC month that
        # contains its period start, and each month has its own durable total.
        cap = snapshot.rule.monthly_cap_minor

        remainder_period_start = window_start
        if first_leg_charged and window_start == created and server.idempotency_key:
            # The capture ledger and business record already prove this leg.
            # Do not include it again in the final-remainder calculation.
            remainder_period_start = created + timedelta(seconds=server.quantum_seconds)
            start = remainder_period_start

        # Leg 1: the still-reserved first quantum, if the window covers it.
        if start == created and server.idempotency_key:
            hold_key = f"server-create:{server.idempotency_key}"
            hold = await self._hold_repo.get_by_idempotency(wallet.id, hold_key)
            if hold is not None and hold.status is HoldStatus.CREATED:
                hold_currency = getattr(hold, "currency", selling_currency)
                if hold_currency != selling_currency:
                    raise ValueError(
                        f"hold currency {hold_currency} does not match selling currency "
                        f"{selling_currency}"
                    )
                assert hold.id is not None
                first_month = month_start_utc(start)
                if cap is not None:
                    first_month_billed = await self._accruals.month_total(
                        wallet.id,
                        first_month,
                        currency=selling_currency,
                        rule_key=_rule_key(snapshot.rule),
                    )
                    first_month_billed += charged_by_month.get(first_month, 0)
                else:
                    first_month_billed = 0
                if cap is not None and first_month_billed + selling_minor > cap:
                    # The cap protects the user: return the reserved funds
                    # instead of overcharging (the platform absorbs the loss).
                    released_hold = await self._holds.release_hold(wallet.id, hold.id, hold_key)
                    if released_hold is None or released_hold.status is not HoldStatus.RELEASED:
                        raise ValueError("hold release was not durably confirmed")
                    capped = True
                    remainder_period_start = start + timedelta(seconds=server.quantum_seconds)
                    start = remainder_period_start
                    await self._cap_audit(
                        server, wallet, cap, first_month_billed, selling_minor, "capture leg"
                    )
                else:
                    captured_hold = await self._holds.capture_hold(wallet.id, hold.id, hold_key)
                    if captured_hold is None or captured_hold.status is not HoldStatus.CAPTURED:
                        raise ValueError("hold capture was not durably confirmed")
                    await self._assert_capture_ledger(
                        wallet_id=wallet.id,
                        hold=captured_hold,
                        capture_key=f"capture-{hold_key}",
                        amount_minor=selling_minor,
                        currency=selling_currency,
                        idempotency_key=hold_key,
                    )
                    captured = True
                    charged += selling_minor
                    charged_by_month[first_month] = (
                        charged_by_month.get(first_month, 0) + selling_minor
                    )
                    charged_quanta += 1
                    first_leg_charged = True
                    posted_key = f"capture-{hold_key}"
                    remainder_period_start = start + timedelta(seconds=server.quantum_seconds)
                    start = remainder_period_start
            elif hold is not None and hold.status is HoldStatus.CAPTURED:
                hold_currency = getattr(hold, "currency", selling_currency)
                hold_amount = getattr(hold, "amount", selling_minor)
                if hold_currency != selling_currency or hold_amount != selling_minor:
                    raise ValueError("captured hold facts do not match final charge snapshot")
                # Re-run the idempotent atomic capture to prove/repair CHARGE;
                # it never debits a second time when the hold is already CAPTURED.
                assert hold.id is not None
                captured_hold = await self._holds.capture_hold(wallet.id, hold.id, hold_key)
                if captured_hold is None or captured_hold.status is not HoldStatus.CAPTURED:
                    raise ValueError("existing hold capture was not durably confirmed")
                await self._assert_capture_ledger(
                    wallet_id=wallet.id,
                    hold=captured_hold,
                    capture_key=f"capture-{hold_key}",
                    amount_minor=selling_minor,
                    currency=selling_currency,
                    idempotency_key=hold_key,
                )
                posted_key = f"capture-{hold_key}"
                if not first_leg_charged:
                    # The capture is already a durable financial movement;
                    # repair its accrual record but do not report a second
                    # charge or add it to the cap budget on replay.
                    charged_quanta += 1
                first_leg_charged = True
                remainder_period_start = start + timedelta(seconds=server.quantum_seconds)
                start = remainder_period_start

        # Leg 2: split the flat remainder at UTC month boundaries.  A single
        # month retains the historical ``final:{server}`` key; multi-month
        # windows use a deterministic start-epoch suffix.
        if deleted > start:
            segments: list[tuple[datetime, datetime]] = []
            cursor = start
            while cursor < deleted:
                month = month_start_utc(cursor)
                next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
                segment_end = min(deleted, next_month)
                if segment_end <= cursor:
                    raise ValueError("final-charge month segmentation made no progress")
                segments.append((cursor, segment_end))
                cursor = segment_end
            remaining_quanta = _final_quanta(start, deleted, server.quantum_seconds)
            for segment_index, (segment_start, segment_end) in enumerate(segments):
                segment_quanta = _final_quanta(segment_start, segment_end, server.quantum_seconds)
                if segment_index < len(segments) - 1:
                    segment_quanta = min(segment_quanta, remaining_quanta)
                else:
                    segment_quanta = remaining_quanta
                remaining_quanta -= segment_quanta
                if segment_quanta <= 0:
                    continue
                segment_amount = _minor_product(segment_quanta, selling_minor)
                final_key = (
                    self.final_charge_key(server.id)
                    if len(segments) == 1
                    else f"{self.final_charge_key(server.id)}:{int(segment_start.timestamp())}"
                )
                description = (
                    f"final usage {segment_start.strftime('%Y-%m-%d %H:%M')} to "
                    f"{segment_end.strftime('%Y-%m-%d %H:%M')} UTC"
                )
                remainder = await self._ledger.get_entry_by_idempotency(wallet.id, final_key)
                month = month_start_utc(segment_start)
                newly_charged = False
                if remainder is not None:
                    if (
                        remainder.amount.amount != Decimal(segment_amount)
                        or remainder.amount.currency.upper() != selling_currency.upper()
                        or remainder.entry_type is not LedgerEntryType.CHARGE
                        or remainder.reference_type != "server"
                        or remainder.reference_id != str(server.id)
                        or remainder.description != description
                    ):
                        raise ValueError(
                            f"ledger idempotency key {final_key!r} has different final-charge facts"
                        )
                    replayed = True
                else:
                    month_billed = 0
                    if cap is not None:
                        month_billed = await self._accruals.month_total(
                            wallet.id,
                            month,
                            currency=selling_currency,
                            rule_key=_rule_key(snapshot.rule),
                        )
                        month_billed += charged_by_month.get(month, 0)
                    if cap is not None and month_billed + segment_amount > cap:
                        capped = True
                        await self._cap_audit(
                            server, wallet, cap, month_billed, segment_amount, "flat leg"
                        )
                        continue
                    atomic_adjust = getattr(self._wallets, "adjust", None)
                    if not callable(atomic_adjust):
                        raise RuntimeError("atomic wallet/ledger adjustment capability is required")
                    await atomic_adjust(
                        server.user_id,
                        -segment_amount,
                        final_key,
                        entry_type=LedgerEntryType.CHARGE,
                        reference_type="server",
                        reference_id=str(server.id),
                        description=description,
                    )
                    newly_charged = True
                if newly_charged:
                    charged += segment_amount
                    charged_by_month[month] = charged_by_month.get(month, 0) + segment_amount
                charged_quanta += segment_quanta
                remainder_periods.append((segment_start, segment_end, segment_quanta, final_key))
                remainder_charged = True
                posted_key = final_key

        # Repair the business records independently for the final legs.
        # A capture-key record must never be rewritten with a different period
        # or quanta when a later retry also settles the flat remainder.
        async def persist_period(
            period_start: datetime, period_end: datetime, quanta: int, key: str
        ) -> None:
            if quanta <= 0:
                return
            with localcontext() as context:
                context.prec = max(
                    50,
                    len(str(exact_base_cost)) + len(str(quanta)) + 20,
                )
                record_cost_amount = exact_base_cost * Decimal(quanta)
            await _persist_accrual_record(
                self._accruals,
                AccrualPeriod(
                    server_id=server.id,
                    wallet_id=wallet_id,
                    period_start=period_start,
                    period_end=period_end,
                    selling_minor=_minor_product(quanta, selling_minor),
                    cost_minor=_minor_product(quanta, cost_minor),
                    currency=selling_currency,
                    cost_currency=cost_currency,
                    selling_currency=selling_currency,
                    cost_amount=record_cost_amount,
                    rule_key=_rule_key(snapshot.rule),
                    idempotency_key=key,
                    quanta=quanta,
                ),
            )

        if first_leg_charged and window_start == created and server.idempotency_key:
            await persist_period(
                created,
                created + timedelta(seconds=server.quantum_seconds),
                1,
                first_leg_key or f"capture-server-create:{server.idempotency_key}",
            )
        if remainder_charged:
            for period_start, period_end, quanta, key in remainder_periods:
                await persist_period(period_start, period_end, quanta, key)

        if charged > 0 or replayed or posted_key is not None or capped or first_leg_charged:
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="billing.final_charge",
                resource_type="billing",
                resource_id=str(server.id),
                reason=(
                    f"final charge {charged}{selling_currency} "
                    f"(replayed={replayed}, hold captured={captured}, "
                    f"cap-capped={capped})"
                ),
                metadata={
                    "server_id": str(server.id),
                    "charged_minor": str(charged),
                    "captured_hold": str(captured).lower(),
                    "replayed": str(replayed).lower(),
                    "capped": str(capped).lower(),
                    "entry_key": posted_key or "",
                },
            )

            server.last_accrued_at = deleted
            await self._servers.save(server)

        return FinalChargeResult(
            charged_minor=charged,
            captured_hold=captured,
            posted_entry_key=posted_key,
            replayed=replayed,
            capped=capped,
        )

    async def _assert_capture_ledger(
        self,
        *,
        wallet_id: UUID,
        hold: Any,
        capture_key: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
    ) -> None:
        if hold is None or getattr(hold, "id", None) is None:
            raise ValueError("hold capture returned no persisted hold")
        entry = await self._ledger.get_entry_by_idempotency(wallet_id, capture_key)
        if entry is None:
            raise ValueError(f"hold capture {capture_key!r} has no CHARGE ledger fact")
        if (
            entry.amount.amount != Decimal(amount_minor)
            or entry.amount.currency.upper() != currency.upper()
            or entry.entry_type is not LedgerEntryType.CHARGE
            or entry.reference_type != "hold"
            or entry.reference_id != str(hold.id)
            or entry.description != f"hold captured for {idempotency_key}"
        ):
            raise ValueError(f"hold capture {capture_key!r} has different CHARGE facts")

    async def _cap_audit(
        self,
        server: CloudServer,
        wallet: Wallet,
        cap: int,
        month_billed: int,
        skipped: int,
        leg: str,
    ) -> None:
        """Audit one cap-capped final-charge leg (the money never moves)."""
        logger.warning(
            "server %s: final charge %s capped: month cap %d, billed %d, skipped %d",
            server.id,
            leg,
            cap,
            month_billed,
            skipped,
        )
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="billing.cap_reached",
            resource_type="server",
            resource_id=str(server.id),
            reason=(
                f"monthly cap {cap} reached at deletion ({leg}): "
                f"billed {month_billed}, skipped {skipped}"
            ),
            metadata={
                "server_id": str(server.id),
                "cap": str(cap),
                "billed": str(month_billed),
                "skipped_charge": str(skipped),
                "leg": leg,
                "currency": wallet.currency,
            },
        )


def _final_quanta(start: datetime, end: datetime, quantum_seconds: int) -> int:
    """Ceiling of the remainder window in quanta (a partial quantum bills full)."""
    elapsed = _elapsed_seconds(start, end)
    with localcontext() as context:
        context.prec = max(50, len(elapsed.as_tuple().digits) + len(str(quantum_seconds)) + 20)
        return int((elapsed / Decimal(quantum_seconds)).to_integral_value(rounding=ROUND_CEILING))


# ---------------------------------------------------------------------------
# Low-balance policy (M06-007)
# ---------------------------------------------------------------------------


class LowBalanceDecision(StrEnum):
    """The deterministic outcome for one server on one evaluation."""

    NONE = "none"  # healthy: at or above the threshold
    WARN = "warn"  # just fell below the threshold: warn, start the grace clock
    GRACE = "grace"  # below threshold, grace window not yet exhausted
    AUTO_DELETE = "auto_delete"  # grace exhausted: the deletion flow takes over
    RECOVERED = "recovered"  # was below, back at/above the threshold


@dataclass(frozen=True, slots=True)
class LowBalancePolicyConfig:
    """Threshold + grace window for the low-balance policy."""

    threshold_minor: int
    grace_hours: int
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.threshold_minor < 0:
            raise ValueError("threshold_minor must be >= 0")
        if self.grace_hours < 0:
            raise ValueError("grace_hours must be >= 0")
        code = str(self.currency or "").strip().upper()
        if code not in SUPPORTED_CURRENCIES:
            raise ValueError("currency must be an audited ISO code")
        object.__setattr__(self, "currency", code)


def decide_low_balance(
    balance_minor: int,
    config: LowBalancePolicyConfig,
    low_balance_since: datetime | None,
    now: datetime,
) -> LowBalanceDecision:
    """Pure, deterministic decision for one server (no I/O).

    The grace clock is the persisted ``low_balance_since`` watermark: it
    starts when the server first falls below the threshold and stops
    (cleared) when the balance recovers. The decision depends only on
    (balance, config, watermark, now) - the same inputs always give the same
    decision, which is what makes warn/grace/auto-delete auditable.
    """
    if balance_minor >= config.threshold_minor:
        return (
            LowBalanceDecision.RECOVERED
            if low_balance_since is not None
            else LowBalanceDecision.NONE
        )
    if low_balance_since is None:
        return LowBalanceDecision.WARN
    since = low_balance_since
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    grace = timedelta(hours=config.grace_hours)
    if (now - since) >= grace:
        return LowBalanceDecision.AUTO_DELETE
    return LowBalanceDecision.GRACE


class BalanceNotifier(Protocol):
    """Port for user notification of balance decisions.

    ``episode`` is the low-balance watermark for the decision: the new
    watermark on WARN, the existing one on AUTO_DELETE, and the cleared one
    on RECOVERED. Deduplicating implementations (M08-011's
    :class:`~cloud_platform.modules.notifications.domain.LowBalanceNotifier`)
    key notifications by (server, level, episode).
    """

    async def notify(
        self,
        user_id: UUID,
        server_id: UUID,
        decision: LowBalanceDecision,
        balance_minor: int,
        episode: datetime | None = None,
    ) -> None:
        """Inform the user about ``decision`` for their server."""
        ...


class _LoggingNotifier:
    """Default notifier: structured log line (the bot integration replaces it)."""

    async def notify(
        self,
        user_id: UUID,
        server_id: UUID,
        decision: LowBalanceDecision,
        balance_minor: int,
        episode: datetime | None = None,
    ) -> None:
        logger.warning(
            "low balance decision %s for server %s (user %s, balance %s, episode %s)",
            decision.value,
            server_id,
            user_id,
            balance_minor,
            episode.isoformat() if episode is not None else "-",
        )


@dataclass(slots=True)
class LowBalancePolicyReport:
    """Summary of one policy evaluation pass."""

    servers_checked: int = 0
    none: int = 0
    warn: int = 0
    grace: int = 0
    auto_delete: int = 0
    recovered: int = 0
    errors: int = 0

    def render(self) -> str:
        return (
            "low-balance run: "
            f"servers={self.servers_checked} none={self.none} warn={self.warn} "
            f"grace={self.grace} auto_delete={self.auto_delete} "
            f"recovered={self.recovered} errors={self.errors}"
        )


class LowBalancePolicyService:
    """Evaluates the low-balance policy for all RUNNING servers.

    Deterministic state machine per server:

    - balance >= threshold -> healthy; a stale ``low_balance_since`` watermark
      is cleared (RECOVERED, audited).
    - balance < threshold, no watermark -> WARN: the watermark is started at
      ``now`` and the user is notified.
    - balance < threshold, watermark within the grace window -> GRACE: the
      user is notified again; the server keeps running.
    - balance < threshold, watermark at/older than the grace window ->
      AUTO_DELETE: the server is transitioned to DELETE_REQUESTED (the
      deletion saga, M07-007, owns the stop + provider deletion + final
      charge) and the user is notified.

    The policy itself never stops or deletes resources and never moves
    money; it only records the watermark, notifies, and requests deletion
    through the lifecycle state.
    """

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        wallet_repo: WalletRepository,
        audit_repo: AuditRepository,
        notifier: BalanceNotifier | None = None,
    ) -> None:
        self._servers = server_repo
        self._wallets = wallet_repo
        self._audit = AuditTrail(audit_repo)
        self._notifier = notifier or _LoggingNotifier()

    async def evaluate(
        self, config: LowBalancePolicyConfig, now: datetime | None = None
    ) -> LowBalancePolicyReport:
        moment = _aware(now or datetime.now(UTC), "now")
        report = LowBalancePolicyReport()
        # Prepaid monthly servers are billed by the renewal checker, never by
        # the hourly low-balance/auto-delete policy (LEASEWEB-MVP).
        for server in await self._servers.list_running():
            if server.is_prepaid_monthly:
                continue
            report.servers_checked += 1
            try:
                decision = await self._evaluate_one(server, config, moment)
            except Exception:
                report.errors += 1
                logger.exception("low-balance evaluation failed for server %s", server.id)
                continue
            if decision is LowBalanceDecision.NONE:
                report.none += 1
            elif decision is LowBalanceDecision.WARN:
                report.warn += 1
            elif decision is LowBalanceDecision.GRACE:
                report.grace += 1
            elif decision is LowBalanceDecision.AUTO_DELETE:
                report.auto_delete += 1
            else:
                report.recovered += 1
        return report

    async def _evaluate_one(
        self,
        server: CloudServer,
        config: LowBalancePolicyConfig,
        now: datetime,
    ) -> LowBalanceDecision:
        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.currency != config.currency:
            # A threshold in one currency must never be applied to another
            # wallet's minor units; leave the server untouched until its
            # currency-specific policy is configured.
            return LowBalanceDecision.NONE
        balance = wallet.balance
        decision = decide_low_balance(balance, config, server.low_balance_since, now)

        if decision is LowBalanceDecision.NONE:
            return decision

        if decision is LowBalanceDecision.WARN:
            server.low_balance_since = now
            await self._notifier.notify(server.user_id, server.id, decision, balance, episode=now)
        elif decision is LowBalanceDecision.RECOVERED:
            # The episode's watermark (cleared now) identifies the episode
            # the recovery closes, so its notification deduplicates too.
            episode = server.low_balance_since
            server.low_balance_since = None
            if episode is not None:
                await self._notifier.notify(
                    server.user_id, server.id, decision, balance, episode=episode
                )
        elif decision is LowBalanceDecision.AUTO_DELETE:
            server.transition_to(ServerLifecycleState.DELETE_REQUESTED)
            await self._notifier.notify(
                server.user_id,
                server.id,
                decision,
                balance,
                episode=server.low_balance_since,
            )
        # GRACE: silent - the user was already warned when the window opened;
        # the server keeps running until the window exhausts.

        if decision in (
            LowBalanceDecision.WARN,
            LowBalanceDecision.RECOVERED,
            LowBalanceDecision.AUTO_DELETE,
        ):
            await self._servers.save(server)

        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action=f"billing.low_balance_{decision.value}",
            resource_type="server",
            resource_id=str(server.id),
            reason=(
                f"low balance decision {decision.value}: balance {balance}, "
                f"threshold {config.threshold_minor}, grace {config.grace_hours}h"
            ),
            metadata={
                "server_id": str(server.id),
                "decision": decision.value,
                "balance": str(balance),
                "threshold": str(config.threshold_minor),
                "grace_hours": str(config.grace_hours),
            },
        )
        return decision


# ---------------------------------------------------------------------------
# Provider-vs-customer margin report (M06-008)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarginAnomaly:
    """One suspicious settled period (visible, never hidden)."""

    server_id: UUID
    idempotency_key: str
    period_start: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class ServerMargin:
    """Per-server totals for the window."""

    server_id: UUID
    quanta: int
    revenue_minor: int
    cost_minor: int
    revenue_currency: str = "USD"
    cost_currency: str = "USD"

    @property
    def margin_minor(self) -> int | None:
        if self.revenue_currency != self.cost_currency:
            return None
        return self.revenue_minor - self.cost_minor


@dataclass(frozen=True, slots=True)
class MarginReport:
    """Aggregated provider-vs-customer margin for a time window.

    Totals are per currency (the platform is multi-currency capable); a
    window containing more than one currency is itself flagged as an
    anomaly so operators notice the mix.
    """

    start: datetime
    end: datetime
    periods: int
    quanta: int
    revenues: dict[str, int]
    costs: dict[str, int]
    per_server: dict[UUID, ServerMargin]
    anomalies: tuple[MarginAnomaly, ...]

    @property
    def is_clean(self) -> bool:
        return not self.anomalies

    def margin(self, currency: str) -> int:
        return self.revenues.get(currency, 0) - self.costs.get(currency, 0)

    def render(self) -> str:
        lines = [
            f"margin report {self.start.strftime('%Y-%m-%d %H:%M')} to "
            f"{self.end.strftime('%Y-%m-%d %H:%M')} UTC: "
            f"periods={self.periods} quanta={self.quanta}"
        ]
        for currency in sorted(set(self.revenues) | set(self.costs)):
            lines.append(
                f"  {currency}: revenue={self.revenues.get(currency, 0)} "
                f"cost={self.costs.get(currency, 0)} margin={self.margin(currency)}"
            )
        for server_id in sorted(self.per_server):
            m = self.per_server[server_id]
            lines.append(
                f"  server {server_id}: quanta={m.quanta} revenue={m.revenue_minor} "
                f"cost={m.cost_minor} {m.cost_currency} "
                f"margin={m.margin_minor if m.margin_minor is not None else 'unavailable'}"
            )
        if self.anomalies:
            lines.append(f"anomalies ({len(self.anomalies)}):")
            for a in self.anomalies:
                lines.append(
                    f"  {a.period_start.strftime('%Y-%m-%d %H:%M')} "
                    f"server {a.server_id}: {a.reason}"
                )
        else:
            lines.append("anomalies: none")
        return "\n".join(lines)


def _as_decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(0)


class MarginReportService:
    """Read-only provider-vs-customer margin reporting over settled usage.

    Source of truth: the accrual_periods business records written by the
    accrual job and the final deletion charge (each carries the provider
    cost and the customer charge of the settled quanta, from the pinned
    price snapshot). The report never mutates state and never converts one
    money side into the other.

    Anomalies make pricing problems visible instead of averaging them out:
    - NEGATIVE_MARGIN: a period in one currency has provider cost >= the
      customer charge (a misconfigured margin rule bleeds money every
      period - it must be seen, not smoothed).
    - COST_UNKNOWN: a period was settled without a price snapshot
      (cost recorded as 0); its margin is unknown.
    - CURRENCY_MISMATCH / MIXED_CURRENCY: the cost and selling sides (or the
      window as a whole) use different currencies and cannot be compared as
      integer minor-unit amounts.
    """

    def __init__(self, accrual_repo: AccrualPeriodRepository) -> None:
        self._accruals = accrual_repo

    async def report(self, start: datetime, end: datetime) -> MarginReport:
        if end <= start:
            raise ValueError("end must be after start")
        periods = await self._accruals.list_between(start, end)

        revenues: dict[str, int] = {}
        exact_costs: dict[str, Decimal] = {}
        per_server: dict[UUID, dict[str, object]] = {}
        anomalies: list[MarginAnomaly] = []
        quanta_total = 0

        for p in periods:
            quanta_total += p.quanta
            cost_currency = getattr(p, "cost_currency", None) or p.currency
            selling_currency = getattr(p, "selling_currency", None) or p.currency
            revenues[selling_currency] = revenues.get(selling_currency, 0) + p.selling_minor
            cost_amount = p.cost_amount
            if cost_amount is None:
                anomalies.append(
                    MarginAnomaly(
                        server_id=p.server_id,
                        idempotency_key=p.idempotency_key,
                        period_start=p.period_start,
                        reason="COST_UNKNOWN: exact native cost is missing",
                    )
                )
            else:
                with localcontext() as context:
                    context.prec = max(50, len(cost_amount.as_tuple().digits) + 30)
                    exact_costs[cost_currency] = (
                        exact_costs.get(cost_currency, Decimal(0)) + cost_amount
                    )

            state = per_server.setdefault(
                p.server_id,
                {
                    "quanta": 0,
                    "revenue": 0,
                    "cost": Decimal(0),
                    "revenue_currency": selling_currency,
                    "cost_currency": cost_currency,
                    "mixed": False,
                },
            )
            state["quanta"] = cast(int, state["quanta"]) + p.quanta
            state["revenue"] = cast(int, state["revenue"]) + p.selling_minor
            if cost_amount is not None:
                with localcontext() as context:
                    context.prec = max(50, len(cost_amount.as_tuple().digits) + 30)
                    state["cost"] = _as_decimal(state["cost"]) + cost_amount
            if state["revenue_currency"] != selling_currency:
                state["mixed"] = True
            if state["cost_currency"] != cost_currency:
                state["mixed"] = True
            state["revenue_currency"] = selling_currency
            state["cost_currency"] = cost_currency

            if cost_currency != selling_currency:
                anomalies.append(
                    MarginAnomaly(
                        server_id=p.server_id,
                        idempotency_key=p.idempotency_key,
                        period_start=p.period_start,
                        reason=(
                            f"CURRENCY_MISMATCH: cost {cost_currency} != selling {selling_currency}"
                        ),
                    )
                )
            elif cost_amount is None:
                pass
            else:
                with localcontext() as context:
                    context.prec = max(50, len(cost_amount.as_tuple().digits) + 30)
                    exact_cost_minor = cost_amount * (
                        Decimal(10) ** currency_exponent(cost_currency)
                    )
                if exact_cost_minor >= p.selling_minor:
                    anomalies.append(
                        MarginAnomaly(
                            server_id=p.server_id,
                            idempotency_key=p.idempotency_key,
                            period_start=p.period_start,
                            reason=(
                                f"NEGATIVE_MARGIN: exact cost {cost_amount} "
                                f"{cost_currency} >= revenue {p.selling_minor} {selling_currency}"
                            ),
                        )
                    )

        costs = {
            currency: major_to_minor(amount, currency, FxPurpose.CHARGE)
            for currency, amount in exact_costs.items()
        }
        if len(revenues) > 1 or len(costs) > 1 or set(revenues) != set(costs):
            currencies = sorted(set(revenues) | set(costs))
            anomalies.append(
                MarginAnomaly(
                    server_id=UUID(int=0),
                    idempotency_key="window",
                    period_start=start,
                    reason="MIXED_CURRENCY: window mixes " + ", ".join(currencies),
                )
            )

        per_server_margin: dict[UUID, ServerMargin] = {}
        for server_id, raw_state in per_server.items():
            state = raw_state
            revenue_currency = str(state["revenue_currency"])
            cost_currency = str(state["cost_currency"])
            mixed = bool(state["mixed"])
            exact_cost = _as_decimal(state["cost"])
            cost_display = (
                0
                if mixed or exact_cost == 0
                else major_to_minor(exact_cost, cost_currency, FxPurpose.CHARGE)
            )
            per_server_margin[server_id] = ServerMargin(
                server_id=server_id,
                quanta=cast(int, state["quanta"]),
                revenue_minor=cast(int, state["revenue"]),
                cost_minor=cost_display,
                revenue_currency=revenue_currency,
                cost_currency="MIXED" if mixed else cost_currency,
            )
        return MarginReport(
            start=start,
            end=end,
            periods=len(periods),
            quanta=quanta_total,
            revenues=revenues,
            costs=costs,
            per_server=per_server_margin,
            anomalies=tuple(sorted(anomalies, key=lambda a: (a.period_start, a.reason))),
        )

    async def report_for_day(self, day: datetime) -> MarginReport:
        """The margin report for one UTC day [day, day + 1)."""
        base = _aware(day, "day")
        return await self.report(base, base + timedelta(days=1))
