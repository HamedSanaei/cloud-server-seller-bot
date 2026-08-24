"""Tests for My Servers list/detail (M08-007).

Acceptance: pagination and ownership checks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
from cloud_platform.modules.compute.service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MyServersService,
)

USER_ID = uuid4()
OTHER_USER_ID = uuid4()
NAIVE_TS = datetime(2026, 8, 23, 10, 30, 0)
AWARE_TS = NAIVE_TS.replace(tzinfo=UTC)


def _server(
    server_id: UUID,
    user_id: UUID = USER_ID,
    state: ServerLifecycleState = ServerLifecycleState.RUNNING,
    created: datetime | None = NAIVE_TS,
) -> CloudServer:
    return CloudServer(
        id=server_id,
        user_id=user_id,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
        provider_server_id=f"prov-{server_id.hex[:8]}",
        created_at=created,
    )


class FakeServerRepo:
    """Implements the slice of ServerRepository MyServersService uses.

    Mirrors the real repository: stored timestamps are returned aware UTC.
    """

    def __init__(self, servers: list[CloudServer]) -> None:
        self.servers = {s.id: _with_aware(s) for s in servers}
        self.last_user: UUID | None = None

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.servers.get(server_id)

    async def list_by_user_paged(
        self, user_id: UUID, *, offset: int, limit: int
    ) -> tuple[list[CloudServer], int]:
        self.last_user = user_id
        owned = [s for s in self.servers.values() if s.user_id == user_id]
        # Newest first (the service preserves this order).
        owned.sort(key=lambda s: s.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return owned[offset : offset + limit], len(owned)


def _with_aware(server: CloudServer) -> CloudServer:
    if server.created_at is not None and server.created_at.tzinfo is None:
        server.created_at = server.created_at.replace(tzinfo=UTC)
    return server


def _make(servers: list[CloudServer]) -> tuple[MyServersService, FakeServerRepo]:
    repo = FakeServerRepo(servers)
    return MyServersService(repo), repo


class TestListOwnership:
    async def test_only_own_servers_returned(self) -> None:
        mine = _server(uuid4())
        theirs = _server(uuid4(), user_id=OTHER_USER_ID)
        service, repo = _make([mine, theirs])

        page = await service.list_servers(USER_ID)

        assert repo.last_user == USER_ID
        assert [s.server_id for s in page.items] == [mine.id]
        assert page.total == 1

    async def test_user_with_no_servers_gets_empty_page(self) -> None:
        service, _ = _make([_server(uuid4())])

        page = await service.list_servers(OTHER_USER_ID)

        assert page.items == ()
        assert page.total == 0
        assert page.has_next is False
        assert page.pages == 0


class TestPagination:
    async def test_page_slice_and_totals(self) -> None:
        # 25 servers, page of 10: 3 pages; middle page has has_next.
        servers = [_server(uuid4(), created=AWARE_TS - timedelta(minutes=i)) for i in range(25)]
        service, _ = _make(servers)

        first = await service.list_servers(USER_ID, offset=0, limit=10)
        assert len(first.items) == 10
        assert first.total == 25
        assert first.pages == 3
        assert first.has_next is True

        middle = await service.list_servers(USER_ID, offset=10, limit=10)
        assert len(middle.items) == 10
        assert middle.has_next is True

        last = await service.list_servers(USER_ID, offset=20, limit=10)
        assert len(last.items) == 5
        assert last.has_next is False

    async def test_newest_first_order_preserved(self) -> None:
        old = _server(uuid4(), created=AWARE_TS - timedelta(hours=1))
        new = _server(uuid4(), created=AWARE_TS)
        service, _ = _make([old, new])

        page = await service.list_servers(USER_ID)

        assert [s.server_id for s in page.items] == [new.id, old.id]
        assert page.items[0].created_at == AWARE_TS

    async def test_default_page_size(self) -> None:
        service, _ = _make([])
        page = await service.list_servers(USER_ID)
        assert page.limit == DEFAULT_PAGE_SIZE

    @pytest.mark.parametrize("offset,limit", [(-1, 10), (0, 0), (0, MAX_PAGE_SIZE + 1)])
    async def test_invalid_pagination_rejected(self, offset: int, limit: int) -> None:
        service, _ = _make([])
        with pytest.raises(ValueError):
            await service.list_servers(USER_ID, offset=offset, limit=limit)


class TestDetailOwnership:
    async def test_own_server_detail(self) -> None:
        mine = _server(uuid4(), state=ServerLifecycleState.STOPPED)
        service, _ = _make([mine])

        detail = await service.get_server(USER_ID, mine.id)

        assert detail is not None
        assert detail.server_id == mine.id
        assert detail.state is ServerLifecycleState.STOPPED
        assert detail.provider_server_id == mine.provider_server_id
        assert detail.created_at == AWARE_TS

    async def test_foreign_server_is_indistinguishable_from_missing(self) -> None:
        theirs = _server(uuid4(), user_id=OTHER_USER_ID)
        service, _ = _make([theirs])

        assert await service.get_server(USER_ID, theirs.id) is None
        assert await service.get_server(USER_ID, uuid4()) is None


class TestRepositoryPaged:
    def _row(self, server_id: UUID) -> MagicMock:
        row = MagicMock()
        row.id = server_id
        row.user_id = USER_ID
        row.provider_account_id = uuid4()
        row.state = ServerLifecycleState.RUNNING.value
        row.provider_server_id = f"prov-{server_id.hex[:8]}"
        row.contained_from = None
        row.idempotency_key = None
        row.created_at = NAIVE_TS  # naive UTC, as stored
        return row

    async def test_count_then_page_and_aware_timestamps(self) -> None:
        s1, s2 = uuid4(), uuid4()
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        count_result = MagicMock(scalar_one=lambda: 2)
        row_result = MagicMock(
            all=lambda: [
                (self._row(s1), "hetzner"),
                (self._row(s2), "hetzner"),
            ]
        )
        session.execute = AsyncMock(side_effect=[count_result, row_result])
        repo = SqlAlchemyServerRepository(lambda: session)  # type: ignore[arg-type]

        items, total = await repo.list_by_user_paged(USER_ID, offset=0, limit=10)

        assert total == 2
        assert [s.id for s in items] == [s1, s2]
        assert all(s.created_at == AWARE_TS for s in items)
        assert all(s.provider_key == "hetzner" for s in items)
        assert session.execute.await_count == 2
