"""Tests for the abuse ownership resolver and case repository (mocked session)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from cloud_platform.modules.abuse.domain import (
    AbuseCase,
    AbuseStatus,
    ResourceRef,
    ResourceType,
)
from cloud_platform.modules.abuse.repository import (
    SqlAlchemyAbuseCaseRepository,
    SqlAlchemyOwnershipResolver,
)


def _server_row(user_id=None, server_id=None, **attrs: object) -> MagicMock:
    row = MagicMock()
    row.id = server_id or uuid4()
    row.user_id = user_id or uuid4()
    for key, value in attrs.items():
        setattr(row, key, value)
    return row


def _provider_row() -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.name = "hetzner"
    return row


def _case_row(status: str = "open") -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.provider_key = "hetzner"
    row.resource_type = "provider_server"
    row.resource_id = "srv-42"
    row.user_id = uuid4()
    row.server_id = uuid4()
    row.reason = "port scan"
    row.reporter = "hetzner-abuse"
    row.status = status
    row.created_at = None
    row.updated_at = None
    row.resolved_at = None
    return row


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    return mock


def _compiled(stmt: object) -> str:
    return str(
        stmt.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


class TestOwnershipResolver:
    async def test_resolves_server_by_provider_server_id(self, db: AsyncMock) -> None:
        provider = _provider_row()
        server = _server_row()
        captured: list[object] = []

        async def execute_side_effect(stmt: object) -> MagicMock:
            captured.append(stmt)
            pick = provider if len(captured) == 1 else server
            return MagicMock(scalars=lambda: MagicMock(first=lambda: pick))

        db.execute = AsyncMock(side_effect=execute_side_effect)
        resolver = SqlAlchemyOwnershipResolver(lambda: db)  # type: ignore[arg-type]

        ownership = await resolver.resolve(
            ResourceRef("hetzner", ResourceType.PROVIDER_SERVER, "srv-42")
        )

        assert ownership is not None
        assert ownership.user_id == server.user_id
        assert ownership.server_id == server.id
        # Second statement filters on provider_server_id
        assert "provider_server_id" in _compiled(captured[1])

    async def test_resolves_by_ipv4(self, db: AsyncMock) -> None:
        provider = _provider_row()
        server = _server_row()
        captured: list[object] = []

        async def execute_side_effect(stmt: object) -> MagicMock:
            captured.append(stmt)
            first = len(captured) == 1
            return MagicMock(scalars=lambda: MagicMock(first=lambda: provider if first else server))

        db.execute = AsyncMock(side_effect=execute_side_effect)
        resolver = SqlAlchemyOwnershipResolver(lambda: db)  # type: ignore[arg-type]

        ownership = await resolver.resolve(ResourceRef("hetzner", ResourceType.IPV4, "1.2.3.4"))

        assert ownership is not None
        assert "ipv4" in _compiled(captured[1])

    async def test_unknown_provider_returns_none(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: None))
        )
        resolver = SqlAlchemyOwnershipResolver(lambda: db)  # type: ignore[arg-type]

        ownership = await resolver.resolve(
            ResourceRef("nosuch", ResourceType.PROVIDER_SERVER, "srv-1")
        )
        assert ownership is None
        db.execute.assert_awaited_once()  # never queried servers

    async def test_unmanaged_resource_returns_none(self, db: AsyncMock) -> None:
        provider = _provider_row()

        # provider found, then no matching server
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: provider)),
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),
            ]
        )
        resolver = SqlAlchemyOwnershipResolver(lambda: db)  # type: ignore[arg-type]

        ownership = await resolver.resolve(ResourceRef("hetzner", ResourceType.IPV6, "2001:db8::1"))
        assert ownership is None


class TestAbuseCaseRepository:
    @pytest.fixture
    def repo(self, db: AsyncMock) -> SqlAlchemyAbuseCaseRepository:
        return SqlAlchemyAbuseCaseRepository(lambda: db)  # type: ignore[arg-type]

    async def test_create_commits_and_returns_persisted(self, repo, db: AsyncMock) -> None:
        new_id = uuid4()

        def refresh_side_effect(row: object) -> None:
            row.id = new_id

        db.refresh = AsyncMock(side_effect=refresh_side_effect)
        case = AbuseCase(
            resource=ResourceRef("hetzner", ResourceType.PROVIDER_SERVER, "srv-42"),
            user_id=uuid4(),
            reason="port scan",
            reporter="hetzner-abuse",
        )

        created = await repo.create(case)

        db.add.assert_called_once()
        db.commit.assert_awaited_once()
        assert created.id == new_id
        assert created.status is AbuseStatus.OPEN

    async def test_get_returns_none_when_missing(self, repo, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: None))
        )
        assert await repo.get(uuid4()) is None

    async def test_get_by_user_maps_rows(self, repo, db: AsyncMock) -> None:
        rows = [_case_row("open"), _case_row("investigating")]
        db.execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: rows)))

        cases = await repo.get_by_user(rows[0].user_id)

        assert len(cases) == 2
        assert all(isinstance(c, AbuseCase) for c in cases)
        assert cases[1].status is AbuseStatus.INVESTIGATING

    async def test_list_open_selects_open_and_investigating(self, repo, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: [_case_row()]))
        )
        await repo.list_open()
        compiled = _compiled(db.execute.call_args[0][0])
        assert "'open'" in compiled and "'investigating'" in compiled

    async def test_save_updates_status_and_resolved_at(self, repo, db: AsyncMock) -> None:
        row = _case_row("open")
        target_id = row.id
        db.execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: row)))
        db.refresh = AsyncMock(
            side_effect=lambda r: (
                setattr(r, "status", "resolved"),
                setattr(r, "resolved_at", "2026-08-22"),
            )
        )

        case = AbuseCase(
            resource=ResourceRef("hetzner", ResourceType.PROVIDER_SERVER, "srv-42"),
            user_id=row.user_id,
            reason="port scan",
            reporter="hetzner-abuse",
            status=AbuseStatus.RESOLVED,
            id=target_id,
            resolved_at=None,
        )
        saved = await repo.save(case)

        assert saved.id == target_id
        assert saved.status is AbuseStatus.RESOLVED
        db.commit.assert_awaited_once()

    async def test_save_missing_raises_lookup_error(self, repo, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: None))
        )
        case = AbuseCase(
            resource=ResourceRef("hetzner", ResourceType.PROVIDER_SERVER, "srv-42"),
            user_id=uuid4(),
            reason="r",
            reporter="x",
            status=AbuseStatus.RESOLVED,
            id=uuid4(),
        )
        with pytest.raises(LookupError, match="not found"):
            await repo.save(case)
