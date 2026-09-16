"""Catalog visibility: why the storefront is empty, and bulk pricing.

The customer catalog is gated five times (provider market/enabled/ordering
adapter, then offer provider-reported/enabled/priced). These tests pin the
diagnostic that names the failing gate, the sync readiness report, and the
operator bulk-pricing tool — including that it never invents a price, never
reprices an existing one, never crosses currency, and never uses a float.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import cloud_platform.cli as cli
from cloud_platform.modules.checkout.service import (
    MarketOptionView,
    ProductGroupView,
    ProductLocationView,
    ProviderOptionView,
)
from cloud_platform.modules.markets.domain import MARKET_ORDER, Market, ProviderCatalog
from cloud_platform.modules.offers.domain import (
    GATE_DISABLED,
    GATE_PROVIDER_UNAVAILABLE,
    GATE_UNPRICED,
    SellableOffer,
    blocking_gate,
    markup_unit_price,
    visibility_summary,
)

OFFER_ID = uuid4()
FAKE_KEY = "tetra_TEST_SUPER_SECRET_API_KEY"
#: A real-looking Leaseweb key, to prove diagnostics never print credentials.
FAKE_LEASEWEB_KEY = "lsw-secret-key-9876"


def _offer(**overrides: Any) -> SellableOffer:
    values: dict[str, Any] = dict(
        id=OFFER_ID,
        provider_key="leaseweb",
        product_id="VPS02_1",
        location_id="AAA-01",
        name="VPS S",
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic="10 TB",
        provider_cost_minor=449,
        provider_cost_currency="EUR",
        selling_price_minor=0,
        selling_currency="EUR",
        billing_parameters={},
        provider_available=True,
        enabled=True,
    )
    values.update(overrides)
    return SellableOffer(**values)


def _fake_repo_class(repo: Any) -> Any:
    return lambda *a, **k: repo  # type: ignore[assignment]


def _patch_offers_repo(monkeypatch: pytest.MonkeyPatch, rows: list[SellableOffer]) -> AsyncMock:
    repo = AsyncMock()
    repo.list_all = AsyncMock(return_value=rows)
    repo.set_selling_price = AsyncMock(
        side_effect=lambda oid, price, cur: _offer(
            id=oid, selling_price_minor=price, selling_currency=cur
        )
    )
    monkeypatch.setattr(
        "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
        _fake_repo_class(repo),
    )
    return repo


class _FakeRegistry:
    def __init__(self, providers: dict[str, Any]) -> None:
        self._providers = providers

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def get(self, key: str) -> Any:
        return self._providers[key]


class _FakeContainer:
    """Minimal container stand-in exposing only what the doctor reads."""

    def __init__(self, catalog: Any, providers: dict[str, Any]) -> None:
        self._catalog = catalog
        self.provider_registry = _FakeRegistry(providers)
        self.initialized = False
        self.closed = False

    async def initialize(self) -> None:
        self.initialized = True

    def market_catalog(self) -> Any:
        return self._catalog

    async def close(self) -> None:
        self.closed = True


def _patch_container(
    monkeypatch: pytest.MonkeyPatch,
    *,
    markets: dict[str, str],
    providers: dict[str, Any],
) -> _FakeContainer:
    catalog = ProviderCatalog(
        markets=markets, display_names={}, enabled=dict.fromkeys(markets, True)
    )
    container = _FakeContainer(catalog, providers)
    monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
    return container


def _ordering_capable() -> Any:
    """Anything exposing the ordering port (``place_order`` + ``get_order``)."""
    return SimpleNamespace(place_order=lambda *a, **k: None, get_order=lambda *a, **k: None)


class TestBlockingGate:
    def test_priced_and_available_is_sellable(self) -> None:
        assert blocking_gate(_offer(selling_price_minor=899)) is None

    def test_gates_report_in_documented_order(self) -> None:
        # Provider-unavailable wins over the operator's own switches, because
        # it is the gate an operator cannot fix by flipping a flag.
        assert (
            blocking_gate(_offer(selling_price_minor=0, enabled=False, provider_available=False))
            == GATE_PROVIDER_UNAVAILABLE
        )
        assert blocking_gate(_offer(selling_price_minor=0, enabled=False)) == GATE_DISABLED
        assert blocking_gate(_offer(selling_price_minor=0)) == GATE_UNPRICED

    def test_summary_counts_every_gate_with_stable_keys(self) -> None:
        summary = visibility_summary(
            [
                _offer(selling_price_minor=899),
                _offer(id=uuid4(), selling_price_minor=0),
                _offer(id=uuid4(), enabled=False, selling_price_minor=899),
                _offer(id=uuid4(), provider_available=False, selling_price_minor=899),
            ]
        )
        assert summary == {
            "sellable": 1,
            GATE_UNPRICED: 1,
            GATE_DISABLED: 1,
            GATE_PROVIDER_UNAVAILABLE: 1,
        }

    def test_summary_of_nothing_is_all_zero(self) -> None:
        assert visibility_summary([]) == {
            "sellable": 0,
            GATE_UNPRICED: 0,
            GATE_DISABLED: 0,
            GATE_PROVIDER_UNAVAILABLE: 0,
        }


class TestMarkupUnitPrice:
    @pytest.mark.parametrize(
        ("cost", "markup", "expected"),
        [
            (1000, 0, 1000),
            (1000, 100, 2000),
            (1000, 50, 1500),
            (449, 30, 584),  # 583.7 -> rounded UP, never below cost
            (1, 1, 2),
            (333, 33, 443),  # 442.89 -> up
        ],
    )
    def test_integer_markup_rounds_up(self, cost: int, markup: int, expected: int) -> None:
        price = markup_unit_price(cost, markup)
        assert price == expected
        assert isinstance(price, int)

    def test_never_below_cost(self) -> None:
        for cost in (1, 7, 449, 12345):
            assert markup_unit_price(cost, 0) == cost

    def test_rejects_non_positive_cost_and_negative_markup(self) -> None:
        with pytest.raises(ValueError):
            markup_unit_price(0, 30)
        with pytest.raises(ValueError):
            markup_unit_price(-5, 30)
        with pytest.raises(ValueError):
            markup_unit_price(100, -1)


class TestPriceBook:
    async def test_prices_only_unpriced_in_the_cost_currency(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        priced = _offer(id=uuid4(), selling_price_minor=1299)
        unpriced = _offer(id=uuid4(), provider_cost_minor=449)
        repo = _patch_offers_repo(monkeypatch, [priced, unpriced])

        assert await cli.offers_price_book("leaseweb", 30, False, False) == 0

        out = capsys.readouterr().out
        assert "priced 1 offer(s)" in out
        repo.set_selling_price.assert_awaited_once()
        args = repo.set_selling_price.await_args
        assert args.args[0] == unpriced.id
        assert args.args[1] == 584  # integer minor units, rounded up
        assert args.args[2] == "EUR"  # the provider cost currency, never relabelled
        assert isinstance(args.args[1], int)

    async def test_dry_run_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        repo = _patch_offers_repo(monkeypatch, [_offer()])
        assert await cli.offers_price_book("leaseweb", 30, True, False) == 0
        repo.set_selling_price.assert_not_awaited()
        assert "would price 1 offer(s)" in capsys.readouterr().out

    async def test_disabled_offers_are_left_alone_unless_asked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        repo = _patch_offers_repo(monkeypatch, [_offer(enabled=False)])
        assert await cli.offers_price_book("leaseweb", 30, False, False) == 0
        repo.set_selling_price.assert_not_awaited()
        assert "disabled (use --include-disabled): 1" in capsys.readouterr().out

        assert await cli.offers_price_book("leaseweb", 30, False, True) == 0
        repo.set_selling_price.assert_awaited_once()

    async def test_missing_or_unknown_cost_is_reported_not_guessed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        repo = _patch_offers_repo(monkeypatch, [_offer(provider_cost_minor=0)])
        assert await cli.offers_price_book("leaseweb", 30, False, False) == 0
        repo.set_selling_price.assert_not_awaited()
        out = capsys.readouterr().out
        assert "no usable provider cost" in out
        assert "no cost: 1" in out

    async def test_negative_markup_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch_offers_repo(monkeypatch, [_offer()])
        assert await cli.offers_price_book("leaseweb", -5, False, False) == 2
        assert "must not be negative" in capsys.readouterr().out

    async def test_empty_provider_points_at_the_sync(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch_offers_repo(monkeypatch, [])
        assert await cli.offers_price_book("leaseweb", 30, False, False) == 1
        assert "sync the catalog first" in capsys.readouterr().out


class TestOffersDoctor:
    async def test_unpriced_catalog_is_explained_with_the_next_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_offers_repo(monkeypatch, [_offer(), _offer(id=uuid4())])
        _patch_container(
            monkeypatch,
            markets={"leaseweb": "foreign"},
            providers={"leaseweb": _ordering_capable()},
        )
        result = await cli.offers_doctor()
        text = "\n".join(result.lines)
        assert not result.ok
        assert "unpriced=2" in text
        assert "missing a CUSTOMER PRICE" in text
        assert "offers price-book --provider leaseweb --markup-percent 30" in text
        assert "would show NO provider" in text

    async def test_priced_catalog_is_ok_and_market_is_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_offers_repo(monkeypatch, [_offer(selling_price_minor=899)])
        _patch_container(
            monkeypatch,
            markets={"leaseweb": "foreign"},
            providers={"leaseweb": _ordering_capable()},
        )
        result = await cli.offers_doctor()
        text = "\n".join(result.lines)
        assert result.ok
        assert "sellable=1" in text
        assert f"market {Market.FOREIGN.value}: leaseweb" in text

    async def test_empty_price_book_points_at_the_sync(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_offers_repo(monkeypatch, [])
        _patch_container(monkeypatch, markets={}, providers={})
        result = await cli.offers_doctor()
        assert not result.ok
        assert any("price book is EMPTY" in line for line in result.lines)
        assert any("leaseweb sync-offers" in line for line in result.lines)

    async def test_provider_without_market_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_offers_repo(monkeypatch, [_offer(selling_price_minor=899)])
        _patch_container(
            monkeypatch,
            markets={},
            providers={"leaseweb": _ordering_capable()},
        )
        result = await cli.offers_doctor()
        assert not result.ok
        assert any("has no market configured" in line for line in result.lines)

    async def test_provider_without_ordering_adapter_is_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_offers_repo(monkeypatch, [_offer(selling_price_minor=899)])
        _patch_container(
            monkeypatch,
            markets={"leaseweb": "foreign"},
            providers={"leaseweb": SimpleNamespace()},
        )
        result = await cli.offers_doctor()
        assert any("no ordering adapter" in line for line in result.lines)

    async def test_unreadable_price_book_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = AsyncMock()
        repo.list_all = AsyncMock(side_effect=RuntimeError("db down"))
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            _fake_repo_class(repo),
        )
        result = await cli.offers_doctor()
        assert not result.ok
        assert any("could not be read (RuntimeError)" in line for line in result.lines)

    async def test_configuration_errors_never_dump_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A settings ValidationError embeds its input — never print that."""
        repo = AsyncMock()
        repo.list_all = AsyncMock(side_effect=ValueError(f"bad value: api_key={FAKE_KEY}"))
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            _fake_repo_class(repo),
        )
        result = await cli.offers_doctor()
        text = "\n".join(result.lines)
        assert FAKE_KEY not in text
        assert "ValueError" in text

    async def test_never_prints_a_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = SimpleNamespace(
            leaseweb_api_key=FAKE_LEASEWEB_KEY,
            tetraminator_api_key=FAKE_KEY,
        )
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        _patch_offers_repo(monkeypatch, [_offer()])
        _patch_container(
            monkeypatch,
            markets={"leaseweb": "foreign"},
            providers={"leaseweb": _ordering_capable()},
        )
        result = await cli.offers_doctor()
        text = "\n".join(result.lines)
        assert FAKE_KEY not in text
        assert FAKE_LEASEWEB_KEY not in text


