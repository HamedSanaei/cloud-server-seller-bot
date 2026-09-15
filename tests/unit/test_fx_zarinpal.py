"""ZarinPal settlement through FX: IRT->IRR exact, EUR->IRT->IRR."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from cloud_platform.modules.fx.cache import InMemoryFxCache
from cloud_platform.modules.fx.domain import FxMarketQuote
from cloud_platform.modules.fx.service import FxConfig, FxResolver
from cloud_platform.modules.payments.recharge import WalletRechargeService
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

    async def get_quote(self, base: str, quote: str) -> FxMarketQuote:
        return _quote(base, "200000" if base == "EUR" else "100000")

    async def close(self) -> None:
        return None


class _ZarinGateway:
    key = "zarinpal"
    supported_currency = "IRR"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create_payment(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return type(
            "Intent",
            (),
            {"gateway_payment_id": "AUTH-1", "redirect_url": "https://pay.example/AUTH-1"},
        )()


class _MemPayments:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def create(self, session: Any) -> Any:
        from types import SimpleNamespace

        # PaymentSession uses slots (no __dict__): copy field by field.
        stored = SimpleNamespace(
            user_id=session.user_id,
            gateway_key=session.gateway_key,
            amount_minor=session.amount_minor,
            currency=session.currency,
            idempotency_key=session.idempotency_key,
            gateway_payment_id=session.gateway_payment_id,
            status=session.status,
            credit_amount_minor=session.credit_amount_minor,
            credit_currency=session.credit_currency,
            id=uuid4(),
        )
        self.rows.append(stored)
        return stored

    async def get_by_external_id(self, *args: Any) -> Any:
        return None

    async def get_by_idempotency_key(self, *args: Any) -> Any:
        return None

    async def save(self, session: Any) -> Any:
        return session


class TestZarinPalSettlement:
    async def test_irt_to_irr_exact_x10(self) -> None:
        fx = FxResolver(source=_FxSource(), cache=InMemoryFxCache(), config=FxConfig())
        gateway = _ZarinGateway()
        service = WalletRechargeService(
            payments_repo=_MemPayments(),  # type: ignore[arg-type]
            gateways={"zarinpal": gateway},  # type: ignore[dict-item]
            fx_resolver=fx,
        )
        start = await service.start(
            user=_user(), amount_minor=50_000, currency="IRT", idempotency_key="fx-zar-001"
        )
        assert start.session.currency == "IRR"
        assert start.session.amount_minor == 500_000
        assert gateway.calls[0]["amount_minor"] == 500_000
        assert gateway.calls[0]["currency"] == "IRR"

    async def test_eur_to_irr_through_irt_anchor(self) -> None:
        fx = FxResolver(source=_FxSource(), cache=InMemoryFxCache(), config=FxConfig())
        gateway = _ZarinGateway()
        service = WalletRechargeService(
            payments_repo=_MemPayments(),  # type: ignore[arg-type]
            gateways={"zarinpal": gateway},  # type: ignore[dict-item]
            fx_resolver=fx,
        )
        # 10 EUR -> 2,000,000 IRT -> 20,000,000 IRR (exact integer chain).
        start = await service.start(
            user=_user(), amount_minor=1_000, currency="EUR", idempotency_key="fx-zar-002"
        )
        assert start.session.currency == "IRR"
        assert start.session.amount_minor == 20_000_000
        assert gateway.calls[0]["amount_minor"] == 20_000_000

    async def test_no_float_in_chain(self) -> None:
        fx = FxResolver(source=_FxSource(), cache=InMemoryFxCache(), config=FxConfig())
        resolved = await fx.resolve(
            1_000,
            "EUR",
            "IRR",
            __import__("cloud_platform.modules.fx.domain", fromlist=["FxPurpose"]).FxPurpose.CHARGE,
        )
        assert isinstance(resolved.target_amount_minor, int)
        assert resolved.target_amount_minor == 20_000_000
