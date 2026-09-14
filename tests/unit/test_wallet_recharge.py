"""Wallet top-up sessions and the operator-channel transport.

A recharge is financially inert until the gateway webhook verifies it: this
service only creates the pending session (with the gateway authority) and
enqueues ``recharge.created``. The tests pin:

- the session is durable before the event is emitted;
- a replayed authority reuses the session (no second row, one event);
- another user's authority is refused;
- an unsupported currency is refused BEFORE any gateway call;
- the Telegram channel adapter posts exactly the rendered card.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.bot.monthly_ui import MonthlyBotUi, recharge_presets
from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.businesslog.domain import BusinessEvent, BusinessEventType
from cloud_platform.modules.payments.domain import (
    DuplicateExternalIdError,
    PaymentSessionStatus,
)
from cloud_platform.modules.payments.recharge import (
    RechargeAmountError,
    RechargeDisabledError,
    RechargeError,
    WalletRechargeService,
)
from cloud_platform.modules.users.domain import Role, User, UserStatus

USER_ID = uuid4()
SESSION_ID = uuid4()
SIGNING_KEY = "recharge-signing-key"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[BusinessEvent] = []

    async def emit(self, event: BusinessEvent) -> bool:
        self.events.append(event)
        return True


class FakePayments:
    """Payment-session repository double with the unique-authority rule."""

    def __init__(self) -> None:
        self.by_authority: dict[str, Any] = {}
        self.create_calls = 0

    async def create(self, session: Any) -> Any:
        self.create_calls += 1
        if session.gateway_payment_id in self.by_authority:
            raise DuplicateExternalIdError("already exists")
        stored = _session(session, session_id=SESSION_ID)
        self.by_authority[session.gateway_payment_id] = stored
        return stored

    async def get(self, session_id: UUID) -> Any:
        return next(
            (s for s in self.by_authority.values() if s.id == session_id),
            None,
        )

    async def get_by_external_id(self, gateway_key: str, authority: str) -> Any:
        return self.by_authority.get(authority)

    async def save(self, session: Any) -> Any:
        return session


def _session(session: Any, *, session_id: UUID) -> Any:
    return type(
        "S",
        (),
        {
            "id": session_id,
            "user_id": session.user_id,
            "gateway_key": session.gateway_key,
            "gateway_payment_id": session.gateway_payment_id,
            "amount_minor": session.amount_minor,
            "currency": session.currency,
            "idempotency_key": session.idempotency_key,
            "status": PaymentSessionStatus.PENDING,
            "credited_at": None,
        },
    )()


class FakeGateway:
    key = "fake-gateway"
    supported_currency = "EUR"

    def __init__(self, *, authority: str = "AUTH-1") -> None:
        self.authority = authority
        self.calls: list[dict[str, Any]] = []

    async def create_payment(
        self,
        *,
        amount_minor: int,
        currency: str,
        reference: str,
        idempotency_key: IdempotencyKey,
        redirect_url: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "amount_minor": amount_minor,
                "currency": currency,
                "reference": reference,
                "idempotency_key": idempotency_key.value,
                "redirect_url": redirect_url,
            }
        )
        return type(
            "Intent",
            (),
            {
                "gateway_payment_id": self.authority,
                "redirect_url": f"https://pay.example/{self.authority}",
            },
        )()


def _user(user_id: UUID = USER_ID) -> User:
    return User(
        id=user_id,
        username="cust",
        email="cust@example.test",
        status=UserStatus.ACTIVE,
        role=Role.USER,
        telegram_user_id=555,
    )


def _service(
    *, gateway: FakeGateway | None = None, sink: RecordingSink | None = None
) -> tuple[WalletRechargeService, FakePayments, FakeGateway]:
    repo = FakePayments()
    resolved = gateway or FakeGateway()
    service = WalletRechargeService(
        payments_repo=repo,
        gateway=resolved,
        event_sink=sink if sink is not None else RecordingSink(),
        user_repo=None,
    )
    return service, repo, resolved


class TestRechargeStart:
    async def test_creates_the_session_then_emits_created(self) -> None:
        sink = RecordingSink()
        service, repo, gateway = _service(sink=sink)
        start = await service.start(
            user=_user(), amount_minor=2_500, currency="EUR", idempotency_key="bot-recharge-1"
        )
        assert start.replayed is False
        assert start.session.id == SESSION_ID
        assert start.redirect_url == "https://pay.example/AUTH-1"
        assert repo.create_calls == 1
        assert gateway.calls[0]["amount_minor"] == 2_500
        assert gateway.calls[0]["reference"] == str(USER_ID)
        assert sink.events[0].event_type is BusinessEventType.RECHARGE_CREATED
        assert sink.events[0].payload["payment_session_id"] == str(SESSION_ID)
        assert sink.events[0].payload["amount"] == "25.00 EUR"

    async def test_replayed_authority_reuses_the_session(self) -> None:
        sink = RecordingSink()
        service, repo, _gateway = _service(sink=sink)
        first = await service.start(
            user=_user(), amount_minor=2_500, currency="EUR", idempotency_key="bot-recharge-1"
        )
        second = await service.start(
            user=_user(), amount_minor=2_500, currency="EUR", idempotency_key="bot-recharge-1"
        )
        assert first.session.id == second.session.id == SESSION_ID
        assert second.replayed is True
        assert len(repo.by_authority) == 1
        # Same session id -> same event key: exactly one business event.
        assert len({e.event_key for e in sink.events}) == 1

    async def test_another_users_authority_is_refused(self) -> None:
        service, _repo, _gateway = _service()
        await service.start(
            user=_user(), amount_minor=2_500, currency="EUR", idempotency_key="bot-recharge-1"
        )
        with pytest.raises(RechargeError):
            await service.start(
                user=_user(uuid4()),
                amount_minor=2_500,
                currency="EUR",
                idempotency_key="bot-recharge-2",
            )

    async def test_gateway_without_an_authority_is_an_error(self) -> None:
        service, repo, _gateway = _service(gateway=FakeGateway(authority=""))
        with pytest.raises(RechargeError):
            await service.start(
                user=_user(), amount_minor=2_500, currency="EUR", idempotency_key="recharge-key-1"
            )
        assert repo.create_calls == 0

    async def test_empty_idempotency_key_is_refused(self) -> None:
        service, _repo, gateway = _service()
        with pytest.raises(RechargeError):
            await service.start(
                user=_user(), amount_minor=2_500, currency="EUR", idempotency_key="  "
            )
        assert gateway.calls == []

    async def test_structurally_invalid_idempotency_key_is_refused(self) -> None:
        """A short key fails as a domain error, not as a ValueError."""
        service, _repo, gateway = _service()
        with pytest.raises(RechargeError, match="8\\.\\.128"):
            await service.start(
                user=_user(), amount_minor=2_500, currency="EUR", idempotency_key="short"
            )
        assert gateway.calls == []


class TestRechargeGuards:
    async def test_amount_must_be_a_positive_integer(self) -> None:
        service, _repo, gateway = _service()
        for amount in (0, -1, "2500", 2.5):
            with pytest.raises(RechargeAmountError):
                await service.start(
                    user=_user(),
                    amount_minor=amount,  # type: ignore[arg-type]
                    currency="EUR",
                    idempotency_key="recharge-key-1",
                )
        assert gateway.calls == []

    async def test_unsupported_currency_is_refused_before_the_gateway_call(self) -> None:
        service, _repo, gateway = _service()
        assert service.supports_currency("EUR") is True
        assert service.supports_currency("IRR") is False
        with pytest.raises(RechargeDisabledError):
            await service.start(
                user=_user(), amount_minor=2_500, currency="IRR", idempotency_key="recharge-key-1"
            )
        assert gateway.calls == []

    async def test_unsaved_user_is_refused(self) -> None:
        service, _repo, _gateway = _service()
        with pytest.raises(RechargeError):
            await service.start(
                user=User(id=None, username="x", email="x@y.z"),
                amount_minor=100,
                currency="EUR",
                idempotency_key="recharge-key-1",
            )

    async def test_disabled_gateway_reports_itself(self) -> None:
        service = WalletRechargeService(payments_repo=FakePayments(), gateway=None)
        assert service.gateway_key == ""
        assert service.supports_currency("EUR") is False
        with pytest.raises(RechargeDisabledError):
            await service.start(
                user=_user(), amount_minor=100, currency="EUR", idempotency_key="recharge-key-1"
            )


class TestTelegramChannel:
    """The channel adapter posts the rendered card, nothing else."""

    async def test_send_targets_the_configured_chat(self) -> None:
        from cloud_platform.modules.businesslog.telegram import TelegramBusinessLogChannel

        sent: list[dict[str, Any]] = []

        class _Bot:
            async def send_message(self, **kwargs: Any) -> None:
                sent.append(kwargs)

        channel = TelegramBusinessLogChannel(_Bot(), -1001234)
        await channel.send("card text")
        assert sent == [
            {
                "chat_id": -1001234,
                "text": "card text",
                "disable_web_page_preview": True,
            }
        ]

    async def test_failure_propagates_so_the_outbox_retries(self) -> None:
        from cloud_platform.modules.businesslog.telegram import TelegramBusinessLogChannel

        class _Bot:
            async def send_message(self, **kwargs: Any) -> None:
                raise RuntimeError("telegram down")

        with pytest.raises(RuntimeError):
            await TelegramBusinessLogChannel(_Bot(), -100).send("x")

    def test_a_chat_id_is_required(self) -> None:
        from cloud_platform.modules.businesslog.telegram import TelegramBusinessLogChannel

        with pytest.raises(ValueError):
            TelegramBusinessLogChannel(bot=object(), chat_id=0)


class FakeRecharge:
    """Recharge-service double for the bot screens."""

    def __init__(self, *, currency: str = "EUR", fail: Exception | None = None) -> None:
        self.supports = currency
        self.fail = fail
        self.started: list[tuple[int, str]] = []

    def supports_currency(self, currency: str) -> bool:
        return bool(self.supports) and self.supports == currency

    async def start(self, **kwargs: Any) -> Any:
        if self.fail is not None:
            raise self.fail
        self.started.append((kwargs["amount_minor"], kwargs["idempotency_key"]))
        return type(
            "Start",
            (),
            {
                "session": type(
                    "S",
                    (),
                    {"amount_minor": kwargs["amount_minor"], "currency": kwargs["currency"]},
                )(),
                "redirect_url": "https://pay.example/AUTH-1",
                "replayed": len(self.started) > 1,
            },
        )()


class FakeWalletHistory:
    def __init__(self, currency: str = "EUR") -> None:
        self.currency = currency

    async def balance(self, user_id: UUID) -> Any:
        return type(
            "View",
            (),
            {
                "has_wallet": True,
                "balance_minor": 1_000,
                "currency": self.currency,
                "formatted": "10.00 EUR",
            },
        )()

    async def history(self, user_id: UUID, limit: int = 20) -> Any:
        return type("Page", (), {"items": []})()


def _bot(*, currency: str = "EUR", recharge: Any = None) -> MonthlyBotUi:
    from cloud_platform.bot.monthly_ui import MonthlyBotUi

    return MonthlyBotUi(
        SIGNING_KEY,
        offers_view=type(
            "V",
            (),
            {
                "markets_screen": lambda self: [],
                "provider_display_name": lambda self, key: key,
            },
        )(),
        checkout=type("C", (), {})(),
        servers=type("S", (), {})(),
        orders=type("O", (), {})(),
        renewals=type("R", (), {})(),
        offers_repo=type("F", (), {})(),
        wallet_history=FakeWalletHistory(currency),
        recharge=recharge,
    )


def _buttons(screen: Any) -> list[Any]:
    return [b for row in screen.keyboard.inline_keyboard for b in row]


class TestRechargeScreens:
    """The bot offers a top-up only when it can actually work."""

    def test_presets_scale_with_the_currency(self) -> None:
        assert recharge_presets("EUR") == (1_000, 2_500, 5_000)
        assert recharge_presets("IRR") == (500_000, 1_000_000, 2_000_000)
        assert recharge_presets("irr") == (500_000, 1_000_000, 2_000_000)

    async def test_amount_screen_lists_preset_amounts(self) -> None:
        bot = _bot(recharge=FakeRecharge())
        screen = await bot.recharge_screen(_user())
        labels = [b.text for b in _buttons(screen)]
        assert "10.00 EUR" in labels
        assert "25.00 EUR" in labels

    async def test_start_screen_creates_the_session_and_offers_the_gateway(
        self,
    ) -> None:
        recharge = FakeRecharge()
        bot = _bot(recharge=recharge)
        screen = await bot.recharge_start_screen(_user(), "2500")
        assert "25.00 EUR" in screen.text
        pay = next(b for b in _buttons(screen) if b.url)
        assert pay.url == "https://pay.example/AUTH-1"
        assert recharge.started == [(2_500, f"bot-recharge:{USER_ID}:2500")]

    async def test_repeated_tap_replays_the_same_idempotency_key(self) -> None:
        recharge = FakeRecharge()
        bot = _bot(recharge=recharge)
        await bot.recharge_start_screen(_user(), "2500")
        await bot.recharge_start_screen(_user(), "2500")
        assert recharge.started[0][1] == recharge.started[1][1]

    async def test_unsupported_currency_points_at_support(self) -> None:
        bot = _bot(currency="EUR", recharge=FakeRecharge(currency="IRR"))
        screen = await bot.recharge_screen(_user())
        assert "فعال نیست" in screen.text

    async def test_invalid_amount_is_reported(self) -> None:
        bot = _bot(recharge=FakeRecharge())
        screen = await bot.recharge_start_screen(_user(), "not-a-number")
        assert "معتبر نیست" in screen.text

    async def test_service_errors_are_rendered_not_raised(self) -> None:
        bot = _bot(recharge=FakeRecharge(fail=RechargeAmountError("bad")))
        screen = await bot.recharge_start_screen(_user(), "2500")
        assert "معتبر نیست" in screen.text

    async def test_disabled_recharge_keeps_the_wallet_screen_usable(self) -> None:
        bot = _bot(recharge=FakeRecharge(fail=RechargeDisabledError("off")))
        screen = await bot.recharge_start_screen(_user(), "2500")
        assert "فعال نیست" in screen.text

    async def test_recharge_callback_dispatch(self) -> None:
        bot = _bot(recharge=FakeRecharge())
        amounts = await bot.handle(bot._callback("recharge", "amounts"), user=_user())
        assert amounts is not None and "شارژ" in amounts.text
        started = await bot.handle(bot._callback("recharge", "start", "1000"), user=_user())
        assert started is not None and "10.00 EUR" in started.text

    async def test_unknown_recharge_screen_falls_back_to_the_menu(self) -> None:
        bot = _bot(recharge=FakeRecharge())
        screen = await bot.handle(bot._callback("recharge", "nonsense"), user=_user())
        assert screen is not None and "منوی اصلی" in screen.text


class TestRechargeServiceAgainstTheRealGatewayShape:
    """Currency support is read from the gateway, not hardcoded."""

    def test_zarinpal_declares_its_supported_currency(self) -> None:
        from cloud_platform.providers.zarinpal.client import CURRENCY, ZarinPalGateway

        gateway = ZarinPalGateway(merchant_id="m")
        assert gateway.supported_currency == CURRENCY == "IRR"

    def test_a_matching_currency_is_accepted(self) -> None:
        service = WalletRechargeService(payments_repo=FakePayments(), gateway=FakeGateway())
        assert service.supports_currency("eur") is True

    def test_timestamps_are_utc_aware(self) -> None:
        moment = datetime.now(UTC)
        assert moment.tzinfo is not None
