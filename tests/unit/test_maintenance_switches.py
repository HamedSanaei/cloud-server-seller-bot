"""Tests for provider/location maintenance switches (M10-005).

Acceptance: new orders stop without breaking existing resources.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.compute.domain import (
    MaintenanceBlock,
    MaintenanceScope,
    is_order_blocked,
)
from cloud_platform.modules.compute.service import (
    MaintenanceSwitchError,
    MaintenanceSwitchService,
)
from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    Role,
    User,
    UserStatus,
)

ADMIN_ID = uuid4()
USER_ID = uuid4()
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _admin() -> User:
    return User(
        id=ADMIN_ID,
        username="root",
        email="r@example.com",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )


def _plain_user() -> User:
    return User(
        id=USER_ID,
        username="alice",
        email="a@example.com",
        role=Role.USER,
        status=UserStatus.ACTIVE,
    )


class FakeSwitchRepo:
    def __init__(self) -> None:
        self.blocks: dict[MaintenanceScope, MaintenanceBlock] = {}

    async def list_blocks(self) -> list[MaintenanceBlock]:
        return list(self.blocks.values())

    async def save_block(self, block: MaintenanceBlock) -> MaintenanceBlock:
        existing = self.blocks.get(block.scope)
        saved = MaintenanceBlock(
            scope=block.scope,
            reason=block.reason,
            created_by=block.created_by,
            created_at=existing.created_at if existing else block.created_at,
            updated_at=NOW,
        )
        self.blocks[block.scope] = saved
        return saved

    async def remove_block(self, scope: MaintenanceScope) -> bool:
        return self.blocks.pop(scope, None) is not None


class _AuditCapture:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.append = AsyncMock(side_effect=self._append)

    def _append(self, event: object) -> object:
        self.events.append(event)  # type: ignore[index]
        return event


def _service(repo: FakeSwitchRepo) -> tuple[MaintenanceSwitchService, _AuditCapture]:
    audit = _AuditCapture()
    return MaintenanceSwitchService(repo, audit), audit  # type: ignore[arg-type]


class TestScopeMatching:
    def test_no_blocks_allows(self) -> None:
        assert is_order_blocked([], "hetzner", "fsn1") is False

    def test_provider_block_covers_all_locations(self) -> None:
        blocks = [
            MaintenanceBlock(
                scope=MaintenanceScope(provider_key="hetzner"),
                reason="down",
                created_by=None,
                created_at=NOW,
            )
        ]
        assert is_order_blocked(blocks, "hetzner", "fsn1") is True
        assert is_order_blocked(blocks, "hetzner", "nbg1") is True
        assert is_order_blocked(blocks, "ovh", "gra1") is False

    def test_location_block_covers_only_that_location(self) -> None:
        blocks = [
            MaintenanceBlock(
                scope=MaintenanceScope(provider_key="hetzner", location_id="fsn1"),
                reason="down",
                created_by=None,
                created_at=NOW,
            )
        ]
        assert is_order_blocked(blocks, "hetzner", "fsn1") is True
        assert is_order_blocked(blocks, "hetzner", "nbg1") is False
        assert is_order_blocked(blocks, "ovh", "fsn1") is False

    def test_scope_validation(self) -> None:
        with pytest.raises(ValueError, match="provider_key"):
            MaintenanceScope(provider_key="  ")
        with pytest.raises(ValueError, match="location_id"):
            MaintenanceScope(provider_key="hetzner", location_id="  ")
        assert MaintenanceScope(provider_key="hetzner").location_id is None


class TestSwitchService:
    async def test_non_admin_rejected_without_writes(self) -> None:
        repo = FakeSwitchRepo()
        service, audit = _service(repo)
        with pytest.raises(PermissionDeniedError):
            await service.block_provider(actor=_plain_user(), provider_key="hetzner", reason="x")
        assert repo.blocks == {}
        assert audit.events == []

    async def test_system_actor_allowed(self) -> None:
        repo = FakeSwitchRepo()
        service, _ = _service(repo)
        block = await service.block_provider(actor=None, provider_key="hetzner", reason="x")
        assert block.scope.location_id is None
        assert block.created_by is None

    async def test_block_provider_persists_and_audits(self) -> None:
        repo = FakeSwitchRepo()
        service, audit = _service(repo)
        block = await service.block_provider(
            actor=_admin(), provider_key="hetzner", reason="maintenance window"
        )
        assert block.scope == MaintenanceScope(provider_key="hetzner")
        assert block.created_by == ADMIN_ID
        assert await service.is_order_blocked("hetzner", "fsn1") is True

        event = audit.events[-1]
        assert event.action == "maintenance.block"  # type: ignore[attr-defined]
        assert event.resource_id == "hetzner"  # type: ignore[attr-defined]
        assert event.reason == "maintenance window"  # type: ignore[attr-defined]

    async def test_block_location_covers_only_that_location(self) -> None:
        repo = FakeSwitchRepo()
        service, audit = _service(repo)
        await service.block_location(
            actor=_admin(),
            provider_key="hetzner",
            location_id="fsn1",
            reason="rack down",
        )
        assert await service.is_order_blocked("hetzner", "fsn1") is True
        assert await service.is_order_blocked("hetzner", "nbg1") is False
        event = audit.events[-1]
        assert event.metadata == {"location_id": "fsn1"}  # type: ignore[attr-defined]

    async def test_empty_reason_rejected(self) -> None:
        repo = FakeSwitchRepo()
        service, _ = _service(repo)
        with pytest.raises(MaintenanceSwitchError, match="reason"):
            await service.block_provider(actor=_admin(), provider_key="hetzner", reason="  ")
        with pytest.raises(MaintenanceSwitchError, match="reason"):
            await service.unblock_provider(actor=_admin(), provider_key="hetzner", reason="")

    async def test_reblock_updates_reason_not_duplicates(self) -> None:
        repo = FakeSwitchRepo()
        service, _ = _service(repo)
        await service.block_provider(actor=_admin(), provider_key="hetzner", reason="first")
        await service.block_provider(actor=_admin(), provider_key="hetzner", reason="second")
        blocks = await service.active_blocks()
        assert len(blocks) == 1
        assert blocks[0].reason == "second"

    async def test_unblock_removes_and_audits(self) -> None:
        repo = FakeSwitchRepo()
        service, audit = _service(repo)
        await service.block_provider(actor=_admin(), provider_key="hetzner", reason="x")
        assert (
            await service.unblock_provider(actor=_admin(), provider_key="hetzner", reason="done")
            is True
        )
        assert await service.is_order_blocked("hetzner", "fsn1") is False
        assert audit.events[-1].action == "maintenance.unblock"  # type: ignore[attr-defined]
        # Unblocking an absent switch reports False.
        assert (
            await service.unblock_provider(actor=_admin(), provider_key="hetzner", reason="again")
            is False
        )

    async def test_blocking_scope_returns_the_matching_block(self) -> None:
        repo = FakeSwitchRepo()
        service, _ = _service(repo)
        await service.block_location(
            actor=_admin(), provider_key="hetzner", location_id="fsn1", reason="x"
        )
        scope = await service.blocking_scope("hetzner", "fsn1")
        assert scope == MaintenanceScope(provider_key="hetzner", location_id="fsn1")
        assert await service.blocking_scope("hetzner", "nbg1") is None
        assert await service.blocking_scope("ovh", "fsn1") is None
