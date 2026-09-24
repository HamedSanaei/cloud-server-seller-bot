"""CRUD coverage for SqlAlchemyServerRepository with scripted sessions.

Results are served in call order, exactly as the implementation issues
them (the same scripted-session shape as the wallet adjust tests): each
``execute`` pops the next queued result, so every test pins the real
query sequence (provider lookup -> insert, row fetch -> mutate, ...).

Complements tests/unit/test_compute_repository_create.py, which uses
MagicMock sessions for the create path only.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerCreateError,
    ServerCreateIntent,
    ServerLifecycleState,
)
from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository

PROVIDER_ID = uuid4()


class _Result:
    """One queued execute result supporting every accessor the repo uses."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def first(self) -> Any:
        return self._value

    def all(self) -> list[Any]:
        if self._value is None:
            return []
        return self._value if isinstance(self._value, list) else [self._value]

    def scalars(self) -> _Result:
        return self

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        assert self._value is not None, "scalar_one on empty result"
        return self._value

    def scalar(self) -> Any:
        return self._value


class _ScriptedSession:
    """Session double serving queued execute results in call order."""

    def __init__(self, results: list[_Result], *, commit_error: Exception | None = None) -> None:
        self._results = list(results)
        self._commit_error = commit_error
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.executes = 0
        self.refreshed: list[Any] = []

    async def __aenter__(self) -> _ScriptedSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> _Result:
        self.executes += 1
        assert self._results, "no queued result left for execute"
        return self._results.pop(0)

    async def get(self, model: Any, identity: Any) -> Any:
        return None

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        if self._commit_error is not None:
            raise self._commit_error
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, row: Any) -> None:
        self.refreshed.append(row)


def _repo(session: _ScriptedSession) -> SqlAlchemyServerRepository:
    return SqlAlchemyServerRepository(lambda: session)  # type: ignore[arg-type]


def _server(**overrides: Any) -> CloudServer:
    values: dict[str, Any] = {
        "id": uuid4(),
        "user_id": uuid4(),
        "provider_key": "hetzner",
        "provider_account_id": uuid4(),
        "state": ServerLifecycleState.REQUESTED,
    }
    values.update(overrides)
    return CloudServer(**values)


def _intent(key: str = "op-1") -> ServerCreateIntent:
    return ServerCreateIntent(
        catalog_id=uuid4(), cost_minor=100, currency="EUR", idempotency_key=key
    )


