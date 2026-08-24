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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
    ServerRepository,
)
from cloud_platform.modules.pricing.domain import ServerPriceSnapshotRepository
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
    currency: str = "EUR"
    id: UUID | None = None

    def __post_init__(self) -> None:
        if self.selling_minor <= 0:
            raise ValueError("selling_minor must be positive")
        if self.quanta <= 0:
            raise ValueError("quanta must be positive")
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")


class AccrualPeriodRepository(Protocol):
    """Port for the accrual-period business records."""

    async def get_by_key(self, idempotency_key: str) -> AccrualPeriod | None: ...

    async def add(self, period: AccrualPeriod) -> AccrualPeriod:
        """Insert the record. Raises AccrualPeriodExistsError on key collision."""
        ...

    async def list_between(self, start: datetime, end: datetime) -> list[AccrualPeriod]:
        """All periods with period_start in [start, end) (margin reporting)."""
        ...

    async def month_total(self, wallet_id: UUID, month_start: datetime) -> int:
        """Sum of selling_minor billed to the wallet at/after month_start (caps)."""
        ...

    async def daily_cost_total(
        self, day_start: datetime, day_end: datetime, server_ids: frozenset[UUID]
    ) -> int:
        """Sum of cost_minor (provider spend) with period_start in [start, end)
        restricted to ``server_ids`` (cost circuit breakers, M10-004)."""
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
    charged_minor: int = 0
    currency: str = ""

    def render(self) -> str:
        return (
            "accrual run: "
            f"servers={self.servers_checked} posted={self.periods_posted} "
            f"replayed={self.periods_replayed} insufficient={self.insufficient_balance} "
            f"capped={self.capped_periods} errors={self.errors} "
            f"charged={self.charged_minor}{self.currency or ''}"
        )


