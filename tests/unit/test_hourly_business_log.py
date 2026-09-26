"""Hourly Cloud lifecycle events in the durable operator business log.

Live incident (2026-09-26): several hourly creates failed (Leaseweb PC-2031,
generic 400) and ``business_log_events`` stayed EMPTY. The Telegram logger
configuration was correct and the worker cron was running — but
:class:`HourlyCloudService` never emitted a business event at all, so the
operator's private channel saw nothing.

These tests pin the fix:

- one card per lifecycle transition, keyed by the durable local identity, so a
  replayed confirmation, a repeated worker pass or a per-minute reconciler can
  never duplicate a card;
- the provider evidence that matters operationally (error code, correlation
  id, credential account) survives into the card;
- a disabled logger changes nothing about the hourly behavior itself;
- a failing Telegram delivery is visible in the application log with safe
  diagnostics and still terminates (RETRY with backoff, then ABANDONED).
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.modules.businesslog.domain import (
    BusinessEventType,
    BusinessLogDispatcher,
    BusinessLogPolicy,
    NullBusinessEventSink,
    format_minor,
    render_event,
)
from cloud_platform.modules.businesslog.events import purchase_failed_event
from cloud_platform.modules.compute.domain import BILLING_MODEL_HOURLY, ServerLifecycleState
from cloud_platform.modules.hourly.service import HourlyCloudService
from cloud_platform.modules.operations.service import (
    ServerStateReconciler,
    StateReconciliationOutcome,
)
from cloud_platform.providers.errors import (
    ProviderCapacityError,
    ProviderError,
    ProviderOutcomeUnknown,
)
from cloud_platform.providers.registry import ProviderRegistry
from tests.unit.test_business_log import FakeRecord, _policy
from tests.unit.test_hourly_cloud_flow import (
    PROVIDER,
    USER,
    FakeAccountRepo,
    FakeAuditRepo,
    FakeOffersRepo,
    FakeOpsRepo,
    FakeServerRepo,
    FakeSnapshots,
    FakeWalletRepo2,
    _usd_offer,
)
from tests.unit.test_hourly_state_machine import (
    FakeCapacityRepo,
    FakeHourlyAdapter,
    _accepted_response,
    _DictResolver,
)
from tests.unit.test_server_state_reconciler import (
    FakeProvider,
)
from tests.unit.test_server_state_reconciler import (
    FakeServerRepo as ReconcilerServerRepo,
)
from tests.unit.test_server_state_reconciler import (
    _server as _reconciler_server,
)

ACCOUNT = "sales-org-north"
CORRELATION_ID = "75e99a9c-13e5-409c-9a59-a735da958cdb"


# ---------------------------------------------------------------------------
# Fakes: the durable outbox, and the safe identity lookup
# ---------------------------------------------------------------------------


class FakeEventSink:
    """In-memory durable outbox: the unique ``event_key`` is the idempotency."""

    def __init__(self) -> None:
        self.events: dict[str, Any] = {}

    async def emit(self, event: Any) -> bool:
        if event.event_key in self.events:
            return False
        self.events[event.event_key] = event
        return True

    def of_type(self, event_type: BusinessEventType) -> list[Any]:
        return [event for event in self.events.values() if event.event_type is event_type]

    def payloads(self, event_type: BusinessEventType) -> list[dict[str, Any]]:
        return [dict(event.payload) for event in self.of_type(event_type)]


class FakeUserRepo:
    """Safe identity lookup (never a credential)."""

    def __init__(self, user: Any = USER) -> None:
        self.user = user
        self.calls: list[UUID] = []

    async def get(self, user_id: UUID) -> Any:
        self.calls.append(user_id)
        return self.user if getattr(self.user, "id", None) == user_id else None


class _CapacityRefusal(ProviderCapacityError):
    """The provider's account-limit refusal, with its documented evidence."""

    def __init__(self, message: str, *, error_code: str, correlation_id: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.correlation_id = correlation_id


class _Ambiguous(ProviderOutcomeUnknown):
    pass


class _HourlyCloudProvider(FakeProvider):
    """The reconciler resolves the adapter through the logical provider key."""

    key = PROVIDER


class _AuditStub:
    async def append(self, event: Any) -> Any:
        return event


async def _offers(account: str | None = ACCOUNT) -> FakeOffersRepo:
    return FakeOffersRepo([await _usd_offer(provider_account_id=account)])


def _hourly(
    offers: FakeOffersRepo,
    cloud: FakeHourlyAdapter,
    *,
    sink: Any | None = None,
    users: Any | None = None,
    ops: FakeOpsRepo | None = None,
    capacity: Any | None = None,
) -> tuple[HourlyCloudService, FakeOpsRepo]:
    operations = ops or FakeOpsRepo()
    service = HourlyCloudService(
        server_repo=FakeServerRepo(),
        offers_repo=offers,  # type: ignore[arg-type]
        account_repo=FakeAccountRepo(),
        wallet_repo=FakeWalletRepo2(),
        snapshot_service=FakeSnapshots(),  # type: ignore[arg-type]
        operation_repo=operations,  # type: ignore[arg-type]
        audit_repo=FakeAuditRepo(),
        cloud_providers={PROVIDER: cloud},
        # The offer is pinned to a credential account, exactly as the
        # multi-account catalog publishes it: account-aware dispatch resolves
        # the adapter that owns that account.
        cloud_resolver=_DictResolver({ACCOUNT: cloud}),
        capacity_repo=capacity,
        event_sink=sink,
        user_repo=users,
    )
    return service, operations


async def _requested(service: HourlyCloudService, offers: FakeOffersRepo) -> Any:
    """Create a REQUESTED hourly server through the real service path."""
    offer = (await offers.list_all())[0]
    result = await service.create_instance(
        user=USER,
        offer_id=offer.id,
        image_id="UBUNTU",
        image_label="Ubuntu",
        idempotency_key=f"op-{uuid4().hex[:8]}",
    )
    return result.server


class TestLifecycleCards:
    """One card per durable hourly lifecycle transition."""

    async def test_local_acceptance_emits_one_purchase_requested(self) -> None:
        offers = await _offers()
        offer = (await offers.list_all())[0]
        sink = FakeEventSink()
        service, _ = _hourly(offers, FakeHourlyAdapter(), sink=sink, users=FakeUserRepo())

        server = await _requested(service, offers)

        assert set(sink.events) == {f"purchase.requested:{server.id}"}
        payload = sink.payloads(BusinessEventType.PURCHASE_REQUESTED)[0]
        assert payload["kind"] == "hourly"
        assert payload["provider"] == PROVIDER
        assert payload["credential_account"] == ACCOUNT
        assert payload["plan"] == "Mini"
        assert payload["product_id"] == "lsw.mini"
        assert payload["location"] == "eu-west-3"
        assert payload["image"] == "UBUNTU"
        assert payload["os"] == "Ubuntu"
        assert payload["currency"] == "USD"
        assert payload["selling_price"] == format_minor(offer.selling_price_minor, "USD")
        assert payload["server_id"] == str(server.id)
        assert payload["operation_key"] == f"server-create:{server.id}"
        assert payload["user_id"] == str(USER.id)
        assert payload["telegram_user_id"] == USER.telegram_user_id
        # The card carries the opaque account label, never a credential.
        assert "api_key" not in payload

    async def test_idempotent_replay_never_duplicates_the_request_card(self) -> None:
        offers = await _offers()
        offer = (await offers.list_all())[0]
        sink = FakeEventSink()
        service, _ = _hourly(offers, FakeHourlyAdapter(), sink=sink, users=FakeUserRepo())
        key = "replay-op"

        first = await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="UBUNTU",
            image_label="Ubuntu",
            idempotency_key=key,
        )
        second = await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="UBUNTU",
            image_label="Ubuntu",
            idempotency_key=key,
        )

        assert first.replayed is False
        assert second.replayed is True
        assert second.server.id == first.server.id
        assert len(sink.of_type(BusinessEventType.PURCHASE_REQUESTED)) == 1

    async def test_provider_acceptance_emits_one_provider_accepted(self) -> None:
        offers = await _offers()
        cloud = FakeHourlyAdapter()
        sink = FakeEventSink()
        service, ops = _hourly(offers, cloud, sink=sink, users=FakeUserRepo())
        server = await _requested(service, offers)
        cloud.create_result = _accepted_response(server)

        assert await service.process_server(server.id) == "provisioned"

        assert len(sink.of_type(BusinessEventType.PROVIDER_ACCEPTED)) == 1
        payload = sink.payloads(BusinessEventType.PROVIDER_ACCEPTED)[0]
        assert payload["kind"] == "hourly"
        assert payload["provider"] == PROVIDER
        assert payload["credential_account"] == ACCOUNT
        assert payload["provider_order_id"] == "lsw-created"
        assert payload["plan"] == "lsw.mini"
        assert payload["location"] == "eu-west-3"
        assert payload["image"] == "UBUNTU"
        assert payload["operation_key"] == f"server-create:{server.id}"
        assert payload["provider_cost"] == format_minor(2, "EUR")
        assert ops.ops[f"server-create:{server.id}"].status.value == "completed"

    async def test_a_disabled_logger_changes_nothing_about_the_hourly_flow(self) -> None:
        """The null sink is the disabled feed: no rows, identical outcome."""
        offers = await _offers()
        cloud = FakeHourlyAdapter()
        service, ops = _hourly(offers, cloud, sink=NullBusinessEventSink())
        server = await _requested(service, offers)
        cloud.create_result = _accepted_response(server)

        assert await service.process_server(server.id) == "provisioned"
        assert await service.process_server(server.id) == "skipped"
        assert cloud.posts == 1
        assert ops.ops[f"server-create:{server.id}"].status.value == "completed"

        # A service built without any sink at all behaves the same: the
        # ambiguous path still records OUTCOME_UNKNOWN and never re-POSTs.
        offers2 = await _offers()
        service2, ops2 = _hourly(offers2, FakeHourlyAdapter())
        server2 = await _requested(service2, offers2)
        assert await service2.process_server(server2.id) == "outcome-unknown"
        assert ops2.ops[f"server-create:{server2.id}"].status.value == "outcome_unknown"


