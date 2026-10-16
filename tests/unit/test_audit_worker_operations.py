"""Worker failure boundaries with no external services or provider mutations."""

from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from arq import Retry

import cloud_platform.worker.settings as ws
from cloud_platform.modules.billing.service import BillingLockBusyError
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus
from cloud_platform.modules.wallet.domain import InsufficientBalanceError
from tests.unit.test_atlaspay_gateway import INQUIRY, MemoryPayments, MemoryWallet

SECRET = "worker-provider-credential-must-not-be-logged"  # pragma: allowlist secret
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


@pytest.fixture
def gateway_worker(monkeypatch):
    """Keep production inquiry/reconciliation/webhook code and replace only I/O."""
    settings = SimpleNamespace(
        app_env="test",
        atlaspay_enabled=False,
        atlaspay_api_key="fixture-key",  # pragma: allowlist secret -- synthetic transport
        atlaspay_base_url="https://atlaspay.invalid/api/v1",
        atlaspay_timeout_seconds=1,
        tetraminator_enabled=False,
        tetraminator_api_key="fixture-key",  # pragma: allowlist secret -- synthetic transport
        tetraminator_base_url="https://tetraminator.invalid/api/v1",
        tetraminator_callback_url="https://callback.invalid/tetra",
        tetraminator_timeout_seconds=1,
    )
    payments, wallet = MemoryPayments(), MemoryWallet()
    audit = SimpleNamespace(append=AsyncMock())
    container = SimpleNamespace(
        session_factory=object(),
        wallet_repository=lambda: wallet,
        ledger_repository=lambda: SimpleNamespace(),
        audit_repository=lambda: audit,
        business_event_sink=lambda: None,
        user_repository=lambda: None,
        close=AsyncMock(),
    )
    factory = MagicMock(return_value=container)
    monkeypatch.setattr("cloud_platform.core.container.create_container", factory)
    monkeypatch.setattr("cloud_platform.core.config.get_settings", lambda: settings)
    monkeypatch.setattr(ws, "get_settings", lambda: settings)
    monkeypatch.setattr(
        "cloud_platform.modules.payments.repository.SqlAlchemyPaymentSessionRepository",
        lambda _: payments,
    )
    monkeypatch.setattr(
        "cloud_platform.modules.wallet.repository.SqlAlchemyWalletRepository", lambda _: wallet
    )
    monkeypatch.setattr(
        "cloud_platform.modules.wallet.repository.SqlAlchemyLedgerRepository",
        lambda _: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "cloud_platform.modules.audit.repository.SqlAlchemyAuditRepository", lambda _: audit
    )
    clients, requests = [], []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if "atlaspay" in request.url.host:
            assert request.method == "POST" and request.url.path.endswith("/verify")
            return httpx.Response(
                200,
                json={
                    **INQUIRY,
                    "status": "confirmed",
                    "paid": True,
                    "actualReceivedAmountToman": INQUIRY["totalAmountToman"],
                },
            )
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "status": True,
                "payment_status": "paid",
                "pay_id": "tetra-invoice-66",
                "amount": 500_000,
            },
        )

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        client = real_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return SimpleNamespace(
        settings=settings,
        payments=payments,
        wallet=wallet,
        audit=audit,
        container=container,
        factory=factory,
        clients=clients,
        requests=requests,
    )


@pytest.mark.parametrize("gateway", ["atlaspay", "tetraminator"])
async def test_disabled_gateway_worker_settles_existing_invoice_exactly_once(
    gateway, gateway_worker
):
    env = gateway_worker
    atlas = gateway == "atlaspay"
    amount = INQUIRY["totalAmountToman"] if atlas else 500_000
    session = await env.payments.create(
        PaymentSession(
            user_id=uuid4(),
            gateway_key=gateway,
            currency="IRT",
            amount_minor=amount,
            idempotency_key=INQUIRY["merchantOrderRef"] if atlas else "tetra-existing-attempt",
            gateway_payment_id="66" if atlas else "tetra-invoice-66",
        )
    )
    job = ws.reconcile_atlaspay_payments if atlas else ws.reconcile_tetraminator_payments
    await job({})
    await job({})
    saved = await env.payments.get(session.id)
    assert saved.status is PaymentSessionStatus.SUCCEEDED
    assert saved.credited_at is not None
    assert env.wallet.balance == amount
    assert len(env.wallet.deposits) == 1
    assert len(env.requests) == 1
    assert all(client.is_closed for client in env.clients)
    assert env.audit.append.await_count == 2
    if atlas:
        assert env.container.close.await_count == 2


