"""Tests for the /v1 wallet and server handlers (router.py).

The handler functions are invoked directly (not through TestClient) with
their dependencies injected, which is deterministic on all platforms.
ApiError mapping is asserted against the router's own raise sites and the
error envelope construction.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.api.v1.errors import ApiError, ErrorCode
from cloud_platform.api.v1.router import (
    delete_server,
    get_server,
    get_wallet,
    list_servers,
    server_action,
)
from cloud_platform.modules.tokens.domain import TokenAuthentication, TokenScope

router_module = importlib.import_module("cloud_platform.api.v1.router")

USER_ID = uuid4()
AUTH = TokenAuthentication(user_id=USER_ID, scopes=frozenset(TokenScope), token_id=uuid4())


class FakeContainer:
    def __init__(self) -> None:
        self.session_factory = MagicMock()
        self.power = MagicMock()

    def power_command_service(self) -> MagicMock:
        return self.power


@pytest.fixture
def fake() -> FakeContainer:
    return FakeContainer()


def _session(fake: FakeContainer) -> MagicMock:
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    fake.session_factory.return_value = session
    return session


def _row_result(row: Any) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=row)
    return result


class TestWalletEndpoint:
    async def test_get_wallet_returns_balance(
        self, fake: FakeContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = MagicMock()
        row.id = uuid4()
        row.user_id = USER_ID
        row.balance = 2500
        row.currency = "EUR"
        row.status = "active"
        session = _session(fake)
        session.get = AsyncMock(return_value=row)
        monkeypatch.setattr(router_module, "get_container", AsyncMock(return_value=fake))
        body = await get_wallet(AUTH)
        assert body["wallet"]["balance_minor"] == 2500
        assert body["wallet"]["currency"] == "EUR"

    async def test_get_wallet_missing_raises_404(
        self, fake: FakeContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _session(fake)
        session.get = AsyncMock(return_value=None)
        monkeypatch.setattr(router_module, "get_container", AsyncMock(return_value=fake))
        with pytest.raises(ApiError) as exc_info:
            await get_wallet(AUTH)
        assert exc_info.value.code is ErrorCode.NOT_FOUND


class TestServersEndpoint:
    def _patch_service(self, monkeypatch: pytest.MonkeyPatch, service: MagicMock) -> None:
        monkeypatch.setattr(
            "cloud_platform.modules.compute.service.MyServersService",
            lambda repo: service,
        )

    async def test_list_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = MagicMock()
        item = MagicMock()
        item.server_id = uuid4()
        item.provider_key = "leaseweb"
        item.state = "running"
        item.created_at = None
        page = MagicMock()
        page.items = [item]
        page.total = 1
        page.offset = 0
        page.limit = 20
        service.list_servers = AsyncMock(return_value=page)
        self._patch_service(monkeypatch, service)
        body = await list_servers(AUTH, service)
        assert body["total"] == 1
        assert body["servers"][0]["provider_key"] == "leaseweb"

    async def test_get_server_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = MagicMock()
        detail = MagicMock()
        detail.server_id = uuid4()
        detail.provider_key = "leaseweb"
        detail.state = "running"
        detail.provider_server_id = "p-1"
        detail.created_at = None
        service.get_server = AsyncMock(return_value=detail)
        self._patch_service(monkeypatch, service)
        body = await get_server(AUTH, detail.server_id, service)
        assert body["server"]["provider_server_id"] == "p-1"

    async def test_get_server_foreign_raises_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = MagicMock()
        service.get_server = AsyncMock(return_value=None)
        self._patch_service(monkeypatch, service)
        with pytest.raises(ApiError) as exc_info:
            await get_server(AUTH, uuid4(), service)
        assert exc_info.value.code is ErrorCode.NOT_FOUND

    async def test_delete_server_is_not_implemented(self) -> None:
        with pytest.raises(ApiError) as exc_info:
            await delete_server(AUTH, uuid4(), "ik-x")
        assert exc_info.value.code is ErrorCode.NOT_IMPLEMENTED


class TestServerActions:
    def _outcome(self) -> MagicMock:
        outcome = MagicMock()
        outcome.replayed = False
        outcome.requeued = False
        outcome.server.state.value = "running"
        return outcome

    async def test_power_on_success(
        self, fake: FakeContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake.power.power_on = AsyncMock(return_value=self._outcome())
        monkeypatch.setattr(router_module, "get_container", AsyncMock(return_value=fake))
        server_id = uuid4()
        body = await server_action(AUTH, server_id, "power-on", "ik-1")
        assert body["action"] == "power-on"
        fake.power.power_on.assert_awaited_once()
        fake.power.power_on.assert_awaited_with(USER_ID, server_id, "ik-1")

    async def test_unknown_action_raises_404(
        self, fake: FakeContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router_module, "get_container", AsyncMock(return_value=fake))
        with pytest.raises(ApiError) as exc_info:
            await server_action(AUTH, uuid4(), "defenestrate", "ik-2")
        assert exc_info.value.code is ErrorCode.NOT_FOUND

    async def test_foreign_server_raises_404(
        self, fake: FakeContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cloud_platform.modules.operations.service import NotServerOwnerError

        fake.power.power_on = AsyncMock(side_effect=NotServerOwnerError("nope"))
        monkeypatch.setattr(router_module, "get_container", AsyncMock(return_value=fake))
        with pytest.raises(ApiError) as exc_info:
            await server_action(AUTH, uuid4(), "power-on", "ik-3")
        assert exc_info.value.code is ErrorCode.NOT_FOUND

    async def test_action_not_allowed_raises_409(
        self, fake: FakeContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cloud_platform.modules.operations.service import PowerActionNotAllowedError

        fake.power.power_on = AsyncMock(side_effect=PowerActionNotAllowedError("cannot power on"))
        monkeypatch.setattr(router_module, "get_container", AsyncMock(return_value=fake))
        with pytest.raises(ApiError) as exc_info:
            await server_action(AUTH, uuid4(), "power-on", "ik-4")
        assert exc_info.value.code is ErrorCode.ACTION_NOT_ALLOWED

    async def test_operation_in_progress_raises_conflict(
        self, fake: FakeContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cloud_platform.modules.operations.service import (
            PowerOperationInProgressError,
        )

        fake.power.power_on = AsyncMock(side_effect=PowerOperationInProgressError("busy"))
        monkeypatch.setattr(router_module, "get_container", AsyncMock(return_value=fake))
        with pytest.raises(ApiError) as exc_info:
            await server_action(AUTH, uuid4(), "power-on", "ik-5")
        assert exc_info.value.code is ErrorCode.OPERATION_IN_PROGRESS

    async def test_power_failed_raises_conflict(
        self, fake: FakeContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cloud_platform.modules.operations.service import (
            PowerOperationFailedError,
        )

        fake.power.power_on = AsyncMock(side_effect=PowerOperationFailedError("boom"))
        monkeypatch.setattr(router_module, "get_container", AsyncMock(return_value=fake))
        with pytest.raises(ApiError) as exc_info:
            await server_action(AUTH, uuid4(), "power-on", "ik-6")
        assert exc_info.value.code is ErrorCode.CONFLICT
