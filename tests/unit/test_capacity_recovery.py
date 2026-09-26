"""Automatic capacity recovery: canary windows, backoff and operator cards.

Production context: PC-2031 (``Customer limit reached``) used to be a one-way
door. The storefront stopped publishing through the affected credential
account, and the ONLY positive path back was the operator remembering
``leaseweb cloud accounts clear``. A freed quota, a deleted instance or a
raised provider limit therefore left the Cloud storefront silently unavailable
forever.

These tests pin the automated replacement:

* a read-only controller keeps the instance baseline, brings the next attempt
  window forward on inventory evidence, and never probes with a billable
  synthetic create;
* ``recovery_candidate`` means exactly ONE real customer order may act as the
  canary (serialized by a durable PostgreSQL lease in the adapter — the SQL
  semantics are covered by the live PostgreSQL test);
* an accepted canary = PROVEN recovery (eligible again, publication refreshed);
  another refusal = backoff (15 m / 30 m / 1 h / 2 h / capped at 6 h);
* time passing alone still never restores eligibility;
* the outage / reminder / storefront-offline / recovered cards are emitted
  through the durable outbox with stable, deduplicating keys.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cloud_platform.modules.businesslog.domain import BusinessEventType
from cloud_platform.modules.provider_capacity.domain import (
    DEFAULT_RECOVERY_BACKOFF_SECONDS,
    AccountCapacity,
    AccountCapacityState,
    CapacityObservation,
    recovery_backoff_seconds,
    validate_recovery_backoff,
)
from cloud_platform.modules.provider_capacity.recovery import (
    AccountInventory,
    CloudCapacityRecoveryService,
    inventory_ids_hash,
)
from cloud_platform.modules.provider_capacity.status import (
    METRIC_CAPACITY_BLOCKED_ACCOUNTS,
    METRIC_CAPACITY_UNKNOWN_ACCOUNTS,
    METRIC_CLOUD_SELLABLE_OFFERS,
    METRIC_CLOUD_STOREFRONT_AVAILABLE,
    METRIC_CLOUD_STOREFRONT_OUTAGE_SECONDS,
    METRIC_RECOVERY_CANDIDATE_ACCOUNTS,
    capacity_status,
)

PROVIDER = "leaseweb"
ACCOUNT = "sales-org-uk"
OTHER = "sales-org-north"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _blocked(*, account: str = ACCOUNT, attempts: int = 0, **overrides: Any) -> AccountCapacity:
    observed = NOW - timedelta(hours=2)
    record = AccountCapacity(
        provider_key=PROVIDER,
        credential_account_id=account,
        state=AccountCapacityState.UNKNOWN_AFTER_LIMIT,
        error_code="PC-2031",
        correlation_id="07376219-7bcd-43d9-a5ea-4128fa57345a",
        location_id="eu-west-3",
        product_id="lsw.mini",
        observations=1,
        observed_at=observed,
        expires_at=observed + timedelta(hours=1),
        recovery_attempts=attempts,
    )
    return replace(record, **overrides) if overrides else record


def _inventory(count: int, ids: list[str] | None = None) -> AccountInventory:
    instance_ids = ids or [f"i-{index}" for index in range(count)]
    return AccountInventory(
        instance_count=count, ids_hash=inventory_ids_hash(instance_ids), regions_read=1
    )


class _FakeCapacityRepo:
    """In-memory capacity store built on the REAL domain transitions."""

    def __init__(self, records: list[AccountCapacity] | None = None) -> None:
        self.records = {record.credential_account_id: record for record in records or []}
        self.calls: list[str] = []

    async def get(self, provider_key: str, credential_account_id: str) -> Any:
        return self.records.get(credential_account_id)

    async def list_for_provider(self, provider_key: str) -> tuple[AccountCapacity, ...]:
        return tuple(self.records.values())

    async def record_inventory_census(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        instance_count: int,
        ids_hash: str,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        self.calls.append("census")
        current = self.records.get(credential_account_id)
        if current is None or not current.is_limit_reached(now=now):
            return current
        updated = current
        if current.baseline_instance_count is None:
            updated = current.with_baseline(
                instance_count=instance_count, ids_hash=ids_hash, now=now
            )
        elif instance_count < current.baseline_instance_count:
            updated = replace(current, next_recovery_attempt_at=NOW)
        self.records[credential_account_id] = updated
        return updated

    async def schedule_recovery(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        delay_seconds: int,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        current = self.records.get(credential_account_id)
        if current is None:
            return None
        if (
            not current.is_limit_reached(now=now)
            or current.next_recovery_attempt_at is not None
            or current.canary_lease_held(now=now)
        ):
            return current
        self.calls.append("schedule")
        updated = current.with_recovery_scheduled(delay_seconds=delay_seconds, now=now)
        self.records[credential_account_id] = updated
        return updated

    async def open_recovery_window(
        self, provider_key: str, credential_account_id: str, *, now: datetime | None = None
    ) -> AccountCapacity | None:
        current = self.records.get(credential_account_id)
        if current is None or not current.recovery_window_due(now=now):
            return None
        self.calls.append("open")
        updated = current.with_recovery_window_open(now=now)
        self.records[credential_account_id] = updated
        return updated

    async def mark_outage_notified(
        self, provider_key: str, credential_account_id: str, *, now: datetime | None = None
    ) -> bool:
        current = self.records.get(credential_account_id)
        if current is None or current.outage_notified_at is not None:
            return False
        self.records[credential_account_id] = replace(current, outage_notified_at=now or NOW)
        return True

    async def mark_reminder_sent(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        sent_at: datetime | None = None,
    ) -> bool:
        current = self.records.get(credential_account_id)
        if current is None:
            return False
        self.records[credential_account_id] = replace(current, last_reminder_at=sent_at or NOW)
        return True

    async def bring_forward_recovery(
        self, provider_key: str, credential_account_id: str, *, now: datetime | None = None
    ) -> bool:
        current = self.records.get(credential_account_id)
        if current is None or not current.is_limit_reached(now=now):
            return False
        self.records[credential_account_id] = replace(current, next_recovery_attempt_at=now or NOW)
        return True


class _RecordingSink:
    """Outbox stand-in: UNIQUE event_key semantics (a duplicate is dropped)."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.keys: set[str] = set()

    async def emit(self, event: Any) -> bool:
        if event.event_key in self.keys:
            return False
        self.keys.add(event.event_key)
        self.events.append(event)
        return True

    def types(self) -> list[BusinessEventType]:
        return [event.event_type for event in self.events]


