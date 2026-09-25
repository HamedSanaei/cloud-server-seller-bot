"""`offers readiness` — the release gate against a GREEN-but-EMPTY storefront.

The global-USD release deployed with green migrations, green services and a
ready API while the customer catalog was EMPTY: 507 stored Leaseweb rows, none
of them sellable because they still carried a legacy selling currency or no FX
provenance. Health checks can never catch that, so the release now asserts the
customer-facing catalog itself.

These tests pin the classification: an enabled, credentialed, auto-priced
provider with stored offers but nothing on sale FAILS; providers the operator
never configured (no market, disabled, no credential, no pricing policy) are
never required to have offers; operator intent (every offer disabled by hand)
is reported, not failed; and an unreadable storefront fails closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

import cloud_platform.cli as cli
from cloud_platform.modules.offers.domain import PricingPolicy, SellableOffer


class _Rates:
    """Deterministic EUR->USD reference rate (no network)."""

    async def get_rate(self, base: str, quote: str, *, allow_catalog_stale: bool = False) -> Any:
        from cloud_platform.modules.fx.domain import FxReferenceQuote
        from cloud_platform.modules.fx.service import ReferenceRateResolution

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


def _legacy_foreign_offer(**overrides: Any) -> SellableOffer:
    """Production shape: priced in the provider's currency, no canonical proof."""
    values: dict[str, Any] = dict(
        id=uuid4(),
        provider_key="leaseweb",
        product_id="VPS02_1",
        location_id="FRA-01",
        name="Leaseweb VPS 1",
        vcpu=4,
        ram_gb=6,
        disk_gb=100,
        traffic="5 TB",
        provider_cost_minor=449,
        provider_cost_currency="EUR",
        selling_price_minor=624,
        selling_currency="EUR",
        billing_parameters={"provider_monthly_rate": "4.49"},
        billing_model="prepaid_monthly_fixed",
        provider_available=True,
        enabled=True,
        auto_priced=True,
    )
    values.update(overrides)
    return SellableOffer(**values)


async def _canonical_offer(**overrides: Any) -> SellableOffer:
    """The same row after the audited normalization (real pricer, fake rate)."""
    import dataclasses

    from cloud_platform.modules.offers.pricing import CatalogOfferPricer

    row = _legacy_foreign_offer(**overrides)
    priced = await CatalogOfferPricer(_Rates(), "USD").price_auto(
        row, PricingPolicy(mode="markup", markup_percent=25, auto_publish=True)
    )
    return dataclasses.replace(
        row,
        selling_price_minor=priced.selling_price_minor,
        selling_currency=priced.selling_currency,
        pricing_metadata=dict(priced.pricing_metadata),
    )


class _Repo:
    def __init__(self, rows: list[SellableOffer], *, fail: bool = False) -> None:
        self._rows = rows
        self._fail = fail

    async def list_all(self) -> list[SellableOffer]:
        if self._fail:
            raise RuntimeError("database is down")
        return list(self._rows)


class _State:
    def __init__(self, provider_key: str, discovered: int) -> None:
        self.provider_key = provider_key
        self.discovered = discovered
        self.persisted = 0
        self.prices_updated = 0
        self.published = 0
        self.retired = 0
        self.warnings: tuple[str, ...] = ()
        self.errors: tuple[str, ...] = ()
        self.last_attempted_at = datetime.now(UTC)
        self.last_success_at = None


class _StateRepo:
    def __init__(self, states: list[_State] | None = None) -> None:
        self._states = states or []

    async def list_all(self) -> list[_State]:
        return list(self._states)


class _Container:
    def __init__(self, catalog: Any, registry_keys: tuple[str, ...]) -> None:
        self._catalog = catalog
        self.provider_registry = type(
            "_Registry", (), {"keys": lambda self_: list(registry_keys)}
        )()
        self.closed = False

    async def initialize(self) -> None:
        return None

    def market_catalog(self) -> Any:
        return self._catalog

    async def close(self) -> None:
        self.closed = True


def _catalog(*, enabled: dict[str, bool] | None = None) -> Any:
    from cloud_platform.modules.markets.domain import ProviderCatalog

    return ProviderCatalog(
        markets={"leaseweb": "foreign", "hetzner": "foreign"},
        display_names={"leaseweb": "Leaseweb", "hetzner": "Hetzner"},
        enabled=dict(enabled or {}),
        families={},
    )


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[SellableOffer],
    *,
    policies: dict[str, Any] | None = None,
    registry_keys: tuple[str, ...] = ("leaseweb",),
    enabled: dict[str, bool] | None = None,
    states: list[_State] | None = None,
    repo_fails: bool = False,
    container_error: Exception | None = None,
) -> None:
    monkeypatch.setattr(
        "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
        lambda *a, **k: _Repo(rows, fail=repo_fails),
    )
    monkeypatch.setattr(
        "cloud_platform.modules.offers.repository.SqlAlchemyCatalogSyncStateRepository",
        lambda *a, **k: _StateRepo(states),
    )
    monkeypatch.setattr(
        "cloud_platform.modules.offers.auto_sync.pricing_policies_from_settings",
        lambda settings: dict(
            policies
            if policies is not None
            else {
                "leaseweb": PricingPolicy(mode="markup", markup_percent=25, auto_publish=True),
                "leaseweb.hourly": PricingPolicy(
                    mode="markup", markup_percent=25, auto_publish=True
                ),
            }
        ),
    )

    def _create() -> Any:
        if container_error is not None:
            raise container_error
        return _Container(_catalog(enabled=enabled), registry_keys)

    monkeypatch.setattr("cloud_platform.core.container.create_container", _create)


