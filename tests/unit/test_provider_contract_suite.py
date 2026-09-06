"""Runs the shared provider-contract suite against a fake adapter.

The contract suite (providers/contract.py) is the compliance gate every
CloudProvider must pass; executing it against a fake provider keeps the
suite itself covered and proves the gate is wired into CI.
"""

from __future__ import annotations

from typing import Any

import pytest

from cloud_platform.providers.base import (
    Capability,
    ProviderImage,
    ProviderLocation,
    ProviderPlan,
    ProviderServer,
)
from cloud_platform.providers.contract import ProviderContractTests
from cloud_platform.providers.errors import ProviderError


class _FakeProvider:
    key = "fake"
    capabilities = frozenset({Capability.COMPUTE, Capability.POWER})

    async def list_locations(self) -> list[ProviderLocation]:
        return [ProviderLocation(id="loc-1", name="Loc One", country_code="NL")]

    async def list_plans(self) -> list[ProviderPlan]:
        return [
            ProviderPlan(
                id="p-1",
                name="Plan One",
                architecture="x86_64",
                vcpu=2,
                memory_mb=4096,
                disk_gb=100,
            )
        ]

    async def list_images(self) -> list[ProviderImage]:
        return [ProviderImage(id="img-1", name="Ubuntu", os_family="linux", architecture="x86_64")]

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        return None  # non-existent id -> None

    async def list_servers(self) -> list[ProviderServer]:
        return []

    async def create_server(self, request: Any, idempotency_key: Any) -> ProviderServer:
        if request.plan_id == "invalid-plan-id":
            raise ProviderError("invalid plan")
        return ProviderServer(id="srv-1", name="srv", status="running")

    async def delete_server(self, provider_server_id: str, idempotency_key: Any) -> None:
        return None

    async def power_on(self, provider_server_id: str, idempotency_key: Any) -> None:
        return None

    async def power_off(self, provider_server_id: str, idempotency_key: Any) -> None:
        return None

    async def reboot(self, provider_server_id: str, idempotency_key: Any) -> None:
        return None


class TestFakeProviderContract(ProviderContractTests):
    @pytest.fixture
    def provider(self) -> Any:
        return _FakeProvider()

    @pytest.fixture
    def test_location_id(self) -> str:
        return "loc-1"

    @pytest.fixture
    def test_plan_id(self) -> str:
        return "p-1"

    @pytest.fixture
    def test_image_id(self) -> str:
        return "img-1"

    @pytest.fixture
    def test_ssh_key_ids(self) -> tuple[str, ...]:
        return ()
