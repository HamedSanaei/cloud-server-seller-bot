"""Tests for the missing-provider-resource detector (M07-010).

Acceptance: finds DB-active resources absent at provider.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.operations.service import (
    MissingResourceDetector,
)
from cloud_platform.providers.base import (
    Capability,
    ProviderServer,
)
from cloud_platform.providers.errors import ProviderUnavailable
from cloud_platform.providers.registry import ProviderRegistry

USER_ID = uuid4()


def _server(
    state: ServerLifecycleState,
    provider_server_id: str | None = "prov-1",
    provider_key: str = "hetzner",
) -> CloudServer:
    return CloudServer(
        id=uuid4(),
        user_id=USER_ID,
        provider_key=provider_key,
        provider_account_id=uuid4(),
        state=state,
        provider_server_id=provider_server_id,
    )


class FakeServerRepo:
    def __init__(self, servers: list[CloudServer]) -> None:
        self.servers = {s.id: s for s in servers}
        self.saved: list[CloudServer] = []

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.servers.get(server_id)

    async def list_provisioning(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.PROVISIONING]

    async def list_running(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.RUNNING]

    async def list_stopped(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.STOPPED]

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server


class FakeProvider:
    def __init__(
        self,
        key: str = "hetzner",
        present: set[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.key = key
        self.capabilities = frozenset({Capability.COMPUTE})
        # present: provider ids that exist. None -> everything exists.
        self.present = present
        self.error = error
        self.get_calls: list[str] = []
        self.delete_calls = 0

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        self.get_calls.append(provider_server_id)
        if self.error is not None:
            raise self.error
        if self.present is not None and provider_server_id not in self.present:
            return None
        return ProviderServer(id=provider_server_id, name="srv-x", status="running")

    async def delete_server(self, provider_server_id: str, idempotency_key: Any) -> None:
        self.delete_calls += 1


def _make(
    servers: list[CloudServer], providers: list[FakeProvider]
) -> tuple[MissingResourceDetector, FakeServerRepo, AsyncMock]:
    repo = FakeServerRepo(servers)
    registry = ProviderRegistry()
    for p in providers:
        registry.register(p)  # type: ignore[arg-type]
    audit = AsyncMock()
    audit.append = AsyncMock(side_effect=lambda e: e)
    detector = MissingResourceDetector(
        server_repo=repo,  # type: ignore[arg-type]
        provider_registry=registry,
        audit_repo=audit,  # type: ignore[arg-type]
    )
    return detector, repo, audit


class TestDetection:
    async def test_running_server_absent_at_provider_is_found(self) -> None:
        server = _server(ServerLifecycleState.RUNNING, "prov-1")
        provider = FakeProvider(present=set())  # nothing exists
        detector, repo, audit = _make([server], [provider])

        missing = await detector.detect()

        assert len(missing) == 1
        m = missing[0]
        assert m.provider_key == "hetzner"
        assert m.provider_server_id == "prov-1"
        assert m.server_id == server.id
        assert m.state is ServerLifecycleState.RUNNING
        event = audit.append.call_args_list[0].args[0]
        assert event.action == "provider.missing_resource_detected"
        assert event.actor_type.value == "system"
        assert event.metadata["state"] == "running"
        # detector reports but never mutates state
        assert repo.saved == []
        assert server.state is ServerLifecycleState.RUNNING

    async def test_stopped_server_absent_is_found(self) -> None:
        server = _server(ServerLifecycleState.STOPPED, "prov-2")
        detector, _, _ = _make([server], [FakeProvider(present=set())])

        missing = await detector.detect()

        assert len(missing) == 1
        assert missing[0].state is ServerLifecycleState.STOPPED

    async def test_provisioning_server_absent_is_found(self) -> None:
        server = _server(ServerLifecycleState.PROVISIONING, "prov-3")
        detector, _, _ = _make([server], [FakeProvider(present=set())])

        missing = await detector.detect()

        assert len(missing) == 1
        assert missing[0].state is ServerLifecycleState.PROVISIONING

    async def test_present_resource_is_not_found(self) -> None:
        server = _server(ServerLifecycleState.RUNNING, "prov-1")
        detector, _, _ = _make([server], [FakeProvider(present={"prov-1"})])

        assert await detector.detect() == []

    @pytest.mark.parametrize(
        "state",
        [
            ServerLifecycleState.REQUESTED,
            ServerLifecycleState.ERROR,
            ServerLifecycleState.MANUAL_REVIEW,
            ServerLifecycleState.DELETE_REQUESTED,
            ServerLifecycleState.DELETING,
            ServerLifecycleState.DELETED,
        ],
    )
    async def test_non_active_states_are_not_scanned(self, state) -> None:
        server = _server(state, "prov-1")
        provider = FakeProvider(present=set())
        detector, _, _ = _make([server], [provider])

        assert await detector.detect() == []
        assert provider.get_calls == []

    async def test_missing_provider_id_is_skipped(self) -> None:
        server = _server(ServerLifecycleState.RUNNING, provider_server_id=None)
        provider = FakeProvider(present=set())
        detector, _, _ = _make([server], [provider])

        assert await detector.detect() == []
        assert provider.get_calls == []

    async def test_provider_error_is_inconclusive_not_missing(self) -> None:
        server = _server(ServerLifecycleState.RUNNING, "prov-1")
        provider = FakeProvider(error=ProviderUnavailable("503"))
        detector, repo, _ = _make([server], [provider])

        assert await detector.detect() == []
        assert repo.saved == []

    async def test_unknown_provider_is_skipped(self) -> None:
        server = _server(ServerLifecycleState.RUNNING, "prov-1", provider_key="ghost")
        provider = FakeProvider(present=set())
        detector, _, _ = _make([server], [provider])

        assert await detector.detect() == []
        assert provider.get_calls == []

    async def test_mixed_batch_only_missing_reported(self) -> None:
        good = _server(ServerLifecycleState.RUNNING, "prov-good")
        gone = _server(ServerLifecycleState.RUNNING, "prov-gone")
        provider = FakeProvider(present={"prov-good"})
        detector, _, audit = _make([good, gone], [provider])

        missing = await detector.detect()

        assert len(missing) == 1
        assert missing[0].provider_server_id == "prov-gone"
        assert len(audit.append.call_args_list) == 1

    async def test_never_deletes(self) -> None:
        server = _server(ServerLifecycleState.RUNNING, "prov-1")
        provider = FakeProvider(present=set())
        detector, repo, _ = _make([server], [provider])

        await detector.detect()

        assert provider.delete_calls == 0
        assert repo.saved == []