class TestFailureCards:
    """Definitive failures and ambiguous outcomes each get ONE triage card."""

    async def test_pc2031_capacity_refusal_keeps_code_and_correlation(self) -> None:
        offers = await _offers()
        cloud = FakeHourlyAdapter()
        sink = FakeEventSink()
        capacity = FakeCapacityRepo()
        service, _ = _hourly(offers, cloud, sink=sink, users=FakeUserRepo(), capacity=capacity)
        server = await _requested(service, offers)
        cloud.create_error = _CapacityRefusal(
            "PC-2031 Customer limit reached",
            error_code="PC-2031",
            correlation_id=CORRELATION_ID,
        )

        assert await service.process_server(server.id) == "failed"

        failures = sink.of_type(BusinessEventType.PURCHASE_FAILED)
        assert len(failures) == 1
        assert failures[0].event_key == f"purchase.failed:{server.id}:provider_capacity"
        payload = dict(failures[0].payload)
        assert payload["category"] == "provider_capacity"
        assert payload["stage"] == "provider_create"
        assert payload["error_code"] == "PC-2031"
        assert payload["correlation_id"] == CORRELATION_ID
        assert payload["credential_account"] == ACCOUNT
        assert payload["plan"] == "lsw.mini"
        assert payload["location"] == "eu-west-3"
        assert payload["server_id"] == str(server.id)
        # The durable capacity signal keeps the same evidence.
        assert capacity.writes[0]["error_code"] == "PC-2031"
        assert capacity.writes[0]["correlation_id"] == CORRELATION_ID

    async def test_a_definitive_provider_rejection_is_categorized(self) -> None:
        offers = await _offers()
        cloud = FakeHourlyAdapter()
        sink = FakeEventSink()
        service, _ = _hourly(offers, cloud, sink=sink, users=FakeUserRepo())
        server = await _requested(service, offers)
        cloud.create_error = ProviderError("provider rejected: invalid rootDiskSize")

        assert await service.process_server(server.id) == "failed"

        failures = sink.of_type(BusinessEventType.PURCHASE_FAILED)
        assert len(failures) == 1
        assert dict(failures[0].payload)["category"] == "provider_rejected"
        assert failures[0].event_key == f"purchase.failed:{server.id}:provider_rejected"

    async def test_an_ambiguous_outcome_is_flagged_once_for_review(self) -> None:
        offers = await _offers()
        cloud = FakeHourlyAdapter()
        sink = FakeEventSink()
        service, ops = _hourly(offers, cloud, sink=sink, users=FakeUserRepo())
        server = await _requested(service, offers)
        cloud.create_error = _Ambiguous("read timeout after the POST was sent")

        assert await service.process_server(server.id) == "outcome-unknown"

        failures = sink.of_type(BusinessEventType.PURCHASE_FAILED)
        assert len(failures) == 1
        assert failures[0].event_key == f"purchase.failed:{server.id}:outcome_unknown"
        assert dict(failures[0].payload)["category"] == "outcome_unknown"
        assert ops.ops[f"server-create:{server.id}"].status.value == "outcome_unknown"

    async def test_repeated_worker_and_reconcile_passes_never_repeat_the_card(self) -> None:
        offers = await _offers()
        cloud = FakeHourlyAdapter()
        sink = FakeEventSink()
        service, _ = _hourly(offers, cloud, sink=sink, users=FakeUserRepo())
        server = await _requested(service, offers)
        cloud.create_error = _Ambiguous("connection dropped mid-request")
        assert await service.process_server(server.id) == "outcome-unknown"

        # The worker keeps ticking and the reconciler keeps sweeping: the
        # deterministic key means the operator sees exactly one warning.
        assert await service.process_server(server.id) == "skipped"
        assert await service.reconcile_server(server.id) == "still-unknown"
        assert await service.reconcile_server(server.id) == "still-unknown"

        assert len(sink.of_type(BusinessEventType.PURCHASE_FAILED)) == 1


