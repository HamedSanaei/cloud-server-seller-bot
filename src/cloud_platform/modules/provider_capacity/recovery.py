"""Automated credential-account capacity RECOVERY (LEASEWEB-MULTIACCOUNT).

PC-2031 used to be a one-way door: once a credential account refused a create,
the storefront stopped publishing through it and the ONLY positive path back
was the operator remembering ``leaseweb cloud accounts clear``. A quota that
was raised, an instance the customer deleted on the provider site, or a
provider-side limit that simply stopped applying therefore left the Cloud
storefront silently unavailable for as long as nobody noticed.

This module is the read-only recovery controller that closes that loop. It
never creates anything: it observes, schedules, and lets ONE real customer
order be the proof.

What it may observe (recovery signals)
--------------------------------------

* **Instance inventory** (``list_instances`` per proven region): the census is
  the baseline captured when the refusal was learned. A LOWER count later means
  an instance was freed, which brings the next attempt window forward
  immediately.
* **Local delete events**: an app-owned instance removed from a blocked account
  reaches the same conclusion through the durable inventory change hook (the
  delete saga calls :meth:`AccountCapacityRepository.record_inventory_census`
  / the bring-forward path), so the window can open without waiting for a
  provider census.
* **The configured schedule**: the exponential backoff (15 m / 30 m / 1 h /
  2 h, capped at 6 h) eventually opens a window even when no signal fired —
  the provider limit may have been raised without anything local changing.
* **Operator configuration**: adding/re-enabling an account is itself a
  positive signal and needs no recovery at all.

What it may NEVER observe
-------------------------

``list_regions`` / ``list_instanceTypes`` / ``list_images`` prove authentication
and catalog access, NOT create quota. They are deliberately absent here. And no
billable synthetic server is ever created to probe capacity: the canary is a
real customer order, serialized by the durable PostgreSQL lease.

State flow (all transitions are durable rows, never in-memory guesses)::

    HEALTHY -> LIMIT_REACHED -> UNKNOWN_AFTER_LIMIT -> RECOVERY_CANDIDATE
    RECOVERY_CANDIDATE -> HEALTHY            (canary accepted: PROVEN)
    RECOVERY_CANDIDATE -> LIMIT_REACHED      (canary refused: backoff)

The controller is idempotent and safe to run from a cron tick, an operator CLI
or two workers at once: every write is guarded, the schedule is "earliest
wins", and the notification dedupe lives in the durable outbox event key.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from cloud_platform.modules.businesslog.domain import BusinessEventSink, emit_safe
from cloud_platform.modules.businesslog.events import (
    capacity_outage_event,
    capacity_recovery_reminder_event,
    capacity_storefront_outage_event,
)
from cloud_platform.modules.provider_capacity.domain import (
    DEFAULT_LIMIT_TTL_SECONDS,
    DEFAULT_RECOVERY_BACKOFF_SECONDS,
    AccountCapacity,
    AccountCapacityRepository,
    recovery_backoff_seconds,
    validate_recovery_backoff,
)
from cloud_platform.modules.provider_capacity.status import (
    StorefrontCapacityStatus,
    capacity_status,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AccountInventory",
    "CapacityRecoveryOutcome",
    "CapacityRecoveryReport",
    "CloudCapacityRecoveryService",
    "CloudInstanceInventorySource",
    "inventory_ids_hash",
]


def inventory_ids_hash(instance_ids: Iterable[object]) -> str:
    """A stable hash of a set of instance ids (order- and duplicate-proof)."""
    normalized = sorted({str(item).strip() for item in instance_ids if str(item).strip()})
    digest = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
    return digest[:64]


@dataclass(frozen=True, slots=True)
class AccountInventory:
    """One account's read-only instance census."""

    instance_count: int
    ids_hash: str
    #: Regions the account currently holds instances in (0 when it holds none).
    regions_read: int = 0
    #: Regions a census could not read. Always empty for an account-scoped
    #: census (one unfiltered read); kept so a source that does read per
    #: region can still report a partial read as unknown-but-attributable.
    unreadable_regions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.instance_count, bool) or not isinstance(self.instance_count, int):
            raise ValueError("instance_count must be an integer")
        if self.instance_count < 0:
            raise ValueError("instance_count must be >= 0")
        if not str(self.ids_hash or "").strip():
            raise ValueError("ids_hash must not be empty")


class CloudInstanceInventorySource(Protocol):
    """Read-only instance census per credential account (no create, no probe)."""

    async def inventories(self) -> Mapping[str, AccountInventory | None]:
        """Configured account id -> census, or ``None`` when unreadable."""
        ...