class TestSyncReadiness:
    async def test_unpriced_sync_says_the_catalog_is_empty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch_offers_repo(monkeypatch, [_offer(id=uuid4()) for _ in range(3)])
        await cli._print_storefront_readiness("leaseweb")
        out = capsys.readouterr().out
        assert "stored offers: 3" in out
        assert "NOTHING is on sale" in out
        assert "offers price-book --provider leaseweb --markup-percent 30" in out

    async def test_priced_sync_confirms_offers_are_on_sale(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch_offers_repo(monkeypatch, [_offer(selling_price_minor=899)])
        await cli._print_storefront_readiness("leaseweb")
        out = capsys.readouterr().out
        assert "sellable=1" in out
        assert "1 offer(s) are on sale now" in out

    async def test_unreachable_database_does_not_break_the_sync_report(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        repo = AsyncMock()
        repo.list_all = AsyncMock(side_effect=RuntimeError("db down"))
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            _fake_repo_class(repo),
        )
        await cli._print_storefront_readiness("leaseweb")
        assert "Storefront readiness: unavailable (RuntimeError)" in capsys.readouterr().out


class _FakeView:
    """Stands in for the view service the Telegram UI also walks."""

    def __init__(
        self,
        *,
        providers: list[Any],
        groups: dict[str, list[Any]],
        locations: dict[str, list[Any]],
        providers_error: Exception | None = None,
    ) -> None:
        self._providers = providers
        self._groups = groups
        self._locations = locations
        self._providers_error = providers_error
        self.products_calls: list[str] = []

    def markets_screen(self) -> list[Any]:
        return [
            MarketOptionView(
                market=market.value,
                label_key="",
                title_key="",
                select_callback="",
            )
            for market in MARKET_ORDER
        ]

    async def providers_screen(self, market: str) -> tuple[list[Any], str]:
        if self._providers_error is not None:
            raise self._providers_error
        return list(self._providers), "back"

    async def products_screen(self, provider_key: str) -> tuple[list[Any], str, str]:
        self.products_calls.append(provider_key)
        return list(self._groups.get(provider_key, [])), "back", "cancel"

    async def product_locations_screen(
        self, provider_key: str, product_id: str, price_minor: int | None = None
    ) -> tuple[list[Any], str, str]:
        return list(self._locations.get(product_id, [])), "back", "cancel"


def _group(product_id: str = "VPS02_1", price: int = 584) -> Any:
    return ProductGroupView(
        product_id=product_id,
        name="VPS S",
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic="10 TB",
        monthly_price_minor=price,
        currency="EUR",
        locations=("AAA-01", "BBB-02"),
        select_callback="cb",
    )


def _location(location_id: str = "AAA-01", price: int = 584) -> Any:
    return ProductLocationView(
        location_id=location_id,
        offer_id=uuid4(),
        name="Frankfurt",
        country_code="DE",
        product_name="VPS S",
        monthly_price_minor=price,
        currency="EUR",
        select_callback="cb",
    )


def _patch_view(monkeypatch: pytest.MonkeyPatch, view: _FakeView) -> _FakeContainer:
    container = _FakeContainer(ProviderCatalog(markets={}), {})
    container.offer_catalog_view_service = lambda: view  # type: ignore[attr-defined]
    monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
    return container


class TestCatalogPreview:
    async def test_prints_the_same_cards_the_bot_builds(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        provider = ProviderOptionView(
            provider_key="leaseweb",
            market="foreign",
            display_name="Leaseweb",
            buyable=True,
            offer_count=2,
            select_callback="cb",
        )
        view = _FakeView(
            providers=[provider],
            groups={"leaseweb": [_group()]},
            locations={"VPS02_1": [_location(), _location("BBB-02")]},
        )
        _patch_view(monkeypatch, view)

        assert await cli.offers_preview(None) == 0
        out = capsys.readouterr().out
        assert "Leaseweb (leaseweb) — buyable, 2 sellable offer(s)" in out
        assert "VPS S — €5.84/month — 2 vCPU / 4 GB RAM / 100 GB disk" in out
        assert "Frankfurt AAA-01 (DE) — €5.84" in out
        assert "Frankfurt BBB-02 (DE) — €5.84" in out

    async def test_market_filter_skips_the_other_market(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch_view(monkeypatch, _FakeView(providers=[], groups={}, locations={}))
        await cli.offers_preview("iran")
        out = capsys.readouterr().out
        assert "market iran" in out
        assert f"market {Market.FOREIGN.value}" not in out

    async def test_empty_catalog_points_at_the_doctor(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch_view(monkeypatch, _FakeView(providers=[], groups={}, locations={}))
        assert await cli.offers_preview(None) == 1
        assert "no product card is visible" in capsys.readouterr().out

    async def test_provider_that_cannot_sell_is_shown_as_soon(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        provider = ProviderOptionView(
            provider_key="leaseweb",
            market="foreign",
            display_name="Leaseweb",
            buyable=False,
            offer_count=0,
            select_callback=None,
        )
        view = _FakeView(providers=[provider], groups={}, locations={})
        _patch_view(monkeypatch, view)
        assert await cli.offers_preview(None) == 1
        assert "shown as soon" in capsys.readouterr().out
        assert view.products_calls == []

    async def test_unavailable_market_is_reported_not_crashed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        from cloud_platform.modules.checkout.service import OfferUnavailableError

        _patch_view(
            monkeypatch,
            _FakeView(
                providers=[],
                groups={},
                locations={},
                providers_error=OfferUnavailableError("unknown market"),
            ),
        )
        assert await cli.offers_preview("iran") == 1
        assert "unavailable: unknown market" in capsys.readouterr().out


class TestCliWiring:
    def test_price_book_requires_an_explicit_markup(self) -> None:
        parser = cli._parser()  # type: ignore[attr-defined]
        args = parser.parse_args(
            ["offers", "price-book", "--provider", "leaseweb", "--markup-percent", "30"]
        )
        assert args.subcommand == "price-book"
        assert args.markup_percent == 30
        assert args.dry_run is False
        assert args.include_disabled is False

    def test_price_book_without_markup_is_refused(self) -> None:
        parser = cli._parser()  # type: ignore[attr-defined]
        with pytest.raises(SystemExit):
            parser.parse_args(["offers", "price-book"])

    def test_doctor_subcommand_is_wired(self) -> None:
        parser = cli._parser()  # type: ignore[attr-defined]
        assert parser.parse_args(["offers", "doctor"]).subcommand == "doctor"

    def test_preview_accepts_a_market_filter(self) -> None:
        parser = cli._parser()  # type: ignore[attr-defined]
        assert parser.parse_args(["offers", "preview"]).market is None
        assert parser.parse_args(["offers", "preview", "--market", "iran"]).market == "iran"
        with pytest.raises(SystemExit):
            parser.parse_args(["offers", "preview", "--market", "moon"])
