"""Privileged Telegram wallet and encrypted database export commands."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update
from aiogram.types import User as TelegramUser

from cloud_platform.bot.admin_ui import AdminBotUi
from cloud_platform.bot.main import _admin_credit, _admin_dump, register_handlers
from cloud_platform.bot.monthly_ui import build_main_reply_keyboard
from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import InMemoryBotSessionStore, SessionStoreUnavailable
from cloud_platform.modules.navigation.domain import Callback, decode_callback, encode_callback
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.modules.wallet.domain import Wallet


def _message(text: str, *, actor: int = 42, chat: int = 42) -> Message:
    return Message(
        message_id=7,
        date=1,
        chat=Chat(id=chat, type="private"),
        from_user=TelegramUser(id=actor, is_bot=False, first_name="A"),
        text=text,
    )


def _container() -> MagicMock:
    container = MagicMock()
    repo = MagicMock()
    admin = User(
        id=uuid4(),
        username="configured-admin",
        email="configured-admin@example.test",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
        telegram_user_id=42,
    )
    repo.get_by_telegram_user_id = AsyncMock(
        side_effect=lambda actor: admin if actor == 42 else None
    )
    repo.get = AsyncMock(side_effect=lambda target: SimpleNamespace(id=target, role=Role.USER))
    container.user_repository.return_value = repo
    service = MagicMock()
    service.adjust_balance = AsyncMock(
        return_value=(SimpleNamespace(balance=800, currency="USD"), object())
    )
    container.wallet_admin_service.return_value = service
    return container


def test_credit_command_cannot_bypass_management_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _container()
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(
        "cloud_platform.bot.main.get_settings",
        lambda: SimpleNamespace(telegram_superadmin_user_ids=[42]),
    )
    target = uuid4()
    asyncio.run(_admin_credit(_message(f"/admin_credit {target} 300 courtesy"), container))
    container.wallet_admin_service.return_value.adjust_balance.assert_not_awaited()
    from cloud_platform.core.i18n import Translator

    assert answer.await_args.args[0] == Translator().t("admin.credit_use_management")

    container.wallet_admin_service.return_value.adjust_balance.reset_mock()
    asyncio.run(
        _admin_credit(_message(f"/admin_credit {target} 300 courtesy", actor=43), container)
    )
    container.wallet_admin_service.return_value.adjust_balance.assert_not_awaited()
    asyncio.run(_admin_credit(_message(f"/admin_credit {target} 300 courtesy", chat=99), container))
    container.wallet_admin_service.return_value.adjust_balance.assert_not_awaited()


def test_dump_sends_encrypted_file_only_to_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _container()
    send = AsyncMock()
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer_document", send)
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(
        "cloud_platform.bot.main.get_settings",
        lambda: SimpleNamespace(telegram_superadmin_user_ids=[42]),
    )

    class Job:
        def __init__(self, config: object) -> None:
            self.config = config

        async def run(self) -> SimpleNamespace:
            path = Path(self.config.output_dir) / "cloud-backup.dump.enc"
            path.write_bytes(b"encrypted")
            return SimpleNamespace(filename=path.name)

    monkeypatch.setattr(
        "cloud_platform.bot.main.build_backup_config",
        lambda settings: SimpleNamespace(output_dir=Path(".")),
    )
    monkeypatch.setattr(
        "cloud_platform.bot.main.replace", lambda config, **kwargs: SimpleNamespace(**kwargs)
    )
    monkeypatch.setattr("cloud_platform.bot.main.PostgresBackupJob", Job)
    asyncio.run(_admin_dump(_message("/admin_dump", actor=43), container))
    send.assert_not_awaited()
    asyncio.run(_admin_dump(_message("/admin_dump"), container))
    assert send.await_args.kwargs["protect_content"] is True
    assert send.await_args.args[0].filename == "cloud-backup.dump.enc"


@pytest.fixture
def admin_flow(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Real UI/session state; the credit spy guards the financial boundary."""
    key = "admin-behavior-signing-key"
    admin = User(
        id=uuid4(),
        username="admin",
        email="admin@example.test",
        role=Role.ADMIN,
        telegram_user_id=42,
    )
    customer = User(
        id=uuid4(),
        username="customer",
        email="customer@example.test",
        telegram_user_id=43,
    )
    second_admin = User(
        id=uuid4(),
        username="second",
        email="second@example.test",
        role=Role.ADMIN,
        telegram_user_id=44,
    )
    users = {u.id: u for u in (admin, customer, second_admin)}
    wallets = {
        u.id: Wallet(id=uuid4(), user_id=u.id, balance=700, currency="USD") for u in users.values()
    }
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
    wallet_repository = SimpleNamespace(get=AsyncMock(side_effect=lambda uid: wallets.get(uid)))
    credit = SimpleNamespace(adjust_balance=AsyncMock())
    store = InMemoryBotSessionStore()
    ui = AdminBotUi(
        key,
        gateways=None,
        users=repository,
        wallets=wallet_repository,
        wallet_admin=credit,
        store=store,
        ttl_seconds=30,
        configured_ids=(42, 44),
    )
    container = MagicMock()
    container.user_repository.return_value = repository
    container.wallet_repository.return_value = wallet_repository
    container.wallet_admin_service.return_value = credit
    answer, edit, ack = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(Message, "edit_text", edit)
    monkeypatch.setattr(CallbackQuery, "answer", ack)
    monkeypatch.setattr(
        "cloud_platform.bot.main.get_settings",
        lambda: SimpleNamespace(callback_signing_key=key, telegram_superadmin_user_ids=[42, 44]),
    )
    fallback = SimpleNamespace(
        handle=AsyncMock(return_value=None),
        handle_text=AsyncMock(return_value=None),
        menu_screen=lambda user: BotScreen("menu", InlineKeyboardMarkup(inline_keyboard=[])),
        reply_keyboard=lambda user: build_main_reply_keyboard(
            Translator(), user, configured_ids=(42, 44)
        ),
    )
    dp = Dispatcher()
    register_handlers(dp, fallback, fallback, container, admin_ui=ui)
    return SimpleNamespace(
        ui=ui,
        store=store,
        admin=admin,
        customer=customer,
        second_admin=second_admin,
        users=users,
        wallets=wallets,
        credit=credit,
        dp=dp,
        key=key,
        answer=answer,
        edit=edit,
        ack=ack,
        repository=repository,
    )


