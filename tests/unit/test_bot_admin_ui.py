"""Owner-only Telegram administration through real dispatcher and shared session store."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiogram import Bot, Dispatcher
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from aiogram.types import User as TelegramUser

from cloud_platform.bot.admin_ui import SUPER_ADMIN_TELEGRAM_ID, AdminUi
from cloud_platform.bot.main import register_handlers
from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import InMemoryBotSessionStore
from cloud_platform.modules.navigation.domain import Callback, encode_callback

SIGNING_KEY = "local-admin-tests-signing-key"
OWNER = SUPER_ADMIN_TELEGRAM_ID


def _message(user_id: int, text: str, *, chat_id: int | None = None) -> Message:
    return Message(
        message_id=100,
        date=1,
        chat=Chat(id=chat_id if chat_id is not None else user_id, type="private"),
        from_user=TelegramUser(id=user_id, is_bot=False, first_name="Test"),
        text=text,
    )


def _callback(user_id: int, screen: str, *args: str, chat_id: int | None = None) -> CallbackQuery:
    return CallbackQuery(
        id=f"query-{screen}",
        from_user=TelegramUser(id=user_id, is_bot=False, first_name="Test"),
        chat_instance="ci",
        data=encode_callback(Callback("admin", screen, args), SIGNING_KEY),
        # A callback message is authored by the bot, NOT by the clicking user.
        message=Message(
            message_id=50,
            date=1,
            chat=Chat(id=chat_id if chat_id is not None else user_id, type="private"),
            from_user=TelegramUser(id=5555, is_bot=True, first_name="Bot"),
        ),
    )


def _setup(monkeypatch):
    import cloud_platform.bot.main as entrypoint

    monkeypatch.setattr(
        entrypoint, "get_settings", lambda: MagicMock(callback_signing_key=SIGNING_KEY)
    )
    answer, edit, ack = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(Message, "edit_text", edit)
    monkeypatch.setattr(CallbackQuery, "answer", ack)
    users = MagicMock()
    users.get_by_telegram_user_id = AsyncMock(return_value=MagicMock())
    wallet = MagicMock()
    container = MagicMock()
    container.user_repository.return_value = users
    container.wallet_repository.return_value = wallet
    monthly = MagicMock()
    monthly.handle_text = AsyncMock(return_value=None)
    monthly.handle = AsyncMock(return_value=None)
    monthly.menu_screen.return_value = BotScreen(
        "storefront",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Buy a server", callback_data="customer-button")]
            ]
        ),
    )
    monthly.reply_keyboard.return_value = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Wallet")]]
    )
    credit = MagicMock()
    credit.credit_admin_toman = AsyncMock()
    admin = AdminUi(
        SIGNING_KEY,
        sessions=InMemoryBotSessionStore(),
        users=users,
        credit=credit,
    )
    dp = Dispatcher()
    register_handlers(dp, MagicMock(), monthly, container, admin)
    bot = MagicMock(spec=Bot)

    async def send(update: Update) -> None:
        await dp.feed_update(bot, update)

    return send, answer, edit, ack, users, credit, monthly


def test_customer_keeps_menu_and_cannot_invoke_signed_admin_callbacks(monkeypatch) -> None:
    send, answer, edit, ack, _users, credit, monthly = _setup(monkeypatch)

    async def scenario() -> None:
        await send(Update(update_id=1, message=_message(17, "/menu")))
        customer_keyboard = answer.await_args.kwargs["reply_markup"]
        assert len(customer_keyboard.inline_keyboard) == 1
        assert customer_keyboard.inline_keyboard[0][0].text == "Buy a server"
        await send(Update(update_id=2, message=_message(17, Translator().t("admin.menu"))))
        assert (
            answer.await_args.kwargs["reply_markup"].inline_keyboard
            == customer_keyboard.inline_keyboard
        )
        await send(Update(update_id=3, callback_query=_callback(17, "credit")))
        assert ack.await_args.kwargs["show_alert"] is True
        edit.assert_not_awaited()
        credit.credit_admin_toman.assert_not_awaited()
        assert monthly.menu_screen.call_count >= 1

    asyncio.run(scenario())


def test_owner_selects_target_confirms_once_and_retains_customer_menu(monkeypatch) -> None:
    send, answer, edit, _ack, users, credit, _monthly = _setup(monkeypatch)

    async def scenario() -> None:
        await send(Update(update_id=1, message=_message(OWNER, "/start")))
        greeting_keyboard = answer.await_args_list[0].kwargs["reply_markup"]
        assert greeting_keyboard.keyboard[0][0].text == "Wallet"
        assert greeting_keyboard.keyboard[-1][0].text == Translator().t("admin.menu")
        main_keyboard = answer.await_args.kwargs["reply_markup"]
        assert main_keyboard.inline_keyboard[0][0].text == "Buy a server"
        assert main_keyboard.inline_keyboard[-1][0].text == Translator().t("admin.menu")

        await send(Update(update_id=2, callback_query=_callback(OWNER, "credit")))
        assert "شناسه" in edit.await_args.args[0]
        await send(Update(update_id=3, message=_message(OWNER, "210")))
        users.get_by_telegram_user_id.assert_any_await(210)
        await send(Update(update_id=4, message=_message(OWNER, "150000")))
        confirm_keyboard = answer.await_args.kwargs["reply_markup"]
        data = confirm_keyboard.inline_keyboard[0][0].callback_data
        assert "150,000" in answer.await_args.args[0]
        query = CallbackQuery(
            id="confirm",
            from_user=TelegramUser(id=OWNER, is_bot=False, first_name="Test"),
            chat_instance="ci",
            data=data,
            message=Message(message_id=4, date=1, chat=Chat(id=OWNER, type="private")),
        )
        await send(Update(update_id=5, callback_query=query))
        credit.credit_admin_toman.assert_awaited_once()
        args = credit.credit_admin_toman.await_args.args
        assert args[:3] == (OWNER, 210, 150000)
        assert args[3].startswith(f"telegram-admin-{OWNER}-")
        await send(Update(update_id=6, callback_query=query))
        credit.credit_admin_toman.assert_awaited_once()

    asyncio.run(scenario())


def test_owner_callback_must_be_private_and_missing_target_never_credits(monkeypatch) -> None:
    send, answer, _edit, ack, users, credit, _monthly = _setup(monkeypatch)
    users.get_by_telegram_user_id.side_effect = lambda id: None if id == 333 else MagicMock()

    async def scenario() -> None:
        await send(Update(update_id=1, callback_query=_callback(OWNER, "credit", chat_id=42)))
        assert ack.await_args.kwargs["show_alert"] is True
        await send(Update(update_id=2, callback_query=_callback(OWNER, "credit")))
        await send(Update(update_id=3, message=_message(OWNER, "333")))
        assert "یافت نشد" in answer.await_args.args[0]
        credit.credit_admin_toman.assert_not_awaited()

    asyncio.run(scenario())


def test_cancel_confirmation_invalidates_credit_and_invalid_amount_is_rejected(
    monkeypatch,
) -> None:
    send, answer, _edit, _ack, _users, credit, _monthly = _setup(monkeypatch)

    async def scenario() -> None:
        await send(Update(update_id=1, callback_query=_callback(OWNER, "credit")))
        await send(Update(update_id=2, message=_message(OWNER, "210")))
        await send(Update(update_id=3, message=_message(OWNER, "99999999999999999999")))
        assert "عدد صحیح" in answer.await_args.args[0]
        await send(Update(update_id=4, message=_message(OWNER, "150000")))
        keyboard = answer.await_args.kwargs["reply_markup"]
        confirm = keyboard.inline_keyboard[0][0].callback_data
        cancel = keyboard.inline_keyboard[1][0].callback_data
        query = _callback(OWNER, "cancel")
        await send(
            Update(
                update_id=5,
                callback_query=query.model_copy(update={"data": cancel}),
            )
        )
        await send(
            Update(
                update_id=6,
                callback_query=query.model_copy(update={"data": confirm}),
            )
        )
        credit.credit_admin_toman.assert_not_awaited()

    asyncio.run(scenario())


def test_owner_can_leave_admin_prompt_for_customer_menu(monkeypatch) -> None:
    send, answer, _edit, _ack, _users, credit, monthly = _setup(monkeypatch)
    monthly.wallet_screen = AsyncMock(return_value=monthly.menu_screen.return_value)

    async def scenario() -> None:
        await send(Update(update_id=1, callback_query=_callback(OWNER, "credit")))
        await send(Update(update_id=2, message=_message(OWNER, Translator().t("menu.wallet"))))
        monthly.wallet_screen.assert_awaited_once()
        assert answer.await_args.args[0] == "storefront"
        await send(Update(update_id=3, message=_message(OWNER, "150000")))
        assert answer.await_args.args[0] == "storefront"
        credit.credit_admin_toman.assert_not_awaited()

    asyncio.run(scenario())


def test_failed_credit_can_retry_same_confirmation_key(monkeypatch) -> None:
    send, answer, edit, _ack, _users, credit, _monthly = _setup(monkeypatch)
    credit.credit_admin_toman.side_effect = [ValueError("unavailable"), object()]

    async def scenario() -> None:
        await send(Update(update_id=1, callback_query=_callback(OWNER, "credit")))
        await send(Update(update_id=2, message=_message(OWNER, "210")))
        await send(Update(update_id=3, message=_message(OWNER, "12000")))
        confirm = answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        query = _callback(OWNER, "confirm").model_copy(update={"data": confirm})
        await send(Update(update_id=4, callback_query=query))
        assert "انجام نشد" in edit.await_args.args[0]
        await send(Update(update_id=5, callback_query=query))
        calls = credit.credit_admin_toman.await_args_list
        assert len(calls) == 2
        assert calls[0].args == calls[1].args
        assert "شارژ شد" in edit.await_args.args[0]

    asyncio.run(scenario())
