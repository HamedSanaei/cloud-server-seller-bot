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
    GATE_CURRENCY,
    GATE_DEPRECATED,
    GATE_DISABLED,
    GATE_OPERATOR_DISABLED,
    GATE_PRICING_PENDING,
    GATE_PRICING_PROVENANCE,
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


class _DeterministicEurUsdRates:
    """Fake EUR/USD reference rates (no network): 1.17, frankfurter family."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_rate(self, base: str, quote: str, *, allow_catalog_stale: bool = False) -> Any:
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        from cloud_platform.modules.fx.domain import FxReferenceQuote
        from cloud_platform.modules.fx.service import ReferenceRateResolution

        self.calls.append((base, quote))
        now = datetime.now(UTC)
        return ReferenceRateResolution(
            FxReferenceQuote(
                base_currency=base,
                quote_currency=quote,
                rate=Decimal("1.17"),
                source="frankfurter",
                source_market=f"{base}/{quote}",
                provider_date=now.date(),
                observed_at=now,
                expires_at=now + timedelta(hours=1),
            ),
            stale=False,
        )

    async def get_catalog_rate(self, base: str, quote: str) -> Any:
        return await self.get_rate(base, quote, allow_catalog_stale=True)

    async def close(self) -> None:
        return None


async def _usd_offer(cost_minor: int = 449, markup_percent: int = 30) -> SellableOffer:
    """Production-shaped monthly EUR offer priced to USD with the real pricer.

    The row carries exactly the provenance production validation requires —
    never a hand-duplicated metadata dict.
    """
    import dataclasses
    from decimal import Decimal

    from cloud_platform.modules.offers.domain import PricingPolicy
    from cloud_platform.modules.offers.pricing import CatalogOfferPricer

    base = _offer(
        provider_cost_minor=cost_minor,
        billing_parameters={"provider_monthly_rate": str(Decimal(cost_minor) / 100)},
    )
    priced = await CatalogOfferPricer(_DeterministicEurUsdRates(), "USD").price_auto(
        base, PricingPolicy(mode="markup", markup_percent=markup_percent, auto_publish=True)
    )
    return dataclasses.replace(
        base,
        selling_price_minor=priced.selling_price_minor,
        selling_currency=priced.selling_currency,
        pricing_metadata=dict(priced.pricing_metadata),
    )


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
    def __init__(
        self,
        providers: dict[str, Any],
        account_ids: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._providers = providers
        self._account_ids = account_ids or {}

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def get(self, key: str) -> Any:
        return self._providers[key]

    def accounts(self, key: str) -> tuple[Any, ...]:
        """Credential accounts of a provider (empty when it is not scoped)."""
        if not self._account_ids:
            raise AttributeError("accounts")
        return tuple(
            SimpleNamespace(account_id=account_id, provider_key=key)
            for account_id in self._account_ids.get(key, ())
        )


class _FakeContainer:
    """Minimal container stand-in exposing only what the doctor reads."""

    def __init__(
        self,
        catalog: Any,
        providers: dict[str, Any],
        account_ids: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._catalog = catalog
        self.provider_registry = _FakeRegistry(providers, account_ids)
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
    account_ids: dict[str, tuple[str, ...]] | None = None,
) -> _FakeContainer:
    catalog = ProviderCatalog(
        markets=markets, display_names={}, enabled=dict.fromkeys(markets, True)
    )
    container = _FakeContainer(catalog, providers, account_ids=account_ids)
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
            GATE_PROVIDER_UNAVAILABLE: 1,
            GATE_DISABLED: 1,
            GATE_OPERATOR_DISABLED: 0,
            GATE_PRICING_PENDING: 0,
            GATE_PRICING_PROVENANCE: 0,
            GATE_DEPRECATED: 0,
            GATE_UNPRICED: 1,
        }

    def test_summary_of_nothing_is_all_zero(self) -> None:
        assert visibility_summary([]) == {
            "sellable": 0,
            GATE_PROVIDER_UNAVAILABLE: 0,
            GATE_DISABLED: 0,
            GATE_OPERATOR_DISABLED: 0,
            GATE_PRICING_PENDING: 0,
            GATE_PRICING_PROVENANCE: 0,
            GATE_DEPRECATED: 0,
            GATE_UNPRICED: 0,
        }

    def test_foreign_non_usd_selling_currency_is_blocked(self) -> None:
        # A priced foreign row still selling EUR is not customer-visible.
        summary = visibility_summary([_offer(selling_price_minor=899)], "USD")
        assert summary["sellable"] == 0
        assert summary[GATE_CURRENCY] == 1

    def test_missing_pricing_provenance_is_blocked(self) -> None:
        # USD selling price without an FX audit is legacy/corrupt, not safe.
        summary = visibility_summary(
            [_offer(selling_price_minor=899, selling_currency="USD")], "USD"
        )
        assert summary["sellable"] == 0
        assert summary[GATE_PRICING_PROVENANCE] == 1

    def test_repricing_pending_is_blocked(self) -> None:
        summary = visibility_summary(
            [_offer(pricing_metadata={"fx_repricing_pending": True})], "USD"
        )
        assert summary["sellable"] == 0
        assert summary[GATE_PRICING_PENDING] == 1


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


class _FakeFxContainer:
    """Container stand-in with a deterministic global FX resolver (no network)."""

    def __init__(self, rates: Any) -> None:
        self._rates = rates
        self.closed = False

    def global_fx_resolver_or_none(self) -> Any:
        return self._rates

    async def close(self) -> None:
        self.closed = True


def _patch_fx_container(monkeypatch: pytest.MonkeyPatch) -> _DeterministicEurUsdRates:
    rates = _DeterministicEurUsdRates()
    container = _FakeFxContainer(rates)
    monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
    return rates


class TestPriceBook:
    async def test_prices_unpriced_to_usd_with_deterministic_fx(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        # 4.49 EUR * 1.17 * 1.30 = 6.82929 USD -> 683 ceiling USD cents.
        priced = _offer(id=uuid4(), selling_price_minor=1299)
        unpriced = _offer(
            id=uuid4(),
            provider_cost_minor=449,
            billing_parameters={"provider_monthly_rate": "4.49"},
        )
        repo = _patch_offers_repo(monkeypatch, [priced, unpriced])
        rates = _patch_fx_container(monkeypatch)

        assert await cli.offers_price_book("leaseweb", 30, False, False) == 0

        out = capsys.readouterr().out
        assert "priced 1 offer(s)" in out
        assert rates.calls == [("EUR", "USD")]
        repo.set_auto_price_if_current.assert_awaited_once()
        call = repo.set_auto_price_if_current.await_args
        assert call.args[0] == unpriced.id
        kwargs = call.kwargs
        assert kwargs["selling_price_minor"] == 683  # integer minor units, ceiling
        assert kwargs["selling_currency"] == "USD"  # canonical catalog currency
        assert isinstance(kwargs["selling_price_minor"], int)

    async def test_dry_run_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        repo = _patch_offers_repo(
            monkeypatch,
            [
                _offer(
                    provider_cost_minor=449,
                    billing_parameters={"provider_monthly_rate": "4.49"},
                )
            ],
        )
        _patch_fx_container(monkeypatch)
        assert await cli.offers_price_book("leaseweb", 30, True, False) == 0
        repo.set_auto_price_if_current.assert_not_awaited()
        assert "would price 1 offer(s)" in capsys.readouterr().out

    async def test_disabled_offers_are_left_alone_unless_asked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        repo = _patch_offers_repo(
            monkeypatch,
            [
                _offer(
                    enabled=False,
                    provider_cost_minor=449,
                    billing_parameters={"provider_monthly_rate": "4.49"},
                )
            ],
        )
        _patch_fx_container(monkeypatch)
        assert await cli.offers_price_book("leaseweb", 30, False, False) == 0
        repo.set_auto_price_if_current.assert_not_awaited()
        assert "disabled (use --include-disabled): 1" in capsys.readouterr().out

        assert await cli.offers_price_book("leaseweb", 30, False, True) == 0
        repo.set_auto_price_if_current.assert_awaited_once()

    async def test_missing_or_unknown_cost_is_reported_not_guessed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        repo = _patch_offers_repo(monkeypatch, [_offer(provider_cost_minor=0)])
        _patch_fx_container(monkeypatch)
        assert await cli.offers_price_book("leaseweb", 30, False, False) == 0
        repo.set_auto_price_if_current.assert_not_awaited()
        out = capsys.readouterr().out
        assert "pricing failed (OfferPricingError" in out
        assert "skipped: 1" in out

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
        assert "offers normalize-selling-currency --target USD --dry-run" in text
        assert "would show NO provider" in text

    async def test_priced_catalog_is_ok_and_market_is_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_offers_repo(monkeypatch, [await _usd_offer()])
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

    async def test_legacy_non_usd_row_fails_the_doctor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A still-priced EUR row is not a valid foreign sellable offer: the
        # doctor must fail it and point at normalization, never relabel it.
        _patch_offers_repo(monkeypatch, [_offer(selling_price_minor=899)])
        _patch_container(
            monkeypatch,
            markets={"leaseweb": "foreign"},
            providers={"leaseweb": _ordering_capable()},
        )
        result = await cli.offers_doctor()
        text = "\n".join(result.lines)
        assert not result.ok
        assert "normalize-selling-currency" in text

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
        # A realistic Settings object carrying fake credentials: the doctor
        # reads real configuration (no defensive getattr defaults in
        # diagnostic paths that need financial invariants) yet prints nothing
        # secret-shaped.
        from cloud_platform.core.config import Settings

        settings = Settings(
            leaseweb_api_key=FAKE_LEASEWEB_KEY,  # pragma: allowlist secret
            tetraminator_api_key=FAKE_KEY,  # pragma: allowlist secret
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


def _patch_routes(
    monkeypatch: pytest.MonkeyPatch,
    routes: Any,
) -> None:
    """Stand in for the durable routing table the doctor reads."""

    class _Repo:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def list_for_provider(self, provider_key: str) -> Any:
            if isinstance(routes, Exception):
                raise routes
            return routes

    monkeypatch.setattr(
        "cloud_platform.modules.provider_routes.repository.SqlAlchemyProviderRouteRepository",
        _Repo,
    )


class TestCredentialProvenanceDiagnostic:
    """An available offer must name the credential account that supplies it.

    Production context: 36 Leaseweb offers arrived from the multi-account era
    pinned to the legacy id ``default`` (migration 0037's backfill), while the
    provider really is served by ``sales-org-north`` (FRA) and ``sales-org-uk``
    (LON). The doctor is the surface that has to say so — and it must never
    rewrite the row: the catalog sync owns provenance.
    """

    @staticmethod
    def _stale() -> list[SellableOffer]:
        return [
            _offer(provider_account_id="default", selling_price_minor=899),
            _offer(id=uuid4(), location_id="BBB-02", provider_account_id="default"),
        ]

    async def test_stale_provenance_is_reported_with_the_fix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_offers_repo(monkeypatch, self._stale())
        _patch_routes(
            monkeypatch,
            [SimpleNamespace(location_id="AAA-01", credential_account_id="sales-org-north")],
        )
        _patch_container(
            monkeypatch,
            markets={"leaseweb": "foreign"},
            providers={"leaseweb": _ordering_capable()},
            account_ids={"leaseweb": ("sales-org-north", "sales-org-uk")},
        )
        result = await cli.offers_doctor()
        text = "\n".join(result.lines)
        assert "[WARN] leaseweb: 2 available offer(s) still carry the legacy" in text
        assert "a supplying account is already known for 1 of them" in text
        assert "cloud_platform.cli leaseweb sync-offers" in text
        # Provenance is reported, never silently repaired. These legacy EUR
        # rows are additionally not valid foreign sellables, so the overall
        # verdict is a failure pointing at normalization — the provenance
        # note itself stays a warning, never the cause.
        assert not result.ok
        assert "normalize-selling-currency" in text

    async def test_a_single_legacy_credential_is_not_stale_provenance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deprecated single-key install IS account ``default``."""
        _patch_offers_repo(monkeypatch, self._stale())
        _patch_container(
            monkeypatch,
            markets={"leaseweb": "foreign"},
            providers={"leaseweb": _ordering_capable()},
            account_ids={"leaseweb": ("default",)},
        )
        result = await cli.offers_doctor()
        assert not any("legacy credential provenance" in line for line in result.lines)

    async def test_offers_that_name_their_account_are_not_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_offers_repo(
            monkeypatch,
            [_offer(provider_account_id="sales-org-north", selling_price_minor=899)],
        )
        _patch_container(
            monkeypatch,
            markets={"leaseweb": "foreign"},
            providers={"leaseweb": _ordering_capable()},
            account_ids={"leaseweb": ("sales-org-north", "sales-org-uk")},
        )
        result = await cli.offers_doctor()
        assert not any("legacy credential provenance" in line for line in result.lines)

    async def test_unreadable_routes_are_reported_without_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_offers_repo(monkeypatch, self._stale())
        _patch_routes(monkeypatch, RuntimeError('relation "provider_routes" does not exist'))
        _patch_container(
            monkeypatch,
            markets={"leaseweb": "foreign"},
            providers={"leaseweb": _ordering_capable()},
            account_ids={"leaseweb": ("sales-org-north",)},
        )
        result = await cli.offers_doctor()
        text = "\n".join(result.lines)
        assert "credential routes unreadable (RuntimeError)" in text
        assert "does not exist" not in text
        # The legacy EUR rows additionally fail currency validation; the
        # routes diagnostic itself is non-fatal and non-crashing.
        assert not result.ok


class TestSyncReadiness:
    async def test_unpriced_sync_says_the_catalog_is_empty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch_offers_repo(monkeypatch, [_offer(id=uuid4()) for _ in range(3)])
        await cli._print_storefront_readiness("leaseweb")
        out = capsys.readouterr().out
        assert "stored offers: 3" in out
        assert "NOTHING is on sale" in out
        assert "offers normalize-selling-currency --target USD --dry-run" in out

    async def test_priced_sync_confirms_offers_are_on_sale(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch_offers_repo(monkeypatch, [await _usd_offer()])
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
        self,
        provider_key: str,
        product_id: str,
        price_minor: int | None = None,
        currency: str | None = None,
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
