"""City grouping / datacenter drill-down (§15/16/17/21) plus currency safety (§12).

- Monthly locations group by synced (country, city) facts: the first location
  step is country/city buttons, never raw hall codes; each city drills down to
  its exact datacenters with real catalog facts (code, plan count, minimum).
- Mixed-currency scopes report no minimum instead of comparing currencies.
- Stale location rows gain normalized metadata through the ordering sync and
  then render flags on the real bot screen.
- Every emitted callback fits Telegram's 64-byte budget; single-location
  cities skip the halls hop; unknown city slugs fail closed to safe text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.core.i18n import Translator
from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.checkout.service import OfferCatalogViewService
from cloud_platform.modules.markets.domain import ProviderCatalog
from cloud_platform.modules.navigation.domain import Callback, decode_callback
from cloud_platform.modules.offers.domain import BILLING_MODEL_MONTHLY, SellableOffer
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.providers.leaseweb.ordering import LocationEligibility, LocationProbe

PROVIDER = "leaseweb"
SIGNING_KEY = "location-city-grouping-signing-key"

USER = User(
    id=uuid4(),
    username="cust",
    email="cust@example.test",
    status=UserStatus.ACTIVE,
    role=Role.USER,
    telegram_user_id=12345,
)

SIX = (
    ("FRA-01", "Frankfurt", "DE"),
    ("FRA-10", "Frankfurt", "DE"),
    ("FRA-14", "Frankfurt", "DE"),
    ("LON-01", "London", "GB"),
    ("LON-11", "London", "GB"),
    ("LON-12", "London", "GB"),
)


def _monthly_offer(
    location_id: str,
    price_minor: int = 624,
    currency: str = "EUR",
    product_id: str = "VPS02_1",
) -> SellableOffer:
    return SellableOffer(
        id=uuid4(),
        provider_key=PROVIDER,
        product_id=product_id,
        location_id=location_id,
        name="Leaseweb VPS 1",
        vcpu=4,
        ram_gb=6,
        disk_gb=100,
        traffic="5 TB",
        provider_cost_minor=499,
        provider_cost_currency=currency,
        selling_price_minor=price_minor,
        selling_currency=currency,
        billing_parameters={},
        billing_model=BILLING_MODEL_MONTHLY,
        provider_available=True,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _synced_records() -> list[LocationRecord]:
    return [
        LocationRecord(
            provider_key=PROVIDER,
            location_id=code,
            name=city,
            country_code=country,
            city=city,
        )
        for code, city, country in SIX
    ]


class FakeOffersRepo:
    def __init__(self, offers: list[SellableOffer]) -> None:
        self.offers = offers

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return next((o for o in self.offers if o.id == offer_id), None)

    async def list_sellable(self, provider_key: str | None = None) -> list[SellableOffer]:
        return [
            o
            for o in self.offers
            if o.sellable and (provider_key is None or o.provider_key == provider_key)
        ]

    async def list_provider_locations(self) -> list[tuple[str, str]]:
        return sorted({(o.provider_key, o.location_id) for o in self.offers if o.sellable})


class FakeLocationsRepo:
    def __init__(self, records: list[LocationRecord] | None = None) -> None:
        self._records = records or []

    async def upsert(self, record: LocationRecord) -> bool:
        return True

    async def list_for_provider(self, provider_key: str) -> list[LocationRecord]:
        return [r for r in self._records if r.provider_key == provider_key]


def _implicit_catalog() -> ProviderCatalog:
    return ProviderCatalog(
        markets={PROVIDER: "foreign"},
        display_names={PROVIDER: "Leaseweb"},
        enabled={},
    )


def _configured_catalog() -> ProviderCatalog:
    from cloud_platform.modules.offers.domain import BILLING_MODEL_HOURLY

    return ProviderCatalog(
        markets={PROVIDER: "foreign"},
        display_names={PROVIDER: "Leaseweb"},
        enabled={},
        families={
            PROVIDER: {
                "vps": {"billing_model": BILLING_MODEL_MONTHLY, "display_name": "VPS"},
                "cloud": {"billing_model": BILLING_MODEL_HOURLY, "display_name": "Cloud"},
            }
        },
    )


def _service(
    offers: list[SellableOffer],
    records: list[LocationRecord],
    catalog: ProviderCatalog | None = None,
) -> OfferCatalogViewService:
    from unittest.mock import MagicMock

    registry = MagicMock()
    registry.get.side_effect = KeyError("no")

    async def _none(*args: Any, **kwargs: Any) -> None:
        return None

    wallet = MagicMock()
    wallet.get = _none
    return OfferCatalogViewService(
        offers_repo=FakeOffersRepo(offers),  # type: ignore[arg-type]
        provider_registry=registry,
        wallet_repo=wallet,
        signing_key=SIGNING_KEY,
        market_catalog=catalog if catalog is not None else _implicit_catalog(),
        location_repo=FakeLocationsRepo(records),
    )


class FakeCheckout:
    async def create_order(self, **kwargs: Any) -> Any:
        raise AssertionError("no checkout in label tests")


class FakeServers:
    async def list_by_user(self, user_id: UUID) -> list[Any]:
        return []


class FakeOrders:
    async def get_by_server(self, server_id: UUID) -> None:
        return None


class FakeRenewals:
    async def get(self, server_id: UUID) -> None:
        return None


class FakeWalletHistory:
    async def balance(self, user_id: UUID) -> Any:
        return type("V", (), {"has_wallet": False})()

    async def history(self, user_id: UUID, limit: int = 20) -> Any:
        return type("Page", (), {"items": []})()


def _ui(service: OfferCatalogViewService) -> MonthlyBotUi:
    return MonthlyBotUi(
        SIGNING_KEY,
        offers_view=service,
        checkout=FakeCheckout(),  # type: ignore[arg-type]
        servers=FakeServers(),  # type: ignore[arg-type]
        orders=FakeOrders(),  # type: ignore[arg-type]
        renewals=FakeRenewals(),  # type: ignore[arg-type]
        offers_repo=FakeOffersRepo([]),
        wallet_history=FakeWalletHistory(),  # type: ignore[arg-type]
    )


def _decode(data: str) -> Callback:
    return decode_callback(data, SIGNING_KEY)


def _buttons(screen: Any) -> list[Any]:
    return [button for row in screen.keyboard.inline_keyboard for button in row]


def _size(data: str) -> int:
    return len(data.encode("utf-8"))


async def _press(ui: MonthlyBotUi, callback: str) -> Any:
    screen = await ui.handle(callback, user=USER)
    assert screen is not None
    return screen


def _city_buttons(screen: Any) -> list[Any]:
    return [
        b
        for b in _buttons(screen)
        if b.callback_data and _decode(b.callback_data).screen == "loc_halls"
    ]


def _plan_buttons(screen: Any) -> list[Any]:
    return [
        b
        for b in _buttons(screen)
        if b.callback_data and _decode(b.callback_data).screen == "vps_plans"
    ]


class TestCityGrouping:
    async def test_cities_screen_returns_exactly_two_groups(self) -> None:
        service = _service(
            [_monthly_offer(code) for code, _city, _country in SIX],
            _synced_records(),
        )
        view = await service.cities_screen(PROVIDER, "monthly", 1)
        assert view.total_count == 2
        assert len(view.items) == 2
        by_city = {group.city: group for group in view.items}
        assert set(by_city) == {"Frankfurt", "London"}
        assert by_city["Frankfurt"].country_code == "DE"
        assert by_city["London"].country_code == "GB"
        assert sorted(by_city["Frankfurt"].location_ids) == ["FRA-01", "FRA-10", "FRA-14"]
        assert sorted(by_city["London"].location_ids) == ["LON-01", "LON-11", "LON-12"]

    async def test_bot_cities_screen_shows_only_city_buttons_and_drills_down(self) -> None:
        bot = _ui(
            _service(
                [_monthly_offer(code) for code, _city, _country in SIX],
                _synced_records(),
            )
        )
        cities = await _press(bot, bot._callback("store", "loc_cities", PROVIDER, "monthly", "1"))
        city_buttons = _city_buttons(cities)
        assert len(city_buttons) == 2
        assert {b.text for b in city_buttons} == {
            "🇩🇪 Frankfurt · از €6.24",
            "🇬🇧 London · از €6.24",
        }
        assert not any("FRA-" in b.text or "LON-" in b.text for b in _buttons(cities))
        frankfurt = next(b for b in city_buttons if "Frankfurt" in b.text)
        halls = await _press(bot, frankfurt.callback_data or "")
        frankfurt_halls = _plan_buttons(halls)
        assert len(frankfurt_halls) == 3
        assert {_decode(b.callback_data or "").args[2] for b in frankfurt_halls} == {
            "FRA-01",
            "FRA-10",
            "FRA-14",
        }
        london = next(b for b in city_buttons if "London" in b.text)
        london_halls_screen = await _press(bot, london.callback_data or "")
        london_halls = _plan_buttons(london_halls_screen)
        assert len(london_halls) == 3
        assert {_decode(b.callback_data or "").args[2] for b in london_halls} == {
            "LON-01",
            "LON-11",
            "LON-12",
        }


class TestHallDifferentiation:
    async def test_hall_rows_carry_code_price_and_count(self) -> None:
        records = [
            LocationRecord(
                provider_key=PROVIDER,
                location_id=code,
                name="Frankfurt",
                country_code="DE",
                city="Frankfurt",
            )
            for code in ("FRA-01", "FRA-10", "FRA-14")
        ]
        offers = [
            _monthly_offer("FRA-01", price_minor=624),
            _monthly_offer("FRA-10", price_minor=562),
            _monthly_offer("FRA-14", price_minor=562),
        ]
        service = _service(offers, records)
        view = await service.city_locations_screen(
            PROVIDER,
            "monthly",
            "DE",
            OfferCatalogViewService.city_slug("Frankfurt"),
            1,
        )
        assert [item.location_id for item in view.items] == ["FRA-01", "FRA-10", "FRA-14"]
        minimums = {item.location_id: (item.min_price_minor, item.currency) for item in view.items}
        assert minimums == {
            "FRA-01": (624, "EUR"),
            "FRA-10": (562, "EUR"),
            "FRA-14": (562, "EUR"),
        }
        bot = _ui(service)
        cities = await _press(bot, bot._callback("store", "loc_cities", PROVIDER, "monthly", "1"))
        frankfurt = next(b for b in _city_buttons(cities) if "Frankfurt" in b.text)
        halls = await _press(bot, frankfurt.callback_data or "")
        labels = [b.text for b in _plan_buttons(halls)]
        assert len(labels) == 3
        assert any("FRA-01" in label for label in labels)
        assert any("FRA-10" in label for label in labels)
        assert any("FRA-14" in label for label in labels)
        assert any("€6.24" in label for label in labels)
        assert any("€5.62" in label for label in labels)
        for label in labels:
            assert "NVMe" not in label
            assert "CPU" not in label


class TestCurrencySafety:
    async def test_mixed_currency_city_reports_no_minimum(self) -> None:
        records = [
            LocationRecord(
                provider_key=PROVIDER,
                location_id="FRA-01",
                name="Frankfurt",
                country_code="DE",
                city="Frankfurt",
            ),
            LocationRecord(
                provider_key=PROVIDER,
                location_id="FRA-10",
                name="Frankfurt",
                country_code="DE",
                city="Frankfurt",
            ),
            LocationRecord(
                provider_key=PROVIDER,
                location_id="LON-01",
                name="London",
                country_code="GB",
                city="London",
            ),
            LocationRecord(
                provider_key=PROVIDER,
                location_id="LON-11",
                name="London",
                country_code="GB",
                city="London",
            ),
        ]
        offers = [
            _monthly_offer("FRA-01", price_minor=624, currency="EUR"),
            _monthly_offer("FRA-10", price_minor=500, currency="GBP"),
            _monthly_offer("LON-01", price_minor=700, currency="EUR"),
            _monthly_offer("LON-11", price_minor=624, currency="EUR"),
        ]
        service = _service(offers, records)
        view = await service.cities_screen(PROVIDER, "monthly", 1)
        by_city = {group.city: group for group in view.items}
        mixed = by_city["Frankfurt"]
        assert mixed.plan_count == 2
        assert mixed.min_price_minor is None
        assert mixed.currency is None
        single = by_city["London"]
        assert single.min_price_minor == 624
        assert single.currency == "EUR"


class _Product:
    def __init__(self, location: str) -> None:
        self.id = "VPS02_1"
        self.name = "Leaseweb VPS 1"
        self.location = location
        self.vcpu = 4
        self.ram_gb = 6
        self.disk_gb = 100
        self.traffic = "5 TB"
        self.currency = "EUR"
        self.monthly_price_minor = 624


class _Detail:
    def __init__(self, location: str) -> None:
        self.product = _Product(location)
        self.available_locations = (location,)
        self.os_options = ()
        self.control_panels = ()


class _OrderingProvider:
    """One credential account serving the stale halls (scripted probes)."""

    discovery_seeds: tuple[str, ...] = ()
    _contract_term = "1_MONTH"
    _billing_cycle = "1_MONTH"

    def __init__(self, locations: tuple[str, ...]) -> None:
        self._locations = locations

    async def list_locations(self) -> list[Any]:
        return []

    def describe_location(self, code: str) -> Any:
        from cloud_platform.providers.leaseweb.ordering import (
            LeaseWebOrderingProvider,
        )

        return LeaseWebOrderingProvider.describe_location(
            LeaseWebOrderingProvider.__new__(LeaseWebOrderingProvider), code
        )

    async def list_products_unscoped(self) -> list[Any]:
        products = []
        for location in ("LON-11", "LON-12"):
            products.append(_Product(location))
        return products

    async def probe_location(self, location: str) -> LocationProbe:
        if location not in self._locations:
            return LocationProbe(location, LocationEligibility.ELIGIBLE_EMPTY, (), (), "empty")
        return LocationProbe(
            location,
            LocationEligibility.ELIGIBLE_AVAILABLE,
            (_Product(location),),
            (),
            "1 products",
        )

    async def get_product(self, location_id: str, product_id: str) -> Any:
        return _Detail(location_id)


class _SyncLocationRepo:
    """In-memory ProviderLocation rows, tracking every upsert (update proof)."""

    def __init__(self, seeded: list[LocationRecord]) -> None:
        self._rows: dict[str, LocationRecord] = {r.location_id: r for r in seeded}
        self.upserts: list[str] = []

    async def list_for_provider(self, provider_key: str) -> list[LocationRecord]:
        return [r for r in self._rows.values() if r.provider_key == provider_key]

    async def upsert(self, record: LocationRecord) -> bool:
        created = record.location_id not in self._rows
        self._rows[record.location_id] = record
        self.upserts.append(record.location_id)
        return created


class _SyncOffersRepo:
    def __init__(self) -> None:
        self._available: set[tuple[str, str]] = set()

    async def list_all(self) -> list[Any]:
        return []

    async def upsert_from_provider(self, **kwargs: Any) -> Any:
        self._available.add((kwargs["product_id"], kwargs["location_id"]))
        return None

    async def mark_unavailable(
        self, provider_key: str, available: set[tuple[str, str]], billing_model: Any = None
    ) -> int:
        return 0


class _SyncRouteRepo:
    async def list_for_provider(self, provider_key: str) -> list[Any]:
        return []

    async def upsert_observations(self, **kwargs: Any) -> int:
        return 0


class TestMetadataBackfill:
    async def test_stale_rows_gain_metadata_and_render_flags(self, monkeypatch: Any) -> None:
        from cloud_platform.providers.leaseweb.ordering_sync import (
            LeaseWebOrderingCatalogSyncer,
        )

        stale_codes = ("FRA-10", "FRA-14", "LON-11", "LON-12")
        locations = _SyncLocationRepo(
            [
                LocationRecord(
                    provider_key=PROVIDER,
                    location_id=code,
                    name=code,
                    country_code=None,
                    city=None,
                )
                for code in stale_codes
            ]
        )
        offers = _SyncOffersRepo()
        routes = _SyncRouteRepo()
        monkeypatch.setattr(
            "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
            lambda *a, **k: locations,
        )
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.ordering_sync.SqlAlchemySellableOfferRepository",
            lambda *a, **k: offers,
        )
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.ordering_sync.SqlAlchemyProviderRouteRepository",
            lambda *a, **k: routes,
        )
        syncer = LeaseWebOrderingCatalogSyncer(
            lambda: None,  # type: ignore[arg-type]
            _OrderingProvider(stale_codes),  # type: ignore[arg-type]
        )
        result = await syncer.sync_products()
        assert result.offers_persisted == len(stale_codes)
        assert not any("location metadata" in w for w in result.warnings)
        rows = {r.location_id: r for r in await locations.list_for_provider(PROVIDER)}
        expected = {
            "FRA-10": ("Frankfurt", "DE"),
            "FRA-14": ("Frankfurt", "DE"),
            "LON-11": ("London", "GB"),
            "LON-12": ("London", "GB"),
        }
        for code, (city, country) in expected.items():
            assert rows[code].city == city, code
            assert rows[code].country_code == country, code
            assert rows[code].name == city, code
        assert set(locations.upserts) >= set(stale_codes)
        bot = _ui(
            _service(
                [_monthly_offer(code) for code in stale_codes],
                list(rows.values()),
            )
        )
        cities = await _press(bot, bot._callback("store", "loc_cities", PROVIDER, "monthly", "1"))
        city_buttons = _city_buttons(cities)
        assert len(city_buttons) == 2
        assert any("🇩🇪" in b.text and "Frankfurt" in b.text for b in city_buttons)
        assert any("🇬🇧" in b.text and "London" in b.text for b in city_buttons)


class TestCallbackBudget:
    async def test_families_cities_halls_callbacks_fit_telegram_budget(self) -> None:
        records = [
            *_synced_records(),
            LocationRecord(
                provider_key=PROVIDER,
                location_id="eu-west-3",
                name="Amsterdam",
                country_code="NL",
                city="Amsterdam",
            ),
        ]
        offers = [_monthly_offer(code) for code, _city, _country in SIX]
        offers.append(
            SellableOffer(
                id=uuid4(),
                provider_key=PROVIDER,
                product_id="lsw.mini",
                location_id="eu-west-3",
                name="Mini",
                vcpu=1,
                ram_gb=1,
                disk_gb=25,
                traffic="1 TB",
                provider_cost_minor=1,
                provider_cost_currency="EUR",
                selling_price_minor=2,
                selling_currency="EUR",
                billing_parameters={},
                billing_model="hourly",
                provider_available=True,
                enabled=True,
                created_at=datetime.now(UTC),
            )
        )
        service = _service(offers, records, catalog=_configured_catalog())
        bot = _ui(service)
        families = await _press(bot, bot._callback("store", "families", PROVIDER))
        cities = await _press(bot, bot._callback("store", "loc_cities", PROVIDER, "vps", "1"))
        frankfurt = next(b for b in _city_buttons(cities) if "Frankfurt" in b.text)
        halls = await _press(bot, frankfurt.callback_data or "")
        for screen in (families, cities, halls):
            for button in _buttons(screen):
                if button.callback_data:
                    assert _size(button.callback_data) <= 64, button.callback_data
        first_page = await service.cities_screen(PROVIDER, "vps", 1, page_size=1)
        assert first_page.next_callback is not None
        assert _size(first_page.next_callback) <= 64
        second_page = await service.cities_screen(PROVIDER, "vps", 2, page_size=1)
        assert second_page.prev_callback is not None
        assert _size(second_page.prev_callback) <= 64
        halls_button = next(iter(_city_buttons(cities)))
        assert halls_button.callback_data is not None
        assert _decode(halls_button.callback_data).screen == "loc_halls"

    async def test_long_city_slug_halls_callback_fits_budget(self) -> None:
        records = [
            LocationRecord(
                provider_key=PROVIDER,
                location_id=code,
                name="Frankfurt am Main",
                country_code="DE",
                city="Frankfurt am Main",
            )
            for code in ("FRA-01", "FRA-10")
        ]
        service = _service(
            [_monthly_offer("FRA-01"), _monthly_offer("FRA-10")],
            records,
            catalog=_configured_catalog(),
        )
        assert OfferCatalogViewService.city_slug("Frankfurt am Main") == "FRANKFURT-AM-MAIN"
        view = await service.cities_screen(PROVIDER, "vps", 1)
        assert len(view.items) == 1
        assert view.items[0].city == "Frankfurt am Main"
        assert _size(view.items[0].select_callback) <= 64
        decoded = decode_callback(view.items[0].select_callback, SIGNING_KEY)
        assert decoded.screen == "loc_halls"
        assert "FRANKFURT-AM-MAIN" in decoded.args


class TestCitySkipRule:
    async def test_single_location_city_enters_plans_directly(self) -> None:
        records = [
            LocationRecord(
                provider_key=PROVIDER,
                location_id="FRA-01",
                name="Frankfurt",
                country_code="DE",
                city="Frankfurt",
            ),
            LocationRecord(
                provider_key=PROVIDER,
                location_id="LON-01",
                name="London",
                country_code="GB",
                city="London",
            ),
            LocationRecord(
                provider_key=PROVIDER,
                location_id="LON-11",
                name="London",
                country_code="GB",
                city="London",
            ),
        ]
        service = _service(
            [_monthly_offer("FRA-01"), _monthly_offer("LON-01"), _monthly_offer("LON-11")],
            records,
        )
        view = await service.cities_screen(PROVIDER, "monthly", 1)
        by_city = {group.city: group for group in view.items}
        single = by_city["Frankfurt"]
        assert len(single.location_ids) == 1
        assert _decode(single.select_callback).screen == "vps_plans"
        multi = by_city["London"]
        assert len(multi.location_ids) == 2
        assert _decode(multi.select_callback).screen == "loc_halls"


class TestUnknownCitySlug:
    async def test_forged_halls_slug_renders_safe_screen(self) -> None:
        bot = _ui(
            _service(
                [_monthly_offer(code) for code, _city, _country in SIX],
                _synced_records(),
            )
        )
        forged = bot._callback("store", "loc_halls", PROVIDER, "monthly", "DE", "NOPE", "1")
        screen = await _press(bot, forged)
        assert screen.text == Translator().t("offers.no_offers")