class TestRecoveryAndActivation:
    """Read-only recovery attaches the provider id; activation is the last step."""

    async def test_read_only_recovery_emits_provider_accepted_exactly_once(self) -> None:
        offers = await _offers()
        cloud = FakeHourlyAdapter()
        sink = FakeEventSink()
        service, _ = _hourly(offers, cloud, sink=sink, users=FakeUserRepo())
        server = await _requested(service, offers)
        cloud.create_error = _Ambiguous("read timeout")
        assert await service.process_server(server.id) == "outcome-unknown"

        # A reference lookup proves the earlier POST landed (read-only).
        cloud.find_result = _accepted_response(server)
        assert await service.reconcile_server(server.id) == "attached"
        # Re-running the recovery path is a no-op for the channel.
        await service.reconcile_server(server.id)

        accepted = sink.of_type(BusinessEventType.PROVIDER_ACCEPTED)
        assert len(accepted) == 1
        assert accepted[0].event_key == f"purchase.provider_accepted:{server.id}"
        payload = dict(accepted[0].payload)
        assert payload["provider_order_id"] == "lsw-created"
        assert payload["credential_account"] == ACCOUNT

    async def test_final_activation_emits_vps_provisioned_once(self) -> None:
        """The state reconciler owns PROVISIONING -> RUNNING for hourly."""
        sink = FakeEventSink()
        server = dataclasses.replace(
            _reconciler_server(ServerLifecycleState.PROVISIONING, provider_key=PROVIDER),
            billing_model=BILLING_MODEL_HOURLY,
            credential_account_id=ACCOUNT,
            image_id="UBUNTU",
            ipv4="198.51.100.7",
            offer_fingerprint={"product_id": "lsw.mini", "location_id": "eu-west-3"},
        )
        registry = ProviderRegistry()
        registry.register(_HourlyCloudProvider("running"))  # type: ignore[arg-type]
        reconciler = ServerStateReconciler(
            server_repo=ReconcilerServerRepo([server]),  # type: ignore[arg-type]
            provider_registry=registry,
            audit_repo=_AuditStub(),  # type: ignore[arg-type]
            event_sink=sink,
            user_repo=FakeUserRepo(),
        )

        counts = await reconciler.reconcile()

        assert counts == {StateReconciliationOutcome.REPAIRED: 1}
        assert server.state is ServerLifecycleState.RUNNING
        provisioned = sink.of_type(BusinessEventType.VPS_PROVISIONED)
        assert len(provisioned) == 1
        assert provisioned[0].event_key == f"purchase.vps_provisioned:{server.id}"
        payload = dict(provisioned[0].payload)
        assert payload["kind"] == "hourly"
        assert payload["ipv4"] == "198.51.100.7"
        assert payload["plan"] == "lsw.mini"
        assert payload["location"] == "eu-west-3"
        assert payload["credential_account"] == ACCOUNT
        # A later pass finds the server consistent: still exactly one card.
        assert await reconciler.reconcile() == {StateReconciliationOutcome.CONSISTENT: 1}
        assert len(sink.of_type(BusinessEventType.VPS_PROVISIONED)) == 1