def _aware(dt: datetime | None, name: str) -> datetime:
    """Normalize to an aware UTC datetime (DB timestamps are UTC)."""
    if dt is None:
        raise ValueError(f"{name} is required")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


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
        servers = await self._servers.list_running()
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
                "charged_minor": str(report.charged_minor),
                "currency": report.currency,
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

        elapsed = (now - last).total_seconds()
        if elapsed < quantum_seconds:
            return 0  # no complete period has elapsed yet

        completed = int(elapsed // quantum_seconds)
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
        selling_minor = int(snapshot.selling_minor)
        cost_minor = int(snapshot.offer.cost_minor)

        # Optional monthly cap (per user, per UTC calendar month of the period's
        # usage, from the price policy). The cap can never be exceeded: when
        # the month's billed total plus this period would pass it, the period
        # is settled as CAPPED - the watermark advances past it without any
        # money movement, so each period is evaluated exactly once and the
        # excess usage of a capped month is never billed (the user is
        # protected for the month they used it; the platform absorbs the
        # difference). A new month starts a fresh cap.
        cap = snapshot.rule.monthly_cap_minor
        if cap is not None:
            month_start = month_start_utc(period_end)
            billed = await self._accruals.month_total(wallet.id, month_start)
            if billed + selling_minor > cap:
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
                await self._advance(server, period_end)
                return 0, False

        # First period: the creation hold was reserved for exactly this charge.
        hold = None
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

        # Idempotent replay: the ledger is the source of truth for "settled".
        for key in candidate_keys:
            if await self._ledger.get_entry_by_idempotency(wallet.id, key) is not None:
                report.periods_replayed += 1
                await self._advance(server, period_end)
                return 0, False

        if hold is not None and hold.status is HoldStatus.CREATED:
            # Atomically consumes the reserved funds (no balance race).
            assert hold.id is not None  # persisted holds carry a DB-assigned id
            hold_id: UUID = hold.id
            await self._holds.capture_hold(
                wallet.id, hold_id, f"server-create:{server.idempotency_key}"
            )
        else:
            charge_key = accrual_charge_key(server.id, period_start)
            try:
                await self._wallets.debit(server.user_id, selling_minor, charge_key)
            except InsufficientBalanceError:
                # Leave the period unsettled: the next run retries it and the
                # low-balance policy (M06-007) gets a deterministic signal.
                raise
            await self._ledger.post_entry(
                wallet.id,
                selling_minor,
                wallet.currency,
                LedgerEntryType.CHARGE,
                charge_key,
                reference_type="server",
                reference_id=str(server.id),
                description=(
                    f"usage {period_start.strftime('%Y-%m-%d %H:%M')} to "
                    f"{period_end.strftime('%H:%M')} UTC"
                ),
            )

        # Business record for the margin report. The ledger entry above is
        # authoritative for the money; a duplicate record (crash + retry)
        # is harmless because the key is unique.
        if hold is not None and hold.status is HoldStatus.CAPTURED:
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
            currency=wallet.currency,
            idempotency_key=record_key,
        )
        try:
            await self._accruals.add(period)
        except AccrualPeriodExistsError:
            pass

        report.charged_minor += selling_minor
        report.currency = wallet.currency
        await self._advance(server, period_end)
        return 1, False

    async def _advance(self, server: CloudServer, period_end: datetime) -> None:
        """Move the server's accrual watermark to the period end."""
        server.last_accrued_at = period_end
        await self._servers.save(server)


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
    ) -> None:
        self._servers = server_repo
        self._wallets = wallet_repo
        self._hold_repo = hold_repo
        self._holds = hold_service
        self._ledger = ledger_repo
        self._accruals = accrual_repo
        self._snapshots = snapshot_repo
        self._audit = AuditTrail(audit_repo)

    @staticmethod
    def final_charge_key(server_id: UUID) -> str:
        return f"final:{server_id}"

    async def charge_final(self, server: CloudServer, deleted_at: datetime) -> FinalChargeResult:
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

        snapshot = await self._snapshots.get(server.id)
        if snapshot is None:
            raise MissingSnapshotError(f"server {server.id} has no price snapshot")
        selling_minor = int(snapshot.selling_minor)
        cost_minor = int(snapshot.offer.cost_minor)

        window_start = _aware(server.last_accrued_at or created, "window start")
        if window_start < created:
            window_start = created

        charged = 0
        captured = False
        capped = False
        posted_key: str | None = None
        replayed = False
        start = window_start

        # Optional monthly cap (per user, per UTC calendar month, from the
        # price policy): what this call may still charge against the month
        # of the deletion instant. A leg that would pass the cap is skipped
        # (the reserved hold is released back to the wallet so the reserved
        # funds are not stranded), never overcharged.
        cap = snapshot.rule.monthly_cap_minor
        month_billed = 0
        if cap is not None:
            month_billed = await self._accruals.month_total(wallet.id, month_start_utc(deleted))

        # Leg 1: the still-reserved first quantum, if the window covers it.
        if start == created and server.idempotency_key:
            hold_key = f"server-create:{server.idempotency_key}"
            hold = await self._hold_repo.get_by_idempotency(wallet.id, hold_key)
            if hold is not None and hold.status is HoldStatus.CREATED:
                assert hold.id is not None
                if cap is not None and month_billed + charged + selling_minor > cap:
                    # The cap protects the user: return the reserved funds
                    # instead of overcharging (the platform absorbs the loss).
                    await self._holds.release_hold(wallet.id, hold.id, hold_key)
                    capped = True
                    await self._cap_audit(
                        server, wallet, cap, month_billed, selling_minor, "capture leg"
                    )
                else:
                    await self._holds.capture_hold(wallet.id, hold.id, hold_key)
                    captured = True
                    charged += selling_minor
                    posted_key = f"capture-{hold_key}"
                    start = start + timedelta(seconds=server.quantum_seconds)
            elif hold is not None and hold.status is HoldStatus.CAPTURED:
                posted_key = f"capture-{hold_key}"  # settled earlier: replayed leg
                # The captured quantum is already covered - advance the window
                # exactly as the capture path does, or the remainder would be
                # billed a second time on replay.
                start = start + timedelta(seconds=server.quantum_seconds)

        # Leg 2: the flat remainder, if any (partial) quantum is left.
        if deleted > start:
            final_key = self.final_charge_key(server.id)
            remainder = await self._ledger.get_entry_by_idempotency(wallet.id, final_key)
            if remainder is not None:
                replayed = True
            else:
                quanta = _final_quanta(start, deleted, server.quantum_seconds)
                amount = quanta * selling_minor
                if cap is not None and month_billed + charged + amount > cap:
                    capped = True
                    await self._cap_audit(server, wallet, cap, month_billed, amount, "flat leg")
                else:
                    await self._wallets.debit(server.user_id, amount, final_key)
                    await self._ledger.post_entry(
                        wallet.id,
                        amount,
                        wallet.currency,
                        LedgerEntryType.CHARGE,
                        final_key,
                        reference_type="server",
                        reference_id=str(server.id),
                        description=(
                            f"final usage {start.strftime('%Y-%m-%d %H:%M')} to "
                            f"{deleted.strftime('%H:%M')} UTC"
                        ),
                    )
                    charged += amount
                    posted_key = final_key

        if charged > 0:
            # Business record for the margin report: only the amount that was
            # actually billed (a cap-capped leg bills nothing and must not
            # appear in revenue). The window recorded is the FULL final
            # window; a partial replay (crash between legs) therefore
            # records the complete amount, not just the leg moved by this
            # call.
            total_quanta = _final_quanta(window_start, deleted, server.quantum_seconds)
            record = AccrualPeriod(
                server_id=server.id,
                wallet_id=wallet.id,
                period_start=window_start,
                period_end=deleted,
                selling_minor=total_quanta * selling_minor,
                cost_minor=total_quanta * cost_minor,
                currency=wallet.currency,
                idempotency_key=posted_key or self.final_charge_key(server.id),
            )
            try:
                await self._accruals.add(record)
            except AccrualPeriodExistsError:
                pass  # replay: the record already exists

        if charged > 0 or replayed or capped:
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="billing.final_charge",
                resource_type="billing",
                resource_id=str(server.id),
                reason=(
                    f"final charge {charged}{wallet.currency} "
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
    elapsed = Decimal(str((end - start).total_seconds()))
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

    def __post_init__(self) -> None:
        if self.threshold_minor < 0:
            raise ValueError("threshold_minor must be >= 0")
        if self.grace_hours < 0:
            raise ValueError("grace_hours must be >= 0")


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
        for server in await self._servers.list_running():
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
        balance = wallet.balance if wallet is not None else 0
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

    @property
    def margin_minor(self) -> int:
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
                f"cost={m.cost_minor} margin={m.margin_minor}"
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


class MarginReportService:
    """Read-only provider-vs-customer margin reporting over settled usage.

    Source of truth: the accrual_periods business records written by the
    accrual job and the final deletion charge (each carries the provider
    cost and the customer charge of the settled quanta, from the pinned
    price snapshot). The report never mutates state.

    Anomalies make pricing problems visible instead of averaging them out:
    - NEGATIVE_MARGIN: a period's provider cost met or exceeded the
      customer charge (a misconfigured margin rule bleeds money every
      period - it must be seen, not smoothed).
    - COST_UNKNOWN: a period was settled without a price snapshot
      (cost recorded as 0); its margin is unknown.
    - MIXED_CURRENCY: the window mixes currencies and cannot be summed.
    """

    def __init__(self, accrual_repo: AccrualPeriodRepository) -> None:
        self._accruals = accrual_repo

    async def report(self, start: datetime, end: datetime) -> MarginReport:
        if end <= start:
            raise ValueError("end must be after start")
        periods = await self._accruals.list_between(start, end)

        revenues: dict[str, int] = {}
        costs: dict[str, int] = {}
        per_server: dict[UUID, dict[str, int]] = {}
        anomalies: list[MarginAnomaly] = []
        quanta_total = 0

        for p in periods:
            quanta_total += p.quanta
            revenues[p.currency] = revenues.get(p.currency, 0) + p.selling_minor
            costs[p.currency] = costs.get(p.currency, 0) + p.cost_minor
            agg = per_server.setdefault(p.server_id, {"quanta": 0, "revenue": 0, "cost": 0})
            agg["quanta"] += p.quanta
            agg["revenue"] += p.selling_minor
            agg["cost"] += p.cost_minor

            if p.cost_minor >= p.selling_minor:
                anomalies.append(
                    MarginAnomaly(
                        server_id=p.server_id,
                        idempotency_key=p.idempotency_key,
                        period_start=p.period_start,
                        reason=(
                            f"NEGATIVE_MARGIN: cost {p.cost_minor} >= revenue {p.selling_minor}"
                        ),
                    )
                )
            elif p.cost_minor == 0:
                anomalies.append(
                    MarginAnomaly(
                        server_id=p.server_id,
                        idempotency_key=p.idempotency_key,
                        period_start=p.period_start,
                        reason="COST_UNKNOWN: settled without a price snapshot",
                    )
                )

        if len(revenues) > 1:
            anomalies.append(
                MarginAnomaly(
                    server_id=UUID(int=0),
                    idempotency_key="window",
                    period_start=start,
                    reason="MIXED_CURRENCY: window mixes " + ", ".join(sorted(revenues)),
                )
            )

        per_server_margin = {
            server_id: ServerMargin(
                server_id=server_id,
                quanta=v["quanta"],
                revenue_minor=v["revenue"],
                cost_minor=v["cost"],
            )
            for server_id, v in per_server.items()
        }
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
