"""Tests for Prometheus metrics (M11-001).

Acceptance: API / jobs / provider / billing metrics emitted.

Singleton-based tests (the instrumented code writes to the module-level
``metrics`` instance) assert on the DELTA across the call, because other tests
in the same process also drive the same code paths and accumulate counts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from cloud_platform.api.app import create_app
from cloud_platform.modules.catalog.domain import (
    CatalogSyncJob,
    CatalogSyncStep,
    CatalogSyncStepReport,
)
from cloud_platform.modules.operations.service import ProvisioningWorker
from cloud_platform.observability.metrics import PlatformMetrics, metrics, provider_outcome
from cloud_platform.providers.errors import (
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.hetzner.client import _operation_label
from cloud_platform.worker.settings import reconcile_provider_resources


def _value(
    sample_name: str, labels: dict[str, str], registry: CollectorRegistry | None = None
) -> float:
    reg = registry or metrics.registry
    value = reg.get_sample_value(sample_name, labels)
    return float(value) if value is not None else 0.0


def _delta(
    before: float,
    sample_name: str,
    labels: dict[str, str],
    registry: CollectorRegistry | None = None,
) -> float:
    return _value(sample_name, labels, registry) - before


class TestMetricLayer:
    async def test_job_records_ok_and_error(self) -> None:
        m = PlatformMetrics()
        async with m.job("unit_test_ok"):
            pass
        with pytest.raises(RuntimeError, match="boom"):
            async with m.job("unit_test_error"):
                raise RuntimeError("boom")
        assert (
            _value(
                "cloud_platform_job_runs_total", {"job": "unit_test_ok", "status": "ok"}, m.registry
            )
            == 1.0
        )
        assert (
            _value(
                "cloud_platform_job_runs_total",
                {"job": "unit_test_error", "status": "error"},
                m.registry,
            )
            == 1.0
        )
        assert (
            _value("cloud_platform_job_duration_seconds_count", {"job": "unit_test_ok"}, m.registry)
            == 1.0
        )

    async def test_provider_call_records_outcome(self) -> None:
        m = PlatformMetrics()
        async with m.provider_call("hetzner", "GET /servers"):
            pass
        with pytest.raises(ProviderRateLimited):
            async with m.provider_call("hetzner", "GET /servers"):
                raise ProviderRateLimited("429")
        labels_ok = {"provider": "hetzner", "operation": "GET /servers", "outcome": "success"}
        labels_rl = {"provider": "hetzner", "operation": "GET /servers", "outcome": "rate_limited"}
        assert _value("cloud_platform_provider_calls_total", labels_ok, m.registry) == 1.0
        assert _value("cloud_platform_provider_calls_total", labels_rl, m.registry) == 1.0

    def test_billing_event_and_render(self) -> None:
        m = PlatformMetrics()
        m.record_billing_event("hold_created", "ok")
        assert (
            _value(
                "cloud_platform_billing_events_total",
                {"event": "hold_created", "result": "ok"},
                m.registry,
            )
            == 1.0
        )
        assert "cloud_platform_billing_events_total" in m.render().decode()

    def test_provider_outcome_mapping(self) -> None:
        assert provider_outcome(ProviderRateLimited("x")) == "rate_limited"
        assert provider_outcome(ProviderUnavailable("x")) == "unavailable"
        assert provider_outcome(RuntimeError("x")) == "error"


class TestApiMetrics:
    def test_requests_and_metrics_endpoint(self) -> None:
        app = create_app()
        before = _value(
            "cloud_platform_api_requests_total",
            {"method": "GET", "path": "/health/live", "status": "200"},
            metrics.registry,
        )
        with TestClient(app) as client:
            assert client.get("/health/live").status_code == 200
            m = client.get("/metrics")
            assert m.status_code == 200
            assert "text/plain" in m.headers["content-type"]
            assert "cloud_platform_api_requests_total" in m.text
            assert 'path="/health/live"' in m.text
        assert (
            _delta(
                before,
                "cloud_platform_api_requests_total",
                {"method": "GET", "path": "/health/live", "status": "200"},
            )
            == 1.0
        )


class TestJobInstrumentation:
    async def test_catalog_sync_job_is_timed(self) -> None:
        lock = AsyncMock()
        lock.guard = lambda: _FakeGuard()
        job = CatalogSyncJob(
            lock,  # type: ignore[arg-type]
            [CatalogSyncStep(name="locations", run=_step_report)],
        )
        before = _value("cloud_platform_job_runs_total", {"job": "catalog_sync", "status": "ok"})
        report = await job.run()
        assert report.ran is True
        assert (
            _delta(before, "cloud_platform_job_runs_total", {"job": "catalog_sync", "status": "ok"})
            == 1.0
        )

    async def test_provisioning_worker_run_once_is_timed(self) -> None:
        server_repo = AsyncMock()
        server_repo.list_requested = AsyncMock(return_value=[])
        worker = ProvisioningWorker(
            operation_repo=AsyncMock(),  # type: ignore[arg-type]
            server_repo=server_repo,  # type: ignore[arg-type]
            provider_registry=AsyncMock(),  # type: ignore[arg-type]
            image_selector=AsyncMock(),  # type: ignore[arg-type]
            wallet_repo=AsyncMock(),  # type: ignore[arg-type]
            hold_repo=AsyncMock(),  # type: ignore[arg-type]
            audit_repo=AsyncMock(),  # type: ignore[arg-type]
        )
        before = _value(
            "cloud_platform_job_runs_total", {"job": "provisioning_worker", "status": "ok"}
        )
        assert await worker.run_once() == {}
        assert (
            _delta(
                before,
                "cloud_platform_job_runs_total",
                {"job": "provisioning_worker", "status": "ok"},
            )
            == 1.0
        )

    async def test_arq_reconcile_job_is_timed(self) -> None:
        before = _value(
            "cloud_platform_job_runs_total",
            {"job": "reconcile_provider_resources", "status": "ok"},
        )
        await reconcile_provider_resources({})
        assert (
            _delta(
                before,
                "cloud_platform_job_runs_total",
                {"job": "reconcile_provider_resources", "status": "ok"},
            )
            == 1.0
        )


class TestWalletBillingEvents:
    async def test_hold_created_event_emitted(self) -> None:
        from cloud_platform.modules.wallet.domain import Hold
        from cloud_platform.modules.wallet.repository import HoldService

        wallet_repo = AsyncMock()
        hold_repo = AsyncMock()
        hold_repo.create_hold = AsyncMock(
            return_value=Hold(
                wallet_id=uuid4(),
                amount=10,
                currency="EUR",
                idempotency_key="key-1",
                id=uuid4(),
            )
        )
        ledger_repo = AsyncMock()
        service = HoldService(wallet_repo, hold_repo, ledger_repo)  # type: ignore[arg-type]
        before = _value(
            "cloud_platform_billing_events_total", {"event": "hold_created", "result": "ok"}
        )
        await service.create_hold(uuid4(), 10, "EUR", "key-1")
        assert (
            _delta(
                before,
                "cloud_platform_billing_events_total",
                {"event": "hold_created", "result": "ok"},
            )
            == 1.0
        )


class _FakeGuard:
    async def __aenter__(self) -> bool:
        return True

    async def __aexit__(self, *args: object) -> None:
        return None


async def _step_report() -> CatalogSyncStepReport:
    return CatalogSyncStepReport(name="locations", fetched=1, upserted=1)


class TestOperationLabel:
    def test_id_segments_are_shaped(self) -> None:
        assert _operation_label("GET", "/servers/12345") == "GET /servers/{id}"
        assert _operation_label("POST", "/servers/12345/actions/poweron") == (
            "POST /servers/{id}/actions/poweron"
        )
        assert _operation_label("GET", "/locations") == "GET /locations"
        assert _operation_label("GET", "/images?type=system") == "GET /images"