class TestCardContent:
    """What the operator actually reads in the private channel."""

    def test_capacity_card_carries_the_operator_labels(self) -> None:
        event = purchase_failed_event(
            user=USER,
            server_id=uuid4(),
            provider_key=PROVIDER,
            category="provider_capacity",
            reason="PC-2031 Customer limit reached",
            stage="provider_create",
            credential_account=ACCOUNT,
            plan_name="lsw.m4.large",
            location_id="eu-central-1",
            error_code="PC-2031",
            correlation_id=CORRELATION_ID,
            image_id="UBUNTU_24_04",
            kind="hourly",
        )
        text = render_event(event.event_type, event.payload)

        assert text.splitlines()[0] == "🚧 ظرفیت حساب پروایدر برای ساخت سرور جدید تکمیل است"
        assert f"حساب پروایدر: {ACCOUNT}" in text
        assert "مرحله: provider_create" in text
        assert "کد خطا: PC-2031" in text
        assert f"Correlation ID: {CORRELATION_ID}" in text
        assert "دسته خطا: provider_capacity" in text

    def test_outcome_unknown_card_warns_against_a_blind_retry(self) -> None:
        event = purchase_failed_event(
            user=USER,
            server_id=uuid4(),
            provider_key=PROVIDER,
            category="outcome_unknown",
            reason="read timeout after the POST was sent",
            stage="provider_create",
            kind="hourly",
        )
        text = render_event(event.event_type, event.payload)

        assert text.splitlines()[0].startswith("⚠️ نتیجه ساخت سرور نامشخص است")
        # The advisory is the operational point of the card.
        assert "از تلاش مجدد (retry) خودداری کنید" in text