class _Source:
    def __init__(self, inventories: Any = None) -> None:
        self._mapping = inventories if inventories is not None else {ACCOUNT: _inventory(3)}
        self.calls = 0

    async def inventories(self) -> dict[str, AccountInventory | None]:
        self.calls += 1
        if isinstance(self._mapping, Exception):
            raise self._mapping
        return dict(self._mapping)


def _service(
    repo: _FakeCapacityRepo,
    source: _Source,
    *,
    sink: _RecordingSink | None = None,
    sellable: int = 5,
    **overrides: Any,
) -> tuple[CloudCapacityRecoveryService, _RecordingSink]:
    recording = sink or _RecordingSink()
    service = CloudCapacityRecoveryService(
        capacity_repo=repo,
        inventory_source=source,
        event_sink=recording,
        sellable_offers_source=lambda: overrides.pop("sellable_value", sellable),
        backoff_seconds=overrides.pop("backoff", DEFAULT_RECOVERY_BACKOFF_SECONDS),
        reminder_delay_seconds=overrides.pop("reminder_delay", 1800),
        reminder_interval_seconds=overrides.pop("reminder_interval", 21600),
        **overrides,
    )
    return service, recording


class TestRecoveryDomain:
    def test_backoff_progression_is_exponential_and_capped(self) -> None:
        assert [recovery_backoff_seconds(index) for index in range(6)] == [
            900,
            1800,
            3600,
            7200,
            21600,
            21600,
        ]

    def test_a_custom_schedule_is_validated(self) -> None:
        assert recovery_backoff_seconds(0, [120, 600]) == 120
        assert recovery_backoff_seconds(5, [120, 600]) == 600
        with pytest.raises(ValueError):
            recovery_backoff_seconds(-1, [120])
        with pytest.raises(ValueError):
            validate_recovery_backoff([])
        with pytest.raises(ValueError):
            validate_recovery_backoff([30])  # below the floor: never a tight loop
        with pytest.raises(ValueError):
            validate_recovery_backoff([True])  # bool is not an integer
        with pytest.raises(ValueError):
            recovery_backoff_seconds(1.5)

    def test_a_blocked_account_is_not_due_without_a_schedule(self) -> None:
        record = _blocked()
        assert record.recovery_window_due(now=NOW) is False
        scheduled = record.with_recovery_scheduled(delay_seconds=900, now=NOW)
        assert scheduled.recovery_window_due(now=NOW) is False
        assert scheduled.recovery_window_due(now=NOW + timedelta(minutes=15)) is True

    def test_an_open_window_accepts_the_canary_but_never_two_at_once(self) -> None:
        record = (
            _blocked()
            .with_recovery_scheduled(delay_seconds=900, now=NOW)
            .with_recovery_window_open(now=NOW)
        )
        assert record.state is AccountCapacityState.RECOVERY_CANDIDATE
        # The catalog may publish it again (one canary is welcome)…
        assert record.accepts_new_orders() is True
        assert record.is_limit_reached() is False
        assert record.accepts_canary(now=NOW) is True
        # …but a second in-flight attempt is refused until the lease frees up.
        leased = record.with_canary_attempt(ref="op-1", lease_seconds=900, now=NOW)
        assert leased.accepts_canary(now=NOW) is False
        assert leased.canary_lease_held(now=NOW + timedelta(minutes=14)) is True
        assert leased.canary_lease_held(now=NOW + timedelta(minutes=16)) is False
        assert leased.accepts_canary(now=NOW + timedelta(minutes=16)) is True

    def test_a_refused_canary_backs_off_and_clears_the_lease(self) -> None:
        leased = (
            _blocked()
            .with_recovery_scheduled(delay_seconds=900, now=NOW)
            .with_recovery_window_open(now=NOW)
            .with_canary_attempt(ref="op-1", lease_seconds=900, now=NOW)
        )
        refused = leased.with_canary_attempt_refused(
            observation=CapacityObservation(error_code="PC-2031"),
            delay_seconds=1800,
            now=NOW + timedelta(minutes=1),
        )
        assert refused.state is AccountCapacityState.LIMIT_REACHED
        assert refused.recovery_attempts == 1
        assert refused.canary_lease_ref is None
        assert refused.next_recovery_attempt_at == NOW + timedelta(minutes=31)
        assert refused.observed_at == NOW + timedelta(minutes=1)

    def test_a_proven_recovery_resets_the_schedule_and_bookkeeping(self) -> None:
        record = _blocked(attempts=3, outage_notified_at=NOW, last_reminder_at=NOW)
        proven = record.with_recovery_proven(now=NOW + timedelta(hours=3))
        assert proven.state is AccountCapacityState.HEALTHY
        assert proven.accepts_new_orders() is True
        assert proven.recovery_attempts == 0
        assert proven.next_recovery_attempt_at is None
        assert proven.outage_notified_at is None
        assert proven.last_reminder_at is None
        # The refusal evidence survives: "this account once refused a create"
        # stays visible to the doctor.
        assert proven.error_code == "PC-2031"
        assert proven.observations == 1

    def test_reminders_need_the_outage_card_and_follow_the_cadence(self) -> None:
        record = _blocked()
        # Without an outage card nothing reminds: the card comes first.
        assert record.reminder_due(now=NOW) is None
        notified = replace(record, outage_notified_at=record.observed_at)
        first_due = notified.observed_at + timedelta(minutes=30)
        assert notified.reminder_due(now=first_due - timedelta(minutes=1)) is None
        assert notified.reminder_due(now=first_due) == first_due
        reminded = replace(notified, last_reminder_at=first_due)
        assert reminded.reminder_due(now=first_due + timedelta(hours=5)) is None
        assert reminded.reminder_due(now=first_due + timedelta(hours=6)) == (
            first_due + timedelta(hours=6)
        )

    def test_inventory_ids_hash_is_stable_and_order_proof(self) -> None:
        assert inventory_ids_hash(["b", "a"]) == inventory_ids_hash(["a", "b", "a"])
        assert inventory_ids_hash(["a"]) != inventory_ids_hash(["a", "b"])

    def test_baseline_requires_a_real_census(self) -> None:
        record = _blocked()
        with pytest.raises(ValueError):
            record.with_baseline(instance_count=1, ids_hash="")
        with pytest.raises(ValueError):
            record.with_baseline(instance_count=-1, ids_hash="hash")


