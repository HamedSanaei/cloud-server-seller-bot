"""Cloud worker job dispatch, recovery, and cadence regressions.

``process_cloud_creates`` independently dispatches modern hourly and legacy
catalog queues; ``reconcile_cloud_creates`` attaches proven instances read-only.
The legacy dispatch regression uses the real provisioning worker and in-memory
intent repositories; container/hourly doubles isolate the job from external
services while checking error observability and resource teardown.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import cloud_platform.worker.settings as ws
from cloud_platform.modules.compute.domain import ServerLifecycleState
from cloud_platform.modules.operations.domain import OperationStatus
from cloud_platform.modules.operations.service import ProvisioningWorker
from cloud_platform.observability.metrics import PlatformMetrics
from tests.unit.test_provisioning_worker import FakeProvider, _Deps
from tests.unit.test_provisioning_worker import _server as _legacy_server


def _server() -> Any:
    return type("S", (), {"id": uuid4()})()


def _fake_service(
    *,
    requested: list[Any] | None = None,
    for_reconcile: list[Any] | None = None,
    process: Any = None,
    reconcile: Any = None,
) -> MagicMock:
    service = MagicMock()
    service.servers_requested = AsyncMock(return_value=list(requested or []))
    service.servers_for_reconcile = AsyncMock(return_value=list(for_reconcile or []))
    service.process_server = AsyncMock(side_effect=process or (lambda server_id: "submitted"))
    service.reconcile_server = AsyncMock(side_effect=reconcile or (lambda server_id: "attached"))
    return service


def _fake_container(service: Any) -> MagicMock:
    container = MagicMock()
    container.initialize = AsyncMock()
    container.close = AsyncMock()
    container.hourly_cloud_service = MagicMock(return_value=service)
    legacy_worker = MagicMock(spec=ProvisioningWorker)
    legacy_worker.run_once = AsyncMock(return_value={})
    container.provisioning_worker.return_value = legacy_worker
    return container


@pytest.fixture
def container_patch(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(service: Any) -> MagicMock:
        container = _fake_container(service)
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        return container

    return _install


class TestProcessCloudCreates:
    async def test_one_failing_intent_does_not_stop_the_batch(self, container_patch: Any) -> None:
        bad, good = _server(), _server()

        def _process(server_id: Any) -> str:
            if server_id == bad.id:
                raise RuntimeError("provider exploded")
            return "submitted"

        service = _fake_service(requested=[bad, good], process=_process)
        container_patch(service)
        await ws.process_cloud_creates({})
        assert service.process_server.await_count == 2
        # the failure is counted as "error", the healthy one as its outcome
        assert service.process_server.await_args_list[-1].args[0] == good.id

    async def test_container_is_closed_when_the_service_raises(self, container_patch: Any) -> None:
        service = _fake_service()
        service.servers_requested = AsyncMock(side_effect=RuntimeError("db down"))
        container = container_patch(service)
        with pytest.raises(RuntimeError):
            await ws.process_cloud_creates({})
        container.close.assert_awaited_once()

    @pytest.mark.parametrize("failure_phase", ["construction", "listing"])
    async def test_legacy_intent_is_attached_despite_hourly_dispatch_failure(
        self,
        container_patch: Any,
        monkeypatch: pytest.MonkeyPatch,
        failure_phase: str,
    ) -> None:
        server = _legacy_server(server_id=uuid4())
        deps = _Deps({server.id: server}, FakeProvider())
        service = _fake_service()
        container = container_patch(service)
        container.provisioning_worker.return_value = deps.worker
        metrics = PlatformMetrics()
        monkeypatch.setattr(ws, "metrics", metrics)
        if failure_phase == "construction":
            failure = ValueError("provider credential encryption key is not configured")
            container.hourly_cloud_service.side_effect = failure
        else:
            failure = RuntimeError("hourly queue unavailable")
            service.servers_requested.side_effect = failure

        # The scheduler sees a failed job, but the independent legacy intent
        # still advances through the real worker without a caller-side requeue.
        with pytest.raises(type(failure)) as raised:
            await ws.process_cloud_creates({})
        assert raised.value is failure
        operation = deps.ops.ops[f"server-create:{server.id}"]
        assert operation.status is OperationStatus.COMPLETED
        assert operation.attempts == 1
        assert operation.provider_response["provider_server_id"] == deps.provider.result.id
        assert server.state is ServerLifecycleState.PROVISIONING
        assert server.provider_server_id == deps.provider.result.id
        assert deps.provider.create_calls == 1
        assert deps.provider.last_request is not None
        assert deps.provider.last_request.image_id == "img-linux"
        deps.holds.release_hold.assert_not_awaited()

        # A later poll after the job failure must not submit that intent again.
        with pytest.raises(type(failure)):
            await ws.process_cloud_creates({})
        assert deps.provider.create_calls == 1
        assert operation.attempts == 1
        assert container.initialize.await_count == 2
        assert container.close.await_count == 2
        assert (
            metrics.registry.get_sample_value(
                "cloud_platform_job_runs_total",
                {"job": "process_cloud_creates", "status": "error"},
            )
            == 2
        )

    async def test_legacy_phase_failure_preserves_hourly_dispatch_and_job_failure(
        self, container_patch: Any
    ) -> None:
        server = _server()
        service = _fake_service(requested=[server])
        container = container_patch(service)
        failure = RuntimeError("legacy queue unavailable")
        container.provisioning_worker.return_value.run_once.side_effect = failure

        with pytest.raises(RuntimeError) as raised:
            await ws.process_cloud_creates({})

        assert raised.value is failure
        service.process_server.assert_awaited_once_with(server.id)
        container.provisioning_worker.return_value.run_once.assert_awaited_once_with(limit=10)
        container.close.assert_awaited_once()

    async def test_both_phase_errors_are_reported_after_both_are_attempted(
        self, container_patch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        service = _fake_service()
        hourly_failure = RuntimeError("hourly queue unavailable")
        service.servers_requested.side_effect = hourly_failure
        container = container_patch(service)
        legacy_failure = ValueError("legacy construction failed")
        container.provisioning_worker.side_effect = legacy_failure

        with pytest.raises(ExceptionGroup) as raised:
            await ws.process_cloud_creates({})

        assert raised.value.exceptions == (hourly_failure, legacy_failure)
        container.provisioning_worker.assert_called_once_with()
        assert "hourly create dispatch failed" in caplog.text
        assert "legacy catalog create dispatch failed" in caplog.text
        container.close.assert_awaited_once()

    async def test_shared_initialization_failure_closes_without_dispatching(
        self, container_patch: Any
    ) -> None:
        service = _fake_service()
        container = container_patch(service)
        container.initialize.side_effect = RuntimeError("shared initialization failed")

        with pytest.raises(RuntimeError, match="shared initialization failed"):
            await ws.process_cloud_creates({})

        container.hourly_cloud_service.assert_not_called()
        container.provisioning_worker.assert_not_called()
        container.close.assert_awaited_once()


class TestReconcileCloudCreates:
    async def test_no_stuck_servers_is_a_clean_pass(self, container_patch: Any) -> None:
        service = _fake_service()
        container = container_patch(service)
        await ws.reconcile_cloud_creates({})
        container.initialize.assert_awaited_once()
        service.reconcile_server.assert_not_awaited()
        container.close.assert_awaited_once()

    async def test_each_stuck_server_is_reconciled_once(self, container_patch: Any) -> None:
        first, second = _server(), _server()
        service = _fake_service(for_reconcile=[first, second])
        container_patch(service)
        await ws.reconcile_cloud_creates({})
        assert [call.args[0] for call in service.reconcile_server.await_args_list] == [
            first.id,
            second.id,
        ]

    async def test_a_failed_reconcile_is_isolated_to_its_server(self, container_patch: Any) -> None:
        bad, good = _server(), _server()

        def _reconcile(server_id: Any) -> str:
            if server_id == bad.id:
                raise RuntimeError("provider 500")
            return "still_unknown"

        service = _fake_service(for_reconcile=[bad, good], reconcile=_reconcile)
        container_patch(service)
        await ws.reconcile_cloud_creates({})
        assert service.reconcile_server.await_count == 2

    async def test_needs_review_outcome_is_reported_without_mutation(
        self, container_patch: Any
    ) -> None:
        server = _server()
        service = _fake_service(for_reconcile=[server], reconcile=lambda server_id: "needs_review")
        container_patch(service)
        await ws.reconcile_cloud_creates({})
        service.reconcile_server.assert_awaited_once_with(server.id)

    async def test_container_is_closed_when_listing_raises(self, container_patch: Any) -> None:
        service = _fake_service()
        service.servers_for_reconcile = AsyncMock(side_effect=RuntimeError("db down"))
        container = container_patch(service)
        with pytest.raises(RuntimeError):
            await ws.reconcile_cloud_creates({})
        container.close.assert_awaited_once()


class TestCloudCronWiring:
    def test_both_cloud_jobs_are_scheduled_and_run_at_startup(self) -> None:
        by_name = {job.coroutine.__name__: job for job in ws._cron_jobs()}
        creates = by_name["process_cloud_creates"]
        reconcile = by_name["reconcile_cloud_creates"]
        assert creates.minute == set(range(0, 60, 2))
        assert reconcile.minute == set(range(0, 60, 3))
        assert creates.run_at_startup is True
        assert reconcile.run_at_startup is True

    def test_cloud_jobs_are_registered_functions(self) -> None:
        names = {getattr(fn, "__name__", None) for fn in ws.WorkerSettings.functions}
        assert {"process_cloud_creates", "reconcile_cloud_creates"} <= names

    def test_cloud_jobs_are_not_in_the_passive_provisioning_queue(self) -> None:
        # Cloud creates are mutating work; they must not ride the billing queue.
        billing = {getattr(fn, "__name__", None) for fn in ws.BILLING_FUNCTIONS}
        assert "process_cloud_creates" not in billing
        assert "reconcile_cloud_creates" not in billing
