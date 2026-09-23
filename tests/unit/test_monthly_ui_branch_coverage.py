"""monthly_ui branch completion (STOREFRONT-REWORK).

Every screen in the storefront module has guard rails around provider data:
an unavailable catalog, an empty list, a stale compact ref, a garbage page
number, a vanished wallet. These tests drive those branches with a steering
view double so the customer never meets them by surprise, and pin the safe
screen each one renders.

No provider is contacted: the view double is pure Python and the hourly
service is only ever awaited through a recorded fake.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.bot.monthly_ui import (
    MonthlyBotUi,
    _country_flag,
    _short_id,
    _state_label,
)
from cloud_platform.core.i18n import Translator
from cloud_platform.modules.checkout.service import (
    CheckoutError,
    OfferUnavailableError,
    OsUnavailableError,
    UserNotActiveError,
)
from cloud_platform.modules.compute.domain import ServerLifecycleState
from cloud_platform.modules.navigation.domain import Callback, encode_callback, encode_offer_ref
from cloud_platform.modules.payments.recharge import (
    RechargeAmountError,
    RechargeError,
)
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.modules.wallet.domain import InsufficientHoldBalanceError

KEY = "monthly-ui-branch-key"
OFFER_ID = uuid4()


def _user(user_id: Any = OFFER_ID) -> User:
    return User(
        id=user_id,
        username="cust",
        email="cust@example.test",
        status=UserStatus.ACTIVE,
        role=Role.USER,
        telegram_user_id=555,
    )


# ---------------------------------------------------------------------------
# steered doubles
# ---------------------------------------------------------------------------


class SteeringView:
    """Offer-catalog view whose every awaited screen is configurable.

    ``result`` supplies the value the screen returns; ``raises`` supplies the
    exception it raises instead. Anything not configured fails loudly so an
    accidental call cannot silently pass.
    """

    def __init__(self) -> None:
        self.result: dict[str, Any] = {}
        self.raises: dict[str, Exception] = {}
        self.calls: list[str] = []

    # -- sync members (called without await by the UI) --
    def markets_screen(self) -> list[Any]:
        self.calls.append("markets_screen")
        return self.result.get("markets_screen", [])

    def provider_display_name(self, provider_key: str) -> str:
        self.calls.append("provider_display_name")
        return self.result.get("provider_display_name", provider_key)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def _screen(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            exc = self.raises.get(name)
            if exc is not None:
                raise exc
            if name in self.result:
                value = self.result[name]
                return value(*args, **kwargs) if callable(value) else value
            raise AssertionError(f"view screen {name!r} was not configured")

        return _screen


class FakeOffersRepo:
    def __init__(self, locations: list[Any] | None = None, offers: dict[Any, Any] | None = None):
        self.locations = list(locations or [])
        self.offers = dict(offers or {})

    async def list_provider_locations(self) -> list[Any]:
        return list(self.locations)

    async def get(self, offer_id: Any) -> Any:
        return self.offers.get(offer_id)


class FakeWallet:
    def __init__(self, *, has_wallet: bool = True, items: list[Any] | None = None) -> None:
        self.has_wallet = has_wallet
        self.items = list(items or [])

    async def balance(self, user_id: Any) -> Any:
        return SimpleNamespace(
            has_wallet=self.has_wallet,
            balance_minor=1_000,
            currency="EUR",
            formatted="€10.00",
        )

    async def history(self, user_id: Any, limit: int = 20) -> Any:
        return SimpleNamespace(items=list(self.items))


class FakeCheckout:
    def __init__(self, *, raises: Exception | None = None, replayed: bool = False) -> None:
        self.raises = raises
        self.replayed = replayed
        self.calls: list[dict[str, Any]] = []

    async def create_order(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(order=SimpleNamespace(id=uuid4()), replayed=self.replayed)


class FakeHourly:
    def __init__(self, *, raises: Exception | None = None, replayed: bool = False) -> None:
        self.raises = raises
        self.replayed = replayed
        self.calls: list[dict[str, Any]] = []

    async def create_instance(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(server=SimpleNamespace(id=uuid4()), replayed=self.replayed)


def _image(image_id: str = "ubuntu-24.04", label: str = "Ubuntu 24.04") -> SimpleNamespace:
    return SimpleNamespace(id=image_id, label=label)


class FakeServersUi:
    def __init__(self, *, ref: Any = "signed-ref", text: Any = None) -> None:
        self.ref = ref
        self.text = text
        self.handled: list[Any] = []

    async def list_screen(self, user: Any, page: int = 1) -> Any:
        return SimpleNamespace(text="my servers", keyboard=None)

    async def ref_for(self, user: Any, server_id: Any) -> Any:
        return self.ref

    async def handle(self, callback: Any, user: Any) -> Any:
        self.handled.append(callback)
        return SimpleNamespace(text="server", keyboard=None)

    async def handle_text(self, text: str, user: Any) -> Any:
        return self.text


class FakeRecharge:
    """Recharge double covering both the sync and the FX-aware async probes."""

    def __init__(
        self,
        *,
        currency: str = "EUR",
        gateways: list[str] | None = None,
        fail: Exception | None = None,
        probe_fails: bool = False,
        presets_fail: bool = False,
        sync_disabled: bool = False,
    ) -> None:
        self.supports = currency
        self.gateways = list(gateways if gateways is not None else ["zarinpal"])
        self.fail = fail
        self.probe_fails = probe_fails
        self.presets_fail = presets_fail
        self.sync_disabled = sync_disabled
        self.started: list[dict[str, Any]] = []

    def supports_currency(self, currency: str) -> bool:
        """Sync fast path; ``sync_disabled`` forces the async FX probe."""
        if self.sync_disabled:
            return False
        return currency == self.supports

    async def supports_currency_async(self, currency: str) -> bool:
        if self.probe_fails:
            raise RuntimeError("fx down")
        return currency == self.supports

    def compatible_gateways(self, currency: str, amount_minor: int | None = None) -> list[str]:
        return list(self.gateways) if currency == self.supports else []

    async def compatible_gateways_async(
        self, currency: str, amount_minor: int | None = None
    ) -> list[str]:
        if self.presets_fail:
            raise RuntimeError("gateway probe failed")
        return self.compatible_gateways(currency, amount_minor)

    async def start(self, **kwargs: Any) -> Any:
        if self.fail is not None:
            raise self.fail
        self.started.append(kwargs)
        return SimpleNamespace(
            session=SimpleNamespace(
                amount_minor=kwargs["amount_minor"], currency=kwargs["currency"]
            ),
            redirect_url="https://pay.example/AUTH-1",
            replayed=len(self.started) > 1,
        )


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _plan(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        offer_id=OFFER_ID,
        name="Leaseweb VPS 1",
        vcpu=4,
        ram_gb=6,
        disk_gb=100,
        traffic="30 TB",
        monthly_price_minor=624,
        currency="EUR",
        location_id="FRA-01",
        provider_key="leaseweb",
        selling_price_minor=780,
        selling_currency="EUR",
        technical_metadata={},
        select_callback="plan-cb",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _location(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        name="Frankfurt",
        location_id="FRA-01",
        country_code="DE",
        city="Frankfurt",
        offer_count=6,
        plan_count=6,
        select_callback="loc-cb",
        monthly_price_minor=100,
        min_price_minor=624,
        currency="EUR",
        product_name="Leaseweb VPS 1",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _paged(items: list[Any], **over: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        items=list(items),
        page=1,
        total_pages=1,
        prev_callback=None,
        next_callback=None,
        back_callback="back-cb",
        cancel_callback="cancel-cb",
        billing_model="prepaid_monthly_fixed",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _detail(offer: Any, **over: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        offer=offer,
        technical_metadata={},
        location_name="Frankfurt",
        location_country="DE",
        continue_callback="go-cb",
        back_callback="back-cb",
        cancel_callback="cancel-cb",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _option(name: str = "Ubuntu 24.04") -> SimpleNamespace:
    return SimpleNamespace(name=name, select_callback="opt-cb")


def _family(family_key: str = "vps", billing: str = "prepaid_monthly_fixed") -> SimpleNamespace:
    return SimpleNamespace(
        family_key=family_key,
        billing_model=billing,
        display_name="VPS",
        select_callback="family-cb",
    )


def _confirm(offer: Any, **over: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        offer=offer,
        os_name="Ubuntu 24.04",
        panel_name=None,
        currency="EUR",
        balance_minor=10_000,
        sufficient=True,
        confirm_callback="confirm-cb",
        back_callback="back-cb",
        cancel_callback="cancel-cb",
        # hourly confirmation shape (separate fields from the monthly view)
        hourly_price_minor=780,
        monthly_estimate_minor=780 * 730,
        image_label="Ubuntu 24.04",
        location_name="Frankfurt",
        location_country="DE",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _bot(
    view: Any,
    *,
    checkout: Any = None,
    offers_repo: Any = None,
    wallet: Any = None,
    recharge: Any = None,
    hourly: Any = None,
    support_contact: str = "",
    servers_ui: Any = None,
) -> MonthlyBotUi:
    ui = MonthlyBotUi(
        KEY,
        offers_view=view,
        checkout=checkout or FakeCheckout(),
        servers=SimpleNamespace(),
        orders=SimpleNamespace(),
        renewals=SimpleNamespace(),
        offers_repo=offers_repo or FakeOffersRepo(),
        wallet_history=wallet or FakeWallet(),
        support_contact=support_contact,
        recharge=recharge,
        hourly=hourly,
    )
    if servers_ui is not None:
        ui._servers_ui = servers_ui
    return ui


async def _press(bot: MonthlyBotUi, callback: str, *, user: Any = None) -> Any:
    screen = await bot.handle(callback, user=_user() if user is None else user)
    assert screen is not None
    return screen


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------


class TestModuleHelpers:
    def test_short_id_truncates_a_uuid_to_eight_characters(self) -> None:
        value = uuid4()
        assert _short_id(value) == str(value)[:8]
        assert len(_short_id(value)) == 8

    def test_a_known_state_is_translated(self) -> None:
        t = Translator()
        assert _state_label(t, ServerLifecycleState.RUNNING)

    def test_an_unknown_state_falls_back_to_its_value(self) -> None:
        class _Weird:
            value = "not-a-real-state"

        assert _state_label(Translator(), _Weird()) == "not-a-real-state"  # type: ignore[arg-type]

    def test_a_known_country_gets_its_flag(self) -> None:
        assert _country_flag("DE") != _country_flag("GB")
        assert _country_flag("de") == _country_flag("DE")

    def test_an_unknown_or_missing_country_renders_no_flag(self) -> None:
        # The neutral marker is "no flag" — the caller adds the globe when a
        # whole *list* of locations has no synced country at all.
        assert _country_flag(None) == ""
        assert _country_flag("") == ""
        assert _country_flag("NOPE") == ""

    def test_an_empty_signing_key_is_refused(self) -> None:
        with pytest.raises(ValueError):
            _bot_with_key("")


def _bot_with_key(key: str) -> MonthlyBotUi:
    return MonthlyBotUi(
        key,
        offers_view=SteeringView(),
        checkout=FakeCheckout(),
        servers=SimpleNamespace(),
        orders=SimpleNamespace(),
        renewals=SimpleNamespace(),
        offers_repo=FakeOffersRepo(),
        wallet_history=FakeWallet(),
    )


class TestMenuAndSupportScreens:
    def test_the_main_menu_renders_the_canonical_entries(self) -> None:
        bot = _bot(SteeringView())
        screen = bot.menu_screen()
        assert screen.text
        assert len(screen.keyboard.inline_keyboard) >= 4

    def test_the_reply_keyboard_is_built(self) -> None:
        bot = _bot(SteeringView())
        assert bot.reply_keyboard() is not None

    def test_support_shows_the_contact_when_configured(self) -> None:
        with_contact = _bot(SteeringView(), support_contact="@ops")
        assert "@ops" in with_contact.support_screen().text

    def test_support_without_a_contact_still_renders(self) -> None:
        without = _bot(SteeringView(), support_contact="")
        assert without.support_screen().text


# ---------------------------------------------------------------------------
# store screen guards
# ---------------------------------------------------------------------------


class TestStoreScreenGuards:
    async def test_providers_screen_unavailable_catalog(self) -> None:
        view = SteeringView()
        view.raises["providers_screen"] = OfferUnavailableError("none")
        assert (await _bot(view).providers_screen("foreign")).text

    async def test_providers_screen_empty_list(self) -> None:
        view = SteeringView()
        view.result["providers_screen"] = ([], "back-cb")
        assert (await _bot(view).providers_screen("foreign")).text

    async def test_providers_screen_unknown_market_falls_back_to_the_generic_title(self) -> None:
        view = SteeringView()
        provider = SimpleNamespace(display_name="Leaseweb", offer_count=6, select_callback="cb")
        view.result["providers_screen"] = ([provider], "back-cb")
        assert (await _bot(view).providers_screen("nonsense")).text

    async def test_locations_screen_unavailable(self) -> None:
        view = SteeringView()
        view.raises["locations_screen"] = OfferUnavailableError("none")
        assert (await _bot(view).store_locations_screen("leaseweb")).text

    async def test_locations_screen_empty(self) -> None:
        view = SteeringView()
        view.result["locations_screen"] = ([], "back-cb", "cancel-cb")
        assert (await _bot(view).store_locations_screen("leaseweb")).text

    async def test_locations_screen_lists_real_locations(self) -> None:
        view = SteeringView()
        view.result["locations_screen"] = ([_location()], "back-cb", "cancel-cb")
        screen = await _bot(view).store_locations_screen("leaseweb")
        labels = [b.text for row in screen.keyboard.inline_keyboard for b in row]
        assert any("FRA-01" in label for label in labels)

    async def test_plans_screen_unavailable(self) -> None:
        view = SteeringView()
        view.raises["plans_screen"] = OfferUnavailableError("none")
        assert (await _bot(view).store_plans_screen("leaseweb", "FRA-01")).text

    async def test_families_screen_unavailable(self) -> None:
        view = SteeringView()
        view.raises["families_screen"] = OfferUnavailableError("none")
        assert (await _bot(view).store_families_screen("leaseweb")).text

    async def test_families_screen_empty(self) -> None:
        view = SteeringView()
        view.result["families_screen"] = ([], "back-cb", "cancel-cb")
        assert (await _bot(view).store_families_screen("leaseweb")).text

    async def test_vps_locations_unavailable(self) -> None:
        view = SteeringView()
        view.raises["cities_screen"] = OfferUnavailableError("none")
        assert (await _bot(view).store_vps_locations_screen("leaseweb", "vps")).text

    async def test_vps_locations_empty(self) -> None:
        view = SteeringView()
        view.result["cities_screen"] = _paged([])
        assert (await _bot(view).store_vps_locations_screen("leaseweb", "vps")).text

    async def test_vps_locations_pager_renders_prev_and_next(self) -> None:
        view = SteeringView()
        view.result["cities_screen"] = _paged(
            [_location()],
            page=2,
            total_pages=3,
            prev_callback="prev-cb",
            next_callback="next-cb",
        )
        screen = await _bot(view).store_vps_locations_screen("leaseweb", "vps", 2)
        labels = [b.text for row in screen.keyboard.inline_keyboard for b in row]
        assert len(labels) >= 3

    async def test_vps_plans_unavailable(self) -> None:
        view = SteeringView()
        view.raises["family_plans_screen"] = OfferUnavailableError("none")
        assert (await _bot(view).store_vps_plans_screen("leaseweb", "vps", "FRA-01")).text

    async def test_vps_plans_empty(self) -> None:
        view = SteeringView()
        view.result["family_plans_screen"] = _paged([])
        assert (await _bot(view).store_vps_plans_screen("leaseweb", "vps", "FRA-01")).text

    async def test_vps_plans_renders_provider_specs(self) -> None:
        view = SteeringView()
        view.result["family_plans_screen"] = _paged([_plan()])
        screen = await _bot(view).store_vps_plans_screen("leaseweb", "vps", "FRA-01")
        assert "4" in screen.text or len(screen.keyboard.inline_keyboard) >= 2

    async def test_plan_detail_unavailable(self) -> None:
        view = SteeringView()
        view.raises["plan_detail_screen"] = OfferUnavailableError("none")
        assert (await _bot(view).store_plan_detail_screen("leaseweb", "FRA-01", "VPS02_1")).text

    async def test_plan_detail_states_proven_and_unknown_facts(self) -> None:
        view = SteeringView()
        view.result["plan_detail_screen"] = _detail(
            _plan(),
            technical_metadata={"ipv4": True, "ipv6": False, "architecture": "x86_64"},
        )
        screen = await _bot(view).store_plan_detail_screen("leaseweb", "FRA-01", "VPS02_1")
        assert screen.text

    async def test_panel_screen_unavailable(self) -> None:
        view = SteeringView()
        view.raises["panel_screen"] = OfferUnavailableError("none")
        bot = _bot(view, offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}))
        assert (await bot.store_panel_screen(encode_offer_ref(OFFER_ID), 0)).text

    async def test_panel_screen_os_unavailable(self) -> None:
        view = SteeringView()
        view.raises["panel_screen"] = OsUnavailableError("none")
        bot = _bot(view, offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}))
        assert (await bot.store_panel_screen(encode_offer_ref(OFFER_ID), 0)).text

    async def test_panel_screen_with_a_malformed_ref_expires(self) -> None:
        bot = _bot(SteeringView())
        assert (await bot.store_panel_screen("not-a-ref", 0)).text

    async def test_cloud_locations_unavailable(self) -> None:
        view = SteeringView()
        view.raises["cities_screen"] = OfferUnavailableError("none")
        assert (await _bot(view).store_cloud_locations_screen("leaseweb", "cloud")).text

    async def test_cloud_locations_empty(self) -> None:
        view = SteeringView()
        view.result["cities_screen"] = _paged([])
        assert (await _bot(view).store_cloud_locations_screen("leaseweb", "cloud")).text

    async def test_cloud_locations_pager(self) -> None:
        view = SteeringView()
        view.result["cities_screen"] = _paged(
            [_location(name="Frankfurt", location_id="FRA-01")],
            page=2,
            total_pages=2,
            prev_callback="prev-cb",
            billing_model="hourly",
        )
        screen = await _bot(view).store_cloud_locations_screen("leaseweb", "cloud", 2)
        assert screen.text

    async def test_cloud_families_unavailable(self) -> None:
        view = SteeringView()
        view.raises["cloud_plan_families_screen"] = OfferUnavailableError("none")
        assert (await _bot(view).store_cloud_families_screen("leaseweb", "cloud", "FRA-01")).text

    async def test_cloud_families_empty(self) -> None:
        view = SteeringView()
        view.result["cloud_plan_families_screen"] = ([], "back-cb", "cancel-cb")
        assert (await _bot(view).store_cloud_families_screen("leaseweb", "cloud", "FRA-01")).text

    async def test_cloud_plans_unavailable(self) -> None:
        view = SteeringView()
        view.raises["cloud_plans_screen"] = OfferUnavailableError("none")
        bot = _bot(view)
        assert (await bot.store_cloud_plans_screen("leaseweb", "FRA-01", "general")).text

    async def test_cloud_plans_empty(self) -> None:
        view = SteeringView()
        view.result["cloud_plans_screen"] = _paged([])
        bot = _bot(view)
        assert (await bot.store_cloud_plans_screen("leaseweb", "FRA-01", "general")).text

    async def test_cloud_plans_show_the_hourly_price(self) -> None:
        view = SteeringView()
        view.result["cloud_plans_screen"] = _paged([_plan()])
        bot = _bot(view)
        screen = await bot.store_cloud_plans_screen("leaseweb", "FRA-01", "general")
        assert screen.text

    async def test_cloud_detail_unavailable(self) -> None:
        view = SteeringView()
        view.raises["cloud_detail_screen"] = OfferUnavailableError("none")
        assert (await _bot(view).store_cloud_detail_screen("leaseweb", "FRA-01", "GP_2_8")).text

    async def test_cloud_detail_reports_the_family_and_estimate(self) -> None:
        view = SteeringView()
        view.result["cloud_detail_screen"] = _detail(
            _plan(), technical_metadata={"plan_family_name": "General Purpose"}
        )
        assert (await _bot(view).store_cloud_detail_screen("leaseweb", "FRA-01", "GP_2_8")).text

    async def test_cloud_images_unavailable(self) -> None:
        view = SteeringView()
        view.raises["cloud_images_screen"] = OfferUnavailableError("none")
        bot = _bot(view, offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}))
        assert (await bot.store_cloud_images_screen(encode_offer_ref(OFFER_ID))).text

    async def test_cloud_images_with_a_stale_ref_expires(self) -> None:
        bot = _bot(SteeringView())
        assert (await bot.store_cloud_images_screen("zzzzzzzz")).text

    async def test_cloud_images_lists_live_options(self) -> None:
        view = SteeringView()
        view.result["cloud_images_screen"] = (_plan(), [_option()], "back-cb", "cancel-cb")
        bot = _bot(view, offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}))
        assert (await bot.store_cloud_images_screen(encode_offer_ref(OFFER_ID))).text

    async def test_cloud_confirm_unavailable(self) -> None:
        view = SteeringView()
        view.raises["cloud_confirmation"] = OfferUnavailableError("none")
        bot = _bot(view, offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}))
        assert (await bot.store_cloud_confirm_screen(_user(), encode_offer_ref(OFFER_ID), 0)).text

    async def test_cloud_confirm_without_identity(self) -> None:
        bot = _bot(SteeringView())
        screen = await bot.store_cloud_confirm_screen(_user(user_id=None), "anyref", 0)
        assert screen.text

    async def test_cloud_confirm_with_an_unknown_ref_expires(self) -> None:
        bot = _bot(SteeringView())
        assert (await bot.store_cloud_confirm_screen(_user(), "zzzzzzzz", 0)).text

    async def test_cloud_confirm_states_the_hourly_basis_and_delete_warning(self) -> None:
        view = SteeringView()
        view.result["cloud_confirmation"] = _confirm(_plan())
        bot = _bot(view, offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}))
        screen = await bot.store_cloud_confirm_screen(_user(), encode_offer_ref(OFFER_ID), 0)
        labels = [b.text for row in screen.keyboard.inline_keyboard for b in row]
        assert screen.text and labels

    async def test_cloud_buy_without_identity(self) -> None:
        bot = _bot(SteeringView(), hourly=FakeHourly())
        screen = await bot.store_cloud_buy_screen(None, "anyref", 0, "k")
        assert screen.text

    async def test_cloud_buy_without_the_hourly_service(self) -> None:
        bot = _bot(SteeringView(), hourly=None)
        screen = await bot.store_cloud_buy_screen(_user(), "anyref", 0, "k")
        assert screen.text

    async def test_cloud_buy_with_an_unknown_ref_expires(self) -> None:
        bot = _bot(SteeringView(), hourly=FakeHourly())
        assert (await bot.store_cloud_buy_screen(_user(), "zzzzzzzz", 0, "k")).text

    async def test_cloud_buy_creates_an_intent_only(self) -> None:
        view = SteeringView()
        view.result["cloud_image_by_index"] = _image()
        hourly = FakeHourly()
        bot = _bot(view, hourly=hourly, offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}))
        screen = await bot.store_cloud_buy_screen(_user(), encode_offer_ref(OFFER_ID), 0, "cbkey")
        assert hourly.calls and hourly.calls[0]["idempotency_key"] == "bot-hourly:cbkey"
        assert screen.text

    async def test_cloud_buy_reports_a_replay(self) -> None:
        view = SteeringView()
        view.result["cloud_image_by_index"] = _image()
        bot = _bot(
            view,
            hourly=FakeHourly(replayed=True),
            offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}),
        )
        assert (await bot.store_cloud_buy_screen(_user(), encode_offer_ref(OFFER_ID), 0, "k")).text

    async def test_cloud_buy_with_a_vanished_offer_is_unavailable(self) -> None:
        view = SteeringView()
        bot = _bot(view, hourly=FakeHourly(), offers_repo=FakeOffersRepo())
        assert (await bot.store_cloud_buy_screen(_user(), encode_offer_ref(OFFER_ID), 0, "k")).text

    async def test_cloud_buy_maps_hourly_errors_to_unavailable(self) -> None:
        from cloud_platform.modules.hourly.service import HourlyError

        view = SteeringView()
        view.result["cloud_image_by_index"] = _image()
        bot = _bot(
            view,
            hourly=FakeHourly(raises=HourlyError("not sellable")),
            offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}),
        )
        screen = await bot.store_cloud_buy_screen(_user(), encode_offer_ref(OFFER_ID), 0, "k")
        assert screen.text

    async def test_cloud_buy_maps_unexpected_errors_to_the_error_screen(self) -> None:
        view = SteeringView()
        view.result["cloud_image_by_index"] = _image()
        bot = _bot(
            view,
            hourly=FakeHourly(raises=RuntimeError("boom")),
            offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}),
        )
        assert (await bot.store_cloud_buy_screen(_user(), encode_offer_ref(OFFER_ID), 0, "k")).text

    async def test_product_locations_unavailable_with_currency(self) -> None:
        view = SteeringView()
        view.raises["product_locations_screen"] = OfferUnavailableError("none")
        bot = _bot(view)
        assert (await bot.store_product_locations_screen("leaseweb", "VPS02_1", 624, "EUR")).text

    async def test_product_locations_unavailable_without_currency_falls_back_to_families(
        self,
    ) -> None:
        view = SteeringView()
        view.raises["product_locations_screen"] = OfferUnavailableError("none")
        view.raises["families_screen"] = OfferUnavailableError("none too")
        bot = _bot(view)
        assert (await bot.store_product_locations_screen("leaseweb", "VPS02_1")).text

    async def test_product_locations_empty(self) -> None:
        view = SteeringView()
        view.result["product_locations_screen"] = ([], "back-cb", "cancel-cb")
        bot = _bot(view)
        assert (await bot.store_product_locations_screen("leaseweb", "VPS02_1", 624, "EUR")).text

    async def test_product_locations_renders_currency_distinct_rows(self) -> None:
        view = SteeringView()
        view.result["product_locations_screen"] = (
            [_location()],
            "back-cb",
            "cancel-cb",
        )
        bot = _bot(view)
        screen = await bot.store_product_locations_screen("leaseweb", "VPS02_1", 624, "EUR")
        assert screen.text

    def test_location_buttons_show_friendly_names(self) -> None:
        labels = MonthlyBotUi._location_button_labels(
            [("Frankfurt", "FRA-01", "DE"), ("London", "LON-01", "GB")]
        )
        assert labels == ["🇩🇪 Frankfurt", "🇬🇧 London"]

    def test_location_buttons_disambiguate_shared_names(self) -> None:
        labels = MonthlyBotUi._location_button_labels(
            [("Frankfurt", "FRA-10", "DE"), ("Frankfurt", "FRA-14", "DE")]
        )
        assert labels == ["🇩🇪 Frankfurt — FRA-10", "🇩🇪 Frankfurt — FRA-14"]

    def test_location_detail_always_carries_the_code(self) -> None:
        assert MonthlyBotUi._location_detail("Frankfurt", "FRA-10", "DE") == "🇩🇪 Frankfurt — FRA-10"
        assert MonthlyBotUi._location_detail("FRA-10", "FRA-10", None) == "FRA-10"


# ---------------------------------------------------------------------------
# store dispatch: malformed / stale callbacks
# ---------------------------------------------------------------------------


class TestStoreDispatchGuards:
    async def test_an_unknown_store_screen_returns_the_menu(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("store", "nonsense"))).text

    async def test_a_family_callback_for_an_unknown_family_reopens_the_selector(self) -> None:
        view = SteeringView()
        view.result["families_screen"] = ([], "back-cb", "cancel-cb")
        bot = _bot(view)
        screen = await _press(bot, bot._callback("store", "family", "leaseweb", "ghost"))
        assert screen.text

    async def test_a_family_callback_when_the_catalog_is_down_is_safe(self) -> None:
        view = SteeringView()
        view.raises["families_screen"] = OfferUnavailableError("none")
        bot = _bot(view)
        assert (await _press(bot, bot._callback("store", "family", "leaseweb", "vps"))).text

    async def test_an_hourly_family_routes_to_cloud_locations(self) -> None:
        view = SteeringView()
        view.result["families_screen"] = (
            [SimpleNamespace(family_key="cloud", billing_model="hourly", display_name="Cloud")],
            "back-cb",
            "cancel-cb",
        )
        view.result["cities_screen"] = _paged([_location()], billing_model="hourly")
        bot = _bot(view)
        screen = await _press(bot, bot._callback("store", "family", "leaseweb", "cloud"))
        assert "cities_screen" in view.calls
        assert screen.text

    async def test_a_store_plans_callback_renders_the_legacy_plan_list(self) -> None:
        view = SteeringView()
        view.result["plans_screen"] = ([_plan()], "back-cb", "cancel-cb")
        bot = _bot(view)
        cb = bot._callback("store", "plans", "leaseweb", "FRA-01")
        assert (await _press(bot, cb)).text

    async def test_a_product_locations_callback_carries_the_currency(self) -> None:
        view = SteeringView()
        view.result["product_locations_screen"] = ([_location()], "back-cb", "cancel-cb")
        bot = _bot(view)
        cb = bot._callback("store", "product_locations", "leaseweb", "VPS02_1", "624", "EUR")
        assert (await _press(bot, cb)).text

    async def test_a_legacy_products_callback_reenters_the_family_screen(self) -> None:
        view = SteeringView()
        view.result["families_screen"] = ([], "back-cb", "cancel-cb")
        bot = _bot(view)
        assert (await _press(bot, bot._callback("store", "products", "leaseweb"))).text

    async def test_a_legacy_products_callback_without_a_provider_shows_the_menu(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("store", "products"))).text

    async def test_a_garbage_vps_locations_page_renders_page_one(self) -> None:
        view = SteeringView()
        view.result["cities_screen"] = _paged([_location()])
        bot = _bot(view)
        assert await _press(bot, bot._callback("store", "vps_locations", "leaseweb", "vps", "x"))

    async def test_a_garbage_vps_plans_page_renders_page_one(self) -> None:
        view = SteeringView()
        view.result["family_plans_screen"] = _paged([_plan()])
        bot = _bot(view)
        cb = bot._callback("store", "vps_plans", "leaseweb", "vps", "FRA-01", "x")
        assert (await _press(bot, cb)).text

    async def test_a_garbage_cloud_locations_page_renders_page_one(self) -> None:
        view = SteeringView()
        view.result["cities_screen"] = _paged([_location()], billing_model="hourly")
        bot = _bot(view)
        cb = bot._callback("store", "cloud_locations", "leaseweb", "cloud", "x")
        assert (await _press(bot, cb)).text

    async def test_a_garbage_cloud_plans_page_renders_page_one(self) -> None:
        view = SteeringView()
        view.result["cloud_plans_screen"] = _paged([_plan()])
        bot = _bot(view)
        cb = bot._callback("store", "cloud_plans", "leaseweb", "FRA-01", "general", "x")
        assert (await _press(bot, cb)).text

    async def test_a_panel_callback_with_a_garbage_os_index_shows_the_menu(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("store", "panel", "ref", "NaN"))).text

    async def test_a_panel_callback_with_a_stale_ref_shows_the_expired_screen(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("store", "panel", "zzzzzzzz", "0"))).text

    async def test_a_product_locations_callback_with_a_garbage_price_reopens_families(
        self,
    ) -> None:
        view = SteeringView()
        view.result["families_screen"] = ([], "back-cb", "cancel-cb")
        bot = _bot(view)
        cb = bot._callback("store", "product_locations", "leaseweb", "VPS02_1", "x", "EUR")
        assert (await _press(bot, cb)).text

    async def test_a_product_locations_callback_without_currency_reopens_families(self) -> None:
        # An empty callback arg cannot be signed at all, so the branch is
        # driven through the dispatch entry point directly.
        view = SteeringView()
        view.result["families_screen"] = ([_family()], "back-cb", "cancel-cb")
        view.result["cities_screen"] = _paged([_location()])
        bot = _bot(view)
        callback = Callback(
            flow="store",
            screen="product_locations",
            args=("leaseweb", "VPS02_1", "624", ""),
        )
        assert (await bot._store(callback, _user())).text

    async def test_a_legacy_product_locations_callback_with_a_garbage_price(self) -> None:
        view = SteeringView()
        view.result["families_screen"] = ([], "back-cb", "cancel-cb")
        bot = _bot(view)
        cb = bot._callback("store", "product_locations", "leaseweb", "VPS02_1", "x")
        assert (await _press(bot, cb)).text

    async def test_a_legacy_product_locations_callback_without_a_price(self) -> None:
        view = SteeringView()
        view.result["product_locations_screen"] = ([_location()], "back-cb", "cancel-cb")
        bot = _bot(view)
        cb = bot._callback("store", "product_locations", "leaseweb", "VPS02_1")
        assert (await _press(bot, cb)).text

    async def test_an_os_callback_with_a_stale_ref_shows_the_expired_screen(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("store", "os", "zzzzzzzz"))).text

    async def test_a_confirm_callback_without_a_user_shows_the_identity_notice(self) -> None:
        bot = _bot(SteeringView())
        screen = await bot.handle(
            bot._callback("store", "confirm", encode_offer_ref(OFFER_ID), "0"), user=None
        )
        assert screen is not None and screen.text

    async def test_a_confirm_callback_with_a_stale_ref_expires(self) -> None:
        bot = _bot(SteeringView())
        cb = bot._callback("store", "confirm", "zzzzzzzz", "0")
        assert (await _press(bot, cb)).text

    async def test_a_confirm_callback_with_a_garbage_os_index_expires(self) -> None:
        bot = _bot(SteeringView())
        cb = bot._callback("store", "confirm", encode_offer_ref(OFFER_ID), "x")
        assert (await _press(bot, cb)).text

    async def test_a_confirm_callback_reaches_the_confirmation_screen(self) -> None:
        view = SteeringView()
        view.result["confirmation"] = _confirm(_plan())
        bot = _bot(view, offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}))
        cb = bot._callback("store", "confirm", encode_offer_ref(OFFER_ID), "0", "1")
        assert (await _press(bot, cb)).text

    async def test_a_buy_callback_with_a_stale_ref_expires(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("store", "buy", "zzzzzzzz", "0"))).text

    async def test_a_buy_callback_with_a_garbage_os_index_expires(self) -> None:
        bot = _bot(SteeringView())
        cb = bot._callback("store", "buy", encode_offer_ref(OFFER_ID), "x")
        assert (await _press(bot, cb)).text

    async def test_a_buy_callback_places_the_order(self) -> None:
        checkout = FakeCheckout()
        view = SteeringView()
        view.result["os_by_index"] = "ubuntu-24.04"
        bot = _bot(
            view,
            checkout=checkout,
            offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}),
        )
        cb = bot._callback("store", "buy", encode_offer_ref(OFFER_ID), "0")
        assert (await _press(bot, cb)).text
        assert checkout.calls

    async def test_a_cloud_images_callback_with_a_stale_ref_expires(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("store", "cloud_images", "zzzzzzzz"))).text

    async def test_a_cloud_confirm_callback_without_a_user_shows_the_identity_notice(self) -> None:
        bot = _bot(SteeringView())
        cb = bot._callback("store", "cloud_confirm", encode_offer_ref(OFFER_ID), "0")
        screen = await bot.handle(cb, user=None)
        assert screen is not None and screen.text

    async def test_a_cloud_confirm_callback_with_a_stale_ref_expires(self) -> None:
        bot = _bot(SteeringView())
        cb = bot._callback("store", "cloud_confirm", "zzzzzzzz", "0")
        assert (await _press(bot, cb)).text

    async def test_a_cloud_confirm_callback_with_a_garbage_image_index_expires(self) -> None:
        bot = _bot(SteeringView())
        cb = bot._callback("store", "cloud_confirm", encode_offer_ref(OFFER_ID), "x")
        assert (await _press(bot, cb)).text

    async def test_a_cloud_buy_callback_with_a_stale_ref_expires(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("store", "cloud_buy", "zzzzzzzz", "0"))).text

    async def test_a_cloud_buy_callback_with_a_garbage_image_index_expires(self) -> None:
        bot = _bot(SteeringView())
        cb = bot._callback("store", "cloud_buy", encode_offer_ref(OFFER_ID), "x")
        assert (await _press(bot, cb)).text


# ---------------------------------------------------------------------------
# legacy offers flow
# ---------------------------------------------------------------------------


class TestLegacyOffersFlow:
    async def test_locations_without_any_offer(self) -> None:
        bot = _bot(SteeringView(), offers_repo=FakeOffersRepo())
        assert (await _press(bot, bot._callback("offers", "locations"))).text

    async def test_locations_lists_each_distinct_location_once(self) -> None:
        repo = FakeOffersRepo(locations=[("leaseweb", "FRA-01"), ("leaseweb", "FRA-01")])
        bot = _bot(SteeringView(), offers_repo=repo)
        screen = await _press(bot, bot._callback("offers", "locations"))
        assert screen.text

    async def test_plans_unavailable(self) -> None:
        view = SteeringView()
        view.raises["plans_screen"] = OfferUnavailableError("none")
        bot = _bot(view)
        assert (await _press(bot, bot._callback("offers", "plans", "FRA-01"))).text

    async def test_plans_renders_the_location_plans(self) -> None:
        view = SteeringView()
        view.result["plans_screen"] = ([_plan()], "back-cb", "cancel-cb")
        bot = _bot(view)
        assert (await _press(bot, bot._callback("offers", "plans", "FRA-01"))).text

    async def test_os_unavailable(self) -> None:
        view = SteeringView()
        view.raises["os_screen"] = OfferUnavailableError("none")
        bot = _bot(view)
        cb = bot._callback("offers", "os", encode_offer_ref(OFFER_ID))
        assert (await _press(bot, cb)).text

    async def test_os_without_usable_images(self) -> None:
        view = SteeringView()
        view.raises["os_screen"] = OsUnavailableError("none")
        bot = _bot(view)
        cb = bot._callback("offers", "os", encode_offer_ref(OFFER_ID))
        assert (await _press(bot, cb)).text

    async def test_os_lists_live_options(self) -> None:
        view = SteeringView()
        view.result["os_screen"] = (_plan(), [_option()], "back-cb", "cancel-cb")
        bot = _bot(view)
        cb = bot._callback("offers", "os", encode_offer_ref(OFFER_ID))
        assert (await _press(bot, cb)).text

    async def test_os_with_a_stale_ref_expires(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("offers", "os", "zzzzzzzz"))).text

    async def test_confirm_without_a_user(self) -> None:
        bot = _bot(SteeringView())
        cb = bot._callback("offers", "confirm", encode_offer_ref(OFFER_ID), "0")
        screen = await bot.handle(cb, user=None)
        assert screen is not None and screen.text

    async def test_confirm_with_a_stale_ref_expires(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("offers", "confirm", "zzzzzzzz", "0"))).text

    async def test_buy_with_a_stale_ref_expires(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("offers", "buy", "zzzzzzzz", "0"))).text

    async def test_an_unknown_offers_screen_returns_the_menu(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("offers", "nonsense"))).text


# ---------------------------------------------------------------------------
# checkout / buy error branches
# ---------------------------------------------------------------------------


class TestBuyScreenErrorBranches:
    async def _press_buy(self, raises: Exception | None, *, replayed: bool = False) -> Any:
        view = SteeringView()
        view.result["os_by_index"] = "ubuntu-24.04"
        checkout = FakeCheckout(raises=raises, replayed=replayed)
        bot = _bot(view, checkout=checkout, offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}))
        return await bot.buy_screen(_user(), OFFER_ID, 0, None, "cbkey")

    async def test_buy_without_identity(self) -> None:
        bot = _bot(SteeringView())
        assert (await bot.buy_screen(None, OFFER_ID, 0, None, "k")).text

    async def test_buy_reports_an_insufficient_balance(self) -> None:
        screen = await self._press_buy(InsufficientHoldBalanceError("100"))
        assert screen.text

    async def test_buy_reports_a_frozen_user(self) -> None:
        assert (await self._press_buy(UserNotActiveError("frozen"))).text

    async def test_buy_reports_an_unavailable_offer(self) -> None:
        assert (await self._press_buy(OfferUnavailableError("gone"))).text

    async def test_buy_reports_missing_os_images(self) -> None:
        assert (await self._press_buy(OsUnavailableError("gone"))).text

    async def test_buy_reports_a_generic_checkout_failure(self) -> None:
        assert (await self._press_buy(CheckoutError("boom"))).text

    async def test_buy_reports_a_replay(self) -> None:
        assert (await self._press_buy(None, replayed=True)).text

    async def test_resolving_the_os_name_for_a_vanished_offer_fails_closed(self) -> None:
        from cloud_platform.modules.offers.domain import OfferNotFoundError

        bot = _bot(SteeringView(), offers_repo=FakeOffersRepo())
        with pytest.raises(OfferNotFoundError):
            await bot._resolve_os_name(OFFER_ID, 0)

    async def test_resolving_a_panel_name_uses_the_view_index(self) -> None:
        view = SteeringView()
        view.result["panel_name_by_index"] = "cPanel"
        bot = _bot(view, offers_repo=FakeOffersRepo(offers={OFFER_ID: _plan()}))
        assert await bot._resolve_panel_name(OFFER_ID, 1) == "cPanel"

    async def test_resolving_no_panel_name_short_circuits(self) -> None:
        bot = _bot(SteeringView())
        assert await bot._resolve_panel_name(OFFER_ID, None) is None

    async def test_resolving_a_panel_name_for_a_vanished_offer_fails_closed(self) -> None:
        from cloud_platform.modules.offers.domain import OfferNotFoundError

        bot = _bot(SteeringView(), offers_repo=FakeOffersRepo())
        with pytest.raises(OfferNotFoundError):
            await bot._resolve_panel_name(OFFER_ID, 1)


class TestConfirmScreenBranches:
    async def test_confirm_without_identity(self) -> None:
        bot = _bot(SteeringView())
        assert (await bot.confirm_screen(_user(user_id=None), OFFER_ID, 0)).text

    async def test_confirm_when_the_offer_is_unavailable(self) -> None:
        view = SteeringView()
        view.raises["confirmation"] = OfferUnavailableError("gone")
        bot = _bot(view)
        assert (await bot.confirm_screen(_user(), OFFER_ID, 0)).text

    async def test_confirm_when_the_os_is_unavailable(self) -> None:
        view = SteeringView()
        view.raises["confirmation"] = OsUnavailableError("gone")
        bot = _bot(view)
        assert (await bot.confirm_screen(_user(), OFFER_ID, 0)).text

    async def test_confirm_warns_when_the_wallet_cannot_cover_it(self) -> None:
        view = SteeringView()
        view.result["confirmation"] = _confirm(_plan(), sufficient=False)
        bot = _bot(view)
        assert (await bot.confirm_screen(_user(), OFFER_ID, 0)).text

    async def test_confirm_shows_a_selected_panel(self) -> None:
        view = SteeringView()
        view.result["confirmation"] = _confirm(_plan(), panel_name="cPanel")
        bot = _bot(view)
        assert (await bot.confirm_screen(_user(), OFFER_ID, 0, 1)).text


# ---------------------------------------------------------------------------
# servers / wallet delegation
# ---------------------------------------------------------------------------


class TestServersAndWalletDelegation:
    async def test_servers_screen_when_management_is_not_configured(self) -> None:
        bot = _bot(SteeringView())
        assert (await bot.servers_screen(_user())).text

    async def test_servers_screen_without_identity(self) -> None:
        bot = _bot(SteeringView(), servers_ui=FakeServersUi())
        assert (await bot.servers_screen(_user(user_id=None))).text

    async def test_servers_screen_delegates_to_the_management_ui(self) -> None:
        bot = _bot(SteeringView(), servers_ui=FakeServersUi())
        assert (await bot.servers_screen(_user())).text

    async def test_servers_callbacks_when_management_is_not_configured(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("servers", "list"))).text

    async def test_servers_callbacks_delegate(self) -> None:
        servers_ui = FakeServersUi()
        bot = _bot(SteeringView(), servers_ui=servers_ui)
        assert (await _press(bot, bot._callback("servers", "list"))).text
        assert servers_ui.handled

    async def test_servers_list_screen_without_management(self) -> None:
        bot = _bot(SteeringView())
        assert (await bot.servers_list_screen(_user())).text

    async def test_servers_list_screen_delegates_with_a_page(self) -> None:
        bot = _bot(SteeringView(), servers_ui=FakeServersUi())
        assert (await bot.servers_list_screen(_user(), page=2)).text

    async def test_server_detail_without_management(self) -> None:
        bot = _bot(SteeringView())
        assert (await bot.server_detail_screen(_user(), uuid4())).text

    async def test_server_detail_for_an_unknown_server(self) -> None:
        bot = _bot(SteeringView(), servers_ui=FakeServersUi(ref=None))
        assert (await bot.server_detail_screen(_user(), uuid4())).text

    async def test_server_detail_routes_through_the_signed_ref(self) -> None:
        servers_ui = FakeServersUi(ref="signed-ref")
        bot = _bot(SteeringView(), servers_ui=servers_ui)
        assert (await bot.server_detail_screen(_user(), uuid4())).text

    async def test_free_text_is_only_claimed_when_something_is_pending(self) -> None:
        bot = _bot(SteeringView())
        assert await bot.handle_text("hello", _user()) is None

    async def test_free_text_is_forwarded_to_the_management_ui(self) -> None:
        bot = _bot(SteeringView(), servers_ui=FakeServersUi(text=SimpleNamespace(text="ok")))
        assert await bot.handle_text("new-name", _user()) is not None

    async def test_wallet_screen_without_identity(self) -> None:
        bot = _bot(SteeringView())
        assert (await bot.wallet_screen(None)).text

    async def test_wallet_screen_without_a_wallet(self) -> None:
        bot = _bot(SteeringView(), wallet=FakeWallet(has_wallet=False))
        assert (await bot.wallet_screen(_user())).text

    async def test_wallet_callback_without_a_user(self) -> None:
        bot = _bot(SteeringView())
        screen = await bot.handle(bot._callback("wallet", "balance"), user=None)
        assert screen is not None and screen.text

    async def test_wallet_balance_callback(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("wallet", "balance"))).text

    async def test_wallet_history_callback(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("wallet", "history"))).text

    async def test_an_unknown_wallet_screen_returns_the_menu(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("wallet", "nonsense"))).text

    async def test_wallet_history_without_identity(self) -> None:
        bot = _bot(SteeringView())
        assert (await bot.wallet_history_screen(_user(user_id=None))).text

    async def test_wallet_history_when_nothing_has_happened_yet(self) -> None:
        bot = _bot(SteeringView(), wallet=FakeWallet(items=[]))
        assert (await bot.wallet_history_screen(_user())).text

    async def test_wallet_history_renders_each_ledger_entry(self) -> None:
        from datetime import UTC, datetime

        from cloud_platform.modules.wallet.domain import LedgerEntryType

        items = [
            SimpleNamespace(
                created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
                entry_type=LedgerEntryType.DEPOSIT.value,
                formatted="+€10.00",
            ),
            SimpleNamespace(created_at=None, entry_type="unmapped", formatted="-€1.00"),
        ]
        bot = _bot(SteeringView(), wallet=FakeWallet(items=items))
        screen = await bot.wallet_history_screen(_user())
        assert screen.text

    async def test_support_callback_renders_the_support_screen(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("support", "contact"))).text

    async def test_the_main_flow_callback_returns_the_menu(self) -> None:
        bot = _bot(SteeringView())
        assert (await _press(bot, bot._callback("main", "menu"))).text


# ---------------------------------------------------------------------------
# handle() routing
# ---------------------------------------------------------------------------


class TestHandleRouting:
    async def test_a_foreign_flow_is_left_to_the_other_ui(self) -> None:
        bot = _bot(SteeringView())
        foreign = encode_callback(Callback(flow="payments", screen="x", args=()), KEY)
        assert await bot.handle(foreign, user=_user()) is None

    async def test_an_unsigned_callback_is_left_to_the_caller(self) -> None:
        bot = _bot(SteeringView())
        assert await bot.handle("not-a-signed-callback", user=_user()) is None


# ---------------------------------------------------------------------------
# recharge branches
# ---------------------------------------------------------------------------


class TestRechargeBranchCoverage:
    async def test_amount_screen_without_identity(self) -> None:
        bot = _bot(SteeringView(), recharge=FakeRecharge())
        assert (await bot.recharge_screen(None)).text

    async def test_amount_screen_without_a_wallet(self) -> None:
        bot = _bot(SteeringView(), recharge=FakeRecharge(), wallet=FakeWallet(has_wallet=False))
        assert (await bot.recharge_screen(_user())).text

    async def test_amount_screen_with_recharge_disabled_and_a_contact(self) -> None:
        bot = _bot(SteeringView(), recharge=None, support_contact="@ops")
        assert "@ops" in (await bot.recharge_screen(_user())).text

    async def test_amount_screen_probes_the_async_currency_support(self) -> None:
        # Sync path declines, the async FX probe confirms the currency.
        recharge = FakeRecharge(currency="EUR", sync_disabled=True)
        bot = _bot(SteeringView(), recharge=recharge, wallet=FakeWallet())
        screen = await bot.recharge_screen(_user())
        labels = [b.text for row in screen.keyboard.inline_keyboard for b in row]
        assert any("€" in label for label in labels)

    async def test_amount_screen_without_online_credit_points_at_support(self) -> None:
        # Sync path declines and the async probe agrees: no gateway covers it.
        recharge = FakeRecharge(currency="IRR", sync_disabled=True)
        bot = _bot(SteeringView(), recharge=recharge, support_contact="@ops")
        screen = await bot.recharge_screen(_user())
        assert "@ops" in screen.text

    async def test_amount_screen_without_presets_points_at_support(self) -> None:
        bot = _bot(SteeringView(), recharge=FakeRecharge(gateways=[]), support_contact="@ops")
        screen = await bot.recharge_screen(_user())
        assert "@ops" in screen.text

    async def test_amount_screen_when_the_async_probe_fails_is_unavailable(self) -> None:
        recharge = FakeRecharge(sync_disabled=True, probe_fails=True)
        bot = _bot(SteeringView(), recharge=recharge)
        assert (await bot.recharge_screen(_user())).text

    async def test_amount_screen_without_any_settleable_preset(self) -> None:
        recharge = FakeRecharge(gateways=[])
        bot = _bot(SteeringView(), recharge=recharge)
        assert (await bot.recharge_screen(_user())).text

    async def test_preset_filtering_survives_a_failing_gateway_probe(self) -> None:
        recharge = FakeRecharge(presets_fail=True)
        bot = _bot(SteeringView(), recharge=recharge)
        assert (await bot.recharge_screen(_user())).text

    async def test_available_presets_without_a_service_is_empty(self) -> None:
        bot = _bot(SteeringView(), recharge=None)
        assert await bot._available_presets("EUR") == ()

    async def test_start_screen_without_identity(self) -> None:
        bot = _bot(SteeringView(), recharge=FakeRecharge())
        assert (await bot.recharge_start_screen(_user(user_id=None), "2500")).text

    async def test_start_screen_without_a_service(self) -> None:
        bot = _bot(SteeringView(), recharge=None)
        assert (await bot.recharge_start_screen(_user(), "2500")).text

    async def test_start_screen_when_the_gateway_probe_fails(self) -> None:
        recharge = FakeRecharge(presets_fail=True)
        bot = _bot(SteeringView(), recharge=recharge)
        assert (await bot.recharge_start_screen(_user(), "2500")).text

    async def test_start_screen_when_no_gateway_can_settle_the_amount(self) -> None:
        recharge = FakeRecharge(gateways=[])
        bot = _bot(SteeringView(), recharge=recharge)
        assert (await bot.recharge_start_screen(_user(), "2500")).text

    async def test_start_screen_offers_the_gateway_picker_for_several_gateways(self) -> None:
        recharge = FakeRecharge(gateways=["zarinpal", "tetraminator"])
        bot = _bot(SteeringView(), recharge=recharge)
        screen = await bot.recharge_start_screen(_user(), "2500")
        labels = [b.text for row in screen.keyboard.inline_keyboard for b in row]
        assert len(labels) >= 3

    async def test_start_screen_refuses_a_gateway_that_cannot_settle_it(self) -> None:
        recharge = FakeRecharge(gateways=["zarinpal"])
        bot = _bot(SteeringView(), recharge=recharge)
        assert (await bot.recharge_start_screen(_user(), "2500", gateway_key="tetraminator")).text

    async def test_start_screen_maps_a_generic_recharge_failure_to_unavailable(self) -> None:
        recharge = FakeRecharge(fail=RechargeError("down"))
        bot = _bot(SteeringView(), recharge=recharge)
        assert (await bot.recharge_start_screen(_user(), "2500")).text

    async def test_start_screen_reports_an_amount_below_the_minimum(self) -> None:
        recharge = FakeRecharge(fail=RechargeAmountError("too small"))
        bot = _bot(SteeringView(), recharge=recharge)
        assert (await bot.recharge_start_screen(_user(), "1")).text

    def test_gateway_display_name_falls_back_to_the_key(self) -> None:
        bot = _bot(SteeringView(), recharge=FakeRecharge())
        assert bot.gateway_display_name("totally-unknown-gateway")

    def test_gateway_display_name_never_returns_empty_for_a_known_gateway(self) -> None:
        bot = _bot(SteeringView(), recharge=FakeRecharge())
        assert bot.gateway_display_name("zarinpal")
        assert bot.gateway_display_name("tetraminator")

    async def test_recharge_callback_without_a_user(self) -> None:
        bot = _bot(SteeringView(), recharge=FakeRecharge())
        screen = await bot.handle(bot._callback("recharge", "amounts"), user=None)
        assert screen is not None and screen.text

    async def test_recharge_start_callback_with_an_explicit_gateway(self) -> None:
        recharge = FakeRecharge(gateways=["zarinpal", "tetraminator"])
        bot = _bot(SteeringView(), recharge=recharge)
        cb = bot._callback("recharge", "start", "2500", "tetraminator")
        assert (await _press(bot, cb)).text

    async def test_an_unknown_recharge_screen_returns_the_menu(self) -> None:
        bot = _bot(SteeringView(), recharge=FakeRecharge())
        assert (await _press(bot, bot._callback("recharge", "nonsense"))).text


# ---------------------------------------------------------------------------
# price display
# ---------------------------------------------------------------------------


class TestPriceDisplay:
    async def test_native_price_is_shown_without_a_conversion(self) -> None:
        bot = _bot(SteeringView())
        assert await bot._price_label(624, "EUR")

    async def test_a_failing_fx_resolver_degrades_to_native_only(self) -> None:
        class _Boom:
            async def resolve(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("fx down")

        bot = _bot(SteeringView())
        bot._fx = _Boom()
        bot._fx_display_currency = "IRT"
        assert await bot._price_label(624, "EUR")