async def _admin_update(flow: SimpleNamespace, text: str, *, actor: int = 42) -> None:
    await flow.dp.feed_update(
        MagicMock(spec=Bot), Update(update_id=1, message=_message(text, actor=actor, chat=actor))
    )


async def _admin_callback(
    flow: SimpleNamespace,
    data: str,
    *,
    actor: int = 42,
    chat: int | None = None,
) -> None:
    query = CallbackQuery(
        id="admin-action",
        from_user=TelegramUser(id=actor, is_bot=False, first_name="A"),
        chat_instance="admin-private",
        message=_message("", actor=actor, chat=chat or actor),
        data=data,
    )
    await flow.dp.feed_update(MagicMock(spec=Bot), Update(update_id=2, callback_query=query))


async def _begin_credit(flow: SimpleNamespace) -> None:
    screen = flow.ui.menu(flow.admin)
    await _admin_callback(flow, screen.keyboard.inline_keyboard[1][0].callback_data)


async def _credit_confirmation(flow: SimpleNamespace) -> tuple[str, str]:
    await _begin_credit(flow)
    await _admin_update(flow, str(flow.customer.telegram_user_id))
    await _admin_update(flow, "10.50")
    await _admin_update(flow, "customer refund")
    keyboard = flow.answer.await_args.kwargs["reply_markup"].inline_keyboard
    return keyboard[0][0].callback_data, keyboard[1][0].callback_data


