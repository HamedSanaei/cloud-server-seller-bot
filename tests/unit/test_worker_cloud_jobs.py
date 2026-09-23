"""Hourly-cloud worker job wiring (STOREFRONT-REWORK).

``process_cloud_creates`` submits hourly create intents once per claimed
operation; ``reconcile_cloud_creates`` attaches proven instances read-only.
Both are exercised with a fake container/service so the job body (claiming,
per-server error isolation, outcome tally logging, container teardown) is
covered without a database, provider or network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import cloud_platform.worker.settings as ws


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
    return container


@pytest.fixture
def container_patch(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(service: Any) -> MagicMock:
        container = _fake_container(service)
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        return container

    return _install


class TestProcessCloudCreates:
    async def test_no_intents_still_initializes_and_closes(self, container_patch: Any) -> None:
        service = _fake_service()
        container = container_patch(service)
        await ws.process_cloud_creates({})
        container.initialize.assert_awaited_once()
        container.hourly_cloud_service.assert_called_once()
        service.process_server.assert_not_awaited()
        container.close.assert_awaited_once()

    async def test_each_requested_server_is_processed_exactly_once(
        self, container_patch: Any
    ) -> None:
        first, second = _server(), _server()
        service = _fake_service(requested=[first, second])
        container = container_patch(service)
        await ws.process_cloud_creates({})
        assert [call.args[0] for call in service.process_server.await_args_list] == [
            first.id,
            second.id,
        ]
        container.close.assert_awaited_once()

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

    async def test_ambiguous_outcome_is_counted_not_retried(self, container_patch: Any) -> None:
        server = _server()
        service = _fake_service(requested=[server], process=lambda server_id: "outcome_unknown")
        container_patch(service)
        await ws.process_cloud_creates({})
        service.process_server.assert_awaited_once_with(server.id)

    async def test_container_is_closed_when_the_service_raises(self, container_patch: Any) -> None:
        service = _fake_service()
        service.servers_requested = AsyncMock(side_effect=RuntimeError("db down"))
        container = container_patch(service)
        with pytest.raises(RuntimeError):
            await ws.process_cloud_creates({})
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
