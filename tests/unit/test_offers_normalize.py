"""Operator `normalize-selling-currency` flow (safe USD migration).

Auto-priced rows recompute from immutable provider cost + policy markup;
manual rows convert their existing selling amount with no second markup and
never a bare relabel (GBP 8.09 becomes converted USD cents, not $8.09).
Dry-run writes nothing; operator-disabled rows, policy-less rows and manual
domestic rows are skipped with clear reasons; a --target outside the
configured catalog currency is refused.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest

import cloud_platform.cli as cli
from cloud_platform.modules.offers.domain import PricingPolicy, SellableOffer


class _DeterministicRates:
    """Fake frankfurter reference rates (no network)."""

    RATES: ClassVar[dict[tuple[str, str], str]] = {
        ("EUR", "USD"): "1.17",
        ("GBP", "USD"): "1.27",
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def _quote(self, base: str, quote: str) -> Any:
        from cloud_platform.modules.fx.domain import FxReferenceQuote
        from cloud_platform.modules.fx.service import ReferenceRateResolution

        self.calls.append((base, quote))
        now = datetime.now(UTC)
        return ReferenceRateResolution(
            FxReferenceQuote(
                base_currency=base,
                quote_currency=quote,
                rate=Decimal(self.RATES[(base, quote)]),
                source="frankfurter",
                source_market=f"{base}/{quote}",
                provider_date=now.date(),
                observed_at=now,
                expires_at=now + timedelta(hours=1),
            ),
            stale=False,
        )

    async def get_rate(self, base: str, quote: str, *, allow_catalog_stale: bool = False) -> Any:
        return await self._quote(base, quote)

    async def get_catalog_rate(self, base: str, quote: str) -> Any:
        return await self._quote(base, quote)

    async def close(self) -> None:
        return None


class _FakeContainer:
    def __init__(self, rates: Any) -> None:
        self._rates = rates
        self.closed = False

    def global_fx_resolver_or_none(self) -> Any:
        return self._rates

    async def close(self) -> None:
        self.closed = True


def _base_offer(**overrides: Any) -> SellableOffer:
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
        selling_price_minor=0,
        selling_currency="EUR",
        billing_parameters={"provider_monthly_rate": "4.49"},
        billing_model="prepaid_monthly_fixed",
        provider_available=True,
        enabled=True,
        auto_priced=True,
    )
    values.update(overrides)
    return SellableOffer(**values)


def _manual_gbp_offer() -> SellableOffer:
    return _base_offer(
        provider_cost_minor=700,
        provider_cost_currency="GBP",
        selling_price_minor=809,
        selling_currency="GBP",
        billing_parameters={},
        auto_priced=False,
    )


class _BookRepo:
    """In-memory price book honoring CAS semantics."""

    def __init__(self, rows: list[SellableOffer]) -> None:
        self._rows: dict[UUID, SellableOffer] = {row.id: row for row in rows}
        self.auto_prices: list[dict[str, Any]] = []
        self.manual_prices: list[dict[str, Any]] = []

    async def list_all(self) -> list[SellableOffer]:
        return list(self._rows.values())

    async def set_auto_price_if_current(self, offer_id: UUID, **kwargs: Any) -> Any:
        import dataclasses

        row = self._rows[offer_id]
        self.auto_prices.append({"id": offer_id, **kwargs})
        self._rows[offer_id] = dataclasses.replace(
            row,
            selling_price_minor=kwargs["selling_price_minor"],
            selling_currency=kwargs["selling_currency"],
            pricing_metadata=dict(kwargs["pricing_metadata"]),
        )
        return self._rows[offer_id]

    async def set_manual_price(self, offer_id: UUID, *args: Any, **kwargs: Any) -> Any:
        import dataclasses

        row = self._rows[offer_id]
        if args:
            minor, currency, metadata = args[0], args[1], args[2]
        else:
            minor, currency, metadata = (
                kwargs["selling_price_minor"],
                kwargs["selling_currency"],
                kwargs["pricing_metadata"],
            )
        self.manual_prices.append({"id": offer_id, "minor": minor, "currency": currency})
        self._rows[offer_id] = dataclasses.replace(
            row,
            selling_price_minor=minor,
            selling_currency=currency,
            pricing_metadata=dict(metadata),
            auto_priced=False,
        )
        return self._rows[offer_id]


def _patch(
    monkeypatch: pytest.MonkeyPatch, rows: list[SellableOffer]
) -> tuple[_BookRepo, _DeterministicRates]:
    repo = _BookRepo(rows)
    rates = _DeterministicRates()
    monkeypatch.setattr(
        "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
        lambda *a, **k: repo,
    )
    monkeypatch.setattr(
        "cloud_platform.core.container.create_container",
        lambda: _FakeContainer(rates),
    )
    monkeypatch.setattr(
        "cloud_platform.modules.offers.auto_sync.pricing_policies_from_settings",
        lambda settings: {
            "leaseweb": PricingPolicy(mode="markup", markup_percent=30, auto_publish=True)
        },
    )
    return repo, rates


class TestNormalizeDryRun:
    async def test_dry_run_shows_conversion_without_writing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        auto = _base_offer()
        manual = _manual_gbp_offer()
        repo, rates = _patch(monkeypatch, [auto, manual])
        assert await cli.offers_normalize_selling_currency(True, "USD") == 0
        out = capsys.readouterr().out
        # Auto: 4.49 EUR * 1.17 * 1.30 = 6.82929 -> 683 USD cents.
        assert f"WOULD {auto.ref}: 0 EUR -> 683 USD" in out
        # Manual: 8.09 GBP * 1.27 = 10.2743 -> 1028 USD cents, no markup,
        # never a bare relabel of 809.
        assert f"WOULD {manual.ref}: 809 GBP -> 1028 USD" in out
        assert "1028 USD" in out
        assert "FX provenance:" in out and "rate=1.27" in out
        assert "would normalize 2 offer(s) to USD" in out
        assert repo.auto_prices == [] and repo.manual_prices == []
        assert set(rates.calls) == {("EUR", "USD"), ("GBP", "USD")}

    async def test_target_outside_configuration_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _patch(monkeypatch, [_base_offer()])
        assert await cli.offers_normalize_selling_currency(True, "EUR") == 2
        assert "differs from configured catalog currency" in capsys.readouterr().out


class TestNormalizeExecute:
    async def test_execute_normalizes_both_ownerships(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        auto = _base_offer()
        manual = _manual_gbp_offer()
        repo, _rates = _patch(monkeypatch, [auto, manual])
        assert await cli.offers_normalize_selling_currency(False, "USD") == 0
        out = capsys.readouterr().out
        assert f"OK   {auto.ref}: 683 USD" in out
        assert f"OK   {manual.ref}: 1028 USD" in out
        assert "normalized 2 offer(s) to USD" in out
        rows = {row.id: row for row in await repo.list_all()}
        assert rows[auto.id].selling_currency == "USD"
        assert rows[auto.id].selling_price_minor == 683
        assert rows[manual.id].selling_currency == "USD"
        assert rows[manual.id].selling_price_minor == 1028
        # Manual provenance kept: originals + no markup.
        assert rows[manual.id].pricing_metadata["original_selling_price_minor"] == 809
        assert rows[manual.id].pricing_metadata["markup_percent"] == "0"
        assert rows[auto.id].pricing_metadata["markup_percent"] == "30"

    async def test_skips_are_explicit_and_untouched(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        disabled = _base_offer(operator_disabled=True)
        foreign = _base_offer(
            provider_key="hetzner",
            product_id="cx22",
            provider_cost_minor=449,
            billing_parameters={"provider_monthly_rate": "4.49"},
        )
        domestic = _base_offer(
            provider_cost_minor=50000,
            provider_cost_currency="IRT",
            selling_price_minor=50000,
            selling_currency="IRT",
            billing_parameters={},
            auto_priced=False,
        )
        repo, _rates = _patch(monkeypatch, [disabled, foreign, domestic])
        # Skipping INTENT (operator-disabled, no policy, manual domestic) is
        # not a failure: the command exits 0 and says so explicitly.
        assert await cli.offers_normalize_selling_currency(False, "USD") == 0
        out = capsys.readouterr().out
        assert "operator-disabled; left untouched" in out
        assert "no automatic pricing policy" in out
        assert "must remain in IRT" in out
        assert "intentionally skipped: 3; failed: 0" in out
        assert repo.auto_prices == [] and repo.manual_prices == []

    async def test_real_failures_are_reported_and_exit_non_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """A row left fail-closed is a FAILURE, unlike an intentional skip.

        Stored provider cost 1300 minor does not match the verbatim provider
        rate 4.49 EUR (the zero-decimal pitfall), so the pricer refuses to
        guess: the row keeps its old price and the command exits 1.
        """
        broken = _base_offer(provider_cost_minor=1300)
        repo, _rates = _patch(monkeypatch, [broken])
        assert await cli.offers_normalize_selling_currency(False, "USD") == 1
        out = capsys.readouterr().out
        assert f"FAIL {broken.ref}: pricing failed (OfferPricingError)" in out
        assert "left fail-closed" in out
        assert "intentionally skipped: 0; failed: 1" in out
        assert repo.auto_prices == [] and repo.manual_prices == []

    async def test_clean_catalog_reports_current(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        from cloud_platform.modules.offers.domain import PricingPolicy as _Policy
        from cloud_platform.modules.offers.pricing import CatalogOfferPricer

        current = _base_offer()
        priced = await CatalogOfferPricer(_DeterministicRates(), "USD").price_auto(
            current, _Policy(mode="markup", markup_percent=30, auto_publish=True)
        )
        import dataclasses

        current = dataclasses.replace(
            current,
            selling_price_minor=priced.selling_price_minor,
            selling_currency=priced.selling_currency,
            pricing_metadata=dict(priced.pricing_metadata),
        )
        _patch(monkeypatch, [current])
        assert await cli.offers_normalize_selling_currency(True, "USD") == 0
        assert "already use USD" in capsys.readouterr().out