@pytest.mark.parametrize(
    "target",
    ["missing_uuid", "unknown_telegram", "malformed", "self", "other_admin", "missing_wallet"],
)
async def test_credit_prompt_rejects_invalid_targets_without_advancing_or_crediting(
    admin_flow: SimpleNamespace,
    target: str,
) -> None:
    flow = admin_flow
    targets = {
        "missing_uuid": str(uuid4()),
        "unknown_telegram": "987654321",
        "malformed": "not-a-user",
        "self": str(flow.admin.id),
        "other_admin": str(flow.second_admin.telegram_user_id),
        "missing_wallet": str(flow.customer.id),
    }
    if target == "missing_wallet":
        flow.wallets.pop(flow.customer.id)
    await _begin_credit(flow)
    await _admin_update(flow, targets[target])
    assert (await flow.store.get("admin-credit", str(flow.admin.id)))["step"] == "target"
    flow.credit.adjust_balance.assert_not_awaited()
    assert all(wallet.balance == 700 for wallet in flow.wallets.values())


@pytest.mark.parametrize("amount", ["0", "-1", "92233720368547758.08", "1.001", "1e2"])
async def test_credit_prompt_rejects_invalid_minor_amount_and_allows_correction(
    admin_flow: SimpleNamespace,
    amount: str,
) -> None:
    flow = admin_flow
    await _begin_credit(flow)
    await _admin_update(flow, str(flow.customer.id))
    await _admin_update(flow, amount)
    pending = await flow.store.get("admin-credit", str(flow.admin.id))
    assert pending["step"] == "amount"
    assert "amount" not in pending
    flow.credit.adjust_balance.assert_not_awaited()
    # Invalid input must leave the same customer/currency selected for correction.
    await _admin_update(flow, "0.01")
    pending = await flow.store.get("admin-credit", str(flow.admin.id))
    assert pending["step"] == "reason"
    assert pending["amount"] == 1
    assert pending["user_id"] == str(flow.customer.id)
    assert pending["currency"] == "USD"
    flow.credit.adjust_balance.assert_not_awaited()


@pytest.mark.parametrize("reason", ["   ", "r" * 501])
async def test_credit_prompt_requires_bounded_nonempty_audit_reason(
    admin_flow: SimpleNamespace,
    reason: str,
) -> None:
    flow = admin_flow
    await _begin_credit(flow)
    await _admin_update(flow, str(flow.customer.id))
    await _admin_update(flow, "10.50")
    await _admin_update(flow, reason)
    assert (await flow.store.get("admin-credit", str(flow.admin.id)))["step"] == "reason"
    flow.credit.adjust_balance.assert_not_awaited()
    await _admin_update(flow, "r" * 500)
    keyboard = flow.answer.await_args.kwargs["reply_markup"].inline_keyboard
    apply = decode_callback(keyboard[0][0].callback_data, flow.key)
    record = await flow.store.get("admin-credit-confirm", f"{flow.admin.id}:{apply.args[0]}")
    assert record["amount"] == 1050
    assert record["reason"] == "r" * 500
    assert await flow.store.get("admin-credit", str(flow.admin.id)) is None
    flow.credit.adjust_balance.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    ["admin_role", "currency", "deleted_user", "deleted_wallet", "unpersisted_user"],
)
async def test_credit_confirmation_revalidates_target_before_money_service(
    admin_flow: SimpleNamespace,
    change: str,
) -> None:
    flow = admin_flow
    apply, _ = await _credit_confirmation(flow)
    if change == "admin_role":
        flow.users[flow.customer.id] = replace(flow.customer, role=Role.ADMIN)
    elif change == "unpersisted_user":
        flow.users[flow.customer.id] = replace(flow.customer, id=None)
    elif change == "currency":
        flow.wallets[flow.customer.id] = replace(flow.wallets[flow.customer.id], currency="EUR")
    elif change == "deleted_user":
        flow.users.pop(flow.customer.id)
    else:
        flow.wallets.pop(flow.customer.id)
    await _admin_callback(flow, apply)
    flow.credit.adjust_balance.assert_not_awaited()
    assert all(wallet.balance == 700 for wallet in flow.wallets.values())


