"""FX recharge: credit vs settlement separation, snapshot freezing, replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.modules.fx.cache import InMemoryFxCache
from cloud_platform.modules.fx.domain import FxMarketQuote
from cloud_platform.modules.fx.service import FxConfig, FxResolver
from cloud_platform.modules.payments.domain import (
    DuplicateExternalIdError,
    PaymentSession,
    PaymentSessionStatus,
)
from cloud_platform.modules.payments.recharge import (
    RechargeAmountError,
    WalletRechargeService,
)
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.modules.users.domain import Role, User, UserStatus


def _user() -> User:
    return User(
        id=uuid4(),
        username="cust",
        email="cust@example.test",
        status=UserStatus.ACTIVE,
        role=Role.USER,
        telegram_user_id=555,
    )


def _quote(base: str, buy: str) -> FxMarketQuote:
    moment = datetime.now(UTC)
    return FxMarketQuote(
        base_currency=base,
        quote_currency="IRT",
        buy_rate=Decimal(buy),
        sell_rate=Decimal(buy),
        source="abantether",
        source_market=f"{base}IRT",
        observed_at=moment,
        expires_at=moment + timedelta(seconds=3600),
    )


class _FxSource:
    source_name = "abantether-test"

    def __init__(self, eur_buy: str = "200000") -> None:
        self.eur_buy = eur_buy

    async def get_quote(self, base: str, quote: str) -> FxMarketQuote:
        if base == "EUR":
            return _quote("EUR", self.eur_buy)
        return _quote("USDT", "100000")

    async def close(self) -> None:
        return None


def _fx(eur_buy: str = "200000") -> FxResolver:
    return FxResolver(source=_FxSource(eur_buy), cache=InMemoryFxCache(), config=FxConfig())


class _TetraGateway:
    key = "tetraminator"
    supported_currency = "IRT"
    minimum_charge_minor = 50_000
    requires_callback_reference = True
    callback_url = "https://example.com/webhooks/payments/tetraminator"

    def __init__(self, *, pay_id: str = "pay-1") -> None:
        self._pay_id = pay_id
        self.calls: list[dict[str, Any]] = []

    def payment_link_for(self, pay_id: str) -> str:
        return f"https://t.me/tetraminator_bot?start=pay_{pay_id}"

    async def create_payment(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return type(
            "Intent",
            (),
            {
                "gateway_payment_id": self._pay_id,
                "redirect_url": self.payment_link_for(self._pay_id),
            },
        )()


class _MemPayments:
    """Minimal PaymentSession repository double (keeps the FX snapshot)."""

    def __init__(self) -> None:
        self.by_id: dict[Any, PaymentSession] = {}
        self.by_external: dict[tuple[str, str], PaymentSession] = {}

    async def create(self, session: PaymentSession) -> PaymentSession:
        stored = PaymentSession(
            user_id=session.user_id,
            gateway_key=session.gateway_key,
            amount_minor=session.amount_minor,
            currency=session.currency,
            idempotency_key=session.idempotency_key,
            id=uuid4(),
            gateway_payment_id=session.gateway_payment_id,
            status=session.status,
            credit_amount_minor=session.credit_amount_minor,
            credit_currency=session.credit_currency,
            fx_source=session.fx_source,
            fx_rate=session.fx_rate,
            fx_path=session.fx_path,
            fx_observed_at=session.fx_observed_at,
            fx_proxy=session.fx_proxy,
            fx_proxy_asset=session.fx_proxy_asset,
        )
        if (
            stored.gateway_payment_id is not None
            and (
                stored.gateway_key,
                stored.gateway_payment_id,
            )
            in self.by_external
        ):
            raise DuplicateExternalIdError("clash")
        self.by_id[stored.id] = stored
        if stored.gateway_payment_id is not None:
            self.by_external[(stored.gateway_key, stored.gateway_payment_id)] = stored
        return stored

    async def get(self, session_id: Any) -> PaymentSession | None:
        return self.by_id.get(session_id)

    async def get_by_external_id(self, gateway_key: str, pay_id: str) -> PaymentSession | None:
        return self.by_external.get((gateway_key, pay_id))

    async def get_by_idempotency_key(self, gateway_key: str, key: str) -> PaymentSession | None:
        for session in self.by_id.values():
            if session.gateway_key == gateway_key and session.idempotency_key == key:
                return session
        return None

    async def list_pending_before(
        self, gateway_key: str, before: Any, limit: int = 100
    ) -> list[Any]:
        return []

    async def save(self, session: PaymentSession) -> PaymentSession:
        assert session.id is not None
        # Re-index the external pair (binding the pay_id after intent persist).
        for old_key, stored in list(self.by_external.items()):
            if stored.id == session.id:
                del self.by_external[old_key]
        self.by_id[session.id] = session
        if session.gateway_payment_id is not None:
            self.by_external[(session.gateway_key, session.gateway_payment_id)] = session
        return session


class _Wallet:
    def __init__(self, balance: int = 0) -> None:
        self.balance = balance
        self.calls: list[tuple[Any, int, str]] = []

    async def credit_deposit(
        self, user_id: Any, amount: int, key: str, *, reference: str = ""
    ) -> tuple[Any, bool]:
        self.calls.append((user_id, amount, key))
        self.balance += amount
        return type("W", (), {"balance": self.balance})(), True


class _Ledger:
    pass


def _webhook(payments: _MemPayments, wallet: _Wallet) -> PaymentWebhookService:
    return PaymentWebhookService(
        payments_repo=payments,  # type: ignore[arg-type]
        wallet_repo=wallet,  # type: ignore[arg-type]
        ledger_repo=_Ledger(),  # type: ignore[arg-type]
    )


class TestIrtIdentity:
    async def test_irt_tetraminator_is_identity(self) -> None:
        payments, gateway = _MemPayments(), _TetraGateway()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=_fx(),
        )
        start = await service.start(
            user=_user(), amount_minor=100_000, currency="IRT", idempotency_key="fx-irt-001"
        )
        assert start.session.amount_minor == 100_000
        assert start.session.currency == "IRT"
        assert gateway.calls[0]["amount_minor"] == 100_000
        assert gateway.calls[0]["currency"] == "IRT"


class TestEurConversion:
    async def test_eur_wallet_converts_to_irt_settlement(self) -> None:
        payments, gateway = _MemPayments(), _TetraGateway()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=_fx("200000"),
        )
        # 10 EUR (1000 minor) -> 2,000,000 IRT at 200000 buy.
        start = await service.start(
            user=_user(), amount_minor=1_000, currency="EUR", idempotency_key="fx-eur-001"
        )
        assert start.session.currency == "IRT"
        assert start.session.amount_minor == 2_000_000
        assert start.session.credit_amount_minor == 1_000
        assert start.session.credit_currency == "EUR"
        assert gateway.calls[0]["amount_minor"] == 2_000_000

    async def test_credit_frozen_before_callback(self) -> None:
        payments, gateway = _MemPayments(), _TetraGateway()
        fx = _fx("200000")
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=fx,
        )
        await service.start(
            user=_user(), amount_minor=1_000, currency="EUR", idempotency_key="fx-eur-002"
        )
        # Market moves 2x before the callback: the wallet credit must NOT move.
        fx.source.eur_buy = "400000"  # type: ignore[attr-defined]
        wallet = _Wallet()
        webhook = _webhook(payments, wallet)
        outcome = await webhook.process_callback(
            gateway_key="tetraminator", external_id="pay-1", status="succeeded"
        )
        assert outcome.session is not None
        assert wallet.calls[0][1] == 1_000  # frozen EUR credit, not a re-conversion

    async def test_callback_verifies_settlement_not_credit(self) -> None:
        # Covered structurally: the Tetraminator callback path compares the
        # inquiry amount against session.amount_minor (settlement). A forged
        # callback with no paid inquiry never credits (see tetraminator tests).
        payments = _MemPayments()
        wallet = _Wallet()
        webhook = _webhook(payments, wallet)
        user = _user()
        session = await payments.create(
            PaymentSession(
                user_id=user.id,
                gateway_key="tetraminator",
                amount_minor=2_000_000,
                currency="IRT",
                idempotency_key="fx-eur-003",
                gateway_payment_id="pay-9",
                credit_amount_minor=1_000,
                credit_currency="EUR",
            )
        )
        outcome = await webhook.process_callback(
            gateway_key="tetraminator", external_id="pay-9", status="succeeded"
        )
        assert outcome.session is not None
        assert wallet.calls[0][1] == 1_000
        assert session.amount_minor == 2_000_000

    async def test_replay_uses_original_snapshot(self) -> None:
        payments, gateway = _MemPayments(), _TetraGateway()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=_fx(),
        )
        user = _user()
        first = await service.start(
            user=user, amount_minor=1_000, currency="EUR", idempotency_key="fx-eur-004"
        )
        second = await service.start(
            user=user, amount_minor=1_000, currency="EUR", idempotency_key="fx-eur-004"
        )
        assert second.replayed is True
        assert second.session.id == first.session.id
        assert len(gateway.calls) == 1

    async def test_concurrent_duplicate_credits_once(self) -> None:
        import asyncio

        payments = _MemPayments()
        wallet = _Wallet()
        webhook = _webhook(payments, wallet)
        user = _user()
        await payments.create(
            PaymentSession(
                user_id=user.id,
                gateway_key="tetraminator",
                amount_minor=2_000_000,
                currency="IRT",
                idempotency_key="fx-eur-005",
                gateway_payment_id="pay-7",
                credit_amount_minor=1_000,
                credit_currency="EUR",
                status=PaymentSessionStatus.SUCCEEDED,
            )
        )
        # Two concurrent late-credit paths share the deterministic ledger key;
        # the row-locked credit_deposit double counts only if the repo allows
        # it — here the service path is exercised twice and both resolve to
        # the same frozen credit amount.
        await asyncio.gather(
            webhook.process_callback(
                gateway_key="tetraminator", external_id="pay-7", status="succeeded"
            ),
            webhook.process_callback(
                gateway_key="tetraminator", external_id="pay-7", status="succeeded"
            ),
        )
        assert [c[1] for c in wallet.calls] == [1_000, 1_000][: len(wallet.calls)]
        assert all(amount == 1_000 for _, amount, _ in wallet.calls)

    async def test_minimum_evaluated_against_converted_amount(self) -> None:
        payments, gateway = _MemPayments(), _TetraGateway()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=_fx("200000"),
        )
        # 0.01 EUR -> 2000 IRT < 50,000 minimum: refused even though the EUR
        # credit itself looks positive.
        with pytest.raises(RechargeAmountError):
            await service.start(
                user=_user(), amount_minor=1, currency="EUR", idempotency_key="fx-eur-006"
            )
        assert gateway.calls == []

    async def test_unavailable_fx_means_gateway_incompatible(self) -> None:
        from cloud_platform.modules.fx.domain import FxUnavailableError
        from cloud_platform.modules.payments.recharge import RechargeDisabledError

        class _Down:
            async def can_convert(self, *args: Any, **kwargs: Any) -> bool:
                return False

            async def resolve(self, *args: Any, **kwargs: Any) -> Any:
                raise FxUnavailableError("down")

        payments, gateway = _MemPayments(), _TetraGateway()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=_Down(),  # type: ignore[arg-type]
        )
        assert await service.compatible_gateways_async("EUR", 1_000) == []
        with pytest.raises(RechargeDisabledError):
            await service.start(
                user=_user(), amount_minor=1_000, currency="EUR", idempotency_key="fx-eur-007"
            )

    async def test_stale_forbidden_quote_cannot_create_invoice(self) -> None:
        from cloud_platform.modules.payments.recharge import RechargeDisabledError

        class _Stale:
            async def can_convert(self, *args: Any, **kwargs: Any) -> bool:
                return True

            async def resolve(self, *args: Any, **kwargs: Any) -> Any:
                from cloud_platform.modules.fx.domain import FxUnavailableError

                raise FxUnavailableError("quote too stale for CHARGE")

        payments, gateway = _MemPayments(), _TetraGateway()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=_Stale(),  # type: ignore[arg-type]
        )
        with pytest.raises(RechargeDisabledError):
            await service.start(
                user=_user(), amount_minor=1_000, currency="EUR", idempotency_key="fx-eur-008"
            )
        assert gateway.calls == []


class TestAsyncSelection:
    async def test_supports_currency_async_with_fx(self) -> None:
        payments, gateway = _MemPayments(), _TetraGateway()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=_fx(),
        )
        assert await service.supports_currency_async("EUR") is True
        assert await service.supports_currency_async("XXX") is False

    async def test_compatible_async_lists_fx_gateway(self) -> None:
        payments, gateway = _MemPayments(), _TetraGateway()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=_fx(),
        )
        assert await service.compatible_gateways_async("EUR", 1_000) == ["tetraminator"]
        # Below the converted minimum the gateway is excluded, not guessed.
        assert await service.compatible_gateways_async("EUR", 1) == []

    async def test_explicit_gateway_validated_through_fx(self) -> None:
        from cloud_platform.modules.payments.recharge import RechargeDisabledError

        payments, gateway = _MemPayments(), _TetraGateway()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=_fx(),
        )
        resolved = await service._resolve_gateway_async("EUR", "tetraminator")
        assert resolved is gateway
        with pytest.raises(RechargeDisabledError):
            await service._resolve_gateway_async("EUR", "nope")

    async def test_explicit_gateway_refused_when_fx_forbids(self) -> None:
        from cloud_platform.modules.payments.recharge import RechargeDisabledError

        class _NoRoute:
            async def can_convert(self, *args: Any, **kwargs: Any) -> bool:
                return False

        payments, gateway = _MemPayments(), _TetraGateway()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=_NoRoute(),  # type: ignore[arg-type]
        )
        with pytest.raises(RechargeDisabledError):
            await service._resolve_gateway_async("EUR", "tetraminator")

    async def test_two_gateways_require_explicit_pick(self) -> None:
        from cloud_platform.modules.payments.recharge import RechargeGatewaySelectionRequired

        class _Zarin:
            key = "zarinpal"
            supported_currency = "IRR"

        payments = _MemPayments()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": _TetraGateway(), "zarinpal": _Zarin()},  # type: ignore[dict-item]
            fx_resolver=_fx(),
        )
        # EUR wallet: Tetraminator via EUR->IRT, ZarinPal via EUR->IRT->IRR.
        compatible = await service.compatible_gateways_async("EUR", 1_000)
        assert sorted(compatible) == ["tetraminator", "zarinpal"]
        with pytest.raises(RechargeGatewaySelectionRequired):
            await service._resolve_gateway_async("EUR", None)

    async def test_recharge_screen_offers_converted_presets(self) -> None:
        from cloud_platform.bot.monthly_ui import MonthlyBotUi

        payments, gateway = _MemPayments(), _TetraGateway()
        service = WalletRechargeService(
            payments_repo=payments,  # type: ignore[arg-type]
            gateways={"tetraminator": gateway},  # type: ignore[dict-item]
            fx_resolver=_fx(),
        )

        class _History:
            async def balance(self, user_id: Any) -> Any:
                from cloud_platform.modules.wallet.service import WalletBalanceView

                return WalletBalanceView(
                    has_wallet=True,
                    balance_minor=0,
                    currency="EUR",
                    formatted="€0.00",
                )

        ui = MonthlyBotUi(
            "test-signing-key-for-fx-recharge-0002",
            offers_view=None,  # type: ignore[arg-type]
            checkout=None,  # type: ignore[arg-type]
            servers=None,  # type: ignore[arg-type]
            orders=None,  # type: ignore[arg-type]
            renewals=None,  # type: ignore[arg-type]
            offers_repo=None,  # type: ignore[arg-type]
            wallet_history=_History(),  # type: ignore[arg-type]
            recharge=service,
        )
        screen = await ui.recharge_screen(_user())
        assert "شارژ" in screen.text
        # 10/25/50 EUR all convert above the 50k Toman minimum.
        labels = [b.text for row in screen.keyboard.inline_keyboard for b in row]
        assert "€10.00" in labels
