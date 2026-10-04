"""Tests for the payment reconciliation job (M09-007): stuck sessions rechecked safely."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus
from cloud_platform.modules.payments.reconcile import PaymentReconciliationService
from cloud_platform.providers.base import PaymentIntent, PaymentStatus


@dataclass
class FakePaymentsRepo:
    sessions: list[PaymentSession]

    async def list_stuck_pending(self, cutoff: datetime) -> list[PaymentSession]:
        return [s for s in self.sessions if s.status is PaymentSessionStatus.PENDING]


class FakeWebhook:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def process_callback(self, **kw: Any) -> Any:
        self.calls.append(kw)

        @dataclass(frozen=True, slots=True)
        class Outcome:
            action: Any

        @dataclass(frozen=True, slots=True)
        class Action:
            value: str = "credited"

        return Outcome(
            action=Action(value="credited" if kw["status"] == "succeeded" else "failed_recorded")
        )


class FakeGateway:
    key = "zarinpal"

    def __init__(self, status: PaymentStatus) -> None:
        self._status = status

    async def verify_with_amount(self, authority: str, amount_minor: int) -> PaymentIntent:
        return PaymentIntent(
            gateway_payment_id=authority,
            status=self._status,
            amount_minor=amount_minor,
            currency="IRR",
        )


class RecordingAuditRepo:
    """Stands in for ``AuditRepository``: it only accepts an ``AuditEvent``.

    A keyword-argument call (the previous production bug) raises ``TypeError``
    here exactly as the real repository does.
    """

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


def _pending(external_id: str | None = "A0001") -> PaymentSession:
    return PaymentSession(
        user_id=uuid4(),
        gateway_key="zarinpal",
        amount_minor=50000,
        currency="IRR",
        idempotency_key=f"reconcile-test-{external_id or 'none'}",
        gateway_payment_id=external_id,
    )


class TestReconciliation:
    async def test_run_appends_one_system_audit_event(self) -> None:
        from cloud_platform.modules.audit.domain import ActorType, AuditEvent

        audit = RecordingAuditRepo()
        service = PaymentReconciliationService(
            payments_repo=FakePaymentsRepo([_pending()]),
            webhook_service=FakeWebhook(),
            gateway=FakeGateway(PaymentStatus.SUCCEEDED),
            audit_repo=audit,
        )
        report = await service.run(now=datetime.now(UTC))
        assert report.credited == 1
        (event,) = audit.events
        # The audit port persists an EVENT, never loose keyword fields.
        assert isinstance(event, AuditEvent)
        assert event.actor_type is ActorType.SYSTEM
        assert event.action == "payments.reconcile"
        assert event.resource_type == "payment"
        assert event.metadata == {"checked": 1, "credited": 1, "failed": 0}

    async def test_succeeded_verify_credits_via_webhook(self) -> None:
        webhook = FakeWebhook()
        service = PaymentReconciliationService(
            payments_repo=FakePaymentsRepo([_pending()]),
            webhook_service=webhook,
            gateway=FakeGateway(PaymentStatus.SUCCEEDED),
        )
        report = await service.run(now=datetime.now(UTC))
        assert report.credited == 1
        assert webhook.calls[0]["status"] == "succeeded"

    async def test_failed_verify_marks_failed(self) -> None:
        webhook = FakeWebhook()
        service = PaymentReconciliationService(
            payments_repo=FakePaymentsRepo([_pending()]),
            webhook_service=webhook,
            gateway=FakeGateway(PaymentStatus.FAILED),
        )
        report = await service.run(now=datetime.now(UTC))
        assert report.marked_failed == 1

    async def test_session_without_external_id_skipped(self) -> None:
        service = PaymentReconciliationService(
            payments_repo=FakePaymentsRepo([_pending(None)]),
            webhook_service=FakeWebhook(),
            gateway=FakeGateway(PaymentStatus.SUCCEEDED),
        )
        report = await service.run(now=datetime.now(UTC))
        assert report.skipped == 1
        assert report.checked == 0

    async def test_gateway_error_counted_not_raised(self) -> None:
        class BoomGateway(FakeGateway):
            async def verify_with_amount(self, authority: str, amount_minor: int) -> PaymentIntent:
                raise RuntimeError("gateway down")

        service = PaymentReconciliationService(
            payments_repo=FakePaymentsRepo([_pending()]),
            webhook_service=FakeWebhook(),
            gateway=BoomGateway(PaymentStatus.SUCCEEDED),
        )
        report = await service.run(now=datetime.now(UTC))
        assert report.errors == 1


async def test_legacy_pending_scan_does_not_verify_other_gateways() -> None:
    from dataclasses import replace

    own = _pending()
    unrelated = replace(_pending("other-66"), gateway_key="atlaspay", currency="IRT")
    webhook = FakeWebhook()
    service = PaymentReconciliationService(
        payments_repo=FakePaymentsRepo([own, unrelated]),
        webhook_service=webhook,
        gateway=FakeGateway(PaymentStatus.SUCCEEDED),
    )
    report = await service.run(now=datetime.now(UTC))
    assert report.checked == report.credited == 1
    assert len(webhook.calls) == 1
    assert webhook.calls[0]["gateway_key"] == "zarinpal"


async def test_unbound_atlas_attempts_do_not_starve_bounded_pending_scan() -> None:
    """Execute the production SQL predicate before LIMIT, then settle real invoices."""
    from dataclasses import replace
    from datetime import timedelta
    from types import SimpleNamespace

    import httpx
    from sqlalchemy import JSON, MetaData, create_engine
    from sqlalchemy.orm import Session

    from cloud_platform.db.base import PaymentSession as PaymentRow
    from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
    from cloud_platform.modules.payments.service import PaymentWebhookService
    from cloud_platform.providers.atlaspay import AtlasPayGateway

    # SQLite executes the real repository queries without a network database.
    # Only the copied DDL's PostgreSQL-specific JSON type/default are adapted.
    engine = create_engine("sqlite://")
    table = PaymentRow.__table__.to_metadata(MetaData())
    table.c.payment_details.type = JSON()
    table.c.id.server_default = None
    table.create(engine)

    class LocalSqlSession:
        def __init__(self) -> None:
            self.session = Session(engine, expire_on_commit=False)

        async def __aenter__(self) -> LocalSqlSession:
            return self

        async def __aexit__(self, *args: Any) -> None:
            self.session.close()

        async def execute(self, statement: Any) -> Any:
            return self.session.execute(statement)

        async def commit(self) -> None:
            self.session.commit()

        async def refresh(self, row: Any) -> None:
            self.session.refresh(row)

    class LocalWallet:
        def __init__(self) -> None:
            self.deposits: dict[str, tuple[Any, int]] = {}

        async def credit_deposit(self, owner: Any, amount: int, key: str, **kwargs: Any) -> Any:
            applied = key not in self.deposits
            if applied:
                self.deposits[key] = (owner, amount)
            balance = sum(value for _, value in self.deposits.values())
            return SimpleNamespace(balance=balance), applied

    cutoff = datetime(2026, 8, 6, tzinfo=UTC)
    owner = uuid4()
    unbound_ids = [uuid4() for _ in range(101)]
    bound_ids = [uuid4() for _ in range(3)]
    other_gateway_id, fresh_id, failed_id = uuid4(), uuid4(), uuid4()
    rows = []
    for index, session_id in enumerate(
        unbound_ids + bound_ids + [other_gateway_id, fresh_id, failed_id]
    ):
        rows.append(
            {
                "id": session_id,
                "user_id": owner,
                "gateway_key": "zarinpal" if session_id == other_gateway_id else "atlaspay",
                "gateway_payment_id": None if session_id in unbound_ids else str(index + 1),
                "amount_minor": 259739,
                "currency": "IRT",
                "status": "failed" if session_id == failed_id else "pending",
                "idempotency_key": f"bounded-atlas-attempt-{index}",
                "credit_amount_minor": 250,
                "credit_currency": "EUR",
                "payment_details": {
                    "tracking_code": "5c23c12c9fa0c8b3",  # pragma: allowlist secret -- fixture
                },
                "created_at": (
                    cutoff + timedelta(seconds=1)
                    if session_id == fresh_id
                    else cutoff - timedelta(hours=2) + timedelta(seconds=index)
                ).replace(tzinfo=None),
                "updated_at": cutoff.replace(tzinfo=None),
            }
        )
    with engine.begin() as connection:
        connection.execute(table.insert(), rows)

    eligible = {row["gateway_payment_id"]: row for row in rows if row["id"] in bound_ids}
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        external_id = request.url.path.split("/")[-2]
        row = eligible[external_id]
        return httpx.Response(
            200,
            json={
                "success": True,
                "id": int(external_id),
                "trackingCode": row["payment_details"]["tracking_code"],
                "merchantOrderRef": row["idempotency_key"],
                "status": "confirmed",
                "paid": True,
                "totalAmountToman": 259739,
                "actualReceivedAmountToman": None,
                "requiresManualDelivery": False,
                "createdAt": "2026-08-05T21:38:56.329Z",
            },
        )

    gateway = AtlasPayGateway("fixture-only-key", transport=httpx.MockTransport(respond))
    repo = SqlAlchemyPaymentSessionRepository(LocalSqlSession)
    wallet = LocalWallet()
    try:
        bounded = await repo.list_pending_before("atlaspay", cutoff, limit=2)
        assert [session.id for session in bounded] == bound_ids[:2]
        assert len(bounded) == 2
        snapshots = {session_id: await repo.get(session_id) for session_id in bound_ids}
        reconciler = PaymentReconciliationService(
            payments_repo=repo,
            webhook_service=PaymentWebhookService(repo, wallet, None),
            gateway=gateway,
            stale_after=timedelta(seconds=0),
        )
        first = await reconciler.run(now=cutoff)
        second = await reconciler.run(now=cutoff)
        assert first.checked == first.credited == 3
        assert first.errors == first.skipped == first.marked_failed == 0
        assert second.checked == second.credited == 0
        assert len(requests) == 3
        assert wallet.deposits == {
            f"deposit-atlaspay-{external_id}": (owner, 250) for external_id in eligible
        }
        for session_id, snapshot in snapshots.items():
            settled = await repo.get(session_id)
            assert settled.status is PaymentSessionStatus.SUCCEEDED
            assert settled.credited_at is not None
            assert (
                replace(
                    settled,
                    status=snapshot.status,
                    credited_at=None,
                    updated_at=snapshot.updated_at,
                )
                == snapshot
            )
        assert await repo.list_pending_before("atlaspay", cutoff, limit=2) == []
        for session_id in unbound_ids:
            pending = await repo.get(session_id)
            assert pending.status is PaymentSessionStatus.PENDING
            assert pending.gateway_payment_id is None
            assert pending.credited_at is None
        assert (await repo.get(other_gateway_id)).status is PaymentSessionStatus.PENDING
        assert (await repo.get(fresh_id)).status is PaymentSessionStatus.PENDING
        assert (await repo.get(failed_id)).status is PaymentSessionStatus.FAILED
    finally:
        await gateway.close()
        engine.dispose()