def _row(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid4(),
        "user_id": uuid4(),
        "provider_account_id": uuid4(),
        "state": "requested",
        "provider_server_id": None,
        "ipv4": None,
        "ipv6": None,
        "contained_from": None,
        "idempotency_key": "op-1",
        "created_at": datetime(2026, 3, 1, 12, 0, 0),
        "updated_at": datetime(2026, 3, 1, 12, 0, 0),
        "last_accrued_at": None,
        "deleted_at": None,
        "low_balance_since": None,
        "quantum_seconds": 3600,
        "billing_model": "hourly",
        "os": None,
        "credential_account_id": None,
        "image_id": None,
        "offer_fingerprint": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _conflict() -> IntegrityError:
    return IntegrityError(
        "INSERT INTO servers (id) VALUES (%(id)s)",
        {"id": "x"},
        Exception('duplicate key value violates constraint "uq_servers_idempotency"'),
    )


class TestCreate:
    async def test_persists_row_and_returns_domain(self) -> None:
        session = _ScriptedSession([_Result(SimpleNamespace(id=PROVIDER_ID))])
        server = _server()

        created = await _repo(session).create(server, _intent("op-9"))

        assert created.id == server.id
        assert created.state is ServerLifecycleState.REQUESTED
        assert created.provider_key == "hetzner"
        assert session.commits == 1
        assert session.rollbacks == 0
        assert len(session.added) == 1
        row = session.added[0]
        assert row.provider_id == PROVIDER_ID
        assert row.provider_account_id == server.provider_account_id
        assert row.state == "requested"
        assert row.idempotency_key == "op-9"

    async def test_unknown_provider_raises_without_writing(self) -> None:
        session = _ScriptedSession([_Result(None)])

        with pytest.raises(LookupError, match="not found"):
            await _repo(session).create(_server(), _intent())

        assert session.added == []
        assert session.commits == 0

    async def test_conflict_maps_to_server_create_error(self) -> None:
        session = _ScriptedSession(
            [_Result(SimpleNamespace(id=PROVIDER_ID))], commit_error=_conflict()
        )

        with pytest.raises(ServerCreateError, match="idempotency key"):
            await _repo(session).create(_server(), _intent("op-dup"))

        assert session.rollbacks == 1
        assert session.commits == 0


class TestGet:
    async def test_hit_maps_row_and_normalizes_timestamps(self) -> None:
        row = _row(
            contained_from="provisioning",
            quantum_seconds=None,
            image_id="img-1",
            offer_fingerprint={"version": 1},
        )
        session = _ScriptedSession([_Result((row, "hetzner"))])

        server = await _repo(session).get(row.id)

        assert server is not None
        assert server.id == row.id
        assert server.provider_key == "hetzner"
        assert server.contained_from is ServerLifecycleState.PROVISIONING
        assert server.image_id == "img-1"
        assert server.offer_fingerprint == {"version": 1}
        assert server.quantum_seconds == 3600
        # Naive DB timestamps are normalized to aware UTC.
        assert server.created_at is not None and server.created_at.tzinfo is not None

    async def test_miss_returns_none(self) -> None:
        session = _ScriptedSession([_Result(None)])

        assert await _repo(session).get(uuid4()) is None


class TestSave:
    async def test_persists_mutation_and_returns_updated_domain(self) -> None:
        row = _row(state="requested")
        session = _ScriptedSession([_Result((row, "hetzner"))])
        server = _server(
            id=row.id,
            user_id=row.user_id,
            provider_account_id=row.provider_account_id,
            state=ServerLifecycleState.PROVISIONING,
            provider_server_id="srv-9",
        )

        saved = await _repo(session).save(server)

        assert row.state == "provisioning"
        assert row.provider_server_id == "srv-9"
        assert saved.state is ServerLifecycleState.PROVISIONING
        assert saved.provider_server_id == "srv-9"
        assert session.commits == 1
        assert session.refreshed == [row]

    async def test_missing_row_raises(self) -> None:
        session = _ScriptedSession([_Result(None)])

        with pytest.raises(LookupError, match="not found"):
            await _repo(session).save(_server())

        assert session.commits == 0


class TestListsAndIdempotency:
    async def test_list_requested_maps_rows(self) -> None:
        rows = [(_row(), "hetzner"), (_row(), "hetzner")]
        session = _ScriptedSession([_Result(rows)])

        servers = await _repo(session).list_requested()

        assert len(servers) == 2
        assert all(s.state is ServerLifecycleState.REQUESTED for s in servers)
        assert all(s.provider_key == "hetzner" for s in servers)

    async def test_list_provisioning_empty(self) -> None:
        session = _ScriptedSession([_Result([])])

        assert await _repo(session).list_provisioning() == []

    async def test_get_by_idempotency_key_hit(self) -> None:
        row = _row(idempotency_key="op-7")
        session = _ScriptedSession([_Result((row, "hetzner"))])

        server = await _repo(session).get_by_idempotency_key("op-7")

        assert server is not None
        assert server.id == row.id
        assert server.idempotency_key == "op-7"

    async def test_get_by_idempotency_key_miss(self) -> None:
        session = _ScriptedSession([_Result(None)])

        assert await _repo(session).get_by_idempotency_key("nope") is None

    async def test_get_by_id_distinguishes_uuid_types(self) -> None:
        wanted: UUID = uuid4()
        session = _ScriptedSession([_Result(None)])

        assert await _repo(session).get(wanted) is None
        assert session.executes == 1
