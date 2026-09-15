"""Gateway instance ownership (PROD-HARDENING).

A payment gateway adapter owns an ``httpx.AsyncClient``. Two invariants follow:

* the process builds the collection ONCE and hands the very same instances to
  the recharge service (a second collection would open HTTP clients that
  nobody ever closes);
* the process owner closes them exactly once at shutdown — the application
  service never owns infrastructure clients.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_platform.core.container import Container


class _FakeGateway:
    """Minimal payment-gateway double that records closes."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.close = AsyncMock()

    async def create_payment(self, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("not used in these tests")


def _bare_container(monkeypatch: pytest.MonkeyPatch) -> Container:
    """A real ``Container`` with only the collaborators these tests touch."""
    container = object.__new__(Container)
    object.__setattr__(container, "session_factory", None)
    monkeypatch.setattr(Container, "business_event_sink", lambda self: None)
    monkeypatch.setattr(Container, "user_repository", lambda self: None)
    return container


def test_supplied_collection_is_reused_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _bare_container(monkeypatch)
    built = {"tetraminator": _FakeGateway("tetraminator"), "zarinpal": _FakeGateway("zarinpal")}

    def _unexpected_build(self: Container) -> Any:  # pragma: no cover - failure path
        raise AssertionError("payment_gateways() must not be called when a collection is supplied")

    monkeypatch.setattr(Container, "payment_gateways", _unexpected_build)

    service = container.wallet_recharge_service(gateways=built)

    assert service._gateways == built
    # Same objects, not copies: shutting down the collection this process built
    # must close the clients the service is actually using.
    assert service._gateways["tetraminator"] is built["tetraminator"]


def test_collection_is_built_once_when_caller_supplies_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _bare_container(monkeypatch)
    builds: list[int] = []

    def _build(self: Container) -> Any:
        builds.append(1)
        return {"zarinpal": _FakeGateway("zarinpal")}

    monkeypatch.setattr(Container, "payment_gateways", _build)

    service = container.wallet_recharge_service()

    assert len(builds) == 1
    assert set(service._gateways) == {"zarinpal"}


async def test_bot_process_builds_once_reuses_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bot.main`` builds the gateways once and closes the very same set."""
    from cloud_platform.bot import main as bot_main

    tetra = _FakeGateway("tetraminator")
    zarin = _FakeGateway("zarinpal")
    gateways = {"tetraminator": tetra, "zarinpal": zarin}

    container = MagicMock()
    container.payment_gateways = MagicMock(return_value=gateways)
    container.wallet_recharge_service = MagicMock(return_value=MagicMock())

    async def _get_container() -> Any:
        return container

    class _Dispatcher:
        def __init__(self) -> None:
            self.start_polling = AsyncMock()

    dispatcher = _Dispatcher()
    monkeypatch.setattr(bot_main, "get_settings", lambda: MagicMock(telegram_bot_token="1:x"))
    monkeypatch.setattr(bot_main, "get_container", _get_container)
    monkeypatch.setattr(bot_main, "Bot", lambda token: MagicMock())
    # ``dp`` is a module-level singleton, so replace the object itself.
    monkeypatch.setattr(bot_main, "dp", dispatcher)
    monkeypatch.setattr(bot_main, "BotUi", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(bot_main, "MonthlyBotUi", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(bot_main, "register_handlers", lambda *args, **kwargs: None)
    close_container = AsyncMock()
    monkeypatch.setattr(bot_main, "close_container", close_container)

    await bot_main.main()

    dispatcher.start_polling.assert_awaited_once()
    # ONE collection for the process: no orphaned second set of HTTP clients.
    container.payment_gateways.assert_called_once()
    # The recharge service got exactly those instances...
    assert container.wallet_recharge_service.call_args.kwargs["gateways"] is gateways
    # ...and shutdown closed each of them exactly once.
    tetra.close.assert_awaited_once()
    zarin.close.assert_awaited_once()
    close_container.assert_awaited_once()


async def test_shutdown_closes_each_gateway_once_and_survives_a_failure() -> None:
    """Best-effort close: one broken client must not strand the others."""
    good = _FakeGateway("zarinpal")
    broken = _FakeGateway("tetraminator")
    broken.close.side_effect = RuntimeError("socket already gone")

    await Container.aclose_gateways({"zarinpal": good, "tetraminator": broken})

    good.close.assert_awaited_once()
    broken.close.assert_awaited_once()