@pytest.mark.parametrize("gateway", ["atlaspay", "tetraminator"])
async def test_gateway_worker_closes_client_on_reconciliation_failure(
    gateway, gateway_worker, monkeypatch
):
    error = RuntimeError("repository scan unavailable")
    if gateway == "atlaspay":
        monkeypatch.setattr(
            "cloud_platform.modules.payments.reconcile.PaymentReconciliationService.run",
            AsyncMock(side_effect=error),
        )
        job = ws.reconcile_atlaspay_payments
    else:
        monkeypatch.setattr(
            "cloud_platform.modules.payments.reconcile.reconcile_tetraminator_pending",
            AsyncMock(side_effect=error),
        )
        job = ws.reconcile_tetraminator_payments
    with pytest.raises(RuntimeError):
        await job({})
    assert len(gateway_worker.clients) == 1
    assert gateway_worker.clients[0].is_closed
    if gateway == "atlaspay":
        gateway_worker.container.close.assert_awaited_once()


async def test_atlaspay_constructor_rejection_does_not_leak_container(gateway_worker):
    env = gateway_worker
    env.settings.atlaspay_base_url = "http://insecure.invalid/api/v1"
    with pytest.raises(ValueError):
        await ws.reconcile_atlaspay_payments({})
    assert not env.clients
    if env.factory.called:
        env.container.close.assert_awaited_once()


async def test_atlaspay_container_closes_even_when_gateway_close_fails(gateway_worker, monkeypatch):
    async def close_then_fail(gateway):
        await gateway._client.aclose()
        raise RuntimeError("transport close failed")

    monkeypatch.setattr(
        "cloud_platform.providers.atlaspay.client.AtlasPayGateway.close", close_then_fail
    )
    with pytest.raises(RuntimeError):
        await ws.reconcile_atlaspay_payments({})
    gateway_worker.container.close.assert_awaited_once()
    assert gateway_worker.clients[0].is_closed


@pytest.mark.parametrize("phase", ["create", "reconcile"])
async def test_hourly_worker_continues_items_then_reports_redacted_failure(
    phase, monkeypatch, caplog
):
    ids = [uuid4(), uuid4(), uuid4()]
    failures = [RuntimeError(SECRET), TimeoutError(SECRET)]
    process = AsyncMock(side_effect=[failures[0], "completed", failures[1]])
    service = SimpleNamespace(
        servers_requested=AsyncMock(return_value=[SimpleNamespace(id=i) for i in ids]),
        servers_for_reconcile=AsyncMock(return_value=[SimpleNamespace(id=i) for i in ids]),
        process_server=process,
        reconcile_server=process,
    )
    legacy = SimpleNamespace(run_once=AsyncMock(return_value={"provisioned": 1}))
    container = SimpleNamespace(
        initialize=AsyncMock(),
        close=AsyncMock(),
        hourly_cloud_service=lambda: service,
        provisioning_worker=lambda: legacy,
    )
    monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
    job = ws.process_cloud_creates if phase == "create" else ws.reconcile_cloud_creates
    with caplog.at_level(logging.WARNING, logger="cloud_platform.worker.settings"):
        with pytest.raises(ExceptionGroup) as raised:
            await job({})
    assert [call.args[0] for call in process.await_args_list] == ids
    assert len(raised.value.exceptions) == 2
    diagnostic = "".join(traceback.format_exception(raised.value))
    assert SECRET not in caplog.text + diagnostic
    assert "RuntimeError" in diagnostic and "TimeoutError" in diagnostic
    if phase == "create":
        legacy.run_once.assert_awaited_once_with(limit=10)
    container.close.assert_awaited_once()


