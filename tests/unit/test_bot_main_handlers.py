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
from cloud_platform.modules.navigation.domain import Callback, encode_callback

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
    monthly_ui.menu_screen = MagicMock(return_value=SCREEN)
    return {"container": container, "ui": ui, "monthly_ui": monthly_ui}


def _dispatch(dp: Dispatcher, update: Update) -> None:
    import asyncio

    bot = MagicMock(spec=Bot)
    asyncio.run(dp.feed_update(bot, update))


def _message(text: str, *, user_id: int = 10, username: str | None = None) -> Message:
    return Message(
        message_id=1,
        date=1,
        chat=Chat(id=100, type="private"),
        from_user=TelegramUser(id=user_id, is_bot=False, first_name="T", username=username),
        text=text,
    )


def test_start_command_shows_greeting_plus_menu(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    from cloud_platform.core.i18n import get_catalog, Locale

    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    _dispatch(dp, Update(update_id=1, message=_message("/start")))
    # Exactly one message: greeting composed with the canonical menu.
    _patch_outgoing["answer"].assert_awaited_once()
    sent = _patch_outgoing["answer"].await_args.args[0]
    assert get_catalog(Locale.FA).table["greeting.start"] in sent
    assert "screen" in sent
    fakes["monthly_ui"].menu_screen.assert_called_once()
    # Onboarding ran for the new contact.
    fakes["container"].user_repository().get_by_telegram_user_id.assert_awaited_once_with(10)


def test_menu_command_renders_menu(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    _dispatch(dp, Update(update_id=2, message=_message("/menu", user_id=42)))
    _patch_outgoing["answer"].assert_awaited_once()
    assert _patch_outgoing["answer"].await_args.args[0] == "screen"
    # The customer menu is the storefront's (market selector first).
    fakes["monthly_ui"].menu_screen.assert_called_once()


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cloud_platform.bot.main as bot_main

    # A signed but non-monthly callback exercises the true fallback chain
    # (unsigned garbage takes the menu-plus-notice path instead).
    signing_key = "test-signing-key-for-fallback"
    monkeypatch.setattr(
        bot_main, "get_settings", lambda: MagicMock(callback_signing_key=signing_key)
    )
    data = encode_callback(Callback(flow="buy", screen="confirm", args=("x",)), signing_key)
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    query = CallbackQuery(
        id="q2",
        from_user=TelegramUser(id=7, is_bot=False, first_name="B"),
        chat_instance="ci",
        data=data,
        message=_message("/menu"),
    )
    _dispatch(dp, Update(update_id=5, callback_query=query))
    fakes["monthly_ui"].handle.assert_awaited_once()
    fakes["ui"].handle.assert_awaited_once()
    assert fakes["ui"].handle.await_args.args[0] == data
    _patch_outgoing["edit"].assert_awaited_once()


def test_foreign_callback_renders_menu_with_notice(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    """Tampered/foreign button data is rejected into menu + notice (no dead end)."""
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    query = CallbackQuery(
        id="q9",
        from_user=TelegramUser(id=7, is_bot=False, first_name="B"),
        chat_instance="ci",
        data="not-a-signed-callback!!!",
        message=_message("/menu"),
    )
    _dispatch(dp, Update(update_id=9, callback_query=query))
    fakes["ui"].handle.assert_not_awaited()
    _patch_outgoing["edit"].assert_awaited_once()
    sent = _patch_outgoing["edit"].await_args.args[0]
    assert "screen" in sent  # canonical menu body is present


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


def test_menu_shows_same_canonical_menu_as_start(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    _dispatch(dp, Update(update_id=11, message=_message("/menu")))
    _patch_outgoing["answer"].assert_awaited_once()
    assert _patch_outgoing["answer"].await_args.args[0] == "screen"
    fakes["monthly_ui"].menu_screen.assert_called_once()


def test_start_creates_user_and_wallet_for_new_contact(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    user_repo = fakes["container"].user_repository()
    user_repo.get_by_telegram_user_id = AsyncMock(return_value=None)
    wallet_repo = fakes["container"].wallet_repository()
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    _dispatch(dp, Update(update_id=12, message=_message("/start", user_id=99, username="newbie")))
    user_repo.get_by_telegram_user_id.assert_awaited_once_with(99)
    user_repo.create.assert_awaited_once()
    wallet_repo.get_or_create.assert_awaited_once()
    _patch_outgoing["answer"].assert_awaited_once()


def test_random_text_without_pending_prompt_shows_menu(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    fakes["monthly_ui"].handle_text = AsyncMock(return_value=None)
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    for update_id, text in ((21, "سلام"), (22, "test"), (23, "?")):
        _patch_outgoing["answer"].reset_mock()
        fakes["monthly_ui"].menu_screen.reset_mock()
        _dispatch(dp, Update(update_id=update_id, message=_message(text)))
        _patch_outgoing["answer"].assert_awaited_once()
        assert _patch_outgoing["answer"].await_args.args[0] == "screen"
        fakes["monthly_ui"].menu_screen.assert_called_once()


def test_unknown_slash_command_shows_menu(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    fakes["monthly_ui"].handle_text = AsyncMock(return_value=None)
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    _dispatch(dp, Update(update_id=24, message=_message("/buy123")))
    _patch_outgoing["answer"].assert_awaited_once()
    assert _patch_outgoing["answer"].await_args.args[0] == "screen"
    fakes["monthly_ui"].menu_screen.assert_called_once()


def _media_message(*, user_id: int = 10) -> Message:
    from aiogram.types import PhotoSize

    return Message(
        message_id=2,
        date=2,
        chat=Chat(id=100, type="private"),
        from_user=TelegramUser(id=user_id, is_bot=False, first_name="T"),
        photo=[PhotoSize(file_id="p", file_unique_id="u", width=1, height=1)],
        caption=None,
    )


def test_unsupported_media_shows_menu(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    fakes["monthly_ui"].handle_text = AsyncMock(return_value=None)
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    _dispatch(dp, Update(update_id=25, message=_media_message()))
    _patch_outgoing["answer"].assert_awaited_once()
    assert _patch_outgoing["answer"].await_args.args[0] == "screen"
    fakes["monthly_ui"].menu_screen.assert_called_once()


def test_active_server_name_prompt_is_not_overridden(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    fakes["monthly_ui"].handle_text = AsyncMock(return_value=SCREEN)
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    _dispatch(dp, Update(update_id=26, message=_message("my-new-server")))
    _patch_outgoing["answer"].assert_awaited_once()
    assert _patch_outgoing["answer"].await_args.args[0] == "screen"
    fakes["monthly_ui"].handle_text.assert_awaited_once()
    fakes["monthly_ui"].menu_screen.assert_not_called()


def test_active_reverse_dns_prompt_is_not_overridden(
    fakes: dict[str, Any],
    _patch_outgoing: dict[str, AsyncMock],
) -> None:
    fakes["monthly_ui"].handle_text = AsyncMock(return_value=SCREEN)
    dp = Dispatcher()
    register_handlers(dp, fakes["ui"], fakes["monthly_ui"], fakes["container"])
    _dispatch(dp, Update(update_id=27, message=_message("host.example.com")))
    _patch_outgoing["answer"].assert_awaited_once()
    fakes["monthly_ui"].handle_text.assert_awaited_once()
    fakes["monthly_ui"].menu_screen.assert_not_called()
