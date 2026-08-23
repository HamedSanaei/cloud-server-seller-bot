"""Tests for the SQLAlchemy audit repository (mocked session, no live DB)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType, AuditEvent
from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository


def _row(event_id=None) -> MagicMock:
    row = MagicMock()
    row.id = event_id or uuid4()
    row.actor_type = "admin"
    row.actor_id = uuid4()
    row.action = "wallet.adjust"
    row.resource_type = "wallet"
    row.resource_id = str(uuid4())
    row.reason = "goodwill credit"
    row.event_metadata = {"amount": "500"}
    row.occurred_at = None
    return row


@pytest.fixture
def session() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    return mock


@pytest.fixture
def repo(session: AsyncMock) -> SqlAlchemyAuditRepository:
    return SqlAlchemyAuditRepository(lambda: session)  # type: ignore[arg-type]


class TestAppend:
    async def test_adds_commits_and_returns_persisted_event(
        self, repo: SqlAlchemyAuditRepository, session: AsyncMock
    ) -> None:
        persisted_id = uuid4()
        captured: list = []

        def refresh_side_effect(row: object) -> None:
            captured.append(row)
            row.id = persisted_id  # simulate DB-assigned PK

        session.refresh = AsyncMock(side_effect=refresh_side_effect)

        event = AuditEvent(
            actor_type=ActorType.ADMIN,
            action="wallet.adjust",
            resource_type="wallet",
            reason="goodwill credit",
            metadata={"amount": "500"},
        )
        result = await repo.append(event)

        session.add.assert_called_once()
        session.commit.assert_awaited_once()
        assert result.id == persisted_id
        assert result.action == "wallet.adjust"
        assert result.actor_type is ActorType.ADMIN
        assert result.metadata == {"amount": "500"}

    async def test_empty_reason_and_resource_id_persist_as_null(
        self, repo: SqlAlchemyAuditRepository, session: AsyncMock
    ) -> None:
        added: list = []
        session.add = MagicMock(side_effect=lambda row: added.append(row))
        session.refresh = AsyncMock()

        await repo.append(
            AuditEvent(actor_type=ActorType.SYSTEM, action="job.run", resource_type="catalog")
        )
        assert added[0].resource_id is None
        assert added[0].reason == ""


class TestQueries:
    async def test_get_by_resource_maps_rows(
        self, repo: SqlAlchemyAuditRepository, session: AsyncMock
    ) -> None:
        r1, r2 = _row(), _row()
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: [r1, r2]))
        )

        events = await repo.get_by_resource("wallet", str(r1.resource_id))

        assert len(events) == 2
        assert all(isinstance(e, AuditEvent) for e in events)
        assert events[0].action == "wallet.adjust"
        session.execute.assert_awaited_once()

    async def test_get_by_resource_empty_result(
        self, repo: SqlAlchemyAuditRepository, session: AsyncMock
    ) -> None:
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: []))
        )
        assert await repo.get_by_resource("wallet", "missing") == []

    async def test_get_by_actor_maps_rows(
        self, repo: SqlAlchemyAuditRepository, session: AsyncMock
    ) -> None:
        actor_id = uuid4()
        row = _row()
        row.actor_id = actor_id
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: [row]))
        )

        events = await repo.get_by_actor(actor_id)

        assert len(events) == 1
        assert events[0].actor_id == actor_id


class TestAppendOnlyDesign:
    def test_repository_exposes_no_mutation_operations(self) -> None:
        """The audit port must not offer update or delete paths."""
        repo = SqlAlchemyAuditRepository(lambda: None)  # type: ignore[arg-type]
        assert not hasattr(repo, "update")
        assert not hasattr(repo, "delete")
        assert not hasattr(repo, "save")