@pytest.mark.parametrize("failure_count", [1, 2])
async def test_cloud_create_phase_failure_still_runs_legacy_queue(
    failure_count, monkeypatch, caplog
):
    service = SimpleNamespace(servers_requested=AsyncMock(side_effect=RuntimeError(SECRET)))
    legacy = SimpleNamespace(
        run_once=AsyncMock(
            side_effect=TimeoutError(SECRET) if failure_count == 2 else None,
            return_value={"provisioned": 1},
        )
    )
    container = SimpleNamespace(
        initialize=AsyncMock(),
        close=AsyncMock(),
        hourly_cloud_service=lambda: service,
        provisioning_worker=lambda: legacy,
    )
    monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
    with caplog.at_level(logging.WARNING, logger="cloud_platform.worker.settings"):
        with pytest.raises(ExceptionGroup) as raised:
            await ws.process_cloud_creates({})
    legacy.run_once.assert_awaited_once_with(limit=10)
    container.close.assert_awaited_once()
    assert len(raised.value.exceptions) == failure_count
    assert SECRET not in caplog.text + "".join(traceback.format_exception(raised.value))


async def test_resource_reconciler_failure_does_not_skip_state_activation(monkeypatch, caplog):
    create = SimpleNamespace(reconcile=AsyncMock(side_effect=TimeoutError(SECRET)))
    state = SimpleNamespace(reconcile=AsyncMock(return_value={"running": 1}))
    container = SimpleNamespace(
        initialize=AsyncMock(),
        close=AsyncMock(),
        provider_registry=object(),
        server_repository=lambda: object(),
        audit_repository=lambda: object(),
        wallet_repository=lambda: object(),
        create_account_attempt_repository=lambda: object(),
        hourly_cloud_service=lambda: SimpleNamespace(capture_first_hour=AsyncMock()),
        business_event_sink=lambda: object(),
        user_repository=lambda: object(),
        customer_provisioned_sink=lambda: object(),
        accrual_job=lambda: SimpleNamespace(prepay_server=AsyncMock()),
    )
    monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
    monkeypatch.setattr(
        "cloud_platform.modules.operations.service.CreateTimeoutReconciler", lambda **kw: create
    )
    monkeypatch.setattr(
        "cloud_platform.modules.operations.service.ServerStateReconciler", lambda **kw: state
    )
    with caplog.at_level(logging.WARNING, logger="cloud_platform.worker.settings"):
        with pytest.raises(ExceptionGroup) as raised:
            await ws.reconcile_provider_resources({})
    create.reconcile.assert_awaited_once()
    state.reconcile.assert_awaited_once()
    container.close.assert_awaited_once()
    assert SECRET not in caplog.text + "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("shortage", ["locked", "funds_before_expiry", "funds_at_expiry"])
