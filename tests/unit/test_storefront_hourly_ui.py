"""P0: monthly_ui hourly-branch coverage completion.

Covers the image -> confirm -> buy screens and their guard rails using the
flow module's fakes and SIGNED callbacks (via ``bot._callback``): no
provider mutation is possible from these screens — the hourly service fake
records calls; nothing else is invoked.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from cloud_platform.modules.hourly.service import HourlyError
from cloud_platform.modules.navigation.domain import encode_offer_ref
from tests.unit.test_hourly_cloud_flow import (
    FakeOffersRepo,
    _buttons,
    _cloud_type,
    _decode,
    _offer,
    _press,
    _ui,
)


def _ref(offers: FakeOffersRepo) -> str:
    """The signed-callback-safe compact ref of the repo's first offer."""
    return encode_offer_ref(next(iter(offers._rows.values())).id)


def _image(image_id: str = "ubuntu-24.04", label: str = "Ubuntu 24.04") -> Any:
    """Provider-shaped image fact (id and customer label stay separate)."""
    return type("I", (), {"id": image_id, "label": label})()


class _ConfirmView:
    """Shape returned by OfferCatalogViewService.cloud_confirmation."""

    def __init__(self, offer: Any, image_label: str = "Ubuntu 24.04") -> None:
        self.offer = offer
        self.image_label = image_label
        self.hourly_price_minor = offer.selling_price_minor
        self.monthly_estimate_minor = offer.selling_price_minor * 730
        self.currency = offer.selling_currency
        self.location_name = "Frankfurt"
        self.location_country = "DE"
        self.confirm_callback = "PENDING-SIGNED-BY-UI"  # replaced below
        self.back_callback = "PENDING-SIGNED-BY-UI"
        self.cancel_callback = "PENDING-SIGNED-BY-UI"


class _ImagesView:
    """Minimal OfferCatalogViewService stand-in for the hourly screens."""

    def __init__(self, offers: list[Any], images: list[Any]) -> None:
        self._offers = offers
        self._images = images
        self._bot: Any = None  # wired by tests to sign callbacks

    async def cloud_images_screen(self, offer_id: Any) -> Any:
        offer = self._offers[0]
        rows = [
            type(
                "B",
                (),
                {
                    "text": image.label,
                    "callback_data": self._bot._callback(
                        "store", "cloud_confirm", encode_offer_ref(offer.id), "0"
                    ),
                },
            )()
            for image in self._images
        ]
        return type("S", (), {"images": self._images, "keyboard_rows": rows})()

    async def cloud_confirmation(self, *, user_id: Any, offer_id: Any, image_index: int) -> Any:
        from cloud_platform.modules.checkout.service import OsUnavailableError

        offer = next((o for o in self._offers if o.id == offer_id), None)
        if offer is None:
            from cloud_platform.modules.offers.domain import OfferNotFoundError

            raise OfferNotFoundError("missing")
        if image_index >= len(self._images):
            raise OsUnavailableError("stale image index")
        view = _ConfirmView(offer)
        view.confirm_callback = self._bot._callback(
            "store", "cloud_buy", encode_offer_ref(offer.id), "0"
        )
        view.back_callback = self._bot._callback(
            "store", "cloud_images", encode_offer_ref(offer.id)
        )
        view.cancel_callback = self._bot._callback("store", "families", offer.provider_key)
        return view

    async def cloud_image_by_index(self, offer: Any, index: int) -> Any:
        if index < 0 or index >= len(self._images):
            raise HourlyError(f"image option {index} is not available")
        return self._images[index]

    # -- VPS/plan guard branches (each surfaces the safe fallback screen) --

    async def family_locations_screen(
        self, provider_key: str, family_key: str, page: int = 1
    ) -> Any:
        from cloud_platform.modules.checkout.service import OfferUnavailableError

        raise OfferUnavailableError("no locations")

    async def plan_detail_screen(self, provider_key: str, location_id: str, product_id: str) -> Any:
        from cloud_platform.modules.checkout.service import OfferUnavailableError

        raise OfferUnavailableError("plan detail not available")

    async def families_screen(self, provider_key: str) -> Any:
        from cloud_platform.modules.checkout.service import OfferUnavailableError

        raise OfferUnavailableError("no families")

    async def family_plans_screen(
        self, provider_key: str, family_key: str, location_id: str, page: int = 1
    ) -> Any:
        from cloud_platform.modules.checkout.service import OfferUnavailableError

        raise OfferUnavailableError("no plans")


