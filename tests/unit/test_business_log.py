"""Private operator business-logger channel (release hardening).

The business logger is an INTEGRATION concern with three hard properties:

1. **It cannot break the financial path** — emission only enqueues, and every
   failure is swallowed with a log line;
2. **It cannot duplicate a business event** — the durable ``event_key`` is
   unique and delivery claims rows atomically;
3. **It cannot leak a secret** — payloads are sanitized before they are
   stored or rendered, and no builder accepts a credential.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.businesslog.domain import (
    STATUS_ABANDONED,
    STATUS_PENDING,
    STATUS_RETRY,
    STATUS_SENDING,
    STATUS_SENT,
    BusinessEvent,
    BusinessEventType,
    BusinessLogDispatcher,
    BusinessLogPolicy,
    NullBusinessEventSink,
    OutboxBusinessEventSink,
    compact,
    emit_safe,
    event_key,
    format_minor,
    render_event,
    uuid_text,
)
from cloud_platform.modules.businesslog.events import (
    admin_adjustment_event,
    provider_accepted_event,
    purchase_failed_event,
    purchase_requested_event,
    recharge_created_event,
    recharge_failed_event,
    recharge_succeeded_event,
    vps_provisioned_event,
)
from cloud_platform.modules.businesslog.repository import (
    SqlAlchemyBusinessLogRepository,
)

SERVER_ID = uuid4()
ORDER_ID = uuid4()
SESSION_ID = uuid4()


class FakeRecord:
    """Stand-in for a claimed outbox row."""

    def __init__(
        self,
        *,
        event_key: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        attempts: int = 0,
        created_at: datetime | None = None,
        status: str = STATUS_PENDING,
    ) -> None:
        self.event_key = event_key
        self.event_type = event_type
        self.payload = payload or {}
        self.attempts = attempts
        self.created_at = created_at or datetime.now(UTC)
        self.status = status


class FakeRepo:
    """In-memory outbox double (unique keys, claim bookkeeping)."""

    def __init__(self) -> None:
        self.rows: dict[str, FakeRecord] = {}
        self.sent: list[str] = []
        self.retried: list[tuple[str, str]] = []
        self.abandoned: list[str] = []
        self.raise_on_enqueue: Exception | None = None

    async def enqueue(
        self,
        *,
        event_key: str,
        event_type: str,
        payload: dict[str, Any],
        at: datetime | None = None,
    ) -> bool:
        if self.raise_on_enqueue is not None:
            raise self.raise_on_enqueue
        if event_key in self.rows:
            return False
        self.rows[event_key] = FakeRecord(
            event_key=event_key, event_type=event_type, payload=payload, created_at=at
        )
        return True

    async def claim_due(
        self, *, limit: int, now: datetime, stale_after_seconds: int
    ) -> list[FakeRecord]:
        # Mirrors the real repository: only claimable rows are returned, and
        # claiming increments the attempt count.
        claimable = [
            row for row in self.rows.values() if row.status in (STATUS_PENDING, STATUS_RETRY)
        ]
        claimed: list[FakeRecord] = []
        for row in claimable[:limit]:
            row.attempts += 1
            row.status = STATUS_SENDING
            claimed.append(row)
        return claimed

    async def mark_sent(self, event_key: str, *, at: datetime) -> None:
        self.sent.append(event_key)
        self.rows[event_key].status = STATUS_SENT

    async def mark_retry(self, event_key: str, *, error: str, next_attempt_at: datetime) -> None:
        self.retried.append((event_key, error))
        self.rows[event_key].status = STATUS_RETRY

    async def mark_abandoned(self, event_key: str, *, error: str) -> None:
        self.abandoned.append(event_key)
        self.rows[event_key].status = STATUS_ABANDONED

    async def get(self, event_key: str) -> FakeRecord | None:
        return self.rows.get(event_key)

    async def counts_by_status(self) -> dict[str, int]:
        return {STATUS_PENDING: len(self.rows)}


def _policy(**overrides: Any) -> BusinessLogPolicy:
    base: dict[str, Any] = {
        "enabled": True,
        "chat_id": -1001234567890,
        "max_attempts": 3,
        "batch_size": 10,
        "backoff_base_seconds": 30,
    }
    base.update(overrides)
    return BusinessLogPolicy(**base)


class FakeChannel:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.messages: list[str] = []
        self._fail_times = fail_times

    async def send(self, text: str) -> None:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("telegram is down (token=SECRET-abc)")
        self.messages.append(text)


class TestPolicy:
    """Which events the operator channel receives."""

    def test_disabled_or_unaddressed_channel_is_inactive(self) -> None:
        assert _policy(enabled=False).active is False
        assert _policy(chat_id=0).active is False
        assert _policy().active is True

    def test_every_event_type_maps_to_one_flag(self) -> None:
        policy = _policy()
        for event_type in BusinessEventType:
            assert hasattr(policy, event_type.log_flag)

    def test_category_flags_gate_the_event_types(self) -> None:
        policy = _policy(log_order_failures=False, log_admin_wallet_adjustments=False)
        assert policy.allows(BusinessEventType.PURCHASE_REQUESTED) is True
        assert policy.allows(BusinessEventType.PURCHASE_FAILED) is False
        assert policy.allows(BusinessEventType.ADMIN_WALLET_ADJUSTMENT) is False

    def test_inactive_policy_allows_nothing(self) -> None:
        policy = _policy(enabled=False)
        assert not any(policy.allows(t) for t in BusinessEventType)

    def test_built_from_settings(self) -> None:
        settings = MagicMock()
        settings.telegram_logger_enabled = True
        settings.telegram_logger_chat_id = -42
        settings.telegram_logger_log_purchases = False
        settings.telegram_logger_log_recharges = True
        settings.telegram_logger_log_payment_failures = False
        settings.telegram_logger_log_order_failures = True
        settings.telegram_logger_log_admin_wallet_adjustments = False
        policy = BusinessLogPolicy.from_settings(settings)
        assert policy.active is True
        assert policy.chat_id == -42
        assert policy.allows(BusinessEventType.PURCHASE_REQUESTED) is False
        assert policy.allows(BusinessEventType.RECHARGE_SUCCEEDED) is True
        assert policy.allows(BusinessEventType.RECHARGE_FAILED) is False


class TestEventAndSink:
    """Emission is durable, idempotent and never raises into the caller."""

    async def test_null_sink_accepts_everything_silently(self) -> None:
        assert (
            await NullBusinessEventSink().emit(
                purchase_requested_event(
                    user=None,
                    server_id=SERVER_ID,
                    order_id=ORDER_ID,
                    provider_key="p",
                    market="foreign",
                    location_id="L",
                    plan_name="P",
                    product_id="X",
                    os_name="Ubuntu",
                    selling_price_minor=1899,
                    currency="EUR",
                )
            )
            is False
        )

    def test_event_key_is_required(self) -> None:
        with pytest.raises(ValueError):
            BusinessEvent(event_key="  ", event_type=BusinessEventType.PURCHASE_REQUESTED)
        with pytest.raises(ValueError):
            BusinessEvent(event_key="x" * 201, event_type=BusinessEventType.PURCHASE_REQUESTED)

    async def test_emission_deduplicates_on_the_event_key(self) -> None:
        repo = FakeRepo()
        sink = OutboxBusinessEventSink(repo, _policy())
        event = recharge_created_event(
            user=None,
            payment_session_id=SESSION_ID,
            amount_minor=2_500,
            currency="EUR",
            gateway="zarinpal",
        )
        assert await emit_safe(sink, event) is True
        # A retried checkout / re-run worker replays the same key: no second row.
        assert await emit_safe(sink, event) is False
        assert len(repo.rows) == 1

    async def test_disabled_channel_never_writes(self) -> None:
        repo = FakeRepo()
        sink = OutboxBusinessEventSink(repo, _policy(enabled=False))
        assert await emit_safe(sink, _failed_event()) is False
        assert repo.rows == {}

    async def test_filtered_event_type_never_writes(self) -> None:
        repo = FakeRepo()
        sink = OutboxBusinessEventSink(repo, _policy(log_order_failures=False))
        assert await emit_safe(sink, _failed_event()) is False
        assert repo.rows == {}

    async def test_enqueue_failure_is_swallowed(self) -> None:
        repo = FakeRepo()
        repo.raise_on_enqueue = RuntimeError("database is down")
        sink = OutboxBusinessEventSink(repo, _policy())
        # The caller is on the financial path: it must not see an exception.
        assert await emit_safe(sink, _failed_event()) is False

    async def test_emit_safe_tolerates_a_broken_sink(self) -> None:
        class ExplodingSink:
            async def emit(self, event: BusinessEvent) -> bool:
                raise RuntimeError("boom")

        assert await emit_safe(ExplodingSink(), _failed_event()) is False
        assert await emit_safe(None, _failed_event()) is False


def _failed_event() -> BusinessEvent:
    return purchase_failed_event(
        user=None,
        server_id=SERVER_ID,
        order_id=ORDER_ID,
        provider_key="p",
        provider_order_id=None,
        operation_key="order-create:x",
        category="outcome_unknown",
        reason="read timeout",
    )


class TestSanitization:
    """Secrets can never reach the channel, whatever the payload."""

    def test_credential_shaped_keys_are_redacted(self) -> None:
        event = BusinessEvent(
            event_key="k",
            event_type=BusinessEventType.PURCHASE_FAILED,
            payload={
                "api_key": "LIVEKEY",
                "authorization": "Bearer abc",
                "x-lsw-auth": "LIVEKEY",
                "password": "hunter2",
                "cloud_init": "root:secret",
                "root_password": "hunter2",
                "reason": "provider rejected",
            },
        )
        payload = event.sanitized_payload()
        assert payload["api_key"] == "***REDACTED***"
        assert payload["authorization"] == "***REDACTED***"
        assert payload["password"] == "***REDACTED***"
        assert payload["reason"] == "provider rejected"
        assert "LIVEKEY" not in str(payload)
        assert "hunter2" not in str(payload)

    def test_nested_values_and_headers_are_redacted(self) -> None:
        event = BusinessEvent(
            event_key="k",
            event_type=BusinessEventType.PURCHASE_FAILED,
            payload={
                "request": {"headers": {"X-LSW-Auth": "LIVEKEY"}},
                "errors": ["token=LIVEKEY leaked", "clean"],
            },
        )
        payload = event.sanitized_payload()
        assert "LIVEKEY" not in str(payload)
        assert payload["errors"][1] == "clean"

    def test_values_are_truncated_and_json_safe(self) -> None:
        event = BusinessEvent(
            event_key="k",
            event_type=BusinessEventType.PURCHASE_FAILED,
            payload={
                "body": "x" * 5_000,
                "when": datetime(2026, 1, 1, tzinfo=UTC),
                "id": uuid4(),
                "none": None,
                "empty": "",
                "count": 3,
            },
        )
        payload = event.sanitized_payload()
        assert len(payload["body"]) <= 300
        assert isinstance(payload["when"], str)
        assert isinstance(payload["id"], str)
        assert "none" not in payload  # unset fields are dropped
        assert "empty" not in payload
        assert payload["count"] == 3

    def test_unsanitizable_payload_degrades_to_empty(self) -> None:
        class Exploding:
            def __str__(self) -> str:
                raise RuntimeError("boom")

        event = BusinessEvent(
            event_key="k",
            event_type=BusinessEventType.PURCHASE_FAILED,
            payload={"weird": Exploding()},
        )
        assert event.sanitized_payload() == {}


class TestRenderer:
    """The operator card is readable and never carries a secret."""

    def test_renders_title_and_labelled_fields(self) -> None:
        text = render_event(
            BusinessEventType.PURCHASE_REQUESTED,
            {"market": "foreign", "provider": "X", "selling_price": "18.99 EUR"},
        )
        assert "🛒" in text
        assert "بازار: foreign" in text
        assert "قیمت فروش: 18.99 EUR" in text

    def test_unknown_keys_are_still_rendered(self) -> None:
        text = render_event(BusinessEventType.VPS_PROVISIONED, {"extra": "value"})
        assert "extra: value" in text

    def test_format_minor_is_integer_only(self) -> None:
        assert format_minor(1_899, "EUR") == "18.99 EUR"
        assert format_minor(-500, "EUR") == "-5.00 EUR"
        assert format_minor(None, "EUR") is None
        assert format_minor("10", "EUR") is None  # never coerce money

    def test_compact_drops_unset_values(self) -> None:
        assert compact(a=1, b=None, c="", d="x") == {"a": 1, "d": "x"}

    def test_event_key_and_uuid_text_helpers(self) -> None:
        assert event_key("a", None, "b") == "a:b"
        assert uuid_text(None) is None
        assert uuid_text("x") == "x"


class TestEventBuilders:
    """Every builder produces its documented, secret-free payload shape."""

    def test_purchase_requested_carries_the_customer_facts(self) -> None:
        user = MagicMock(id=SERVER_ID, telegram_user_id=42, username="cust")
        event = purchase_requested_event(
            user=user,
            server_id=SERVER_ID,
            order_id=ORDER_ID,
            provider_key="provider",
            market="foreign",
            location_id="AMS-01",
            plan_name="Small",
            product_id="VPS02_1",
            os_name="Ubuntu 24.04",
            selling_price_minor=1_899,
            currency="EUR",
        )
        assert event.event_key == f"purchase.requested:{SERVER_ID}"
        assert event.payload["market"] == "foreign"
        assert event.payload["selling_price"] == "18.99 EUR"
        assert event.payload["telegram_user_id"] == 42
        assert event.payload["os"] == "Ubuntu 24.04"

    def test_provider_accepted_logs_the_provider_cost_not_the_sale_price(self) -> None:
        event = provider_accepted_event(
            user=None,
            server_id=SERVER_ID,
            order_id=ORDER_ID,
            provider_key="provider",
            provider_order_id="LS-1",
            product_id="VPS02_1",
            location_id="AMS-01",
            plan_name="Small",
            provider_cost_minor=1_299,
            currency="EUR",
            operation_key="order-create:x",
        )
        assert event.payload["provider_cost"] == "12.99 EUR"
        assert "selling_price" not in event.payload
        assert event.payload["provider_order_id"] == "LS-1"

    def test_vps_provisioned_lists_reachable_addresses_only(self) -> None:
        event = vps_provisioned_event(
            user=None,
            server_id=SERVER_ID,
            provider_key="provider",
            provider_order_id="LS-1",
            location_id="AMS-01",
            plan_name="Small",
            state="running",
            ipv4="203.0.113.10",
            ipv6=None,
        )
        assert event.payload["ipv4"] == "203.0.113.10"
        assert "ipv6" not in event.payload
        assert "password" not in str(event.payload)

    def test_purchase_failed_keeps_triage_identifiers(self) -> None:
        event = purchase_failed_event(
            user=None,
            server_id=SERVER_ID,
            order_id=ORDER_ID,
            provider_key="provider",
            provider_order_id="LS-1",
            operation_key="order-create:x",
            category="outcome_unknown",
            reason="read timeout",
        )
        assert event.event_key == f"purchase.failed:{SERVER_ID}:outcome_unknown"
        assert event.payload["category"] == "outcome_unknown"
        assert event.payload["operation_key"] == "order-create:x"

    def test_recharge_events_carry_amount_and_session(self) -> None:
        created = recharge_created_event(
            user=None,
            payment_session_id=SESSION_ID,
            amount_minor=2_500,
            currency="EUR",
            gateway="zarinpal",
        )
        succeeded = recharge_succeeded_event(
            user=None,
            payment_session_id=SESSION_ID,
            amount_minor=2_500,
            currency="EUR",
            gateway="zarinpal",
            gateway_reference="AUTH-1",
            balance_after_minor=7_500,
        )
        failed = recharge_failed_event(
            user=None,
            payment_session_id=SESSION_ID,
            amount_minor=2_500,
            currency="EUR",
            gateway="zarinpal",
            state="failed",
        )
        assert created.payload["amount"] == "25.00 EUR"
        assert succeeded.payload["gateway_reference"] == "AUTH-1"
        assert succeeded.payload["balance_after"] == "75.00 EUR"
        assert failed.payload["state"] == "failed"
        # Distinct keys: one event per fact.
        assert len({created.event_key, succeeded.event_key, failed.event_key}) == 3

    def test_admin_adjustment_names_the_actor(self) -> None:
        admin = MagicMock(id=uuid4(), username="ops")
        event = admin_adjustment_event(
            admin=admin,
            user=MagicMock(id=SERVER_ID, telegram_user_id=1, username="cust"),
            amount_minor=-500,
            currency="EUR",
            entry_type="adjustment",
            reason="chargeback",
            balance_after_minor=1_000,
            idempotency_key="adj-1",
        )
        assert event.event_key == "admin.wallet_adjustment:adj-1"
        assert event.payload["actor"] == "ops"
        assert event.payload["amount"] == "-5.00 EUR"
        assert event.payload["reason"] == "chargeback"


class TestDispatcher:
    """Delivery is claimed, bounded and backoff-driven."""

    async def test_successful_delivery_marks_sent(self) -> None:
        repo = FakeRepo()
        channel = FakeChannel()
        await repo.enqueue(
            event_key="k1",
            event_type=BusinessEventType.RECHARGE_CREATED.value,
            payload={"amount": "25.00 EUR"},
        )
        report = await BusinessLogDispatcher(repo, channel, _policy()).deliver()
        assert report.sent == 1
        assert repo.sent == ["k1"]
        assert channel.messages and "🧾" in channel.messages[0]

    async def test_disabled_channel_delivers_nothing(self) -> None:
        repo = FakeRepo()
        channel = FakeChannel()
        await repo.enqueue(
            event_key="k1", event_type=BusinessEventType.RECHARGE_CREATED.value, payload={}
        )
        report = await BusinessLogDispatcher(repo, channel, _policy(enabled=False)).deliver()
        assert report.sent == 0
        assert channel.messages == []

    async def test_failure_schedules_a_bounded_retry(self) -> None:
        repo = FakeRepo()
        channel = FakeChannel(fail_times=1)
        await repo.enqueue(
            event_key="k1", event_type=BusinessEventType.RECHARGE_FAILED.value, payload={}
        )
        report = await BusinessLogDispatcher(repo, channel, _policy()).deliver()
        assert report.retried == 1
        key, error = repo.retried[0]
        assert key == "k1"
        # The stored error is redacted, not the raw message.
        assert "SECRET-abc" not in error
        assert repo.sent == []

    async def test_repeated_failures_are_abandoned_at_the_cap(self) -> None:
        repo = FakeRepo()
        channel = FakeChannel(fail_times=99)
        await repo.enqueue(
            event_key="k1", event_type=BusinessEventType.RECHARGE_FAILED.value, payload={}
        )
        dispatcher = BusinessLogDispatcher(repo, channel, _policy(max_attempts=2))
        reports = [await dispatcher.deliver() for _ in range(4)]
        assert reports[0].retried == 1  # first pass backs off
        assert any(r.abandoned == 1 for r in reports)  # then the cap is hit
        assert repo.abandoned == ["k1"]
        assert channel.messages == []
        # A broken channel can never flood: the row is no longer delivered.
        assert all(r.sent == 0 for r in reports)
        assert (await dispatcher.deliver()).sent == 0

    async def test_unknown_event_type_still_renders(self) -> None:
        repo = FakeRepo()
        channel = FakeChannel()
        await repo.enqueue(event_key="k1", event_type="future.event", payload={"a": "b"})
        await BusinessLogDispatcher(repo, channel, _policy()).deliver()
        assert "future.event" in channel.messages[0]

    async def test_created_at_is_rendered_when_the_payload_has_none(self) -> None:
        record = FakeRecord(
            event_key="k",
            event_type=BusinessEventType.RECHARGE_CREATED.value,
            payload={},
            created_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )
        text = BusinessLogDispatcher.render(record)
        assert "2026-05-01" in text


class TestRepository:
    """SQLAlchemy outbox adapter (mocked session, established pattern)."""

    @pytest.fixture
    def db(self) -> AsyncMock:
        mock = AsyncMock()
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=None)
        mock.add = MagicMock()
        mock.commit = AsyncMock()
        mock.rollback = AsyncMock()
        return mock

    def _repo(self, db: AsyncMock) -> SqlAlchemyBusinessLogRepository:
        return SqlAlchemyBusinessLogRepository(lambda: db)

    async def test_enqueue_returns_true_for_a_new_key(self, db: AsyncMock) -> None:
        assert (
            await self._repo(db).enqueue(event_key="k1", event_type="t", payload={"a": 1}) is True
        )
        db.add.assert_called_once()
        db.commit.assert_awaited_once()

    async def test_enqueue_returns_false_on_a_unique_violation(self, db: AsyncMock) -> None:
        db.commit.side_effect = IntegrityError("stmt", {}, Exception("duplicate"))
        assert await self._repo(db).enqueue(event_key="k1", event_type="t", payload={}) is False
        db.rollback.assert_awaited_once()

    async def test_claim_due_returns_claimed_rows(self, db: AsyncMock) -> None:
        now = datetime.now(UTC)
        key_result = MagicMock()
        key_result.scalars.return_value.all.return_value = ["k1"]
        row = MagicMock()
        row.id = uuid4()
        row.event_key = "k1"
        row.event_type = "t"
        row.payload = {"a": 1}
        row.status = STATUS_SENDING
        row.created_at = now
        row.sent_at = None
        row.attempts = 1
        row.last_error = None
        row_result = MagicMock()
        row_result.scalar_one.return_value = row
        update_result = MagicMock()
        update_result.rowcount = 1
        db.execute.side_effect = [key_result, update_result, row_result]
        claimed = await self._repo(db).claim_due(limit=5, now=now, stale_after_seconds=300)
        assert [c.event_key for c in claimed] == ["k1"]
        assert claimed[0].attempts == 1
        db.commit.assert_awaited()

    async def test_lost_claim_race_yields_no_row(self, db: AsyncMock) -> None:
        now = datetime.now(UTC)
        key_result = MagicMock()
        key_result.scalars.return_value.all.return_value = ["k1"]
        update_result = MagicMock()
        update_result.rowcount = 0
        db.execute.side_effect = [key_result, update_result]
        assert await self._repo(db).claim_due(limit=5, now=now, stale_after_seconds=300) == []

    async def test_status_transitions_commit(self, db: AsyncMock) -> None:
        repo = self._repo(db)
        now = datetime.now(UTC)
        await repo.mark_sent("k1", at=now)
        await repo.mark_retry("k1", error="e", next_attempt_at=now + timedelta(seconds=30))
        await repo.mark_abandoned("k1", error="e")
        assert db.commit.await_count == 3

    async def test_get_returns_none_for_a_missing_key(self, db: AsyncMock) -> None:
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute.return_value = result
        assert await self._repo(db).get("nope") is None

    async def test_counts_by_status(self, db: AsyncMock) -> None:
        result = MagicMock()
        result.all.return_value = [(STATUS_SENT, 2), (STATUS_RETRY, 1)]
        db.execute.return_value = result
        assert await self._repo(db).counts_by_status() == {STATUS_SENT: 2, STATUS_RETRY: 1}

    async def test_get_returns_a_record(self, db: AsyncMock) -> None:
        now = datetime.now(UTC)
        row = MagicMock()
        row.id = uuid4()
        row.event_key = "k1"
        row.event_type = "t"
        row.payload = {}
        row.status = STATUS_PENDING
        row.created_at = now
        row.sent_at = None
        row.attempts = 0
        row.last_error = None
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        db.execute.return_value = result
        record = await self._repo(db).get("k1")
        assert record is not None and record.event_key == "k1"