class TestStorefrontStatus:
    def test_metrics_expose_the_documented_names(self) -> None:
        fresh = _blocked(
            account=OTHER,
            state=AccountCapacityState.LIMIT_REACHED,
            observed_at=NOW - timedelta(minutes=5),
            expires_at=NOW + timedelta(minutes=55),
        )
        records = (
            _blocked(),
            fresh,
            _blocked(
                account="sales-org-candidate",
                state=AccountCapacityState.RECOVERY_CANDIDATE,
            ),
        )
        status = capacity_status(PROVIDER, records, sellable_offers=0, now=NOW)
        metrics = status.metrics(now=NOW)
        assert metrics[METRIC_CLOUD_SELLABLE_OFFERS] == 0
        assert metrics[METRIC_CAPACITY_BLOCKED_ACCOUNTS] == 2
        assert metrics[METRIC_CAPACITY_UNKNOWN_ACCOUNTS] == 1
        assert metrics[METRIC_RECOVERY_CANDIDATE_ACCOUNTS] == 1
        assert metrics[METRIC_CLOUD_STOREFRONT_AVAILABLE] is False
        # The outage clock starts at the EARLIEST still-blocked refusal.
        assert metrics[METRIC_CLOUD_STOREFRONT_OUTAGE_SECONDS] == 2 * 3600
        available = capacity_status(PROVIDER, records, sellable_offers=3, now=NOW)
        assert available.metrics(now=NOW)[METRIC_CLOUD_STOREFRONT_AVAILABLE] is True
        assert available.metrics(now=NOW)[METRIC_CLOUD_STOREFRONT_OUTAGE_SECONDS] == 0


