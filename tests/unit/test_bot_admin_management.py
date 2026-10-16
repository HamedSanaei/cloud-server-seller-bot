"""Real dispatcher + admin UI + application facades, without external calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, ReplyKeyboardMarkup, Update
from aiogram.types import User as TelegramUser

from cloud_platform.bot.admin_ui import AdminBotUi
from cloud_platform.bot.main import register_handlers
from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.core.i18n import Translator
from cloud_platform.modules.admin_console.users import AdminUserService
from cloud_platform.modules.navigation.domain import (
    Callback,
    decode_callback,
    encode_telegram_callback,
)
from cloud_platform.modules.users.domain import UserStatus
from tests.unit.test_admin_console_wallet import case as wallet_case  # noqa: F401

KEY = "admin-management-test-key"


@pytest.fixture
def flow(wallet_case, monkeypatch):  # noqa: F811
    case = wallet_case
    receipts = {}
    case.users.search = AsyncMock(return_value=[case.customer])

    async def set_status(uid, status, **facts):
        receipts.setdefault(facts["idempotency_key"], {"user_id": uid, "status": status, **facts})
        case.customer.status = status
        return status

    case.users.change_status_idempotently = AsyncMock(side_effect=set_status)
    servers = SimpleNamespace(list_by_user_paged=AsyncMock(return_value=([], 0)))
    user_service = AdminUserService(case.users, case.wallets, servers, configured_ids=(101,))
    admin_ui = AdminBotUi(
        KEY,
        gateways=SimpleNamespace(list=AsyncMock(return_value=[])),
        users=case.users,
        wallets=case.wallets,
        wallet_admin=case.wallet_admin,
        store=case.store,
        configured_ids=(101,),
        user_admin=user_service,
        wallet_adjustments=case.service,
    )
    monthly = MonthlyBotUi(
        KEY,
        offers_view=SimpleNamespace(),
        checkout=SimpleNamespace(),
        servers=servers,
        orders=SimpleNamespace(),
        renewals=SimpleNamespace(),
        offers_repo=SimpleNamespace(),
        wallet_history=SimpleNamespace(),
        configured_ids=(101,),
    )
    container = SimpleNamespace(
        user_repository=lambda: case.users, wallet_repository=lambda: case.wallets
    )
    answer, edit, ack = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(Message, "edit_text", edit)
    monkeypatch.setattr(CallbackQuery, "answer", ack)
    monkeypatch.setattr(
        "cloud_platform.bot.main.get_settings",
        lambda: SimpleNamespace(callback_signing_key=KEY, telegram_superadmin_user_ids=(101,)),
    )
    dp = Dispatcher()
    register_handlers(
        dp, SimpleNamespace(handle=AsyncMock()), monthly, container, admin_ui=admin_ui
    )
    return SimpleNamespace(
        case=case,
        dp=dp,
        ui=admin_ui,
        answer=answer,
        edit=edit,
        ack=ack,
        receipts=receipts,
        servers=servers,
    )


async def text(flow, value, actor=101):
    message = Message(
        message_id=1,
        date=1,
        chat=Chat(id=actor, type="private"),
        from_user=TelegramUser(id=actor, first_name="Test", is_bot=False),
        text=value,
    )
    await flow.dp.feed_update(MagicMock(spec=Bot), Update(update_id=1, message=message))


async def click(flow, screen, *args, actor=101):
    data = (
        screen
        if screen.startswith("v1|")
        else encode_telegram_callback(Callback("admin", screen, args), KEY)
    )
    query = CallbackQuery(
        id="admin",
        from_user=TelegramUser(id=actor, first_name="Test", is_bot=False),
        chat_instance="private",
        data=data,
        message=Message(message_id=1, date=1, chat=Chat(id=actor, type="private")),
    )
    await flow.dp.feed_update(MagicMock(spec=Bot), Update(update_id=2, callback_query=query))


def assert_root(flow):
    keyboards = [
        call.kwargs["reply_markup"]
        for call in flow.answer.await_args_list
        if isinstance(call.kwargs.get("reply_markup"), ReplyKeyboardMarkup)
    ]
    assert keyboards
    for keyboard in keyboards:
        labels = [button.text for row in keyboard.keyboard for button in row]
        assert Translator().t("admin.create_server") in labels
        assert Translator().t("menu.wallet") not in labels


async def test_users_search_profile_and_services_are_bounded_and_keep_root(flow):
    await click(flow, "users")
    flow.case.users.search.assert_awaited_once_with("", offset=0, limit=10)
    profile_button = flow.edit.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert len(profile_button.callback_data.encode()) <= 64
    await click(flow, profile_button.callback_data)
    flow.servers.list_by_user_paged.assert_awaited_once_with(
        flow.case.customer.id, offset=0, limit=10
    )
    assert "210" in flow.edit.await_args.args[0]
    await click(flow, "u_search")
    await text(flow, "customer")
    flow.case.users.search.assert_awaited_with("customer", offset=0, limit=10)
    assert_root(flow)


async def status_confirmation(flow):
    await click(flow, "u_status", "210", "banned")
    assert not flow.receipts
    await text(flow, "Reviewed support abuse")
    return flow.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data


async def test_status_change_requires_reason_confirmation_and_replays_same_receipt(flow):
    confirm = await status_confirmation(flow)
    assert not flow.receipts
    assert decode_callback(confirm, KEY).screen == "u_apply"
    await click(flow, confirm)
    await click(flow, confirm)
    assert flow.case.customer.status == UserStatus.BANNED
    assert len(flow.receipts) == 1
    (receipt,) = flow.receipts.values()
    assert receipt["actor_id"] == flow.case.admin.id
    assert receipt["reason"] == "Reviewed support abuse"
    assert_root(flow)


@pytest.mark.parametrize("action", ["cancel", "revoked", "foreign"])
async def test_status_confirmation_is_owner_bound_cancelable_and_freshly_authorized(flow, action):
    confirm = await status_confirmation(flow)
    if action == "cancel":
        await text(flow, "/menu")
    if action == "revoked":
        flow.case.admin.status = UserStatus.FROZEN
    await click(flow, confirm, actor=210 if action == "foreign" else 101)
    assert not flow.receipts


@pytest.mark.parametrize("amount", ["+1200", "-1200", "-۱۲۰۰"])
async def test_toman_credit_debit_is_previewed_and_applied_once(flow, amount):
    await click(flow, "wallet_adjust")
    await text(flow, "210")
    await text(flow, amount)
    await text(flow, "Support correction")
    assert flow.case.wallet.balance == 5000
    confirm = flow.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert len(confirm.encode()) <= 64
    await click(flow, confirm)
    await click(flow, confirm)
    assert flow.case.wallet.balance == (3800 if amount.startswith("-") else 6200)
    assert len(flow.case.entries) == 1
    assert_root(flow)


async def test_wallet_confirmation_canceled_when_entering_users(flow):
    await click(flow, "w_target", "210")
    await text(flow, "+1000")
    await text(flow, "Support correction")
    confirm = flow.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    await click(flow, "users")
    await click(flow, confirm)
    assert flow.case.wallet.balance == 5000 and not flow.case.entries
