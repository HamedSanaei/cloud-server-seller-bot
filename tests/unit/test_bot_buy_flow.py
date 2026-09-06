"""Tests for the full bot buy flow (M08-002..006).

The whole click path — main menu -> datacenter -> plans -> OS -> confirm ->
order — runs through the REAL domain services (BuyFlowViewService,
OsSelectionService, PurchaseConfirmationService) with fake repos and a fake
provider, over the signed M08-001 callbacks. Verifies navigation, the
idempotent order placement and the friendly error mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cloud_platform.bot.ui import BotUi
from cloud_platform.modules.catalog.domain import CatalogOffer, LocationRecord, OfferRef
from cloud_platform.modules.catalog.service import (
    BuyFlowViewService,
    OsSelectionService,
    PurchaseConfirmationService,
)
from cloud_platform.modules.compute.service import QuotaExceededError
from cloud_platform.modules.navigation.domain import decode_callback
from cloud_platform.modules.pricing.domain import MarginRule, SellingPrice
from cloud_platform.modules.users.domain import User
from cloud_platform.modules.wallet.domain import Wallet
from cloud_platform.providers.base import ProviderImage

KEY = "test-signing-key"


def _offer(offer_id: UUID, plan_id: str = "cx22") -> CatalogOffer:
    return CatalogOffer(
        id=offer_id,
        provider_key="hetzner",
        plan_id=plan_id,
        location_id="fsn1",
        name="CX22",
        architecture="x86",
        vcpu=2,
        memory_mb=4096,
        disk_gb=40,
        currency="EUR",
        price_per_quantum=219,
        quantum_seconds=3600,
        enabled=True,
    )


def _loc() -> LocationRecord:
    return LocationRecord(
        provider_key="hetzner",
        location_id="fsn1",
        name="Falkenstein",
        country_code="DE",
        city="Falkenstein",
    )


@dataclass
class FakeCatalog:
    offers: list[CatalogOffer]

    async def list_offers(self) -> list[CatalogOffer]:
        return list(self.offers)

    async def get_by_id(self, offer_id: UUID) -> CatalogOffer | None:
        for offer in self.offers:
            if offer.id == offer_id:
                return offer
        return None


@dataclass
class FakeLocations:
    records: list[LocationRecord]

    async def list_for_provider(self, provider_key: str) -> list[LocationRecord]:
        return [r for r in self.records if r.provider_key == provider_key]


class FakeProvider:
    async def list_images(self) -> list[ProviderImage]:
        return [
            ProviderImage(
                id="ubuntu-24.04", name="Ubuntu 24.04", os_family="ubuntu", architecture="x86"
            ),
            ProviderImage(id="debian-12", name="Debian 12", os_family="debian", architecture="x86"),
            ProviderImage(
                id="arm-ubuntu", name="Ubuntu ARM", os_family="ubuntu", architecture="arm"
            ),
        ]


class FakeRegistry:
    def get(self, provider_key: str) -> FakeProvider:
        if provider_key != "hetzner":
            raise KeyError(provider_key)
        return FakeProvider()


@dataclass
class FakeBooks:
    def __init__(self) -> None:
        self._rule = MarginRule(
            provider="hetzner",
            plan="cx22",
            location="fsn1",
            margin_factor=Decimal("1.15"),
            monthly_cap_minor=5000,
        )

    async def sell_price(self, *, book_name: str, offer: object, at: datetime) -> SellingPrice:
        return SellingPrice(
            offer=offer,  # type: ignore[arg-type]
            selling_minor=107,
            rule=self._rule,
            book_name=book_name,
            version=3,
            priced_at=at,
        )


@dataclass
class FakeWallets:
    balance: int = 1000

    async def get(self, user_id: UUID) -> Wallet:
        return Wallet(user_id=user_id, balance=self.balance)


class FakeCreate:
    """Records order calls; replays by idempotency key like the real command."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[tuple[User, OfferRef, str]] = []
        self._replayed: set[str] = set()
        self._fail = fail

    async def create_server(
        self,
        *,
        user: User,
        offer_ref: OfferRef,
        idempotency_key: str,
        at: object | None = None,
    ) -> SimpleNamespace:
        del at
        if self._fail is not None:
            raise self._fail
        # like the real command: a replay returns the original outcome
        # without running the mutation again
        replayed = idempotency_key in self._replayed
        if not replayed:
            self._replayed.add(idempotency_key)
            self.calls.append((user, offer_ref, idempotency_key))
        return SimpleNamespace(
            server=SimpleNamespace(id=UUID("11111111-1111-1111-1111-111111111111")),
            snapshot=None,
            hold=None,
            replayed=replayed,
        )


def _user() -> User:
    return User(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        username="alice",
        email="alice@example.com",
    )


def _ui(
    *,
    balance: int = 1000,
    create: FakeCreate | None = None,
) -> tuple[BotUi, FakeCatalog]:
    offer_id = uuid4()
    catalog = FakeCatalog([_offer(offer_id)])
    flow = BuyFlowViewService(catalog, FakeLocations([_loc()]), KEY)
    os_service = OsSelectionService(catalog, FakeRegistry(), KEY)
    confirm_service = PurchaseConfirmationService(
        catalog,
        FakeBooks(),
        FakeWallets(balance=balance),
        KEY,
        book_name="retail-eur",
    )
    ui = BotUi(
        KEY,
        flow,
        os_selection=os_service,
        confirmation=confirm_service,
        catalog=catalog,
        create=create or FakeCreate(),
    )
    return ui, catalog


