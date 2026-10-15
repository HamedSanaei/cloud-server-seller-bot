"""Startup reconciliation runs before serving, and fails closed on database errors."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from cloud_platform.api.app import create_app
from cloud_platform.core import container as container_module
from cloud_platform.worker import settings as worker_module


def test_api_reconciles_before_serving_and_closes_container(monkeypatch):
    reconcile = AsyncMock()
    close = AsyncMock()
    monkeypatch.setattr(
        container_module,
        "get_container",
        AsyncMock(return_value=SimpleNamespace(reconcile_superadmins=reconcile)),
    )
    monkeypatch.setattr(container_module, "close_container", close)
    with TestClient(create_app()) as client:
        reconcile.assert_awaited_once()
        assert client.get("/health/live").status_code == 200
    close.assert_awaited_once()


def test_api_cannot_serve_with_failed_role_reconciliation(monkeypatch):
    reconcile = AsyncMock(side_effect=RuntimeError("test policy database unavailable"))
    close = AsyncMock()
    monkeypatch.setattr(
        container_module,
        "get_container",
        AsyncMock(return_value=SimpleNamespace(reconcile_superadmins=reconcile)),
    )
    monkeypatch.setattr(container_module, "close_container", close)
    with pytest.raises(RuntimeError, match="policy database unavailable"), TestClient(create_app()):
        pytest.fail("API must not start before successful policy reconciliation")
    close.assert_awaited_once()


async def test_worker_reconciles_before_business_jobs(monkeypatch):
    sequence = []

    async def reconcile():
        sequence.append("superadmins")

    async def capacity():
        sequence.append("business")

    monkeypatch.setattr(
        container_module,
        "get_container",
        AsyncMock(return_value=SimpleNamespace(reconcile_superadmins=reconcile)),
    )
    monkeypatch.setattr(worker_module, "reconcile_capacity_evidence_once", capacity)
    await worker_module.startup({})
    assert sequence == ["superadmins", "business"]


async def test_worker_does_not_run_jobs_after_failed_policy_sync(monkeypatch):
    capacity = AsyncMock()
    monkeypatch.setattr(
        container_module,
        "get_container",
        AsyncMock(
            return_value=SimpleNamespace(
                reconcile_superadmins=AsyncMock(side_effect=RuntimeError("policy sync failed"))
            )
        ),
    )
    monkeypatch.setattr(worker_module, "reconcile_capacity_evidence_once", capacity)
    with pytest.raises(RuntimeError, match="policy sync failed"):
        await worker_module.startup({})
    capacity.assert_not_awaited()
