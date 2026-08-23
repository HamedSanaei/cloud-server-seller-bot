"""Tests for the AuditTrail facade: structural actor/reason linkage (M10-002)."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType, AuditError
from cloud_platform.modules.audit.service import AuditTrail


def _trail() -> tuple[AuditTrail, AsyncMock]:
    repo = AsyncMock()
    repo.append = AsyncMock(side_effect=lambda event: event)  # echo the event
    return AuditTrail(repo), repo


class TestAdminReasonEnforcement:
    async def test_admin_mutation_without_reason_rejected(self) -> None:
        trail, repo = _trail()
        with pytest.raises(AuditError, match="reason"):
            await trail.record_mutation(
                actor_type=ActorType.ADMIN,
                action="wallet.adjust",
                resource_type="wallet",
                resource_id="w-1",
            )
        repo.append.assert_not_awaited()

    async def test_admin_mutation_with_whitespace_reason_rejected(self) -> None:
        trail, repo = _trail()
        with pytest.raises(AuditError):
            await trail.record_mutation(
                actor_type=ActorType.ADMIN,
                action="wallet.adjust",
                resource_type="wallet",
                reason="   ",
            )
        repo.append.assert_not_awaited()

    async def test_admin_mutation_with_reason_appended(self) -> None:
        trail, repo = _trail()
        admin_id = uuid4()
        event = await trail.record_mutation(
            actor_type=ActorType.ADMIN,
            actor_id=admin_id,
            action="wallet.force_release",
            resource_type="wallet",
            resource_id="w-1",
            reason="customer dispute resolution",
            metadata={"amount": "300"},
        )
        repo.append.assert_awaited_once()
        assert event.actor_type is ActorType.ADMIN
        assert event.actor_id == admin_id
        assert event.reason == "customer dispute resolution"
        assert event.metadata == {"amount": "300"}


class TestNonAdminActors:
    async def test_system_actor_without_reason_allowed(self) -> None:
        trail, repo = _trail()
        event = await trail.record_mutation(
            actor_type=ActorType.SYSTEM,
            action="job.run",
            resource_type="catalog",
        )
        repo.append.assert_awaited_once()
        assert event.reason == ""

    async def test_user_actor_without_reason_allowed(self) -> None:
        trail, repo = _trail()
        user_id = uuid4()
        event = await trail.record_mutation(
            actor_type=ActorType.USER,
            actor_id=user_id,
            action="server.create",
            resource_type="server",
            resource_id="s-9",
        )
        repo.append.assert_awaited_once()
        assert event.actor_id == user_id