@pytest.mark.parametrize("invalidation", ["expiry", "cancel", "another_admin", "cross_chat"])
async def test_unusable_credit_confirmation_never_reaches_money_service(
    admin_flow: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    invalidation: str,
) -> None:
    flow = admin_flow
    now = [100.0]
    monkeypatch.setattr("cloud_platform.core.session_store.time.monotonic", lambda: now[0])
    apply, cancel = await _credit_confirmation(flow)
    actor, chat = 42, 42
    if invalidation == "expiry":
        now[0] += 31
    elif invalidation == "cancel":
        await _admin_callback(flow, cancel)
    elif invalidation == "another_admin":
        actor, chat = 44, 44
        # Even another administrator cannot cancel someone else's confirmation.
        await _admin_callback(flow, cancel, actor=actor, chat=chat)
        cb = decode_callback(apply, flow.key)
        assert (
            await flow.store.get("admin-credit-confirm", f"{flow.admin.id}:{cb.args[0]}")
            is not None
        )
    else:
        chat = 44
    await _admin_callback(flow, apply, actor=actor, chat=chat)
    flow.credit.adjust_balance.assert_not_awaited()
    assert flow.wallets[flow.customer.id].balance == 700


async def test_canceling_credit_prompt_makes_subsequent_answers_inert(
    admin_flow: SimpleNamespace,
) -> None:
    flow = admin_flow
    await _begin_credit(flow)
    cancel = flow.edit.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    await _admin_callback(flow, cancel)
    await _admin_update(flow, str(flow.customer.id))
    await _admin_update(flow, "10.50")
    await _admin_update(flow, "refund")
    assert await flow.store.get("admin-credit", str(flow.admin.id)) is None
    flow.credit.adjust_balance.assert_not_awaited()


@pytest.mark.parametrize("entrypoint", ["command", "reply_button", "signed_callback"])
async def test_customer_cannot_open_admin_credit_prompt(
    admin_flow: SimpleNamespace,
    entrypoint: str,
) -> None:
    from cloud_platform.core.i18n import Translator

    flow = admin_flow
    if entrypoint == "command":
        await _admin_update(flow, "/admin", actor=43)
    elif entrypoint == "reply_button":
        await _admin_update(flow, Translator().t("menu.admin"), actor=43)
    else:
        await _admin_callback(
            flow, encode_callback(Callback("admin", "credit"), flow.key), actor=43
        )
    assert await flow.store.get("admin-credit", str(flow.customer.id)) is None
    assert await flow.store.get("admin-credit", str(flow.admin.id)) is None
    flow.credit.adjust_balance.assert_not_awaited()


