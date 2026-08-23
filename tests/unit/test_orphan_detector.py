"""Tests for the orphan provider-resource detector (M07-009).

Acceptance: finds tagged provider resources absent from the DB.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.operations.service import (
    PLATFORM_SERVER_ID_LABEL,
    OrphanDetector,
)
from cloud_platform.providers.base import (
    Capability,
    ProviderServer,
)
from cloud_platform.providers.errors import ProviderUnavailable
from cloud_platform.providers.registry import ProviderRegistry

USER_ID = uuid4()
CLAIMED_ID = uuid4()


def _remote(
    remote_id: str,
    label: str | None = None,
    nested: bool = True,
    status: str = "running",
    name: str = "srv-x",
) -> ProviderServer:
    if label is None:
        metadata: dict[str, Any] = {}
    elif nested:
        metadata = {"labels": {PLATFORM_SERVER_ID_LABEL: label}}
    else:
        metadata = {PLATFORM_SERVER_ID_LABEL: label}
    return ProviderServer(id=remote_id, name=name, status=status, metadata=metadata)


def _server(state: ServerLifecycleState = ServerLifecycleState.RUNNING) -> CloudServer:
    return CloudServer(
        id=CLAIMED_ID,
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
    )


class FakeServerRepo:
    def __init__(self, servers: dict[UUID, CloudServer]) -> None:
        self.servers = dict(servers)

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.servers.get(server_id)


class FakeProvider:
    def __init__(
        self,
        key: str = "hetzner",
        servers: list[ProviderServer] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.key = key
        self.capabilities = frozenset({Capability.COMPUTE})
        self.servers = servers or []
        self.error = error
        self.list_calls = 0
        self.delete_calls = 0

    async def list_servers(self) -> list[ProviderServer]:
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        return list(self.servers)

    async def delete_server(self, provider_server_id: str, idempotency_key: Any) -> None:
        self.delete_calls += 1


def _make(
    providers: list[FakeProvider], servers: dict[UUID, CloudServer]
) -> tuple[OrphanDetector, AsyncMock]:
    registry = ProviderRegistry()
    for p in providers:
        registry.register(p)  # type: ignore[arg-type]
    audit = AsyncMock()
    audit.append = AsyncMock(side_effect=lambda e: e)
    detector = OrphanDetector(
        server_repo=FakeServerRepo(servers),  # type: ignore[arg-type]
        provider_registry=registry,
        audit_repo=audit,  # type: ignore[arg-type]
    )
    return detector, audit


class TestDetection:
    async def test_tagged_server_without_row_is_orphan(self) -> None:
        provider = FakeProvider(servers=[_remote("prov-1", label=str(CLAIMED_ID))])
        detector, audit = _make([provider], {})

        orphans = await detector.detect()

        assert len(orphans) == 1
        o = orphans[0]
        assert o.provider_key == "hetzner"
        assert o.provider_server_id == "prov-1"
        assert o.claimed_server_id == str(CLAIMED_ID)
        event = audit.append.call_args_list[0].args[0]
        assert event.action == "provider.orphan_detected"
        assert event.actor_type.value == "system"
        assert event.metadata["provider_key"] == "hetzner"
        assert event.metadata["claimed_server_id"] == str(CLAIMED_ID)

    async def test_tagged_server_with_running_row_is_not_orphan(self) -> None:
        provider = FakeProvider(servers=[_remote("prov-1", label=str(CLAIMED_ID))])
        detector, _ = _make([provider], {CLAIMED_ID: _state_running()})

        assert await detector.detect() == []

    async def test_tagged_server_with_deleted_row_is_not_orphan(self) -> None:
        # The deletion flow owns a DELETED row; the detector must not steal it.
        provider = FakeProvider(servers=[_remote("prov-1", label=str(CLAIMED_ID))])
        detector, _ = _make([provider], {CLAIMED_ID: _state_deleted()})

        assert await detector.detect() == []

    async def test_untagged_server_is_ignored(self) -> None:
        provider = FakeProvider(
            servers=[
                _remote("prov-1", label=None),
                _remote("prov-2", label=None),
            ]
        )
        detector, _ = _make([provider], {})

        assert await detector.detect() == []

    async def test_malformed_label_is_reported(self) -> None:
        provider = FakeProvider(servers=[_remote("prov-1", label="not-a-uuid")])
        detector, _ = _make([provider], {})

        orphans = await detector.detect()

        assert len(orphans) == 1
        assert orphans[0].claimed_server_id == "not-a-uuid"

    async def test_blank_label_is_ignored(self) -> None:
        provider = FakeProvider(servers=[_remote("prov-1", label="   ")])
        detector, _ = _make([provider], {})

        assert await detector.detect() == []

    async def test_top_level_label_placement_is_read(self) -> None:
        provider = FakeProvider(servers=[_remote("prov-1", label=str(CLAIMED_ID), nested=False)])
        detector, _ = _make([provider], {})

        assert len(await detector.detect()) == 1

    async def test_provider_error_is_skipped_not_fatal(self) -> None:
        bad = FakeProvider(key="broken", error=ProviderUnavailable("500"))
        good = FakeProvider(key="hetzner", servers=[_remote("prov-9", label=str(CLAIMED_ID))])
        detector, _ = _make([bad, good], {})

        orphans = await detector.detect()

        assert bad.list_calls == 1
        assert good.list_calls == 1
        assert len(orphans) == 1
        assert orphans[0].provider_key == "hetzner"

    async def test_multiple_providers_aggregated(self) -> None:
        other_claimed = uuid4()
        providers = [
            FakeProvider(
                key="hetzner",
                servers=[
                    _remote("prov-1", label=str(CLAIMED_ID)),
                    _remote("prov-2", label=None),
                ],
            ),
            FakeProvider(
                key="local-ir",
                servers=[_remote("prov-3", label=str(other_claimed))],
            ),
        ]
        detector, _ = _make(providers, {})

        orphans = await detector.detect()

        assert {(o.provider_key, o.provider_server_id) for o in orphans} == {
            ("hetzner", "prov-1"),
            ("local-ir", "prov-3"),
        }

    async def test_empty_registry(self) -> None:
        detector, audit = _make([], {})

        assert await detector.detect() == []
        audit.append.assert_not_awaited()

    async def test_never_deletes(self) -> None:
        # The detector has no delete path at all: only list + audit.
        provider = FakeProvider(servers=[_remote("prov-1", label=str(CLAIMED_ID))])
        detector, _ = _make([provider], {})

        await detector.detect()

        assert provider.delete_calls == 0


def _state_running() -> CloudServer:
    return _server(ServerLifecycleState.RUNNING)


def _state_deleted() -> CloudServer:
    return _server(ServerLifecycleState.DELETED)