# ---------------------------------------------------------------------------
# Delivery visibility: a broken Telegram call is never silent
# ---------------------------------------------------------------------------


class _TrackingRepo:
    """Minimal outbox double that records the retry/abandon decisions."""

    def __init__(self, event_type: str, *, attempts: int = 0) -> None:
        self.row = FakeRecord(
            event_key="purchase.failed:srv:outcome_unknown",
            event_type=event_type,
            attempts=attempts,
            payload={"category": "outcome_unknown"},
        )
        self.claim_count = 0
        self.retried: list[tuple[str, datetime]] = []
        self.abandoned: list[str] = []

    async def claim_due(self, *, limit: int, now: datetime, stale_after_seconds: int) -> list[Any]:
        if self.abandoned:
            return []
        self.claim_count += 1
        self.row.attempts += 1
        self.row.status = "SENDING"
        return [self.row]

    async def mark_sent(self, event_key: str, *, at: datetime) -> None:  # pragma: no cover
        raise AssertionError("a failing channel must never be marked sent")

    async def mark_retry(self, event_key: str, *, error: str, next_attempt_at: datetime) -> None:
        self.retried.append((event_key, next_attempt_at))

    async def mark_abandoned(self, event_key: str, *, error: str) -> None:
        self.abandoned.append(event_key)


class _FailingChannel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, text: str) -> None:
        self.calls += 1
        raise RuntimeError("telegram 400 Bad Request: token=123:SECRET chat not found")


class TestDeliveryVisibility:
    async def test_a_telegram_error_is_logged_and_retried_with_backoff(self, caplog: Any) -> None:
        repo = _TrackingRepo(BusinessEventType.PURCHASE_FAILED.value)
        channel = _FailingChannel()
        dispatcher = BusinessLogDispatcher(repo, channel, _policy(backoff_base_seconds=30))
        before = datetime.now(UTC)

        with caplog.at_level(logging.WARNING, logger="cloud_platform.modules.businesslog.domain"):
            report = await dispatcher.deliver()

        assert report.retried == 1
        key, next_attempt_at = repo.retried[0]
        assert key == repo.row.event_key
        # next_attempt_at is populated with the first backoff step.
        assert before + timedelta(seconds=30) <= next_attempt_at <= before + timedelta(seconds=90)
        logged = caplog.text
        assert repo.row.event_key in logged
        assert "attempt=1" in logged
        assert "next_attempt_at=" in logged
        assert "RuntimeError" in logged
        # Logged and stored diagnostics are redacted: never a token.
        assert "123:SECRET" not in logged
        assert "***REDACTED***" in logged

    async def test_the_attempt_cap_abandons_once_and_stops(self, caplog: Any) -> None:
        repo = _TrackingRepo(BusinessEventType.PURCHASE_FAILED.value)
        channel = _FailingChannel()
        dispatcher = BusinessLogDispatcher(repo, channel, _policy(max_attempts=2))

        with caplog.at_level(logging.ERROR, logger="cloud_platform.modules.businesslog.domain"):
            reports = [await dispatcher.deliver() for _ in range(4)]

        assert [r.retried for r in reports] == [1, 1, 0, 0]
        assert [r.abandoned for r in reports] == [0, 0, 1, 0]
        assert repo.abandoned == [repo.row.event_key]
        assert repo.claim_count == 3
        assert "ABANDONED" in caplog.text
        assert repo.row.event_key in caplog.text
        # A broken channel can never flood: nothing is retried after the cap.
        assert channel.calls == 2


def test_policy_defaults_are_documented_for_the_hourly_feed() -> None:
    """A cheap guard: the categories this module relies on stay testable."""
    policy = BusinessLogPolicy(enabled=True, chat_id=-100)
    assert policy.allows(BusinessEventType.PURCHASE_REQUESTED)
    assert policy.allows(BusinessEventType.PROVIDER_ACCEPTED)
    assert policy.allows(BusinessEventType.PURCHASE_FAILED)
    assert policy.allows(BusinessEventType.VPS_PROVISIONED)