def _first_button(screen) -> str:  # type: ignore[no-untyped-def]
    return screen.keyboard.inline_keyboard[0][0].callback_data or ""


@pytest.mark.asyncio()
class TestOsScreen:
    async def test_renders_compatible_images_only(self) -> None:
        ui, catalog = _ui()
        offer = catalog.offers[0]
        screen = await ui.os_screen("hetzner", "fsn1", offer.id)
        buttons = [b for row in screen.keyboard.inline_keyboard for b in row]
        texts = [b.text for b in buttons]
        assert any("Ubuntu 24.04" in t for t in texts)
        assert any("Debian 12" in t for t in texts)
        assert not any("ARM" in t for t in texts)  # incompatible architecture
        os_ids = [
            decode_callback(b.callback_data or "", KEY).args[3]
            for b in buttons
            if "buy:os" in (b.callback_data or "")
        ]
        assert set(os_ids) == {"ubuntu-24.04", "debian-12"}

    async def test_unknown_provider_shows_unavailable(self) -> None:
        ui, catalog = _ui()
        screen = await ui.os_screen("nope", "fsn1", catalog.offers[0].id)
        assert "در دسترس نیست" in screen.text


@pytest.mark.asyncio()
class TestConfirmScreen:
    async def test_renders_price_wallet_and_actions(self) -> None:
        ui, catalog = _ui(balance=500)
        offer = catalog.offers[0]
        screen = await ui.confirm_screen(_user(), "hetzner", "fsn1", offer.id, "ubuntu-24.04")
        assert "CX22" in screen.text
        assert "1.07 EUR" in screen.text  # selling_minor=107, hourly quantum
        assert "5.00 EUR" in screen.text  # wallet balance 500 minor
        texts = [b.text for row in screen.keyboard.inline_keyboard for b in row]
        assert any("تأیید و پرداخت" in t for t in texts)
        assert any("بازگشت" in t for t in texts)

    async def test_insufficient_wallet_is_flagged(self) -> None:
        ui, catalog = _ui(balance=50)  # hold 107 > 50
        offer = catalog.offers[0]
        screen = await ui.confirm_screen(_user(), "hetzner", "fsn1", offer.id, "debian-12")
        assert "کافی نیست" in screen.text


@pytest.mark.asyncio()
class TestFullClickPath:
    async def _walk_to_confirm(self, ui: BotUi, catalog: FakeCatalog) -> str:
        """Click through menu -> locations -> plans -> os, return the confirm data."""
        del catalog  # the walk only needs the keyboard buttons
        menu = ui.menu_screen()
        buy_data = menu.keyboard.inline_keyboard[0][0].callback_data or ""
        locations = await ui.handle(buy_data, user=_user(), chat_id=1)
        location_data = _first_button(locations)
        plans = await ui.handle(location_data, user=_user(), chat_id=1)
        plan_data = _first_button(plans)
        os_screen = await ui.handle(plan_data, user=_user(), chat_id=1)
        image_data = _first_button(os_screen)
        confirm = await ui.handle(image_data, user=_user(), chat_id=1)
        assert "CX22" in confirm.text
        return _first_button(confirm)

    async def test_full_path_places_order_once(self) -> None:
        create = FakeCreate()
        ui, catalog = _ui(create=create)
        user = _user()
        confirm_data = await self._walk_to_confirm(ui, catalog)

        done = await ui.handle(confirm_data, user=user)
        assert "سفارش سرور ثبت شد" in done.text
        assert len(create.calls) == 1
        placed_user, ref, key = create.calls[0]
        assert placed_user is user
        assert ref == OfferRef(provider_key="hetzner", plan_id="cx22", location_id="fsn1")
        assert key.startswith("bot-create:buy:confirm:")

        # pressing the same button again replays, never double-charges
        done2 = await ui.handle(confirm_data, user=user)
        assert "قبلاً ثبت شده بود" in done2.text
        assert len(create.calls) == 1

    async def test_order_error_maps_to_friendly_message(self) -> None:
        create = FakeCreate(fail=QuotaExceededError("quota full"))
        ui, catalog = _ui(create=create)
        confirm_data = await self._walk_to_confirm(ui, catalog)
        screen = await ui.handle(confirm_data, user=_user())
        assert "سقف تعداد سرورهای فعال" in screen.text

    async def test_confirm_without_identity_shows_notice(self) -> None:
        create = FakeCreate()
        ui, catalog = _ui(create=create)
        confirm_data = await self._walk_to_confirm(ui, catalog)
        screen = await ui.handle(confirm_data, user=None)
        assert "شناسه" in screen.text
        assert len(create.calls) == 0

    async def test_back_navigation_returns_to_plans(self) -> None:
        ui, _catalog = _ui()
        # arrive at the OS screen through the dispatcher so the session state
        # is tracked, then press back
        plans = await ui.plans_screen("hetzner", "fsn1")
        plan_data = _first_button(plans)
        os_screen = await ui.handle(plan_data, user=_user(), chat_id=1)
        assert "انتخاب سیستمعامل" in os_screen.text
        back = next(
            b
            for row in os_screen.keyboard.inline_keyboard
            for b in row
            if "buy:plans" in (b.callback_data or "")
        )
        back_plans = await ui.handle(back.callback_data or "", user=_user(), chat_id=1)
        assert "Falkenstein" in back_plans.text