class TestRecoveryController:
    async def test_first_pass_captures_the_baseline_and_schedules(self) -> None:
        repo = _FakeCapacityRepo([_blocked()])
        service, sink = _service(repo, _Source({ACCOUNT: _inventory(3)}))

        report = await service.run(PROVIDER, now=NOW)

        outcome = report.outcomes[0]
        assert outcome.baseline_captured is True
        assert outcome.baseline_count == 3
        assert outcome.inventory_count == 3
        assert outcome.scheduled_in_seconds == 900
        assert outcome.window_opened is False
        assert repo.records[ACCOUNT].state is AccountCapacityState.UNKNOWN_AFTER_LIMIT
        # One outage card, keyed on the refusal itself.
        assert sink.types() == [BusinessEventType.PROVIDER_CAPACITY_LIMIT_REACHED]
        assert "capacity.outage" in sink.events[0].event_key

    async def test_a_freed_instance_opens_the_window_in_the_same_pass(self) -> None:
        record = _blocked().with_baseline(instance_count=3, ids_hash="hash", now=NOW)
        deferred = replace(record, next_recovery_attempt_at=NOW + timedelta(hours=1))
        repo = _FakeCapacityRepo([deferred])
        service, _ = _service(repo, _Source({ACCOUNT: _inventory(2)}))

        report = await service.run(PROVIDER, now=NOW)

        outcome = report.outcomes[0]
        assert outcome.brought_forward is True
        assert outcome.window_opened is True
        assert outcome.state == AccountCapacityState.RECOVERY_CANDIDATE.value
        assert repo.records[ACCOUNT].next_recovery_attempt_at is None

    async def test_the_scheduled_window_opens_only_when_due(self) -> None:
        record = _blocked().with_recovery_scheduled(delay_seconds=900, now=NOW)
        repo = _FakeCapacityRepo([record])
        service, _ = _service(repo, _Source({ACCOUNT: _inventory(3)}))

        early = await service.run(PROVIDER, now=NOW + timedelta(minutes=5))
        assert early.outcomes[0].window_opened is False
        due = await service.run(PROVIDER, now=NOW + timedelta(minutes=16))
        assert due.outcomes[0].window_opened is True
        assert repo.records[ACCOUNT].state is AccountCapacityState.RECOVERY_CANDIDATE

    async def test_the_outage_card_is_emitted_once_per_refusal(self) -> None:
        repo = _FakeCapacityRepo([_blocked()])
        sink = _RecordingSink()
        service, _ = _service(repo, _Source({ACCOUNT: _inventory(3)}), sink=sink)

        await service.run(PROVIDER, now=NOW)
        await service.run(PROVIDER, now=NOW + timedelta(minutes=5))

        assert sink.types().count(BusinessEventType.PROVIDER_CAPACITY_LIMIT_REACHED) == 1

    async def test_reminders_follow_the_cadence_and_deduplicate(self) -> None:
        record = _blocked(outage_notified_at=None)
        repo = _FakeCapacityRepo([record])
        sink = _RecordingSink()
        # A 6-hour first window keeps the account BLOCKED (and therefore
        # reminder-eligible) for the whole test; the reminder interval is short
        # so two cards are reachable without waiting.
        service, _ = _service(
            repo,
            _Source({ACCOUNT: _inventory(3)}),
            sink=sink,
            backoff=[21600],
            reminder_interval=600,
        )

        await service.run(PROVIDER, now=NOW)  # outage card
        await service.run(PROVIDER, now=NOW + timedelta(minutes=31))  # first reminder
        await service.run(PROVIDER, now=NOW + timedelta(minutes=31))  # deduplicated
        await service.run(PROVIDER, now=NOW + timedelta(minutes=42))  # second reminder

        reminders = [
            event
            for event in sink.events
            if event.event_type is BusinessEventType.PROVIDER_CAPACITY_RECOVERY_REMINDER
        ]
        assert len(reminders) == 2
        assert reminders[0].payload["next_attempt"]
        assert reminders[0].event_key != reminders[1].event_key
        assert "capacity.reminder" in reminders[0].event_key
        # The reminder is stamped with the SEND instant, so a controller that
        # was down for a day sends ONE catch-up card instead of a burst.
        assert reminders[0].payload["at"] == (NOW + timedelta(minutes=31)).isoformat()
        assert reminders[1].payload["at"] == (NOW + timedelta(minutes=42)).isoformat()

    async def test_all_accounts_blocked_emits_one_storefront_card(self) -> None:
        repo = _FakeCapacityRepo(
            [
                _blocked(),
                _blocked(account=OTHER),
            ]
        )
        sink = _RecordingSink()
        service, _ = _service(
            repo,
            _Source({ACCOUNT: _inventory(1), OTHER: _inventory(0)}),
            sink=sink,
            sellable=0,
        )

        await service.run(PROVIDER, now=NOW)
        await service.run(PROVIDER, now=NOW + timedelta(minutes=5))

        offline = [
            event
            for event in sink.events
            if event.event_type is BusinessEventType.PROVIDER_CAPACITY_STOREFRONT_UNAVAILABLE
        ]
        assert len(offline) == 1
        assert offline[0].payload["accounts_blocked"] == 2
        assert offline[0].payload["sellable_offers"] == 0
        # The account outage cards are still one each.
        assert sink.types().count(BusinessEventType.PROVIDER_CAPACITY_LIMIT_REACHED) == 2

    async def test_healthy_accounts_are_never_touched(self) -> None:
        healthy = AccountCapacity(
            provider_key=PROVIDER,
            credential_account_id="sales-org-eu",
            state=AccountCapacityState.HEALTHY,
        )
        repo = _FakeCapacityRepo([healthy])
        sink = _RecordingSink()
        service, _ = _service(repo, _Source({"sales-org-eu": _inventory(2)}), sink=sink)

        report = await service.run(PROVIDER, now=NOW)

        assert report.outcomes == ()
        assert sink.events == []
        assert repo.calls == []

    async def test_an_unreadable_census_still_uses_durable_state(self) -> None:
        repo = _FakeCapacityRepo([_blocked()])
        sink = _RecordingSink()
        service, _ = _service(repo, _Source(RuntimeError("provider down")), sink=sink)

        report = await service.run(PROVIDER, now=NOW)

        assert report.errors and "inventory" in report.errors[0]
        assert report.outcomes[0].scheduled_in_seconds == 900
        assert "inventory-unavailable" in report.outcomes[0].notes

    async def test_disabled_recovery_is_a_read_only_noop(self) -> None:
        repo = _FakeCapacityRepo([_blocked()])
        sink = _RecordingSink()
        service, _ = _service(repo, _Source(), sink=sink, enabled=False)

        report = await service.run(PROVIDER, now=NOW)

        assert report.skipped == "disabled"
        assert sink.events == []
        assert repo.calls == []

    async def test_the_controller_never_treats_time_as_recovery(self) -> None:
        """A long-outage account with no schedule only gets a schedule — never
        eligibility."""
        repo = _FakeCapacityRepo([_blocked(state=AccountCapacityState.UNKNOWN_AFTER_LIMIT)])
        service, _ = _service(repo, _Source({ACCOUNT: _inventory(3)}))

        await service.run(PROVIDER, now=NOW + timedelta(days=3))

        assert repo.records[ACCOUNT].state is AccountCapacityState.UNKNOWN_AFTER_LIMIT
        scheduled_at = NOW + timedelta(days=3) + timedelta(minutes=15)
        assert repo.records[ACCOUNT].next_recovery_attempt_at == scheduled_at
