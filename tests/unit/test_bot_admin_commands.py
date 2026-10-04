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
from cloud_platform.bot.ui import BotScreen
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
    admin = SimpleNamespace(
        id=uuid4(),
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


def test_credit_requires_configured_private_admin_and_uses_message_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _container()
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(
        "cloud_platform.bot.main.get_settings", lambda: SimpleNamespace(telegram_admin_chat_id=42)
    )
    target = uuid4()
    asyncio.run(_admin_credit(_message(f"/admin_credit {target} 300 courtesy"), container))
    kwargs = container.wallet_admin_service.return_value.adjust_balance.await_args.kwargs
    assert kwargs["user_id"] == target
    assert kwargs["amount"] == 300
    assert kwargs["reason"] == "courtesy"
    assert kwargs["idempotency_key"] == "telegram-admin-credit:42:7"
    assert "800" in answer.await_args.args[0]

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
        "cloud_platform.bot.main.get_settings", lambda: SimpleNamespace(telegram_admin_chat_id=42)
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
        lambda: SimpleNamespace(callback_signing_key=key),
    )
    fallback = SimpleNamespace(
        handle=AsyncMock(return_value=None),
        handle_text=AsyncMock(return_value=None),
        menu_screen=lambda user: BotScreen("menu", InlineKeyboardMarkup(inline_keyboard=[])),
        reply_keyboard=lambda user: None,
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