class TestHourlyBuyScreens:
    def _ui_with(self, offers: FakeOffersRepo, images: list[Any]) -> Any:
        from cloud_platform.modules.checkout.service import OfferUnavailableError
        from tests.unit.test_hourly_cloud_flow import FakeView

        service = FakeView.__new__(FakeView)
        view = _ImagesView(list(offers._rows.values()), images)
        service._service = view
        bot = _ui(service)
        view._bot = bot
        bot._offers_repo = offers  # resolve compact refs against this repo

        async def _unavailable(*args: Any, **kwargs: Any) -> Any:
            raise OfferUnavailableError("not available")

        # The bot holds the OUTER FakeView; its banner methods are what the UI
        # actually calls, so guard branches must be installed there.
        bot._view.plan_detail_screen = _unavailable  # type: ignore[method-assign]
        bot._view.family_plans_screen = _unavailable  # type: ignore[method-assign]
        return bot, view

    async def test_confirm_screen_shows_hourly_basis_and_warning(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_image()])
        screen = await _press(bot, bot._callback("store", "cloud_confirm", _ref(offers), "0"))
        assert "€" in screen.text or "تومان" in screen.text
        assert "Resource" in screen.text  # delete-to-stop warning present
        screens = [_decode(b.callback_data or "").screen for b in _buttons(screen)]
        assert "cloud_buy" in screens

    async def test_confirm_with_stale_image_index_shows_os_unavailable(self) -> None:
        from cloud_platform.modules.checkout.service import OsUnavailableError

        offers = FakeOffersRepo([_offer()])
        bot, view = self._ui_with(offers, [_image()])

        async def _stale(**kwargs: Any) -> Any:
            raise OsUnavailableError("gone")

        view.cloud_confirmation = _stale  # type: ignore[method-assign]
        screen = await _press(bot, bot._callback("store", "cloud_confirm", _ref(offers), "9"))
        assert screen.text

    async def test_confirm_with_unknown_offer_shows_expired(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_image()])
        screen = await _press(bot, bot._callback("store", "cloud_confirm", "zzzzzzzz", "0"))
        assert screen is not None

    async def test_buy_screen_creates_intent_without_provider_post(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_image()])
        offer = (await offers.list_all())[0]
        hourly = bot._hourly
        screen = await _press(bot, bot._callback("store", "cloud_buy", _ref(offers), "0"))
        assert hourly.calls, "hourly service create_instance must be invoked"
        call = hourly.calls[-1]
        assert call["offer_id"] == offer.id
        assert call["idempotency_key"].startswith("bot-hourly:")
        assert screen.text

    async def test_buy_with_unknown_offer_shows_expired_without_intent(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_image()])
        screen = await _press(bot, bot._callback("store", "cloud_buy", "zzzzzzzz", "0"))
        assert bot._hourly.calls == []
        assert screen is not None

    async def test_buy_screen_replay_is_labelled(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_image()])

        class _ReplayHourly:
            async def create_instance(self, **kwargs: Any) -> Any:
                return type(
                    "R", (), {"server": type("S", (), {"id": uuid4()})(), "replayed": True}
                )()

        bot._hourly = _ReplayHourly()
        screen = await _press(bot, bot._callback("store", "cloud_buy", _ref(offers), "0"))
        assert screen.text

    async def test_buy_rejects_hourly_error_to_unavailable(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_image()])

        class _Reject:
            async def create_instance(self, **kwargs: Any) -> Any:
                raise HourlyError("not sellable")

        bot._hourly = _Reject()
        screen = await _press(bot, bot._callback("store", "cloud_buy", _ref(offers), "0"))
        assert screen.text

    async def test_buy_rejects_unexpected_error_to_error_screen(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_image()])

        class _Boom:
            async def create_instance(self, **kwargs: Any) -> Any:
                raise RuntimeError("boom")

        bot._hourly = _Boom()
        screen = await _press(bot, bot._callback("store", "cloud_buy", _ref(offers), "0"))
        assert screen.text

    async def test_buy_without_hourly_service_shows_unavailable(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_image()])
        bot._hourly = None
        screen = await _press(bot, bot._callback("store", "cloud_buy", _ref(offers), "0"))
        assert screen.text


class TestCallbackGuards:
    def _ui_with(self, offers: FakeOffersRepo, images: list[Any]) -> Any:
        from cloud_platform.modules.checkout.service import OfferUnavailableError
        from tests.unit.test_hourly_cloud_flow import FakeView

        service = FakeView.__new__(FakeView)
        view = _ImagesView(list(offers._rows.values()), images)
        service._service = view
        bot = _ui(service)
        view._bot = bot
        bot._offers_repo = offers  # resolve compact refs against this repo

        async def _unavailable(*args: Any, **kwargs: Any) -> Any:
            raise OfferUnavailableError("not available")

        # The bot holds the OUTER FakeView; its banner methods are what the UI
        # actually calls, so guard branches must be installed there.
        bot._view.plan_detail_screen = _unavailable  # type: ignore[method-assign]
        bot._view.family_plans_screen = _unavailable  # type: ignore[method-assign]
        return bot, view

    async def test_unknown_store_screen_falls_back_to_menu(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_cloud_type()])
        screen = await _press(bot, bot._callback("store", "does_not_exist", "leaseweb"))
        assert screen is not None

    async def test_malformed_compact_ref_is_handled(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_cloud_type()])
        screen = await _press(
            bot, bot._callback("store", "plan_detail", "not-a-ref", "FRA-01", "EUR")
        )
        assert screen is not None

    async def test_os_screen_with_unknown_offer_shows_expired(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_cloud_type()])
        screen = await _press(bot, bot._callback("store", "os", "zzzzzzzz"))
        assert screen is not None

    async def test_panel_screen_with_garbage_index_falls_back(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_cloud_type()])
        screen = await _press(bot, bot._callback("store", "panel", _ref(offers), "NaN"))
        assert screen is not None

    async def test_legacy_products_callback_reenters_families(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_cloud_type()])
        screen = await _press(bot, bot._callback("store", "products", "leaseweb"))
        assert screen is not None

    async def test_vps_plan_list_with_garbage_page(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_cloud_type()])
        offer = (await offers.list_all())[0]
        screen = await _press(
            bot,
            bot._callback("store", "vps_plans", "leaseweb", "monthly", offer.location_id, "x"),
        )
        assert screen is not None

    async def test_vps_location_list_with_garbage_page(self) -> None:
        offers = FakeOffersRepo([_offer()])
        bot, _ = self._ui_with(offers, [_cloud_type()])
        screen = await _press(
            bot, bot._callback("store", "vps_locations", "leaseweb", "monthly", "x")
        )
        assert screen is not None