async def test_hourly_renewal_retry_preserves_paid_time_and_closes_container(shortage, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    boundary = NOW + timedelta(seconds=5) if shortage == "funds_before_expiry" else NOW
    server = CloudServer(
        id=uuid4(),
        user_id=uuid4(),
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.RUNNING,
        last_accrued_at=boundary,
    )
    prepay = AsyncMock(
        side_effect=(BillingLockBusyError() if shortage == "locked" else InsufficientBalanceError())
    )
    container = SimpleNamespace(
        server_repository=lambda: SimpleNamespace(get=AsyncMock(return_value=server)),
        accrual_job=lambda: SimpleNamespace(prepay_server=prepay),
        close=AsyncMock(),
    )
    redis = SimpleNamespace(enqueue_job=AsyncMock())
    reconcile, delete = AsyncMock(), AsyncMock()
    monkeypatch.setattr(ws, "datetime", FrozenDateTime)
    monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
    monkeypatch.setattr(ws, "reconcile_deletes", reconcile)
    monkeypatch.setattr(ws, "process_deletes", delete)
    if shortage == "funds_at_expiry":
        await ws.renew_hourly_coverage({"redis": redis}, str(server.id))
        reconcile.assert_awaited_once()
        delete.assert_awaited_once()
    else:
        with pytest.raises(Retry) as raised:
            await ws.renew_hourly_coverage({"redis": redis}, str(server.id))
        assert raised.value.defer_score == (250 if shortage == "locked" else 5_000)
        reconcile.assert_not_awaited()
        delete.assert_not_awaited()
    assert server.last_accrued_at == boundary
    redis.enqueue_job.assert_not_awaited()
    container.close.assert_awaited_once()


async def test_successful_hourly_renewal_schedules_exact_next_paid_boundary(monkeypatch):
    server = CloudServer(
        id=uuid4(),
        user_id=uuid4(),
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.RUNNING,
        last_accrued_at=NOW,
    )
    next_boundary = NOW + timedelta(hours=1, seconds=17)

    async def prepay(row, *, renew_ahead_seconds):
        assert renew_ahead_seconds == 5
        row.last_accrued_at = next_boundary
        return row

    container = SimpleNamespace(
        server_repository=lambda: SimpleNamespace(get=AsyncMock(return_value=server)),
        accrual_job=lambda: SimpleNamespace(prepay_server=prepay),
        close=AsyncMock(),
    )
    redis = SimpleNamespace(enqueue_job=AsyncMock())
    monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
    await ws.renew_hourly_coverage({"redis": redis}, str(server.id))
    redis.enqueue_job.assert_awaited_once_with(
        "renew_hourly_coverage",
        str(server.id),
        _job_id=f"hourly-renew:{server.id}:{next_boundary.isoformat()}",
        _defer_until=next_boundary - timedelta(seconds=5),
    )
    container.close.assert_awaited_once()


@pytest.fixture
def prepaid_worker(monkeypatch):
    now = datetime.now(UTC)
    rows = {}
    balances = {}

    def add(*, model="hourly_prepaid_irt", state=ServerLifecycleState.RUNNING, paid=True):
        row = CloudServer(
            id=uuid4(),
            user_id=uuid4(),
            provider_key="hetzner",
            provider_account_id=uuid4(),
            state=state,
            billing_model=model,
            prepaid_paid_until=now + timedelta(hours=1) if paid else now - timedelta(hours=1),
        )
        rows[row.id] = row
        balances[row.user_id] = 300_000
        return row

    expired = [add(paid=False), add(paid=False)]
    covered = add()
    legacy = add(model="hourly", paid=False)
    stopped = add(state=ServerLifecycleState.STOPPED, paid=False)

    class Servers:
        async def list_running(self):
            return [row for row in rows.values() if row.state is ServerLifecycleState.RUNNING]

        async def list_stopped(self):
            return [row for row in rows.values() if row.state is ServerLifecycleState.STOPPED]

        async def get(self, server_id):
            return rows.get(server_id)

        async def save_prepaid_balance_markers(self, server_id, **facts):
            row = rows[server_id]
            row.low_balance_since = facts["warning_since"]
            row.prepaid_zero_since = facts["zero_since"]

    class Wallets:
        async def get(self, owner):
            return SimpleNamespace(currency="IRT", balance=balances[owner])

    class Lifecycle:
        def __init__(self):
            self.unpaid, self.deleted = [], []
            self.fail_unpaid, self.fail_delete = set(), set()

        async def enforce_unpaid(self, server_id):
            self.unpaid.append(server_id)
            if server_id in self.fail_unpaid:
                raise TimeoutError(SECRET)
            rows[server_id].transition_to(ServerLifecycleState.STOPPED)

        async def enforce(self, server_id):
            self.deleted.append(server_id)
            if server_id in self.fail_delete:
                raise RuntimeError(SECRET)
            rows[server_id].transition_to(ServerLifecycleState.DELETE_REQUESTED)

    settings = SimpleNamespace(
        fx_enabled=True, fx_domestic_enabled=True, fx_allow_usdt_proxy_for_settlement=True
    )
    lifecycle = Lifecycle()
    from cloud_platform.modules.billing.service import PrepaidHourlyRunReport

    billing = SimpleNamespace(run=AsyncMock(return_value=PrepaidHourlyRunReport()))
    servers, wallets = Servers(), Wallets()
    container = SimpleNamespace(
        initialize=AsyncMock(),
        close=AsyncMock(),
        server_repository=lambda: servers,
        wallet_repository=lambda: wallets,
        prepaid_hourly_billing_service=lambda: billing,
        prepaid_provider_lifecycle_service=lambda: lifecycle,
    )
    monkeypatch.setattr("cloud_platform.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
    monkeypatch.setattr(ws, "_telegram_bot_token", lambda: None)
    return SimpleNamespace(
        add=add,
        rows=rows,
        balances=balances,
        expired=expired,
        covered=covered,
        legacy=legacy,
        stopped=stopped,
        lifecycle=lifecycle,
        billing=billing,
        settings=settings,
        container=container,
        now=now,
    )


@pytest.mark.parametrize(
    "disabled", ["fx_enabled", "fx_domestic_enabled", "fx_allow_usdt_proxy_for_settlement"]
)
async def test_disabled_fx_still_enforces_expired_hourly_coverage_only(disabled, prepaid_worker):
    env = prepaid_worker
    setattr(env.settings, disabled, False)
    await ws.reconcile_prepaid_hourly({})
    env.billing.run.assert_awaited_once_with(customer_enabled=False)
    assert env.lifecycle.unpaid == [row.id for row in env.expired]
    assert all(row.state is ServerLifecycleState.STOPPED for row in env.expired)
    assert env.covered.state is env.legacy.state is ServerLifecycleState.RUNNING
    assert env.stopped.state is ServerLifecycleState.STOPPED
    assert not env.lifecycle.deleted
    env.container.close.assert_awaited_once()


async def test_unpaid_and_zero_balance_failures_continue_and_surface_redacted_group(
    prepaid_worker, caplog
):
    env = prepaid_worker
    env.settings.fx_enabled = False
    zero = [env.add(state=ServerLifecycleState.STOPPED, paid=False) for _ in range(2)]
    for row in zero:
        env.balances[row.user_id] = 0
        row.prepaid_zero_since = row.low_balance_since = env.now - timedelta(hours=25)
    env.lifecycle.fail_unpaid.add(env.expired[0].id)
    env.lifecycle.fail_delete.add(zero[0].id)
    with caplog.at_level(logging.WARNING, logger="cloud_platform.worker.settings"):
        with pytest.raises(ExceptionGroup) as raised:
            await ws.reconcile_prepaid_hourly({})
    assert env.lifecycle.unpaid == [row.id for row in env.expired]
    assert env.lifecycle.deleted == [row.id for row in zero]
    assert env.expired[0].state is ServerLifecycleState.RUNNING
    assert env.expired[1].state is ServerLifecycleState.STOPPED
    assert zero[0].state is ServerLifecycleState.STOPPED
    assert zero[1].state is ServerLifecycleState.DELETE_REQUESTED
    assert len(raised.value.exceptions) >= 2
    assert SECRET not in caplog.text + "".join(traceback.format_exception(raised.value))
    env.container.close.assert_awaited_once()


async def test_prepaid_service_report_errors_are_not_a_successful_worker_pass(
    prepaid_worker, caplog
):
    env = prepaid_worker
    # A real policy lookup fails for this missing wallet, while renewal returns
    # an error-bearing report. Neither report contains permission to ignore it.
    del env.balances[env.covered.user_id]
    env.billing.run.return_value = SimpleNamespace(
        stop_requested_server_ids=[row.id for row in env.expired],
        errors={env.covered.id: SECRET},
        operator_suspend_server_ids=[],
    )
    with caplog.at_level(logging.WARNING, logger="cloud_platform.worker.settings"):
        with pytest.raises(ExceptionGroup) as raised:
            await ws.reconcile_prepaid_hourly({})
    assert env.lifecycle.unpaid == [row.id for row in env.expired]
    assert all(row.state is ServerLifecycleState.STOPPED for row in env.expired)
    assert len(raised.value.exceptions) >= 2
    assert SECRET not in caplog.text + "".join(traceback.format_exception(raised.value))
    env.container.close.assert_awaited_once()
