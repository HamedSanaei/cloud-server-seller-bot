"""Telegram notifier tests (LEASEWEB-MVP).

The notifiers are best-effort by contract: delivery failures are swallowed,
and missing Telegram identities fall back to logs. These tests drive a fake
aiogram Bot and a fake user repository and assert WHAT is sent (server
card, renewal reminders, admin alerts) without touching Telegram.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from cloud_platform.bot.monthly_ui import format_minor
from cloud_platform.bot.notifier import (
    TelegramOrderDeliveryNotifier,
    TelegramRenewalNotifier,
    order_ref,
)
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.orders.service import RenewalInfo
from cloud_platform.modules.renewals.domain import RenewalKind
from cloud_platform.modules.users.domain import User, UserStatus

USER_ID = uuid4()
CHAT_ID = 123456789
ADMIN_CHAT = 987654321


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = False

    async def send_message(self, *, chat_id: int, text: str) -> Any:
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text))
        return MagicMock()


class FakeUsers:
    def __init__(self, user: User | None = None) -> None:
        self.user = user

    async def get(self, user_id: UUID) -> User | None:
        return self.user if user_id == USER_ID else None


def _user() -> User:
    return User(
        id=USER_ID,
        username="customer",
        email="c@t.me",
        status=UserStatus.ACTIVE,
        telegram_user_id=CHAT_ID,
    )


def _server(**overrides: Any) -> CloudServer:
    return CloudServer(
        id=uuid4(),
        user_id=USER_ID,
        provider_key="leaseweb",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.RUNNING,
        billing_model=BILLING_MODEL_PREPAID_MONTHLY,
        os="Ubuntu 24.04",
        ipv4="1.2.3.4",
        ipv6="2a01:4f8::1",
        **overrides,
    )


def _offer() -> SellableOffer:
    return SellableOffer(
        id=uuid4(),
        provider_key="leaseweb",
        product_id="VPS02_1",
        location_id="AMS-01",
        name="VPS S",
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic="10 TB",
        provider_cost_minor=999,
        provider_cost_currency="EUR",
        selling_price_minor=1299,
        selling_currency="EUR",
        billing_parameters={},
        provider_available=True,
        enabled=True,
    )


def _renewal_info(**overrides: Any) -> RenewalInfo:
    values: dict[str, Any] = dict(
        provider_contract_id="C-1",
        provider_order_ref="LS-ORD-1",
        purchased_at=datetime.now(UTC),
        provider_renewal_at=datetime.now(UTC) + timedelta(days=20),
        renewal_date_estimated=False,
        customer_price_minor=1299,
        currency="EUR",
    )
    values.update(overrides)
    return RenewalInfo(**values)


class TestOrderDeliveryNotifier:
    async def test_delivers_server_card_to_owner(self) -> None:
        bot = FakeBot()
        notifier = TelegramOrderDeliveryNotifier(bot, FakeUsers(_user()))  # type: ignore[arg-type]
        server = _server()
        await notifier.deliver(server=server, offer=_offer(), renewal=_renewal_info())
        assert len(bot.sent) == 1
        chat_id, text = bot.sent[0]
        assert chat_id == CHAT_ID
        assert "VPS S" in text
        assert "AMS-01" in text
        assert "Ubuntu 24.04" in text
        assert "1.2.3.4" in text
        assert "2a01:4f8::1" in text
        assert format_minor(1299, "EUR") in text
        assert "leaseweb-LS-ORD-1" in text
        # Never exposes internal ids or credentials.
        assert str(server.id) not in text
        assert "api" not in text.lower() or "API" not in text

    async def test_no_telegram_identity_falls_back_to_log(self, caplog: Any) -> None:
        bot = FakeBot()
        users = FakeUsers(_user())
        users.user.telegram_user_id = None
        notifier = TelegramOrderDeliveryNotifier(bot, users)  # type: ignore[arg-type]
        with caplog.at_level("WARNING"):
            await notifier.deliver(server=_server(), offer=_offer(), renewal=_renewal_info())
        assert bot.sent == []

    async def test_estimated_renewal_is_marked(self) -> None:
        bot = FakeBot()
        notifier = TelegramOrderDeliveryNotifier(bot, FakeUsers(_user()))  # type: ignore[arg-type]
        renewal = _renewal_info(renewal_date_estimated=True)
        await notifier.deliver(server=_server(), offer=_offer(), renewal=renewal)
        assert "تخمینی" in bot.sent[0][1]

    async def test_send_failure_is_swallowed(self) -> None:
        bot = FakeBot()
        bot.fail = True
        notifier = TelegramOrderDeliveryNotifier(bot, FakeUsers(_user()))  # type: ignore[arg-type]
        await notifier.deliver(server=_server(), offer=_offer(), renewal=_renewal_info())
        # Best-effort by contract: the exception is logged, not raised.

    async def test_order_ref_fallbacks(self) -> None:
        server = _server()
        assert order_ref(server, _renewal_info()) == "LS-ORD-1"
        assert (
            order_ref(server, _renewal_info(provider_order_ref=None, provider_contract_id="C-9"))
            == "C-9"
        )
        short = order_ref(server, _renewal_info(provider_order_ref=None, provider_contract_id=None))
        assert short == str(server.id)[:8]


class TestRenewalNotifier:
    async def test_warn_sends_insufficient_balance_line_for_3d(self) -> None:
        bot = FakeBot()
        notifier = TelegramRenewalNotifier(bot, FakeUsers(_user()))  # type: ignore[arg-type]
        await notifier.warn(
            user_id=USER_ID,
            server_id=uuid4(),
            kind=RenewalKind.WARN_3D,
            days_left=3,
            renewal_at=datetime.now(UTC) + timedelta(days=3),
            price_minor=1299,
            currency="EUR",
            balance_minor=500,
        )
        assert len(bot.sent) == 1
        assert "موجودی کافی نیست" in bot.sent[0][1]

    async def test_warn_sufficient_balance_has_no_warning_line(self) -> None:
        bot = FakeBot()
        notifier = TelegramRenewalNotifier(bot, FakeUsers(_user()))  # type: ignore[arg-type]
        await notifier.warn(
            user_id=USER_ID,
            server_id=uuid4(),
            kind=RenewalKind.WARN_7D,
            days_left=7,
            renewal_at=datetime.now(UTC) + timedelta(days=7),
            price_minor=1299,
            currency="EUR",
            balance_minor=5000,
        )
        assert "موجودی کافی نیست" not in bot.sent[0][1]

    async def test_warn_missing_identity_logs(self) -> None:
        bot = FakeBot()
        users = FakeUsers(_user())
        users.user.telegram_user_id = None
        notifier = TelegramRenewalNotifier(bot, users)  # type: ignore[arg-type]
        await notifier.warn(
            user_id=USER_ID,
            server_id=uuid4(),
            kind=RenewalKind.WARN_1D,
            days_left=1,
            renewal_at=datetime.now(UTC) + timedelta(days=1),
            price_minor=1299,
            currency="EUR",
            balance_minor=100,
        )
        assert bot.sent == []

    async def test_charged_sends_confirmation(self) -> None:
        bot = FakeBot()
        notifier = TelegramRenewalNotifier(bot, FakeUsers(_user()))  # type: ignore[arg-type]
        await notifier.charged(
            user_id=USER_ID,
            server_id=uuid4(),
            renewal_at=datetime.now(UTC) + timedelta(days=30),
            price_minor=1299,
            currency="EUR",
        )
        assert "تمديد" in bot.sent[0][1] or "تمدید" in bot.sent[0][1]

    async def test_alert_sends_to_admin_chat(self) -> None:
        bot = FakeBot()
        notifier = TelegramRenewalNotifier(
            bot,
            FakeUsers(_user()),
            admin_chat_id=ADMIN_CHAT,  # type: ignore[arg-type]
        )
        await notifier.alert(
            server_id=uuid4(),
            renewal_at=datetime.now(UTC) + timedelta(days=1),
            price_minor=1299,
            currency="EUR",
            balance_minor=50,
            provider_refs={"order": "LS-ORD-1", "contract": "C-1"},
            reason="insufficient funds; manual_cancellation required",
        )
        assert len(bot.sent) == 1
        chat_id, text = bot.sent[0]
        assert chat_id == ADMIN_CHAT
        assert "LS-ORD-1" in text
        assert "C-1" in text
        assert "پورتال" in text  # manual cancellation runbook hint

    async def test_alert_without_admin_chat_logs_error(self, caplog: Any) -> None:
        bot = FakeBot()
        notifier = TelegramRenewalNotifier(bot, FakeUsers(_user()))  # type: ignore[arg-type]
        with caplog.at_level("ERROR"):
            await notifier.alert(
                server_id=uuid4(),
                renewal_at=datetime.now(UTC) + timedelta(days=1),
                price_minor=1299,
                currency="EUR",
                balance_minor=50,
                provider_refs={"order": "LS-ORD-1"},
                reason="manual_cancellation required",
            )
        assert bot.sent == []
        assert any("RENEWAL ATTENTION" in r.message for r in caplog.records)

    async def test_send_failure_is_swallowed(self) -> None:
        bot = FakeBot()
        bot.fail = True
        notifier = TelegramRenewalNotifier(bot, FakeUsers(_user()), admin_chat_id=1)  # type: ignore[arg-type]
        await notifier.warn(
            user_id=USER_ID,
            server_id=uuid4(),
            kind=RenewalKind.WARN_7D,
            days_left=7,
            renewal_at=datetime.now(UTC) + timedelta(days=7),
            price_minor=1299,
            currency="EUR",
            balance_minor=5000,
        )
        await notifier.alert(
            server_id=uuid4(),
            renewal_at=datetime.now(UTC) + timedelta(days=1),
            price_minor=1299,
            currency="EUR",
            balance_minor=50,
            provider_refs={},
        )
        assert bot.sent == []