class TestStorefrontReadiness:
    async def test_enabled_provider_with_stored_but_unsellable_rows_fails(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        rows = [_legacy_foreign_offer() for _ in range(3)]
        _patch(monkeypatch, rows)
        assert await cli.offers_readiness() == 1
        out = capsys.readouterr().out
        assert "[FAIL] leaseweb: 3 stored offer(s) but ZERO sellable in USD" in out
        assert "storefront readiness: FAIL" in out
        # The operator is told the supported repair, never a manual SQL edit.
        assert "offers normalize-selling-currency --target USD --dry-run" in out

    async def test_one_canonical_offer_is_enough(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        rows = [await _canonical_offer(), _legacy_foreign_offer(), _legacy_foreign_offer()]
        _patch(monkeypatch, rows)
        assert await cli.offers_readiness() == 0
        out = capsys.readouterr().out
        assert "[OK  ] leaseweb: 1 sellable of 3 stored" in out
        assert "storefront readiness: OK" in out

    async def test_disabled_provider_is_never_required_to_have_offers(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch(
            monkeypatch,
            [],
            policies={
                "leaseweb": PricingPolicy(mode="markup", markup_percent=25, auto_publish=True),
                "hetzner": PricingPolicy(mode="markup", markup_percent=25, auto_publish=True),
            },
            registry_keys=("leaseweb", "hetzner"),
            enabled={"hetzner": False},
        )
        assert await cli.offers_readiness() == 0
        out = capsys.readouterr().out
        assert "[SKIP] hetzner: not configured/enabled in the storefront" in out
        assert "storefront readiness: OK" in out

    async def test_uncredentialed_provider_is_not_required_to_have_offers(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        # Hetzner has a pricing policy and stored rows, but no credential is
        # configured for it, so the store cannot be expected to sell through
        # it: its unsellable rows are not a release failure.
        rows = [_legacy_foreign_offer(provider_key="hetzner", product_id="cx22")]
        _patch(
            monkeypatch,
            rows,
            policies={
                "hetzner": PricingPolicy(mode="markup", markup_percent=25, auto_publish=True)
            },
            registry_keys=("leaseweb",),
        )
        assert await cli.offers_readiness() == 0
        out = capsys.readouterr().out
        assert "[SKIP] hetzner: no credential configured" in out
        assert "storefront readiness: OK" in out

    async def test_provider_without_a_pricing_policy_is_not_required(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        rows = [_legacy_foreign_offer()]
        _patch(monkeypatch, rows, policies={})
        assert await cli.offers_readiness() == 0
        out = capsys.readouterr().out
        assert "[SKIP] leaseweb: no automatic pricing policy" in out

    async def test_all_offers_operator_disabled_is_intent_not_an_outage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        rows = [
            _legacy_foreign_offer(operator_disabled=True, selling_price_minor=0),
            _legacy_foreign_offer(operator_disabled=True, selling_price_minor=0),
        ]
        _patch(monkeypatch, rows)
        assert await cli.offers_readiness() == 0
        out = capsys.readouterr().out
        assert "all 2 stored offer(s) are operator-disabled" in out
        assert "storefront readiness: OK" in out

    async def test_provider_that_never_synced_is_only_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch(monkeypatch, [])
        assert await cli.offers_readiness() == 0
        out = capsys.readouterr().out
        assert "no stored offers yet" in out

    async def test_discovery_without_persistence_fails(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch(monkeypatch, [], states=[_State("leaseweb.hourly", discovered=471)])
        assert await cli.offers_readiness() == 1
        out = capsys.readouterr().out
        assert "[FAIL] leaseweb: the last catalog run discovered 471 plan(s)" in out

    async def test_unreadable_price_book_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch(monkeypatch, [], repo_fails=True)
        assert await cli.offers_readiness() == 1
        assert "offer price book could not be read (RuntimeError)" in capsys.readouterr().out

    async def test_unbuildable_container_never_turns_into_a_pass(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        # Without the market catalog the configuration gates cannot be read, so
        # the providers that own stored rows are still evaluated.
        rows = [_legacy_foreign_offer()]
        _patch(monkeypatch, rows, container_error=RuntimeError("no configuration"))
        assert await cli.offers_readiness() == 1
        out = capsys.readouterr().out
        assert "provider registry unavailable (RuntimeError)" in out
        assert "[FAIL] leaseweb: 1 stored offer(s) but ZERO sellable in USD" in out
