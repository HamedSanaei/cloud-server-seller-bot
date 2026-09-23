"""Storefront family availability (§14): zero-sellable families stay visible.

- ``families_screen`` lists ALL configured families with ``sellable_count``
  and ``available``; a family with no sellable offers is shown as unavailable,
  never hidden, and still resolves by existence.
- The bot renders counts plus the unavailable marker, routes an unavailable
  press to the honest unavailable screen, and auto-forwards only for a single
  AVAILABLE family.
- Generic storefront code never branches on provider/geo literals (second pin
  besides ``TestNoGeoBranching``).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.core.i18n import Translator
from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.checkout.service import OfferCatalogViewService
from cloud_platform.modules.markets.domain import ProviderCatalog
from cloud_platform.modules.navigation.domain import Callback, decode_callback
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    BILLING_MODEL_MONTHLY,
    SellableOffer,
)
from cloud_platform.modules.users.domain import Role, User, UserStatus

SIGNING_KEY = "family-availability-signing-key"
PROVIDER = "leaseweb"

USER = User(
    id=uuid4(),
    username="cust",
    email="cust@example.test",
    status=UserStatus.ACTIVE,
    role=Role.USER,
    telegram_user_id=12345,
)


def _monthly_offer(
    *,
    location_id: str,
    price_minor: int = 624,
    currency: str = "EUR",
    billing_model: str = BILLING_MODEL_MONTHLY,
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
        billing_model=billing_model,
        provider_available=True,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _hourly_offer(*, location_id: str = "eu-west-3") -> SellableOffer:
    return _monthly_offer(
        location_id=location_id,
        price_minor=2,
        currency="EUR",
        billing_model=BILLING_MODEL_HOURLY,
        product_id="lsw.mini",
    )


def _catalog() -> ProviderCatalog:
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


class FakeRegistry:
    def get(self, key: str) -> Any:
        raise KeyError(key)


class FakeWalletRepo:
    async def get(self, user_id: UUID) -> Any:
        return type("W", (), {"balance": 50_000})()


def _service(
    offers: list[SellableOffer],
    locations: FakeLocationsRepo | None = None,
    catalog: ProviderCatalog | None = None,
) -> OfferCatalogViewService:
    return OfferCatalogViewService(
        offers_repo=FakeOffersRepo(offers),
        provider_registry=FakeRegistry(),
        wallet_repo=FakeWalletRepo(),
        signing_key=SIGNING_KEY,
        market_catalog=catalog if catalog is not None else _catalog(),
        location_repo=locations,
    )


class FakeCheckout:
    async def create_order(self, **kwargs: Any) -> Any:
        raise AssertionError("no checkout in render tests")


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


def _frankfurt_records() -> list[LocationRecord]:
    return [
        LocationRecord(
            provider_key=PROVIDER,
            location_id=code,
            name="Frankfurt",
            country_code="DE",
            city="Frankfurt",
        )
        for code in ("FRA-01", "FRA-10")
    ]


class TestFamilyAvailabilityCounts:
    async def test_zero_sellable_family_listed_as_unavailable(self) -> None:
        offers = [_monthly_offer(location_id="FRA-01") for _ in range(35)]
        service = _service(offers)
        families, _, _ = await service.families_screen(PROVIDER)
        assert [f.family_key for f in families] == ["vps", "cloud"]
        monthly = next(f for f in families if f.family_key == "vps")
        hourly = next(f for f in families if f.family_key == "cloud")
        assert monthly.available is True
        assert monthly.sellable_count == 35
        assert hourly.available is False
        assert hourly.sellable_count == 0

    async def test_configured_family_resolves_by_existence(self) -> None:
        service = _service([_monthly_offer(location_id="FRA-01")])
        family = await service.resolve_family(PROVIDER, "cloud")
        assert family.family_key == "cloud"
        assert family.billing_model == BILLING_MODEL_HOURLY


class TestFamilyAvailabilityBot:
    async def test_family_screen_shows_count_and_unavailable_marker(self) -> None:
        bot = _ui(_service([_monthly_offer(location_id="FRA-01") for _ in range(35)]))
        screen = await _press(bot, bot._callback("store", "families", PROVIDER))
        labels = [b.text for b in _buttons(screen)]
        monthly_label = next(label for label in labels if "VPS" in label)
        hourly_label = next(label for label in labels if "Cloud" in label)
        assert "35" in monthly_label
        assert "موقتاً ناموجود" in hourly_label
        for button in _buttons(screen):
            assert button.callback_data is not None
            assert _size(button.callback_data) <= 64

    async def test_pressing_monthly_family_opens_city_hierarchy(self) -> None:
        offers = [
            _monthly_offer(location_id="FRA-01"),
            _monthly_offer(location_id="FRA-10"),
        ]
        bot = _ui(_service(offers, FakeLocationsRepo(_frankfurt_records())))
        families = await _press(bot, bot._callback("store", "families", PROVIDER))
        monthly_button = next(b for b in _buttons(families) if "VPS" in b.text)
        cities = await _press(bot, monthly_button.callback_data or "")
        city_buttons = [
            b
            for b in _buttons(cities)
            if b.callback_data and _decode(b.callback_data).screen == "loc_halls"
        ]
        assert len(city_buttons) == 1
        assert "Frankfurt" in city_buttons[0].text

    async def test_pressing_unavailable_hourly_shows_unavailable_screen(self) -> None:
        bot = _ui(_service([_monthly_offer(location_id="FRA-01")]))
        families = await _press(bot, bot._callback("store", "families", PROVIDER))
        hourly_button = next(b for b in _buttons(families) if "Cloud" in b.text)
        screen = await _press(bot, hourly_button.callback_data or "")
        assert screen.text == Translator().t("store.family_unavailable_text")
        back_text = Translator().t("nav.back")
        menu_text = Translator().t("nav.menu")
        decoded = [(b.text, _decode(b.callback_data)) for b in _buttons(screen) if b.callback_data]
        back = next(cb for text, cb in decoded if text == back_text)
        assert back.flow == "store"
        assert back.screen == "families"
        assert back.args == (PROVIDER,)
        menu = next(cb for text, cb in decoded if text == menu_text)
        assert menu.flow == "main"
        assert menu.screen == "menu"

    async def test_pressing_cloud_with_inventory_opens_hourly_cities(self) -> None:
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
                location_id="eu-west-3",
                name="Amsterdam",
                country_code="NL",
                city="Amsterdam",
            ),
        ]
        bot = _ui(
            _service(
                [_monthly_offer(location_id="FRA-01"), _hourly_offer()],
                FakeLocationsRepo(records),
            )
        )
        families = await _press(bot, bot._callback("store", "families", PROVIDER))
        cloud_button = next(b for b in _buttons(families) if "Cloud" in b.text)
        cities = await _press(bot, cloud_button.callback_data or "")
        assert cities.text == Translator().t("store.cloud_locations_title", provider="Leaseweb")
        assert any("Amsterdam" in b.text for b in _buttons(cities))

    async def test_single_configured_family_auto_forwards_to_cities(self) -> None:
        catalog = ProviderCatalog(
            markets={PROVIDER: "foreign"},
            display_names={PROVIDER: "Leaseweb"},
            enabled={},
            families={
                PROVIDER: {
                    "vps": {"billing_model": BILLING_MODEL_MONTHLY, "display_name": "VPS"},
                }
            },
        )
        bot = _ui(
            _service(
                [_monthly_offer(location_id="FRA-01")],
                FakeLocationsRepo(_frankfurt_records()),
                catalog=catalog,
            )
        )
        screen = await _press(bot, bot._callback("store", "families", PROVIDER))
        assert screen.text == Translator().t("store.vps_locations_title")
        assert not any(
            b.callback_data and _decode(b.callback_data).screen == "family"
            for b in _buttons(screen)
        )
        assert any("Frankfurt" in b.text for b in _buttons(screen))


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


class TestFamilyAvailabilityGenericCode:
    """Second pin (besides TestNoGeoBranching): no provider/geo comparisons."""

    def test_no_provider_or_geo_literals_in_storefront_comparisons(self) -> None:
        root = Path(__file__).resolve().parents[2]
        for relative in (
            "src/cloud_platform/modules/checkout/service.py",
            "src/cloud_platform/bot/monthly_ui.py",
        ):
            for literal in _branch_strings(root / relative):
                assert "leaseweb" not in literal.lower(), f"{relative}: {literal!r}"
                assert "FRA" not in literal, f"{relative}: {literal!r}"
                assert "LON" not in literal, f"{relative}: {literal!r}"
                assert "Frankfurt" not in literal, f"{relative}: {literal!r}"
                assert "London" not in literal, f"{relative}: {literal!r}"


class TestConfigLoadedFamilies:
    """Production-shaped TOML families drive the two-row selector.

    The catalog is built from real ``Settings`` (parsed from a TOML file,
    exactly like the container builds it), not from a hand-made catalog —
    so a misconfigured/missing families section would fail here first.
    """

    _TOML = """\
