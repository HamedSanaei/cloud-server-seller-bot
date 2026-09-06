"""Tests for ``cloud_platform.bot.main`` handler wiring.

Exercises the real aiogram ``Dispatcher`` with fake UI/container objects so
the entrypoint's command and callback handlers are covered end-to-end
without a network or database. aiogram model instances are frozen, so the
outgoing ``Message.answer`` / ``edit_text`` / ``CallbackQuery.answer``
methods are patched at the class level.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser

from cloud_platform.bot.main import register_handlers
from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.bot.ui import BotUi

SCREEN = MagicMock(text="screen", keyboard=MagicMock())


@pytest.fixture(autouse=True)
def _patch_outgoing(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, AsyncMock]:
    answer = AsyncMock()
    edit = AsyncMock()
    ack = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(Message, "edit_text", edit)
    monkeypatch.setattr(CallbackQuery, "answer", ack)
    return {"answer": answer, "edit": edit, "ack": ack}


@pytest.fixture
def fakes() -> dict[str, Any]:
    container = MagicMock()
    user_repo = AsyncMock()
    user_repo.get_by_telegram_user_id = AsyncMock(return_value=MagicMock())
    wallet_repo = AsyncMock()
    container.user_repository = MagicMock(return_value=user_repo)
    container.wallet_repository = MagicMock(return_value=wallet_repo)
    ui = MagicMock(spec=BotUi)
    ui.menu_screen = MagicMock(return_value=SCREEN)
    ui.handle = AsyncMock(return_value=SCREEN)
    monthly_ui = MagicMock(spec=MonthlyBotUi)
    monthly_ui.handle = AsyncMock(return_value=None)
    return {"container": container, "ui": ui, "monthly_ui": monthly_ui}


def _dispatch(dp: Dispatcher, update: Update) -> None:
    import asyncio

    bot = MagicMock(spec=Bot)
    asyncio.run(dp.feed_update(bot, update))


def _message(text: str, *, user_id: int = 10) -> Message:
    return Message(
        message_id=1,
        date=1,
        chat=Chat(id=100, type="private"),
        from_user=TelegramUser(id=user_id, is_bot=False, first_name="T"),
        text=text,
    )


def test_start_command_answers_greeting(
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    from cloud_platform.bot.main import dp

    _dispatch(dp, Update(update_id=1, message=_message("/start")))
    _patch_outgoing["answer"].assert_awaited_once()


def test_menu_command_renders_menu(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    _dispatch(dp, Update(update_id=2, message=_message("/menu", user_id=42)))
    _patch_outgoing["answer"].assert_awaited_once()
    assert _patch_outgoing["answer"].await_args.args[0] == "screen"
    fakes["ui"].menu_screen.assert_called_once()


def test_help_command_answers(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    _dispatch(dp, Update(update_id=3, message=_message("/help")))
    _patch_outgoing["answer"].assert_awaited_once()


def test_callback_routes_to_monthly_ui_first(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    query = CallbackQuery(
        id="q1",
        from_user=TelegramUser(id=7, is_bot=False, first_name="B"),
        chat_instance="ci",
        data="lsw:offers:list",
        message=_message("/menu"),
    )
    fakes["monthly_ui"].handle.return_value = SCREEN
    _dispatch(dp, Update(update_id=4, callback_query=query))
    fakes["monthly_ui"].handle.assert_awaited_once()
    _patch_outgoing["edit"].assert_awaited_once()
    _patch_outgoing["ack"].assert_awaited_once()


def test_callback_falls_back_to_legacy_ui(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    query = CallbackQuery(
        id="q2",
        from_user=TelegramUser(id=7, is_bot=False, first_name="B"),
        chat_instance="ci",
        data="hetzner:rebuild:confirm",
        message=_message("/menu"),
    )
    _dispatch(dp, Update(update_id=5, callback_query=query))
    fakes["monthly_ui"].handle.assert_awaited_once()
    fakes["ui"].handle.assert_awaited_once()
    assert fakes["ui"].handle.await_args.args[0] == "hetzner:rebuild:confirm"
    _patch_outgoing["edit"].assert_awaited_once()


def test_callback_edit_failure_is_tolerated(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    query = CallbackQuery(
        id="q3",
        from_user=TelegramUser(id=7, is_bot=False, first_name="B"),
        chat_instance="ci",
        data="lsw:offers:list",
        message=_message("/menu"),
    )
    _patch_outgoing["edit"].side_effect = RuntimeError("message too old")
    fakes["monthly_ui"].handle.return_value = SCREEN
    _dispatch(dp, Update(update_id=6, callback_query=query))
    _patch_outgoing["ack"].assert_awaited_once()
