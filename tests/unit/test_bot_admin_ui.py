"""Private, role-authorized credits through the real Telegram dispatcher."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser

from cloud_platform.bot.admin_ui import AdminBotUi
from cloud_platform.bot.main import register_handlers
from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.core.i18n import Translator
from cloud_platform.core.money import Money
from cloud_platform.core.session_store import InMemoryBotSessionStore
from cloud_platform.modules.navigation.domain import Callback, encode_telegram_callback
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.modules.wallet.domain import LedgerEntry, LedgerEntryType, Wallet
from cloud_platform.modules.wallet.service import WalletAdminService

KEY = "admin-dispatch-regression-key"
ADMIN_TID = 101
CUSTOMER_TID = 210


class Accounts:
    def __init__(self):
        self.admin = User(
            id=uuid4(),
            username="administrator",
            email="admin@example.test",
            role=Role.ADMIN,
            telegram_user_id=ADMIN_TID,
        )
        self.customer = User(
            id=uuid4(),
            username="customer",
            email="customer@example.test",
            telegram_user_id=CUSTOMER_TID,
        )
        self.users = {user.id: user for user in (self.admin, self.customer)}
        self.wallet = Wallet(id=uuid4(), user_id=self.customer.id, currency="IRT")
        self.entries = {}
        self.fail_next = False
        self.lock = asyncio.Lock()

    async def get(self, identity):
        if identity == self.customer.id:
            return self.wallet
        return None

    async def get_user(self, identity):
        return self.users.get(identity)

    async def get_by_telegram_user_id(self, identity):
        return next(
            (user for user in self.users.values() if user.telegram_user_id == identity), None
        )

    async def adjust(self, user_id, amount, key, **facts):
        async with self.lock:
            if self.fail_next:
                self.fail_next = False
                raise ValueError("temporary unavailable wallet")
            if key not in self.entries:
                assert user_id == self.customer.id
                self.wallet.add_funds(amount)
                self.entries[key] = LedgerEntry(
                    id=uuid4(),
                    wallet_id=self.wallet.id,
                    entry_type=LedgerEntryType.ADJUSTMENT,
                    amount=Money(Decimal(amount), self.wallet.currency),
                    reference_type=facts["reference_type"],
                    reference_id=facts["reference_id"],
                    idempotency_key=key,
                    description=facts["description"],
                )
                return self.wallet, True
            return self.wallet, False

    async def get_entry_by_idempotency(self, wallet_id, key):
        assert wallet_id == self.wallet.id
        return self.entries.get(key)


@pytest.fixture
def route(monkeypatch):
    accounts = Accounts()
    users = SimpleNamespace(
        get=accounts.get_user,
        get_by_telegram_user_id=accounts.get_by_telegram_user_id,
    )
    store = InMemoryBotSessionStore()
    admin = AdminBotUi(
        KEY,
        gateways=SimpleNamespace(list=AsyncMock(return_value=[])),
        users=users,
        wallets=accounts,
        wallet_admin=WalletAdminService(accounts, accounts, user_repo=users),
        store=store,
    )
    monthly = MonthlyBotUi(
        KEY,
        offers_view=MagicMock(),
        checkout=MagicMock(),
        servers=MagicMock(),
        orders=MagicMock(),
        renewals=MagicMock(),
        offers_repo=MagicMock(),
        wallet_history=MagicMock(),
    )
    container = SimpleNamespace(user_repository=lambda: users, wallet_repository=lambda: accounts)
    answer, edit, ack = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(Message, "edit_text", edit)
    monkeypatch.setattr(CallbackQuery, "answer", ack)
    monkeypatch.setattr(
        "cloud_platform.bot.main.get_settings", lambda: SimpleNamespace(callback_signing_key=KEY)
    )
    dp = Dispatcher()
    register_handlers(dp, MagicMock(), monthly, container, admin_ui=admin)
    return SimpleNamespace(
        accounts=accounts,
        admin=admin,
        dp=dp,
        answer=answer,
        edit=edit,
        ack=ack,
        store=store,
    )


async def _text(route, value, actor=ADMIN_TID):
    message = Message(
        message_id=100,
        date=1,
        chat=Chat(id=actor, type="private"),
        from_user=TelegramUser(id=actor, is_bot=False, first_name="Test"),
        text=value,
    )
    await route.dp.feed_update(MagicMock(spec=Bot), Update(update_id=1, message=message))


async def _click(route, data, *, actor=ADMIN_TID, chat=None):
    message = Message(
        message_id=5, date=1, chat=Chat(id=actor if chat is None else chat, type="private")
    )
    query = CallbackQuery(
        id="credit-action",
        from_user=TelegramUser(id=actor, is_bot=False, first_name="Test"),
        chat_instance="private",
        message=message,
        data=data,
    )
    await route.dp.feed_update(MagicMock(spec=Bot), Update(update_id=2, callback_query=query))


def _action(screen, *args):
    return encode_telegram_callback(Callback("admin", screen, args), KEY)


async def _confirmation(route, amount="150000"):
    await _click(route, _action("credit"))
    await _text(route, str(CUSTOMER_TID))
    await _text(route, amount)
    await _text(route, "Refund approved by support")
    return route.answer.await_args.kwargs["reply_markup"]


async def test_confirmed_credit_is_authorized_auditable_and_concurrently_replay_safe(route):
    keyboard = await _confirmation(route)
    confirm = keyboard.inline_keyboard[0][0].callback_data
    assert len(confirm.encode("utf-8")) <= 64
    await asyncio.gather(_click(route, confirm), _click(route, confirm))
    assert route.accounts.wallet.balance == 150000
    (entry,) = route.accounts.entries.values()
    assert entry.amount == Money(Decimal(150000), "IRT")
    assert entry.reference_id == str(route.accounts.admin.id)
    assert entry.description == "Refund approved by support"


@pytest.mark.parametrize("denial", ["customer", "frozen", "cross_chat", "tampered"])
async def test_credit_confirmation_never_grants_privilege_to_a_different_actor(route, denial):
    keyboard = await _confirmation(route)
    confirm = keyboard.inline_keyboard[0][0].callback_data
    actor, chat = ADMIN_TID, ADMIN_TID
    if denial == "customer":
        actor = chat = CUSTOMER_TID
    elif denial == "frozen":
        route.accounts.admin.status = UserStatus.FROZEN
    elif denial == "cross_chat":
        chat = CUSTOMER_TID
    else:
        confirm = confirm[:-1] + ("0" if confirm[-1] != "0" else "1")
    await _click(route, confirm, actor=actor, chat=chat)
    assert route.accounts.wallet.balance == 0
    assert route.accounts.entries == {}


async def test_cancelled_confirmation_cannot_later_credit(route):
    keyboard = await _confirmation(route)
    await _click(route, keyboard.inline_keyboard[1][0].callback_data)
    await _click(route, keyboard.inline_keyboard[0][0].callback_data)
    assert route.accounts.wallet.balance == 0
    assert route.accounts.entries == {}


@pytest.mark.parametrize("navigation", ["/menu", "/start", "nav.menu", "menu.support"])
async def test_navigation_leaves_admin_prompt_instead_of_treating_menu_text_as_credit(
    route, navigation
):
    await _click(route, _action("credit"))
    text = navigation if navigation.startswith("/") else Translator().t(navigation)
    await _text(route, text)
    assert await route.store.get("admin-credit", str(route.accounts.admin.id)) is None
    await _text(route, str(CUSTOMER_TID))
    assert route.accounts.wallet.balance == 0
    assert route.accounts.entries == {}


async def test_failed_credit_reuses_confirmation_without_minting_another_ledger_fact(route):
    keyboard = await _confirmation(route)
    confirm = keyboard.inline_keyboard[0][0].callback_data
    route.accounts.fail_next = True
    await _click(route, confirm)
    assert route.accounts.wallet.balance == 0
    await _click(route, confirm)
    await _click(route, confirm)
    assert route.accounts.wallet.balance == 150000
    assert len(route.accounts.entries) == 1


async def test_confirmation_freezes_wallet_denomination_and_decimal_precision(route):
    route.accounts.wallet.currency = "USD"
    keyboard = await _confirmation(route, "10.50")
    confirm = keyboard.inline_keyboard[0][0].callback_data
    route.accounts.wallet.currency = "IRT"
    await _click(route, confirm)
    assert route.accounts.wallet.balance == 0
    route.accounts.wallet.currency = "USD"
    await _click(route, confirm)
    assert route.accounts.wallet.balance == 1050
    (entry,) = route.accounts.entries.values()
    assert entry.amount == Money(Decimal(1050), "USD")