@dataclass(frozen=True, slots=True)
class CapacityRecoveryOutcome:
    """What the controller did for ONE account in one pass."""

    credential_account_id: str
    state: str
    inventory_count: int | None = None
    baseline_count: int | None = None
    baseline_captured: bool = False
    brought_forward: bool = False
    window_opened: bool = False
    scheduled_in_seconds: int | None = None
    recovery_attempts: int = 0
    outage_notified: bool = False
    reminder_sent: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CapacityRecoveryReport:
    """One controller pass (safe facts only, no credential material)."""

    provider_key: str
    outcomes: tuple[CapacityRecoveryOutcome, ...] = ()
    status: StorefrontCapacityStatus | None = None
    errors: tuple[str, ...] = field(default=())
    skipped: str | None = None

    @property
    def opened_windows(self) -> tuple[str, ...]:
        return tuple(
            outcome.credential_account_id for outcome in self.outcomes if outcome.window_opened
        )

    def summary(self) -> str:
        if self.skipped is not None:
            return f"provider={self.provider_key} skipped={self.skipped}"
        opened = ",".join(self.opened_windows) or "-"
        return (
            f"provider={self.provider_key} accounts={len(self.outcomes)} "
            f"windows_opened={opened} errors={len(self.errors)}"
        )


class CloudCapacityRecoveryService:
    """Schedules and observes the recovery of blocked credential accounts."""

    def __init__(
        self,
        *,
        capacity_repo: AccountCapacityRepository,
        inventory_source: CloudInstanceInventorySource,
        event_sink: BusinessEventSink | None = None,
        sellable_offers_source: Callable[[], Awaitable[int] | int] | None = None,
        backoff_seconds: tuple[int, ...] = DEFAULT_RECOVERY_BACKOFF_SECONDS,
        reminder_delay_seconds: int = 1800,
        reminder_interval_seconds: int = 21600,
        ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS,
        enabled: bool = True,
    ) -> None:
        self._capacity = capacity_repo
        self._inventory = inventory_source
        self._events = event_sink
        self._sellable_offers = sellable_offers_source
        self._backoff = validate_recovery_backoff(backoff_seconds)
        self._reminder_delay_seconds = _positive(reminder_delay_seconds, "reminder_delay_seconds")
        self._reminder_interval_seconds = _positive(
            reminder_interval_seconds, "reminder_interval_seconds"
        )
        self._ttl_seconds = _positive(ttl_seconds, "ttl_seconds")
        self._enabled = bool(enabled)

    async def run(
        self,
        provider_key: str = "leaseweb",
        *,
        now: datetime | None = None,
    ) -> CapacityRecoveryReport:
        """One idempotent recovery pass (never creates anything)."""
        reference = _aware(now)
        if not self._enabled:
            return CapacityRecoveryReport(provider_key=provider_key, skipped="disabled")
        errors: list[str] = []
        try:
            inventories = dict(await self._inventory.inventories())
        except Exception as exc:
            logger.warning(
                "capacity recovery: instance census unavailable (%s); "
                "scheduling and reminders continue from durable state",
                type(exc).__name__,
                exc_info=True,
            )
            errors.append(f"inventory: {type(exc).__name__}")
            inventories = {}
        try:
            records = {
                record.credential_account_id: record
                for record in await self._capacity.list_for_provider(provider_key)
            }
        except Exception as exc:
            logger.warning("capacity recovery: capacity store unreadable (%s)", type(exc).__name__)
            return CapacityRecoveryReport(
                provider_key=provider_key,
                errors=(f"capacity store: {type(exc).__name__}",),
            )
        outcomes: list[CapacityRecoveryOutcome] = []
        for account_id in sorted(set(inventories) | set(records)):
            record = records.get(account_id)
            if record is None or not record.is_limit_reached(now=reference):
                # Eligible (healthy or an open canary window): nothing to do.
                continue
            try:
                outcome = await self._evaluate_blocked(
                    provider_key,
                    record,
                    inventories.get(account_id),
                    now=reference,
                    errors=errors,
                )
            except Exception as exc:
                logger.warning(
                    "capacity recovery: account %s could not be evaluated (%s)",
                    account_id,
                    type(exc).__name__,
                    exc_info=True,
                )
                errors.append(f"{account_id}: {type(exc).__name__}")
                continue
            outcomes.append(outcome)
        sellable = await self._sellable_count()
        try:
            status = capacity_status(
                provider_key,
                await self._capacity.list_for_provider(provider_key),
                sellable_offers=sellable,
                now=reference,
            )
        except Exception as exc:
            errors.append(f"status: {type(exc).__name__}")
            status = None
        if status is not None and status.storefront_unavailable and status.blocked_accounts:
            await emit_safe(
                self._events,
                capacity_storefront_outage_event(
                    provider_key=provider_key,
                    blocked_accounts=status.blocked_accounts,
                    total_accounts=max(len(records), len(status.blocked_accounts)),
                    sellable_offers=status.sellable_offers,
                    outage_since=status.outage_since,
                ),
            )
        report = CapacityRecoveryReport(
            provider_key=provider_key,
            outcomes=tuple(outcomes),
            status=status,
            errors=tuple(errors),
        )
        logger.info("capacity recovery pass: %s", report.summary())
        return report

    async def _evaluate_blocked(
        self,
        provider_key: str,
        record: AccountCapacity,
        inventory: AccountInventory | None,
        *,
        now: datetime,
        errors: list[str],
    ) -> CapacityRecoveryOutcome:
        """Inventory baseline (and bring-forward), scheduling, one-time cards."""
        account_id = record.credential_account_id
        notes: list[str] = []
        baseline_captured = False
        brought_forward = False
        if inventory is not None:
            baseline_missing = record.baseline_instance_count is None
            updated = await self._capacity.record_inventory_census(
                provider_key,
                account_id,
                instance_count=inventory.instance_count,
                ids_hash=inventory.ids_hash,
                now=now,
            )
            if updated is not None:
                record = updated
            baseline_captured = baseline_missing and record.baseline_instance_count is not None
            if (
                not baseline_missing
                and record.baseline_instance_count is not None
                and inventory.instance_count < record.baseline_instance_count
            ):
                brought_forward = True
                notes.append("inventory-decrease")
            if inventory.unreadable_regions:
                notes.append(f"unreadable-regions={len(inventory.unreadable_regions)}")
        else:
            notes.append("inventory-unavailable")

        outage_notified = False
        if record.outage_notified_at is None:
            outage_notified = await emit_safe(
                self._events,
                capacity_outage_event(
                    provider_key=provider_key,
                    credential_account=account_id,
                    error_code=record.error_code,
                    correlation_id=record.correlation_id,
                    location_id=record.location_id,
                    product_id=record.product_id,
                    observations=record.observations,
                    attempts=record.recovery_attempts,
                    next_attempt_at=record.next_recovery_attempt_at,
                    blocked_reason=record.blocked_reason(now=now),
                    at=record.observed_at or now,
                ),
            )
            if outage_notified:
                await self._capacity.mark_outage_notified(provider_key, account_id, now=now)

        reminder_sent = False
        reminder_due_at = record.reminder_due(
            now=now,
            delay_seconds=self._reminder_delay_seconds,
            interval_seconds=self._reminder_interval_seconds,
        )
        if reminder_due_at is not None:
            # The card is stamped with the SEND instant and the row records it,
            # so the next reminder is one interval from now: a controller that
            # was down for a day sends ONE catch-up card, not a burst of them.
            reminder_sent = await emit_safe(
                self._events,
                capacity_recovery_reminder_event(
                    provider_key=provider_key,
                    credential_account=account_id,
                    attempts=record.recovery_attempts,
                    next_attempt_at=record.next_recovery_attempt_at,
                    blocked_reason=record.blocked_reason(now=now),
                    at=now,
                ),
            )
            if reminder_sent:
                await self._capacity.mark_reminder_sent(provider_key, account_id, sent_at=now)

        scheduled_in_seconds: int | None = None
        if record.next_recovery_attempt_at is None:
            delay = recovery_backoff_seconds(record.recovery_attempts, self._backoff)
            scheduled = await self._capacity.schedule_recovery(
                provider_key, account_id, delay_seconds=delay, now=now
            )
            if scheduled is not None:
                record = scheduled
                scheduled_in_seconds = delay
                notes.append(f"scheduled={delay}s")

        window_opened = False
        if record.recovery_window_due(now=now):
            opened = await self._capacity.open_recovery_window(provider_key, account_id, now=now)
            if opened is not None:
                record = opened
                window_opened = True
                logger.warning(
                    "capacity recovery window opened: provider=%s account=%s attempts=%d "
                    "baseline=%s notes=%s",
                    provider_key,
                    account_id,
                    record.recovery_attempts,
                    record.baseline_instance_count,
                    ",".join(notes) or "-",
                )

        return CapacityRecoveryOutcome(
            credential_account_id=account_id,
            state=record.state.value,
            inventory_count=inventory.instance_count if inventory is not None else None,
            baseline_count=record.baseline_instance_count,
            baseline_captured=baseline_captured,
            brought_forward=brought_forward,
            window_opened=window_opened,
            scheduled_in_seconds=scheduled_in_seconds,
            recovery_attempts=record.recovery_attempts,
            outage_notified=outage_notified,
            reminder_sent=reminder_sent,
            notes=tuple(notes),
        )

    # -- read-only inputs used for metrics ---------------------------------

    async def _sellable_count(self) -> int:
        source = self._sellable_offers
        if source is None:
            return 0
        try:
            value = source()
            if hasattr(value, "__await__"):
                value = await value
            return max(int(value or 0), 0)
        except Exception as exc:
            logger.warning(
                "capacity recovery: sellable offer count unavailable (%s)",
                type(exc).__name__,
            )
            return 0


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _aware(value: datetime | None) -> datetime:
    resolved = value or datetime.now(UTC)
    return resolved if resolved.tzinfo is not None else resolved.replace(tzinfo=UTC)
