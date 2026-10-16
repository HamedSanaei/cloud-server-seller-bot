"""Superadmin root reply keyboard, admin console and wallet interception.

A configured Telegram Super Admin navigates the same bot as a customer but
must never be offered a personal wallet: no responsive button, no signed
wallet/recharge callback and, once the identity is revoked, an immediate
fallback to the customer keyboard. Everything here uses synthetic Telegram id
``42``/``44`` and a throwaway signing key.

The dispatcher tests mirror ``tests/unit/test_bot_admin_commands.py``: a real
aiogram ``Dispatcher`` with fake container/UI objects, and the outgoing
``Message.answer`` / ``Message.edit_text`` / ``CallbackQuery.answer`` methods
patched at class level because aiogram model instances are frozen. A real
``MonthlyBotUi`` (with a shared ``ServerSessions`` store) is registered so the
admin-root reply keyboard, the admin menu screen and ``clear_transient_input``
are the production implementations.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from aiogram.types import User as TelegramUser

from cloud_platform.bot.admin_ui import AdminBotUi
from cloud_platform.bot.identity_flow import IdentityFlow
from cloud_platform.bot.main import register_handlers
from cloud_platform.bot.monthly_ui import (
    MonthlyBotUi,
    build_admin_reply_keyboard,
    build_main_reply_keyboard,
)
from cloud_platform.bot.sessions import ServerSessions
from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import (
    NS_PROMPT,
    InMemoryBotSessionStore,
    SessionStoreUnavailable,
)
from cloud_platform.modules.navigation.domain import (
    Callback,
    decode_callback,
    encode_callback,
)
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.modules.users.identity import IdentityService

SIGNING_KEY = "superadmin-root-test-signing-key"
CONFIGURED_IDS = (42,)
ADMIN_ID = 42
CUSTOMER_ID = 44

ADMIN_ROOT_KEYS = (
    "admin.create_server",
    "admin.renew_server",
    "menu.servers",
    "menu.admin",
    "menu.support",
    "nav.menu",
)


# -- small helpers ---------------------------------------------------------


def _labels(keyboard: ReplyKeyboardMarkup) -> list[str]:
    return [button.text for row in keyboard.keyboard for button in row]


def _flows(keyboard: InlineKeyboardMarkup) -> set[str]:
    return {
        decode_callback(button.callback_data, SIGNING_KEY).flow
        for row in keyboard.inline_keyboard
        for button in row
    }


def _answer_texts(answer: AsyncMock) -> list[str]:
    return [str(call.args[0]) for call in answer.await_args_list if call.args]


def _reply_keyboards(answer: AsyncMock) -> list[ReplyKeyboardMarkup]:
    return [
        call.kwargs["reply_markup"]
        for call in answer.await_args_list
        if isinstance(call.kwargs.get("reply_markup"), ReplyKeyboardMarkup)
    ]


def _admin(**overrides: object) -> User:
    data: dict[str, object] = {
        "id": uuid4(),
        "username": "admin",
        "email": "a@example.test",
        "role": Role.ADMIN,
        "status": UserStatus.ACTIVE,
        "telegram_user_id": ADMIN_ID,
    }
    data.update(overrides)
    return User(**data)


def _customer(**overrides: object) -> User:
    data: dict[str, object] = {
        "id": uuid4(),
        "username": "customer",
        "email": "c@example.test",
        "role": Role.USER,
        "status": UserStatus.ACTIVE,
        "telegram_user_id": CUSTOMER_ID,
    }
    data.update(overrides)
    return User(**data)


def _ui(
    *,
    store: InMemoryBotSessionStore | None = None,
    configured_ids: tuple[int, ...] = CONFIGURED_IDS,
) -> MonthlyBotUi:
    sessions = ServerSessions(store) if store is not None else None
    return MonthlyBotUi(
        SIGNING_KEY,
        offers_view=SimpleNamespace(),
        checkout=SimpleNamespace(),
        servers=SimpleNamespace(),
        orders=SimpleNamespace(),
        renewals=SimpleNamespace(),
        offers_repo=SimpleNamespace(),
        wallet_history=SimpleNamespace(),
        sessions=sessions,
        configured_ids=configured_ids,
    )


def _message(text: str, *, actor: int = ADMIN_ID) -> Message:
    return Message(
        message_id=7,
        date=1,
        chat=Chat(id=actor, type="private"),
        from_user=TelegramUser(id=actor, is_bot=False, first_name="A"),
        text=text,
    )


# -- keyboard + menu rendering (pure) --------------------------------------


def test_admin_reply_keyboard_has_no_personal_wallet() -> None:
    t = Translator()
    keyboards = (
        build_admin_reply_keyboard(t),
        build_main_reply_keyboard(t, _admin(), configured_ids=CONFIGURED_IDS),
    )
    for keyboard in keyboards:
        texts = _labels(keyboard)
        assert keyboard.is_persistent is True
        assert keyboard.resize_keyboard is True
        for key in ADMIN_ROOT_KEYS:
            assert t.t(key) in texts
        assert t.t("menu.wallet") not in texts
        assert t.t("menu.recharge") not in texts


def test_customer_reply_keyboard_unchanged() -> None:
    t = Translator()
    keyboard = build_main_reply_keyboard(t, _customer(), configured_ids=CONFIGURED_IDS)
    assert [[button.text for button in row] for row in keyboard.keyboard] == [
        [t.t("menu.buy"), t.t("menu.servers")],
        [t.t("menu.wallet"), t.t("menu.recharge")],
        [t.t("menu.support"), t.t("nav.menu")],
    ]
    texts = _labels(keyboard)
    assert t.t("menu.wallet") in texts
    assert t.t("menu.recharge") in texts
    assert t.t("admin.create_server") not in texts


def test_admin_menu_screen_flows_exclude_wallet() -> None:
    t = Translator()
    screen = _ui().menu_screen(_admin())
    assert screen.text == t.t("admin.root_title")
    flows = _flows(screen.keyboard)
    assert {"admin", "servers", "support", "store", "main"} <= flows
    assert "wallet" not in flows
    assert "recharge" not in flows


def test_customer_menu_screen_unchanged() -> None:
    flows = _flows(_ui().menu_screen(_customer()).keyboard)
    assert "recharge" in flows
    assert "admin" not in flows


@pytest.mark.parametrize(
    "case",
    ["role_user", "unconfigured", "frozen", "banned", "none", "non_user"],
)
def test_role_only_or_unconfigured_or_inactive_is_not_admin(case: str) -> None:
    t = Translator()
    ui = _ui()
    if case == "role_user":
        user: object = _admin(role=Role.USER)
    elif case == "unconfigured":
        user = _admin(telegram_user_id=99)
    elif case == "frozen":
        user = _admin(status=UserStatus.FROZEN)
    elif case == "banned":
        user = _admin(status=UserStatus.BANNED)
    elif case == "none":
        user = None
    else:
        user = object()

    keyboard = build_main_reply_keyboard(t, user, configured_ids=CONFIGURED_IDS)
    texts = _labels(keyboard)
    assert t.t("admin.create_server") not in texts
    assert t.t("menu.recharge") in texts

    screen = ui.menu_screen(user)
    flows = _flows(screen.keyboard)
    assert "admin" not in flows
    assert "recharge" in flows
    assert "admin" not in _flows(ui.admin_menu_screen(user).keyboard)


# -- dispatcher wiring -----------------------------------------------------


@pytest.fixture
def flow(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    admin = _admin()
    customer = _customer()
    users = {admin.id: admin, customer.id: customer}
    repository = SimpleNamespace(
        get=AsyncMock(side_effect=lambda uid: users.get(uid)),
        onboard_telegram=AsyncMock(
            side_effect=lambda tid, _username, _email: next(
                (u for u in users.values() if u.telegram_user_id == tid), None
            )
        ),
        get_by_telegram_user_id=AsyncMock(
            side_effect=lambda tid: next(
                (u for u in users.values() if u.telegram_user_id == tid), None
            )
        ),
    )
    store = InMemoryBotSessionStore()
    monthly_ui = _ui(store=store)
    admin_ui = AdminBotUi(
        SIGNING_KEY,
        gateways=SimpleNamespace(list=AsyncMock(return_value=[])),
        users=repository,
        wallets=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(currency="USD"))),
        wallet_admin=SimpleNamespace(adjust_balance=AsyncMock()),
        store=store,
        configured_ids=CONFIGURED_IDS,
    )
    monkeypatch.setattr(admin_ui, "menu", MagicMock(wraps=admin_ui.menu))
    monkeypatch.setattr(admin_ui, "handle", AsyncMock(wraps=admin_ui.handle))
    identity = IdentityFlow(IdentityService(repository), store, ttl_seconds=900)
    container = MagicMock()
    container.user_repository.return_value = repository
    container.wallet_repository.return_value = SimpleNamespace()

    answer, edit, ack = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(Message, "edit_text", edit)
    monkeypatch.setattr(CallbackQuery, "answer", ack)
    monkeypatch.setattr(
        "cloud_platform.bot.main.get_settings",
        lambda: SimpleNamespace(
            callback_signing_key=SIGNING_KEY,
            telegram_superadmin_user_ids=[ADMIN_ID],
        ),
    )
    dp = Dispatcher()
    legacy = SimpleNamespace(handle=AsyncMock(return_value=None))
    register_handlers(dp, legacy, monthly_ui, container, admin_ui=admin_ui, identity_flow=identity)
    return SimpleNamespace(
        dp=dp,
        ui=monthly_ui,
        admin_ui=admin_ui,
        store=store,
        admin=admin,
        customer=customer,
        users=users,
        repository=repository,
        container=container,
        answer=answer,
        edit=edit,
        ack=ack,
        identity=identity,
        legacy=legacy,
    )


async def _send_text(flow: SimpleNamespace, text: str, *, actor: int = ADMIN_ID) -> None:
    await flow.dp.feed_update(
        MagicMock(spec=Bot), Update(update_id=1, message=_message(text, actor=actor))
    )


async def _send_callback(flow: SimpleNamespace, data: str, *, actor: int = ADMIN_ID) -> None:
    query = CallbackQuery(
        id="admin-root",
        from_user=TelegramUser(id=actor, is_bot=False, first_name="A"),
        chat_instance="admin-private",
        message=_message("", actor=actor),
        data=data,
    )
    await flow.dp.feed_update(MagicMock(spec=Bot), Update(update_id=2, callback_query=query))


@pytest.mark.parametrize("command", ["/start", "/menu", "/help", "/admin", "/unknown", "cancel"])
async def test_start_menu_help_install_admin_root(flow: SimpleNamespace, command: str) -> None:
    t = Translator()
    await _send_text(flow, command)
    if command == "/start":
        assert flow.answer.await_count == 2

    reply_keyboards = _reply_keyboards(flow.answer)
    assert reply_keyboards, "an admin-root reply keyboard must be installed"
    for keyboard in reply_keyboards:
        texts = _labels(keyboard)
        assert t.t("menu.recharge") not in texts
    assert any(t.t("admin.create_server") in _labels(kb) for kb in reply_keyboards)
    assert all(
        set(t.t(key) for key in ADMIN_ROOT_KEYS) <= set(_labels(kb)) for kb in reply_keyboards
    )


@pytest.mark.parametrize(
    ("label_key", "spy_name"),
    [("menu.wallet", "wallet_screen"), ("menu.recharge", "recharge_screen")],
)
async def test_stale_customer_wallet_text_returns_admin_root_notice(
    flow: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    label_key: str,
    spy_name: str,
) -> None:
    t = Translator()
    spy = AsyncMock(return_value=BotScreen("personal", InlineKeyboardMarkup(inline_keyboard=[])))
    monkeypatch.setattr(flow.ui, spy_name, spy)

    await _send_text(flow, t.t(label_key))

    assert any(t.t("admin.no_personal_wallet") in text for text in _answer_texts(flow.answer))
    reply_keyboards = _reply_keyboards(flow.answer)
    assert any(t.t("admin.create_server") in _labels(kb) for kb in reply_keyboards)
    for keyboard in reply_keyboards:
        assert t.t("menu.recharge") not in _labels(keyboard)
    spy.assert_not_awaited()


@pytest.mark.parametrize(
    "callback",
    [Callback("wallet", "balance"), Callback("recharge", "amounts")],
)
async def test_signed_wallet_recharge_callback_from_admin_is_intercepted(
    flow: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    callback: Callback,
) -> None:
    t = Translator()
    wallet_spy = AsyncMock(
        return_value=BotScreen("wallet", InlineKeyboardMarkup(inline_keyboard=[]))
    )
    recharge_spy = AsyncMock(
        return_value=BotScreen("recharge", InlineKeyboardMarkup(inline_keyboard=[]))
    )
    monkeypatch.setattr(flow.ui, "wallet_screen", wallet_spy)
    monkeypatch.setattr(flow.ui, "recharge_screen", recharge_spy)

    await _send_callback(flow, encode_callback(callback, SIGNING_KEY))

    assert any(t.t("admin.no_personal_wallet") in text for text in _answer_texts(flow.answer))
    wallet_spy.assert_not_awaited()
    recharge_spy.assert_not_awaited()
    flow.edit.assert_not_awaited()


async def test_management_entry_clears_prompt_but_preserves_identity_state(
    flow: SimpleNamespace,
) -> None:
    t = Translator()
    admin = flow.admin
    await flow.store.put(NS_PROMPT, str(admin.id), {"kind": "server_name"}, ttl_seconds=900)
    await flow.store.put("identity", str(admin.id), {"phone": "verified"}, ttl_seconds=900)

    await _send_text(flow, t.t("menu.admin"))

    assert await flow.store.get(NS_PROMPT, str(admin.id)) is None
    assert await flow.store.get("identity", str(admin.id)) is None


@pytest.mark.parametrize("revocation", ["demoted", "frozen", "config_removed"])
@pytest.mark.parametrize("entry", ["menu.admin", "/menu", "/start", "/unknown", "unknown text"])
async def test_revoked_admin_regains_customer_keyboard(
    flow: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
    entry: str,
) -> None:
    t = Translator()
    if revocation == "demoted":
        flow.users[flow.admin.id] = replace(flow.admin, role=Role.USER)
    elif revocation == "frozen":
        flow.users[flow.admin.id] = replace(flow.admin, status=UserStatus.FROZEN)
    else:
        monkeypatch.setattr(
            "cloud_platform.bot.main.get_settings",
            lambda: SimpleNamespace(
                callback_signing_key=SIGNING_KEY,
                telegram_superadmin_user_ids=[],
            ),
        )

    await _send_text(flow, t.t(entry) if entry == "menu.admin" else entry)

    reply_keyboards = _reply_keyboards(flow.answer)
    assert reply_keyboards
    texts = [label for keyboard in reply_keyboards for label in _labels(keyboard)]
    assert t.t("menu.recharge") in texts
    assert t.t("menu.admin") not in texts
    flow.admin_ui.menu.assert_not_called()
    flow.admin_ui.handle.assert_not_awaited()


async def test_clear_transient_input_preserves_identity_only_removes_prompt() -> None:
    store = InMemoryBotSessionStore()
    ui = _ui(store=store)
    admin = _admin()
    await store.put(NS_PROMPT, str(admin.id), {"kind": "server_name"}, ttl_seconds=900)
    await store.put("identity", str(admin.id), {"national_id": "0000000000"}, ttl_seconds=900)

    await ui.clear_transient_input(admin)

    assert await store.get(NS_PROMPT, str(admin.id)) is None
    assert await store.get("identity", str(admin.id)) is not None


@pytest.mark.parametrize("entry", ["menu.admin", "admin.credit", "admin.gateways"])
async def test_management_preempts_stale_identity_without_changing_user(flow, entry):
    admin = flow.admin
    admin.phone_number = "+989123456789"
    admin.phone_verified_at = datetime.now(UTC)
    admin.national_id = "0013541480"
    facts = (admin.phone_number, admin.phone_verified_at, admin.national_id)
    await flow.store.put("identity", str(admin.id), {"step": "national"}, ttl_seconds=900)
    await flow.store.put(NS_PROMPT, str(admin.id), {"kind": "server_name"}, ttl_seconds=900)
    await _send_text(flow, Translator().t(entry))
    assert await flow.store.get("identity", str(admin.id)) is None
    assert await flow.store.get(NS_PROMPT, str(admin.id)) is None
    assert (admin.phone_number, admin.phone_verified_at, admin.national_id) == facts
    assert Translator().t("identity.invalid_national_id") not in _answer_texts(flow.answer)


async def test_active_credit_input_preempts_new_stale_identity_prompt(flow):
    await _send_text(flow, Translator().t("admin.credit"))
    await flow.store.put("identity", str(flow.admin.id), {"step": "phone"}, ttl_seconds=900)
    await _send_text(flow, str(CUSTOMER_ID))
    pending = await flow.store.get("admin-credit", str(flow.admin.id))
    assert pending["step"] == "amount"
    assert await flow.store.get("identity", str(flow.admin.id)) is None
    assert Translator().t("admin.amount_prompt", currency="USD") in _answer_texts(flow.answer)


@pytest.mark.parametrize(
    "entry",
    [
        "/admin",
        "/start",
        "/menu",
        "/help",
        "menu.admin",
        "menu.support",
        "nav.menu",
        "cancel",
        "/unknown",
    ],
)
async def test_session_failure_refuses_navigation_and_stale_input(flow, monkeypatch, entry):
    await flow.store.put("admin-credit", str(flow.admin.id), {"step": "target"}, ttl_seconds=900)
    monkeypatch.setattr(
        flow.store, "delete", AsyncMock(side_effect=SessionStoreUnavailable("offline"))
    )
    text = entry if entry.startswith("/") or entry == "cancel" else Translator().t(entry)
    await _send_text(flow, text)
    flow.answer.assert_awaited_once_with(Translator().t("servers.err_retry"))
    assert await flow.store.get("admin-credit", str(flow.admin.id)) is not None


async def test_clear_transient_failure_propagates(flow, monkeypatch):
    monkeypatch.setattr(
        flow.store, "delete", AsyncMock(side_effect=SessionStoreUnavailable("offline"))
    )
    with pytest.raises(SessionStoreUnavailable):
        await flow.ui.clear_transient_input(flow.admin)


@pytest.mark.parametrize("flow_name", ["wallet", "recharge", "admin"])
async def test_missing_management_ui_keeps_admin_corporate_keyboard(flow, flow_name):
    flow.dp = Dispatcher()
    register_handlers(flow.dp, flow.legacy, flow.ui, flow.container, admin_ui=None)
    await _send_callback(flow, encode_callback(Callback(flow_name, "menu"), SIGNING_KEY))
    assert _reply_keyboards(flow.answer)
    for keyboard in _reply_keyboards(flow.answer):
        assert Translator().t("admin.create_server") in _labels(keyboard)
        assert Translator().t("menu.wallet") not in _labels(keyboard)


async def test_missing_management_ui_text_is_safe(flow):
    flow.dp = Dispatcher()
    register_handlers(flow.dp, flow.legacy, flow.ui, flow.container, admin_ui=None)
    await _send_text(flow, Translator().t("menu.admin"))
    assert Translator().t("admin.create_server") in _labels(_reply_keyboards(flow.answer)[-1])


@pytest.mark.parametrize(
    "callback",
    [
        Callback("admin", "menu"),
        Callback("admin", "credit"),
        Callback("wallet", "balance"),
        Callback("main", "menu"),
    ],
)
async def test_session_failure_callbacks_fail_closed(flow, monkeypatch, callback):
    await flow.store.put(NS_PROMPT, str(flow.admin.id), {"kind": "server_name"}, ttl_seconds=900)
    monkeypatch.setattr(
        flow.store, "delete", AsyncMock(side_effect=SessionStoreUnavailable("offline"))
    )
    await _send_callback(flow, encode_callback(callback, SIGNING_KEY))
    flow.ack.assert_awaited_once_with(Translator().t("servers.err_retry"), show_alert=True)
    flow.edit.assert_not_awaited()
    flow.admin_ui.handle.assert_not_awaited()


async def test_unknown_command_cancels_identity_and_installs_admin_root(flow):
    await flow.store.put("identity", str(flow.admin.id), {"step": "phone"}, ttl_seconds=900)
    await _send_text(flow, "/unknown")
    assert await flow.store.get("identity", str(flow.admin.id)) is None
    assert Translator().t("admin.create_server") in _labels(_reply_keyboards(flow.answer)[-1])


async def test_admin_purchase_still_starts_identity_verification(flow):
    await _send_callback(flow, encode_callback(Callback("offers", "confirm"), SIGNING_KEY))
    assert (await flow.store.get("identity", str(flow.admin.id)))["step"] == "phone"
    assert _reply_keyboards(flow.answer)[-1].keyboard[0][0].request_contact is True
    flow.legacy.handle.assert_not_awaited()


async def test_removed_configuration_restores_customer_callback_root(flow, monkeypatch):
    monkeypatch.setattr("cloud_platform.bot.main._configured_superadmins", lambda: ())
    await _send_callback(flow, encode_callback(Callback("main", "menu"), SIGNING_KEY))
    assert Translator().t("menu.recharge") in _labels(_reply_keyboards(flow.answer)[-1])
    assert "admin" not in _flows(flow.edit.await_args.kwargs["reply_markup"])


async def test_stale_customer_amount_does_not_open_personal_wallet(flow, monkeypatch):
    await flow.store.put(
        NS_PROMPT,
        str(flow.admin.id),
        {"kind": "wallet_recharge_amount", "currency": "USD"},
        ttl_seconds=900,
    )
    wallet = AsyncMock()
    monkeypatch.setattr(flow.ui, "wallet_screen", wallet)
    await _send_text(flow, "25")
    wallet.assert_not_awaited()
    assert await flow.store.get(NS_PROMPT, str(flow.admin.id)) is None
    assert Translator().t("admin.create_server") in _labels(_reply_keyboards(flow.answer)[-1])


async def test_identity_completion_restores_corporate_root(flow):
    admin = flow.admin
    admin.phone_number = "+989123456789"
    admin.phone_verified_at = datetime.now(UTC)
    saved = replace(admin, national_id="1234567891")

    async def save_national(user, **kwargs):
        flow.users[user.id] = saved
        return saved

    flow.identity._service.collect_national_id = AsyncMock(side_effect=save_national)
    resume = encode_callback(Callback("offers", "confirm"), SIGNING_KEY)
    await flow.store.put(
        "identity", str(admin.id), {"step": "national", "resume": resume}, ttl_seconds=900
    )
    # The continuation is synthetic; identity verification and dispatcher restoration are real.
    flow.ui.handle = AsyncMock(
        return_value=BotScreen("continue", InlineKeyboardMarkup(inline_keyboard=[]))
    )
    await _send_text(flow, "1234567891")
    assert await flow.store.get("identity", str(admin.id)) is None
    assert Translator().t("admin.create_server") in _labels(_reply_keyboards(flow.answer)[-1])
    assert flow.users[admin.id].national_id == "1234567891"