[providers.leaseweb]
enabled = true
market = "foreign"
display_name = "Leaseweb"

[providers.leaseweb.families.vps]
billing_model = "prepaid_monthly_fixed"
display_name = "وی‌پی‌اس"

[providers.leaseweb.families.cloud]
billing_model = "hourly"
display_name = "کلود"
"""

    def _catalog_from_toml(self, tmp_path: Any) -> ProviderCatalog:
        from cloud_platform.core.config import load_settings

        path = tmp_path / "configuration.toml"
        path.write_text(self._TOML, encoding="utf-8")
        settings = load_settings(path)
        assert settings.provider_families["leaseweb"]["vps"]["billing_model"] == (
            "prepaid_monthly_fixed"
        )
        assert settings.provider_families["leaseweb"]["cloud"]["billing_model"] == "hourly"
        # Same construction the container uses for the storefront catalog.
        return ProviderCatalog(
            markets=settings.provider_markets,
            display_names=settings.provider_display_names,
            enabled=settings.providers_enabled,
            families=settings.provider_families,
        )

    async def test_toml_families_render_two_rows_with_zero_hourly(self, tmp_path: Any) -> None:
        catalog = self._catalog_from_toml(tmp_path)
        service = _service(
            [_monthly_offer(location_id="FRA-01")],
            catalog=catalog,
        )
        families, _, _ = await service.families_screen(PROVIDER)
        assert [(f.family_key, f.available) for f in families] == [
            ("vps", True),
            ("cloud", False),
        ]
        assert [f.display_name for f in families] == ["وی‌پی‌اس", "کلود"]