@pytest.mark.parametrize("entrypoint", ["callback", "text"])
async def test_credit_state_outage_fails_closed_before_money_service(
    admin_flow: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    flow = admin_flow
    apply, _ = await _credit_confirmation(flow)
    monkeypatch.setattr(
        flow.store, "get", AsyncMock(side_effect=SessionStoreUnavailable("offline"))
    )
    if entrypoint == "callback":
        await _admin_callback(flow, apply)
    else:
        await _admin_update(flow, "10.50")
    flow.credit.adjust_balance.assert_not_awaited()
    assert flow.wallets[flow.customer.id].balance == 700


@pytest.mark.parametrize("amount", ["0", "-1", "9223372036854775808", "1.5"])
async def test_admin_credit_command_rejects_nonpositive_out_of_range_or_fractional_minor(
    admin_flow: SimpleNamespace,
    amount: str,
) -> None:
    flow = admin_flow
    await _admin_update(flow, f"/admin_credit {flow.customer.id} {amount} refund")
    flow.credit.adjust_balance.assert_not_awaited()


@pytest.mark.parametrize("target", ["self", "other_admin", "missing", "malformed"])
async def test_admin_credit_command_rejects_noncustomer_target(
    admin_flow: SimpleNamespace,
    target: str,
) -> None:
    flow = admin_flow
    target_ref = {
        "self": str(flow.admin.id),
        "other_admin": str(flow.second_admin.telegram_user_id),
        "missing": str(uuid4()),
        "malformed": "not-a-user",
    }[target]
    await _admin_update(flow, f"/admin_credit {target_ref} 100 refund")
    flow.credit.adjust_balance.assert_not_awaited()


@pytest.mark.parametrize("revocation", ["demoted", "frozen", "banned"])
async def test_credit_confirmation_rechecks_current_actor_privileges(
    admin_flow: SimpleNamespace,
    revocation: str,
) -> None:
    flow = admin_flow
    apply, _ = await _credit_confirmation(flow)
    if revocation == "demoted":
        flow.users[flow.admin.id] = replace(flow.admin, role=Role.USER)
    else:
        status = UserStatus.FROZEN if revocation == "frozen" else UserStatus.BANNED
        flow.users[flow.admin.id] = replace(flow.admin, status=status)
    await _admin_callback(flow, apply)
    flow.credit.adjust_balance.assert_not_awaited()
    assert flow.wallets[flow.customer.id].balance == 700


async def test_admin_credit_command_without_audit_reason_cannot_adjust_wallet(
    admin_flow: SimpleNamespace,
) -> None:
    flow = admin_flow
    await _admin_update(flow, f"/admin_credit {flow.customer.id} 100    ")
    flow.credit.adjust_balance.assert_not_awaited()


@pytest.mark.parametrize("actor", [42, 44])
async def test_each_configured_superadmin_gets_dedicated_persistent_management_keyboard(
    admin_flow: SimpleNamespace, actor: int
) -> None:
    from cloud_platform.core.i18n import Translator

    flow = admin_flow
    await _admin_update(flow, "/admin", actor=actor)
    first, second = flow.answer.await_args_list
    keyboard = first.kwargs["reply_markup"]
    assert keyboard.is_persistent is True
    assert keyboard.resize_keyboard is True
    assert [button.text for row in keyboard.keyboard for button in row] == [
        Translator().t("admin.gateways"),
        Translator().t("admin.credit"),
        Translator().t("nav.menu"),
    ]
    assert second.kwargs["reply_markup"].inline_keyboard
    flow.credit.adjust_balance.assert_not_awaited()


@pytest.mark.parametrize("actor", [42, 44])
async def test_management_reply_button_enters_existing_credit_confirmation_flow(
    admin_flow: SimpleNamespace, actor: int
) -> None:
    from cloud_platform.core.i18n import Translator

    flow = admin_flow
    await _admin_update(flow, Translator().t("menu.admin"), actor=actor)
    await _admin_update(flow, Translator().t("admin.credit"), actor=actor)
    owner = flow.admin if actor == 42 else flow.second_admin
    assert await flow.store.get("admin-credit", str(owner.id)) == {"step": "target"}
    flow.credit.adjust_balance.assert_not_awaited()


@pytest.mark.parametrize("configured", [[], [44]])
async def test_active_database_admin_is_denied_without_matching_config(
    admin_flow: SimpleNamespace, monkeypatch, configured
) -> None:
    flow = admin_flow
    monkeypatch.setattr(
        "cloud_platform.bot.main.get_settings",
        lambda: SimpleNamespace(
            callback_signing_key=flow.key,
            telegram_superadmin_user_ids=configured,
            telegram_admin_chat_id=42,
        ),
    )
    await _admin_update(flow, "/admin")
    keyboard = flow.answer.await_args.kwargs["reply_markup"]
    assert Translator().t("menu.admin") not in [
        button.text for row in keyboard.keyboard for button in row
    ]
    flow.credit.adjust_balance.assert_not_awaited()
    assert await flow.store.get("admin-credit", str(flow.admin.id)) is None


@pytest.mark.parametrize("context", ["group", "bot", "anonymous", "chat_mismatch", "sender_chat"])
async def test_untrusted_start_never_onboards_or_exposes_management(
    admin_flow: SimpleNamespace, context: str
) -> None:
    flow = admin_flow
    message = _message("/start")
    if context == "group":
        message = message.model_copy(update={"chat": Chat(id=-100, type="supergroup")})
    elif context == "bot":
        actor = TelegramUser(id=42, is_bot=True, first_name="Not a human")
        message = message.model_copy(update={"from_user": actor})
    elif context == "anonymous":
        message = message.model_copy(update={"from_user": None})
    elif context == "sender_chat":
        message = message.model_copy(update={"sender_chat": Chat(id=-100, type="supergroup")})
    else:
        message = message.model_copy(update={"chat": Chat(id=44, type="private")})
    flow.repository.get_by_telegram_user_id.reset_mock()
    await flow.dp.feed_update(MagicMock(spec=Bot), Update(update_id=1, message=message))
    flow.repository.get_by_telegram_user_id.assert_not_awaited()
    flow.repository.onboard_telegram.assert_not_awaited()
    assert "reply_markup" not in flow.answer.await_args.kwargs
    flow.credit.adjust_balance.assert_not_awaited()


async def test_second_configured_admin_cannot_claim_another_admins_confirmation(admin_flow) -> None:
    flow = admin_flow
    apply, _ = await _credit_confirmation(flow)
    await _admin_callback(flow, apply, actor=44)
    flow.credit.adjust_balance.assert_not_awaited()
    assert flow.wallets[flow.customer.id].balance == 700


async def test_confirmation_reloads_authority_even_when_resolved_identity_is_stale(
    admin_flow, monkeypatch
) -> None:
    flow = admin_flow
    apply, _ = await _credit_confirmation(flow)
    monkeypatch.setattr("cloud_platform.bot.main._resolve_user", AsyncMock(return_value=flow.admin))
    flow.users[flow.admin.id] = replace(flow.admin, status=UserStatus.FROZEN)
    await _admin_callback(flow, apply)
    flow.credit.adjust_balance.assert_not_awaited()
    assert flow.wallets[flow.customer.id].balance == 700


@pytest.mark.parametrize("revocation", ["demoted", "frozen", "config_removed"])
@pytest.mark.parametrize(
    "entrypoint", ["menu.admin", "admin.credit", "admin.gateways", "/admin", "callback"]
)
async def test_revoked_management_entry_replaces_persistent_privileged_keyboard(
    admin_flow, monkeypatch, revocation, entrypoint
) -> None:
    flow = admin_flow
    await _admin_update(flow, "/admin")
    assert flow.answer.await_args_list[0].kwargs["reply_markup"].is_persistent is True
    flow.answer.reset_mock()
    if revocation == "config_removed":
        # Even a UI instance holding old configuration must render no admin
        # keyboard when the authoritative edge has rejected the identity.
        monkeypatch.setattr(
            "cloud_platform.bot.main.get_settings",
            lambda: SimpleNamespace(callback_signing_key=flow.key, telegram_superadmin_user_ids=[]),
        )
    elif revocation == "demoted":
        flow.users[flow.admin.id] = replace(flow.admin, role=Role.USER)
    else:
        flow.users[flow.admin.id] = replace(flow.admin, status=UserStatus.FROZEN)
    if entrypoint == "callback":
        data = encode_callback(Callback("admin", "menu"), flow.key)
        await _admin_callback(flow, data)
    else:
        text = entrypoint if entrypoint.startswith("/") else Translator().t(entrypoint)
        await _admin_update(flow, text)
    keyboard = flow.answer.await_args.kwargs["reply_markup"]
    assert keyboard.is_persistent is True
    texts = [button.text for row in keyboard.keyboard for button in row]
    assert not any(
        Translator().t(key) in texts for key in ("menu.admin", "admin.credit", "admin.gateways")
    )
    flow.credit.adjust_balance.assert_not_awaited()
