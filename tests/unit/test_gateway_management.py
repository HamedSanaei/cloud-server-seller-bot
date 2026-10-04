"""Live availability policy, authorization and old-invoice settlement behavior."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update
from aiogram.types import User as TelegramUser

from cloud_platform.bot.admin_ui import AdminBotUi
from cloud_platform.bot.main import register_handlers
from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.session_store import InMemoryBotSessionStore
from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus
from cloud_platform.modules.payments.gateway_repository import SqlAlchemyGatewaySettingsRepository
from cloud_platform.modules.payments.gateways import GatewayManagementService
from cloud_platform.modules.payments.inquiry import PaymentInquiryService
from cloud_platform.modules.payments.recharge import (
    RechargeAdminForbiddenError,
    RechargeDisabledError,
    WalletRechargeService,
)
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.modules.users.domain import PermissionDeniedError, Role, User, UserStatus
from cloud_platform.providers.base import PaymentIntent, PaymentStatus


class Settings:
    def __init__(self) -> None:
        self.values: dict[str, bool] = {}
        self.actor_id = None

    async def get(self, key: str) -> bool | None:
        return self.values.get(key)

    async def list(self) -> dict[str, bool]:
        return dict(self.values)

    async def set_enabled(self, key: str, enabled: bool, actor_id: object) -> None:
        self.values[key] = enabled
        self.actor_id = actor_id


@pytest.mark.parametrize(
    "role,status",
    [
        (Role.USER, UserStatus.ACTIVE),
        (Role.ADMIN, UserStatus.FROZEN),
        (Role.ADMIN, UserStatus.BANNED),
    ],
)
async def test_unauthorized_gateway_toggle_and_listing_are_rejected(
    role: Role, status: UserStatus
) -> None:
    repository = Settings()
    management = GatewayManagementService(repository, ["atlaspay"])
    user = User(
        username="fixture-customer",
        email="fixture@example.test",
        id=uuid4(),
        role=role,
        status=status,
    )
    with pytest.raises(PermissionDeniedError):
        await management.set_enabled(user, "atlaspay", False)
    with pytest.raises(PermissionDeniedError):
        await management.list(user)
    assert repository.values == {}


async def test_management_defaults_and_durable_toggle_attribution() -> None:
    repository = Settings()
    service = GatewayManagementService(repository, ["atlaspay", "zarinpal"])
    admin = User(
        username="fixture-customer", email="fixture@example.test", id=uuid4(), role=Role.ADMIN
    )
    assert [(s.key, s.enabled) for s in await service.list(admin)] == [
        ("atlaspay", True),
        ("zarinpal", True),
    ]
    await service.set_enabled(admin, "atlaspay", False)
    # Reconstructing the application service does not reset durable policy.
    reopened = GatewayManagementService(repository, ["atlaspay", "zarinpal"])
    assert [(s.key, s.enabled) for s in await reopened.list(admin)] == [
        ("atlaspay", False),
        ("zarinpal", True),
    ]
    assert repository.actor_id == admin.id
    with pytest.raises(ValueError, match="not configured"):
        await reopened.set_enabled(admin, "unconfigured", True)
    with pytest.raises(ValueError, match="boolean"):
        await reopened.set_enabled(admin, "atlaspay", "false")


def _gateway() -> SimpleNamespace:
    return SimpleNamespace(
        key="atlaspay",
        supported_currency="IRT",
        minimum_charge_minor=50000,
        create_payment=AsyncMock(
            return_value=PaymentIntent(
                gateway_payment_id="66",
                status=PaymentStatus.PENDING,
                amount_minor=50000,
                currency="IRT",
                redirect_url="https://t.me/atlaspaybot/pay?startapp=real",
            )
        ),
        verify_with_reference=AsyncMock(
            return_value=PaymentIntent(
                gateway_payment_id="66",
                status=PaymentStatus.SUCCEEDED,
                amount_minor=50000,
                currency="IRT",
            )
        ),
    )


async def test_disabled_gateway_blocks_new_orders_but_pending_inquiry_still_credits() -> None:
    user = User(username="fixture-customer", email="fixture@example.test", id=uuid4())
    session = PaymentSession(
        id=uuid4(),
        user_id=user.id,
        gateway_key="atlaspay",
        gateway_payment_id="66",
        amount_minor=50000,
        currency="IRT",
        idempotency_key="gateway-toggle-existing",
    )
    rows = {session.id: session}

    async def save(updated: PaymentSession) -> PaymentSession:
        rows[updated.id] = updated
        return updated

    repo = SimpleNamespace(
        get=AsyncMock(side_effect=lambda sid: rows.get(sid)),
        get_by_external_id=AsyncMock(side_effect=lambda key, eid: rows[session.id]),
        save=AsyncMock(side_effect=save),
        create=AsyncMock(),
    )
    wallet = SimpleNamespace(
        credit_deposit=AsyncMock(return_value=(SimpleNamespace(balance=50000), True))
    )
    settings, gateway = Settings(), _gateway()
    management = GatewayManagementService(settings, [gateway.key])
    recharge = WalletRechargeService(payments_repo=repo, gateway=gateway, gateway_settings=settings)
    admin = User(
        username="fixture-customer", email="fixture@example.test", id=uuid4(), role=Role.ADMIN
    )
    assert await recharge.supports_currency_async("IRT")
    await management.set_enabled(admin, gateway.key, False)
    assert await recharge.compatible_gateways_async("IRT", 50000) == []
    assert not await recharge.supports_currency_async("IRT")
    with pytest.raises(RechargeDisabledError):
        await recharge.start(
            user=user, amount_minor=50000, currency="IRT", idempotency_key="new-gateway-order"
        )
    with pytest.raises(RechargeDisabledError):
        await recharge.start(
            user=user,
            amount_minor=50000,
            currency="IRT",
            idempotency_key="new-gateway-order",
            gateway_key="atlaspay",
        )
    gateway.create_payment.assert_not_awaited()
    inquiry = PaymentInquiryService(
        payments_repo=repo,
        webhook_service=PaymentWebhookService(repo, wallet, None),
        gateways={gateway.key: gateway},
    )
    settled = await inquiry.check_status(user, session.id)
    assert settled.status is PaymentSessionStatus.SUCCEEDED
    gateway.verify_with_reference.assert_awaited_once_with("66", 50000, session.idempotency_key)
    wallet.credit_deposit.assert_awaited_once()
    await management.set_enabled(admin, gateway.key, True)
    assert await recharge.compatible_gateways_async("IRT", 50000) == ["atlaspay"]


@pytest.mark.parametrize("status", list(UserStatus))
async def test_administrator_recharge_is_refused_at_application_boundary(
    status: UserStatus,
) -> None:
    gateway = _gateway()
    repository = SimpleNamespace(create=AsyncMock())
    recharge = WalletRechargeService(payments_repo=repository, gateway=gateway)
    with pytest.raises(RechargeAdminForbiddenError):
        await recharge.start(
            user=User(
                username="fixture-customer",
                email="fixture@example.test",
                id=uuid4(),
                role=Role.ADMIN,
                status=status,
            ),
            amount_minor=50000,
            currency="IRT",
            idempotency_key="admin-self-topup",
        )
    gateway.create_payment.assert_not_awaited()
    repository.create.assert_not_awaited()


async def test_gateway_repository_preserves_missing_false_and_true_values() -> None:
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.__aexit__.return_value = None
    repository = SqlAlchemyGatewaySettingsRepository(lambda: db)
    db.execute.return_value = MagicMock(scalar_one_or_none=lambda: None)
    assert await repository.get("atlaspay") is None
    db.execute.return_value = MagicMock(scalar_one_or_none=lambda: False)
    assert await repository.get("atlaspay") is False
    db.execute.return_value = MagicMock(scalar_one_or_none=lambda: True)
    assert await repository.get("atlaspay") is True
    db.execute.return_value = MagicMock(all=lambda: [("atlaspay", False), ("zarinpal", True)])
    assert await repository.list() == {"atlaspay": False, "zarinpal": True}


async def test_gateway_repository_upsert_commits_actor_and_setting_atomically() -> None:
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.__aexit__.return_value = None
    repository = SqlAlchemyGatewaySettingsRepository(lambda: db)
    actor_id = uuid4()
    await repository.set_enabled("atlaspay", False, actor_id)
    statement = db.execute.await_args.args[0]
    # Inspect actual bind values passed to the database, not implementation source.
    values = statement.compile().params
    assert values["key"] == "atlaspay"
    assert values["enabled"] is False
    assert values["updated_by"] == actor_id
    assert values["created_at"] == values["updated_at"]
    db.commit.assert_awaited_once()


@pytest.fixture
def gateway_admin_route(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    admin = User(
        id=uuid4(),
        username="gateway-admin",
        email="admin@example.test",
        role=Role.ADMIN,
        telegram_user_id=42,
    )
    customer = User(
        id=uuid4(),
        username="gateway-customer",
        email="customer@example.test",
        telegram_user_id=43,
    )
    users = {42: admin, 43: customer}
    repository = Settings()
    gateway = _gateway()
    management = GatewayManagementService(repository, [gateway.key])
    recharge = WalletRechargeService(
        payments_repo=SimpleNamespace(create=AsyncMock()),
        gateway=gateway,
        gateway_settings=repository,
    )
    key = "gateway-behavior-signing-key"
    user_repo = SimpleNamespace(
        get_by_telegram_user_id=AsyncMock(side_effect=lambda tid: users.get(tid))
    )
    container = MagicMock()
    container.user_repository.return_value = user_repo
    ui = AdminBotUi(
        key,
        gateways=management,
        users=user_repo,
        wallets=None,
        wallet_admin=None,
        store=InMemoryBotSessionStore(),
    )
    screen = BotScreen("menu", InlineKeyboardMarkup(inline_keyboard=[]))
    fallback = SimpleNamespace(
        handle=AsyncMock(return_value=screen),
        menu_screen=lambda user: screen,
    )
    dp = Dispatcher()
    register_handlers(dp, fallback, fallback, container, admin_ui=ui)
    monkeypatch.setattr(Message, "edit_text", AsyncMock())
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    monkeypatch.setattr(
        "cloud_platform.bot.main.get_settings",
        lambda: SimpleNamespace(callback_signing_key=key),
    )
    return SimpleNamespace(
        admin=admin,
        users=users,
        repository=repository,
        recharge=recharge,
        ui=ui,
        dp=dp,
    )


async def _gateway_callback(
    route: SimpleNamespace,
    data: str,
    *,
    actor: int = 42,
    chat: int = 42,
    chat_type: str = "private",
) -> None:
    query = CallbackQuery(
        id="gateway-setting",
        from_user=TelegramUser(id=actor, is_bot=False, first_name="G"),
        chat_instance="gateway-private",
        message=Message(message_id=8, date=1, chat=Chat(id=chat, type=chat_type)),
        data=data,
    )
    await route.dp.feed_update(MagicMock(spec=Bot), Update(update_id=1, callback_query=query))


async def test_signed_private_admin_toggle_changes_customer_gateway_availability(
    gateway_admin_route: SimpleNamespace,
) -> None:
    route = gateway_admin_route
    screen = await route.ui.gateway_screen(route.admin)
    disable = screen.keyboard.inline_keyboard[0][0].callback_data
    await _gateway_callback(route, disable)
    assert not await route.recharge.supports_currency_async("IRT")
    assert await route.recharge.compatible_gateways_async("IRT", 50000) == []
    assert route.repository.actor_id == route.admin.id
    screen = await route.ui.gateway_screen(route.admin)
    enable = screen.keyboard.inline_keyboard[0][0].callback_data
    await _gateway_callback(route, enable)
    assert await route.recharge.compatible_gateways_async("IRT", 50000) == ["atlaspay"]


@pytest.mark.parametrize(
    "rejection",
    ["customer", "frozen_admin", "banned_admin", "cross_chat", "group", "tampered"],
)
async def test_signed_gateway_controls_do_not_grant_customer_or_cross_chat_access(
    gateway_admin_route: SimpleNamespace,
    rejection: str,
) -> None:
    route = gateway_admin_route
    screen = await route.ui.gateway_screen(route.admin)
    disable = screen.keyboard.inline_keyboard[0][0].callback_data
    actor, chat, chat_type = 42, 42, "private"
    if rejection == "customer":
        actor, chat = 43, 43
    elif rejection == "frozen_admin":
        route.users[42] = replace(route.admin, status=UserStatus.FROZEN)
    elif rejection == "banned_admin":
        route.users[42] = replace(route.admin, status=UserStatus.BANNED)
    elif rejection == "cross_chat":
        chat = 43
    elif rejection == "group":
        chat, chat_type = -100, "supergroup"
    else:
        disable = disable[:-1] + ("0" if disable[-1] != "0" else "1")
    await _gateway_callback(route, disable, actor=actor, chat=chat, chat_type=chat_type)
    assert route.repository.values == {}
    assert route.repository.actor_id is None
    assert await route.recharge.compatible_gateways_async("IRT", 50000) == ["atlaspay"]
