"""Ordering location metadata invariant (P0 production fix).

Root cause of the missing flags: ProviderLocation rows for ordering-only
locations (FRA-10/FRA-14/LON-11/LON-12) were created without a usable
country/name, and nothing guaranteed that every definitively eligible
location ends up with normalized metadata. The UI contract itself is
correct (flags render from synced country codes only).

Proven here:
- a sync that starts with only FRA-01/LON-01 rows and discovers all six
  locations from ordering probes persists correct normalized metadata
  (country + friendly city name) for all six;
- persisted rows render exact flag labels on buttons and detail lines;
- generic storefront code contains no FRA/LON/provider branch for flags.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from uuid import uuid4

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.checkout.service import OfferCatalogViewService
from cloud_platform.modules.markets.domain import ProviderCatalog
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.providers.leaseweb.ordering import LocationEligibility, LocationProbe

PROVIDER = "leaseweb"
SIGNING_KEY = "location-metadata-signing-key"

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
    """One credential account serving all six halls (scripted probes)."""

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
        # Unscoped responses carry their own location: this is how codes
        # absent from every static list (LON-11/LON-12) enter discovery.
        products = []
        for location in ("LON-11", "LON-12"):
            product = _Product(location)
            products.append(product)
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


class _LocationRepo:
    """In-memory ProviderLocation rows (seeded with two, like production)."""

    def __init__(self, seeded: list[LocationRecord]) -> None:
        self._rows: dict[str, LocationRecord] = {r.location_id: r for r in seeded}

    async def list_for_provider(self, provider_key: str) -> list[LocationRecord]:
        return [r for r in self._rows.values() if r.provider_key == provider_key]

    async def upsert(self, record: LocationRecord) -> bool:
        created = record.location_id not in self._rows
        self._rows[record.location_id] = record
        return created


class _OffersRepo:
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


class _RouteRepo:
    async def list_for_provider(self, provider_key: str) -> list[Any]:
        return []

    async def upsert_observations(self, **kwargs: Any) -> int:
        return 0


def _seeded_repo() -> _LocationRepo:
    return _LocationRepo(
        [
            LocationRecord(
                provider_key=PROVIDER,
                location_id="FRA-01",
                name="FRA-01",
                country_code="DE",
                city="Frankfurt",
            ),
            LocationRecord(
                provider_key=PROVIDER,
                location_id="LON-01",
                name="LON-01",
                country_code="GB",
                city="London",
            ),
        ]
    )


class TestEligibleLocationsGainMetadata:
    async def test_six_halls_have_normalized_metadata_after_sync(self, monkeypatch: Any) -> None:
        from cloud_platform.providers.leaseweb.ordering_sync import (
            LeaseWebOrderingCatalogSyncer,
        )

        locations = _seeded_repo()
        offers = _OffersRepo()
        routes = _RouteRepo()
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
        halls = tuple(code for code, _city, _country in SIX)
        syncer = LeaseWebOrderingCatalogSyncer(
            lambda: None,  # type: ignore[arg-type]
            _OrderingProvider(halls),  # type: ignore[arg-type]
        )
        result = await syncer.sync_products()
        assert result.offers_persisted == len(halls)
        rows = {r.location_id: r for r in await locations.list_for_provider(PROVIDER)}
        assert set(rows) >= set(halls)
        for code, city, country in SIX:
            record = rows[code]
            assert record.country_code == country, code
            assert record.city == city, code
            assert record.name == city, code

    async def test_metadata_write_failure_never_hides_products(self, monkeypatch: Any) -> None:
        from cloud_platform.providers.leaseweb.ordering_sync import (
            LeaseWebOrderingCatalogSyncer,
        )

        class _FailingLocations(_LocationRepo):
            async def upsert(self, record: LocationRecord) -> bool:
                raise RuntimeError("location db down")

        locations = _FailingLocations([])
        offers = _OffersRepo()
        routes = _RouteRepo()
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
        halls = ("FRA-10",)
        syncer = LeaseWebOrderingCatalogSyncer(
            lambda: None,  # type: ignore[arg-type]
            _OrderingProvider(halls),  # type: ignore[arg-type]
        )
        result = await syncer.sync_products()
        assert result.offers_persisted == 1
        assert any("location metadata FRA-10" in w for w in result.warnings)


def _offer_row(location_id: str) -> Any:
    from datetime import UTC, datetime

    from cloud_platform.modules.offers.domain import SellableOffer

    return SellableOffer(
        id=uuid4(),
        provider_key=PROVIDER,
        product_id="VPS02_1",
        location_id=location_id,
        name="Leaseweb VPS 1",
        vcpu=4,
        ram_gb=6,
        disk_gb=100,
        traffic="5 TB",
        provider_cost_minor=499,
        provider_cost_currency="EUR",
        selling_price_minor=624,
        selling_currency="EUR",
        billing_parameters={},
        provider_available=True,
        enabled=True,
        created_at=datetime.now(UTC),
    )


class _OffersViewRepo:
    def __init__(self, offers: list[Any]) -> None:
        self.offers = offers

    async def get(self, offer_id: Any) -> Any:
        return next((o for o in self.offers if o.id == offer_id), None)

    async def list_sellable(self, provider_key: Any = None) -> list[Any]:
        return list(self.offers)

    async def list_provider_locations(self) -> list[Any]:
        return []


class _ViewLocationsRepo:
    def __init__(self, records: list[LocationRecord]) -> None:
        self._records = records

    async def upsert(self, record: LocationRecord) -> bool:
        return True

    async def list_for_provider(self, provider_key: str) -> list[LocationRecord]:
        return [r for r in self._records if r.provider_key == provider_key]


def _view_service(offers: list[Any], records: list[LocationRecord]) -> OfferCatalogViewService:
    from unittest.mock import MagicMock

    registry = MagicMock()
    registry.get.side_effect = KeyError("no")
    wallet = MagicMock()
    wallet.get = _async_none()
    return OfferCatalogViewService(
        offers_repo=_OffersViewRepo(offers),  # type: ignore[arg-type]
        provider_registry=registry,
        wallet_repo=wallet,
        signing_key=SIGNING_KEY,
        market_catalog=ProviderCatalog(
            markets={PROVIDER: "foreign"},
            display_names={PROVIDER: "Leaseweb"},
            enabled={},
        ),
        location_repo=_ViewLocationsRepo(records),
    )


def _async_none() -> Any:
    async def _none(*args: Any, **kwargs: Any) -> None:
        return None

    return _none


class FakeCheckout:
    async def create_order(self, **kwargs: Any) -> Any:
        raise AssertionError("no checkout in label tests")


class FakeServers:
    async def list_by_user(self, user_id: Any) -> list[Any]:
        return []

    async def get(self, server_id: Any) -> None:
        return None


class FakeOrders:
    async def get_by_server(self, server_id: Any) -> None:
        return None


class FakeRenewals:
    async def get(self, server_id: Any) -> None:
        return None


class FakeWalletHistory:
    async def balance(self, user_id: Any) -> Any:
        return type("V", (), {"has_wallet": False})()

    async def history(self, user_id: Any, limit: int = 20) -> Any:
        return type("Page", (), {"items": []})()


def _ui(service: OfferCatalogViewService) -> MonthlyBotUi:
    return MonthlyBotUi(
        SIGNING_KEY,
        offers_view=service,  # type: ignore[arg-type]
        checkout=FakeCheckout(),  # type: ignore[arg-type]
        servers=FakeServers(),  # type: ignore[arg-type]
        orders=FakeOrders(),  # type: ignore[arg-type]
        renewals=FakeRenewals(),  # type: ignore[arg-type]
        offers_repo=_OffersViewRepo([]),
        wallet_history=FakeWalletHistory(),  # type: ignore[arg-type]
    )


def _buttons(screen: Any) -> list[Any]:
    return [button for row in screen.keyboard.inline_keyboard for button in row]


async def _press(ui: MonthlyBotUi, callback: str) -> Any:
    screen = await ui.handle(callback, user=USER)
    assert screen is not None
    return screen


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


class TestFlagLabels:
    async def test_persisted_rows_render_exact_flag_labels(self) -> None:
        from cloud_platform.modules.navigation.domain import decode_callback

        bot = _ui(
            _view_service(
                [_offer_row(code) for code, _city, _country in SIX],
                _synced_records(),
            )
        )
        screen = await _press(
            bot, bot._callback("store", "vps_locations", PROVIDER, "monthly", "1")
        )
        # First location step is country/city only: exactly two buttons, no
        # raw hall codes.
        city_buttons = [
            b
            for b in _buttons(screen)
            if b.callback_data
            and decode_callback(b.callback_data, SIGNING_KEY).screen == "loc_halls"
        ]
        assert {b.text for b in city_buttons} == {
            "🇩🇪 Frankfurt · از €6.24",
            "🇬🇧 London · از €6.24",
        }
        # Frankfurt drills down to its three exact datacenters, each carrying
        # its code plus real catalog facts (plan count, minimum price).
        frankfurt = next(b for b in city_buttons if "Frankfurt" in b.text)
        halls = await _press(bot, frankfurt.callback_data or "")
        hall_labels = [b.text for b in _buttons(halls) if b.callback_data]
        assert "FRA-01 · 1 پلن · شروع از €6.24" in hall_labels
        assert "FRA-10 · 1 پلن · شروع از €6.24" in hall_labels
        assert "FRA-14 · 1 پلن · شروع از €6.24" in hall_labels
        london = next(b for b in city_buttons if "London" in b.text)
        london_halls = await _press(bot, london.callback_data or "")
        london_labels = [b.text for b in _buttons(london_halls) if b.callback_data]
        assert "LON-01 · 1 پلن · شروع از €6.24" in london_labels
        assert "LON-11 · 1 پلن · شروع از €6.24" in london_labels
        assert "LON-12 · 1 پلن · شروع از €6.24" in london_labels

    async def test_single_hall_button_has_no_code(self) -> None:
        from cloud_platform.modules.navigation.domain import decode_callback

        bot = _ui(
            _view_service(
                [_offer_row("FRA-10")],
                [r for r in _synced_records() if r.location_id == "FRA-10"],
            )
        )
        screen = await _press(
            bot, bot._callback("store", "vps_locations", PROVIDER, "monthly", "1")
        )
        labels = [
            b.text
            for b in _buttons(screen)
            if b.callback_data
            and decode_callback(b.callback_data, SIGNING_KEY).screen == "vps_plans"
        ]
        # A single-hall city enters plans directly; the button carries the
        # friendly city (plus its minimum), never the raw code.
        assert labels == ["🇩🇪 Frankfurt · از €6.24"]

    async def test_detail_line_carries_code_and_flag(self) -> None:
        bot = _ui(
            _view_service(
                [_offer_row("FRA-10")],
                [r for r in _synced_records() if r.location_id == "FRA-10"],
            )
        )
        detail = await _press(
            bot, bot._callback("store", "plan_detail", PROVIDER, "FRA-10", "VPS02_1")
        )
        assert "🇩🇪 Frankfurt — FRA-10" in detail.text


def _code_strings(path: Path) -> list[str]:
    """String literals of a module, excluding every docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []

    def body_children(node: ast.AST) -> list[ast.AST]:
        children = list(ast.iter_child_nodes(node))
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                children = [c for c in children if c is not body[0]]
        return children

    def visit(node: ast.AST) -> None:
        for child in body_children(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                found.append(child.value)
            elif isinstance(child, ast.AST):
                visit(child)

    visit(tree)
    return found


def _branch_strings(path: Path) -> list[str]:
    """String literals used in comparisons/calls of a module (branching)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Compare):
            for part in (node.left, *node.comparators):
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    found.append(part.value)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AST):
                visit(child)

    visit(tree)
    return found


class TestNoGeoBranching:
    """Generic storefront code decides flags from synced country codes only."""

    def test_no_geo_literals_in_storefront_code(self) -> None:
        root = Path(__file__).resolve().parents[2]
        checked = 0
        for relative in (
            "src/cloud_platform/bot/monthly_ui.py",
            "src/cloud_platform/modules/checkout/service.py",
            "src/cloud_platform/modules/markets/domain.py",
        ):
            checked += 1
            for literal in _code_strings(root / relative):
                assert "FRA-" not in literal, f"{relative}: {literal!r}"
                assert "LON-" not in literal, f"{relative}: {literal!r}"
        assert checked == 3

    def test_no_provider_branch_in_storefront_code(self) -> None:
        root = Path(__file__).resolve().parents[2]
        checked = 0
        for relative in (
            "src/cloud_platform/bot/monthly_ui.py",
            "src/cloud_platform/modules/checkout/service.py",
            "src/cloud_platform/modules/markets/domain.py",
        ):
            checked += 1
            for literal in _branch_strings(root / relative):
                lowered = literal.lower()
                assert "leaseweb" not in lowered, f"{relative}: {literal!r}"
                assert "hetzner" not in lowered, f"{relative}: {literal!r}"
                assert "arvancloud" not in lowered, f"{relative}: {literal!r}"
        assert checked == 3
