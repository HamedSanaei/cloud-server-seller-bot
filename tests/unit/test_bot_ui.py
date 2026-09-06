"""Tests for the bot UI layer (M08-001/M08-002/M08-003).

Acceptance: the bot's screens render the synced catalog and every button is
a signed callback that the dispatcher resolves to the correct screen —
without a live Telegram connection or database (framework-light UI + fake
repos).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_platform.bot.ui import BotUi
from cloud_platform.modules.catalog.domain import CatalogOffer, LocationRecord
from cloud_platform.modules.catalog.service import BuyFlowViewService
from cloud_platform.modules.navigation.domain import Callback, decode_callback

KEY = "test-signing-key"


def _offer(
    *,
    plan_id: str = "cx22",
    location_id: str = "fsn1",
    name: str = "CX22",
    price: int = 219,
) -> CatalogOffer:
    return CatalogOffer(
        id=uuid4(),
        provider_key="hetzner",
        plan_id=plan_id,
        location_id=location_id,
        name=name,
        architecture="x86",
        vcpu=2,
        memory_mb=4096,
        disk_gb=40,
        currency="EUR",
        price_per_quantum=price,
        quantum_seconds=3600,
        enabled=True,
    )


def _loc(
    location_id: str = "fsn1",
    name: str = "Falkenstein",
    country_code: str | None = "DE",
    city: str | None = "Falkenstein",
) -> LocationRecord:
    return LocationRecord(
        provider_key="hetzner",
        location_id=location_id,
        name=name,
        country_code=country_code,
        city=city,
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


class _Unused:
    """Any method access raises: the test never reaches these services."""

    def __getattr__(self, name: str):
        async def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"unexpected service call: {name}")

        return _boom


def _ui(offers: list[CatalogOffer], records: list[LocationRecord]) -> BotUi:
    flow = BuyFlowViewService(FakeCatalog(offers), FakeLocations(records), KEY)
    return BotUi(
        KEY,
        flow,
        os_selection=_Unused(),
        confirmation=_Unused(),
        catalog=FakeCatalog(offers),
        create=_Unused(),
    )


def _buttons(markup: InlineKeyboardMarkup) -> list[InlineKeyboardButton]:
    return [button for row in markup.inline_keyboard for button in row]


def test_ui_rejects_empty_signing_key() -> None:
    flow = BuyFlowViewService(FakeCatalog([]), FakeLocations([]), KEY)
    with pytest.raises(ValueError):
        BotUi(
            "",
            flow,
            os_selection=_Unused(),
            confirmation=_Unused(),
            catalog=FakeCatalog([]),
            create=_Unused(),
        )


class TestMainMenu:
    def test_menu_has_four_flow_entries(self) -> None:
        screen = _ui([], []).menu_screen()
        buttons = _buttons(screen.keyboard)
        assert len(buttons) == 4
        decoded = [decode_callback(b.callback_data or "", KEY) for b in buttons]
        assert [(c.flow, c.screen) for c in decoded] == [
            ("buy", "locations"),
            ("servers", "list"),
            ("wallet", "balance"),
            ("recharge", "amount"),
        ]


@pytest.mark.asyncio()
class TestLocationsScreen:
    async def test_groups_locations_with_select_callbacks(self) -> None:
        offers = [
            _offer(plan_id="cx22", location_id="fsn1", name="CX22"),
            _offer(plan_id="cpx11", location_id="fsn1", name="CPX11"),
            _offer(plan_id="cx22", location_id="nbg1", name="CX22"),
        ]
        records = [
            _loc("fsn1", "Falkenstein", "DE", "Falkenstein"),
            _loc("nbg1", "Nuremberg", "DE", "Nuremberg"),
        ]
        ui = _ui(offers, records)

        screen = await ui.locations_screen()
        assert "انتخاب دیتاسنتر" in screen.text
        buttons = _buttons(screen.keyboard)
        # two locations + the cancel button
        assert len(buttons) == 3
        locations = [b for b in buttons if "buy:locations" in (b.callback_data or "")]
        assert all("Falkenstein" in b.text or "Nuremberg" in b.text for b in locations)

        decoded = [decode_callback(b.callback_data or "", KEY) for b in locations]
        assert {(c.args[0], c.args[1]) for c in decoded} == {
            ("hetzner", "fsn1"),
            ("hetzner", "nbg1"),
        }

    async def test_empty_catalog_shows_empty_notice(self) -> None:
        screen = await _ui([], []).locations_screen()
        assert "فعلاً سروری برای فروش موجود نیست" in screen.text


@pytest.mark.asyncio()
class TestPlansScreen:
    async def test_renders_plans_with_signed_callbacks_and_nav(self) -> None:
        offers = [_offer(plan_id="cx22", name="CX22", price=219)]
        ui = _ui(offers, [_loc()])

        screen = await ui.plans_screen("hetzner", "fsn1")
        assert "Falkenstein" in screen.text
        buttons = _buttons(screen.keyboard)
        assert any("CX22" in b.text and "2.19 EUR" in b.text for b in buttons)
        plan = next(b for b in buttons if "buy:plans" in (b.callback_data or ""))
        cb = decode_callback(plan.callback_data or "", KEY)
        assert cb.flow == "buy"
        assert cb.screen == "plans"
        assert cb.args[:2] == ("hetzner", "fsn1")
        assert cb.args[2] == str(offers[0].id)
        # back + cancel navigation present
        texts = [b.text for b in buttons]
        assert any("بازگشت" in t for t in texts)
        assert any("انصراف" in t for t in texts)

    async def test_missing_location_shows_empty_notice(self) -> None:
        ui = _ui([_offer()], [])
        screen = await ui.plans_screen("hetzner", "nope")
        assert "فعلاً سروری برای فروش موجود نیست" in screen.text


@pytest.mark.asyncio()
class TestDispatch:
    async def test_menu_buy_click_opens_locations(self) -> None:
        ui = _ui([_offer()], [_loc()])
        menu = ui.menu_screen()
        buy_button = _buttons(menu.keyboard)[0]
        screen = await ui.handle(buy_button.callback_data or "")
        assert "انتخاب دیتاسنتر" in screen.text

    async def test_location_select_opens_plans(self) -> None:
        ui = _ui([_offer()], [_loc()])
        locations = await ui.locations_screen()
        location_button = _buttons(locations.keyboard)[0]
        screen = await ui.handle(location_button.callback_data or "")
        assert "پلن" in screen.text

    async def test_plan_select_opens_os_selection(self) -> None:
        # the OS screen details live in test_bot_buy_flow; here we pin that
        # the dispatcher routes a plan callback into the OS service and that
        # an unknown provider degrades to a friendly notice
        from cloud_platform.modules.catalog.service import OsSelectionService

        offers = [_offer()]
        catalog = FakeCatalog(offers)
        flow = BuyFlowViewService(catalog, FakeLocations([_loc()]), KEY)

        class _NoProviders:
            def get(self, provider_key: str) -> object:
                raise KeyError(provider_key)

        ui = BotUi(
            KEY,
            flow,
            os_selection=OsSelectionService(catalog, _NoProviders(), KEY),
            confirmation=_Unused(),
            catalog=catalog,
            create=_Unused(),
        )
        plans = await ui.plans_screen("hetzner", "fsn1")
        plan_button = next(
            b for b in _buttons(plans.keyboard) if "buy:plans" in (b.callback_data or "")
        )
        screen = await ui.handle(plan_button.callback_data or "")
        assert "در دسترس نیست" in screen.text

    async def test_tampered_callback_shows_expired_notice(self) -> None:
        ui = _ui([], [])
        menu = ui.menu_screen()
        tampered = _buttons(menu.keyboard)[0].callback_data or ""
        tampered = "v1|main:menu|deadbeefdeadbeef"
        screen = await ui.handle(tampered)
        assert "معتبر نیست" in screen.text

    async def test_unbuilt_flow_shows_coming_soon(self) -> None:
        from cloud_platform.modules.navigation.domain import encode_callback

        ui = _ui([], [])
        data = encode_callback(Callback(flow="servers", screen="list"), KEY)
        screen = await ui.handle(data)
        assert "در حال آمادهسازی" in screen.text
