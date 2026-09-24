"""Unit tests for provider-neutral USD pricing in auto_sync.

These tests verify the current P0 pricing contract:
- Foreign offers are converted to the canonical USD selling currency.
- Domestic offers retain their native currency and still receive identity
  pricing provenance.
- Exact provider rates and current pricing metadata are persisted through CAS
  methods.
- FX failures leave a retained row unpriced and auditable.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from cloud_platform.modules.fx.domain import FxReferenceQuote
from cloud_platform.modules.fx.service import ReferenceRateResolution
from cloud_platform.modules.offers.auto_sync import CatalogAutoSyncCoordinator
from cloud_platform.modules.offers.domain import (
    CatalogSyncReport,
    PricingPolicy,
    SellableOffer,
)

pytestmark = []


@pytest.fixture
def mock_policy() -> PricingPolicy:
    return PricingPolicy(
        mode="markup",
        markup_percent=30,
        auto_publish=True,
    )


def _offer(
    *,
    offer_id: str,
    product_id: str,
    location_id: str,
    name: str,
    cost_minor: int,
    cost_currency: str,
    billing_parameters: dict[str, object],
    selling_currency: str,
) -> SellableOffer:
    return SellableOffer(
        id=UUID(offer_id),
        provider_key="leaseweb",
        product_id=product_id,
        location_id=location_id,
        name=name,
        vcpu=1,
        ram_gb=2,
        disk_gb=10,
        traffic=100,
        provider_cost_minor=cost_minor,
        provider_cost_currency=cost_currency,
        selling_price_minor=0,
        selling_currency=selling_currency,
        billing_parameters=billing_parameters,
        technical_metadata={},
        billing_model="prepaid_monthly_fixed",
        provider_available=True,
        enabled=True,
        operator_disabled=False,
        auto_priced=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        provider_account_id=None,
    )


@pytest.fixture
def mock_row_irt() -> SellableOffer:
    return _offer(
        offer_id="00000000-0000-0000-0000-000000000001",
        product_id="product-ir",
        location_id="loc-ir",
        name="IRT offer",
        cost_minor=1000,
        cost_currency="IRT",
        billing_parameters={"provider_monthly_rate": "1000"},
        selling_currency="IRT",
    )


@pytest.fixture
def mock_row_usd() -> SellableOffer:
    return _offer(
        offer_id="00000000-0000-0000-0000-000000000002",
        product_id="product-us",
        location_id="loc-us",
        name="EUR offer",
        cost_minor=5000,
        cost_currency="EUR",
        billing_parameters={"provider_monthly_rate": "50"},
        selling_currency="USD",
    )


@pytest.fixture
def mock_row_fx_disabled() -> SellableOffer:
    return _offer(
        offer_id="00000000-0000-0000-0000-000000000003",
        product_id="product-nfx",
        location_id="loc-nfx",
        name="No FX offer",
        cost_minor=3000,
        cost_currency="GBP",
        billing_parameters={"provider_monthly_rate": "30"},
        selling_currency="USD",
    )


def _report(*verified: tuple[str, str]) -> CatalogSyncReport:
    pairs = frozenset(verified)
    return CatalogSyncReport(
        provider_key="leaseweb",
        ok=True,
        complete=True,
        discovered=len(pairs),
        persisted=len(pairs),
        verified=pairs,
        billing_model="prepaid_monthly_fixed",
    )


def _reference_resolution(*, rate: str = "1.10", stale: bool = False) -> ReferenceRateResolution:
    now = datetime.now(UTC)
    quote = FxReferenceQuote(
        base_currency="EUR",
        quote_currency="USD",
        rate=Decimal(rate),
        source="frankfurter",
        source_market="EUR/USD",
        provider_date=now.date(),
        observed_at=now,
        expires_at=now + timedelta(hours=1),
    )
    return ReferenceRateResolution(quote, stale=stale)


def _reference_rates(*, stale: bool = False) -> MagicMock:
    rates = MagicMock()
    resolution = _reference_resolution(stale=stale)
    rates.get_rate = AsyncMock(return_value=resolution)
    rates.get_catalog_rate = AsyncMock(return_value=resolution)
    rates.catalog_stale_limit = 3600
    return rates


def _coordinator(
    *,
    policy: PricingPolicy,
    reference_rates: MagicMock | None = None,
) -> CatalogAutoSyncCoordinator:
    return CatalogAutoSyncCoordinator(
        sources=[],
        offers=MagicMock(),
        state=MagicMock(),
        lock=MagicMock(),
        pricing_policies={"leaseweb": policy},
        reference_rates=reference_rates,
    )


async def test_domestic_offer_receives_identity_provenance(
    mock_row_irt: SellableOffer,
    mock_policy: PricingPolicy,
) -> None:
    """Domestic IRT offers keep IRT and still get exact-rate audit metadata."""
    coordinator = _coordinator(policy=mock_policy)
    coordinator._offers.get_by_ref = AsyncMock(return_value=mock_row_irt)
    coordinator._offers.set_auto_price_if_current = AsyncMock(return_value=mock_row_irt)

    updated = await coordinator._auto_price("leaseweb", _report(("product-ir", "loc-ir")), [])

    assert updated == 1
    call = coordinator._offers.set_auto_price_if_current.call_args
    assert call.args[0] == mock_row_irt.id
    assert call.kwargs["selling_price_minor"] == 1300
    assert call.kwargs["selling_currency"] == "IRT"
    metadata = call.kwargs["pricing_metadata"]
    assert metadata["provider_monthly_rate"] == "1000"
    assert metadata["fx_rate"] == "1"
    assert metadata["fx_provider"] == "identity"


async def test_foreign_offer_converted_to_usd_with_fx(
    mock_row_usd: SellableOffer,
    mock_policy: PricingPolicy,
) -> None:
    """A foreign EUR offer is converted to USD through current FX provenance."""
    coordinator = _coordinator(policy=mock_policy, reference_rates=_reference_rates())
    coordinator._offers.get_by_ref = AsyncMock(return_value=mock_row_usd)
    coordinator._offers.set_auto_price_if_current = AsyncMock(return_value=mock_row_usd)

    updated = await coordinator._auto_price("leaseweb", _report(("product-us", "loc-us")), [])

    assert updated == 1
    call = coordinator._offers.set_auto_price_if_current.call_args
    assert call.args[0] == mock_row_usd.id
    assert call.kwargs["selling_price_minor"] == 7150
    assert call.kwargs["selling_currency"] == "USD"
    metadata = call.kwargs["pricing_metadata"]
    assert metadata["provider_cost_minor"] == 5000
    assert metadata["provider_cost_currency"] == "EUR"
    assert metadata["provider_monthly_rate"] == "50"
    assert metadata["fx_rate"] == "1.10"
    assert metadata["fx_provider"] == "frankfurter"
    assert metadata["fx_source_market"] == "EUR/USD"
    assert metadata["fx_purpose"] == "charge"


async def test_foreign_offer_skipped_when_fx_disabled(
    mock_row_fx_disabled: SellableOffer,
    mock_policy: PricingPolicy,
) -> None:
    """An unavailable FX resolver leaves the row unpriced and auditable."""
    coordinator = _coordinator(policy=mock_policy, reference_rates=None)
    coordinator._offers.get_by_ref = AsyncMock(return_value=mock_row_fx_disabled)
    coordinator._offers.set_auto_price_if_current = AsyncMock()
    coordinator._offers.record_auto_pricing_failure_if_current = AsyncMock(
        return_value=mock_row_fx_disabled
    )

    warnings: list[str] = []
    updated = await coordinator._auto_price(
        "leaseweb", _report(("product-nfx", "loc-nfx")), warnings
    )

    assert updated == 0
    assert any("FX unavailable for GBP->USD" in warning for warning in warnings)
    coordinator._offers.set_auto_price_if_current.assert_not_called()
    failure = coordinator._offers.record_auto_pricing_failure_if_current.call_args
    assert failure.args[0] == mock_row_fx_disabled.id
    assert failure.kwargs["preserve_valid_price"] is False


async def test_fx_stale_quote_is_bounded_and_priced(
    mock_row_usd: SellableOffer,
    mock_policy: PricingPolicy,
) -> None:
    """Bounded stale reference rates remain usable for catalog repricing."""
    coordinator = _coordinator(policy=mock_policy, reference_rates=_reference_rates(stale=True))
    coordinator._offers.get_by_ref = AsyncMock(return_value=mock_row_usd)
    coordinator._offers.set_auto_price_if_current = AsyncMock(return_value=mock_row_usd)

    updated = await coordinator._auto_price("leaseweb", _report(("product-us", "loc-us")), [])

    assert updated == 1
    metadata = coordinator._offers.set_auto_price_if_current.call_args.kwargs["pricing_metadata"]
    assert metadata["fx_stale"] is True
    assert metadata["fx_stale_limit_seconds"] == 3600


async def test_domestic_offer_stores_metadata(
    mock_row_irt: SellableOffer,
    mock_policy: PricingPolicy,
) -> None:
    """The CAS write includes valid identity provenance for domestic rows too."""
    coordinator = _coordinator(policy=mock_policy)
    coordinator._offers.get_by_ref = AsyncMock(return_value=mock_row_irt)
    coordinator._offers.set_auto_price_if_current = AsyncMock(return_value=mock_row_irt)

    await coordinator._auto_price("leaseweb", _report(("product-ir", "loc-ir")), [])

    metadata = coordinator._offers.set_auto_price_if_current.call_args.kwargs["pricing_metadata"]
    assert metadata["pricing_schema_version"] == 1
    assert metadata["source_amount"] == "1000"
    assert metadata["source_amount_basis"] == "exact_provider_rate_major"
    assert metadata["final_selling_price_minor"] == 1300


async def test_automation_disabled_offers_are_skipped(
    mock_row_usd: SellableOffer,
    mock_policy: PricingPolicy,
) -> None:
    """Operator-disabled offers are never touched by auto-price."""
    disabled_row = replace(mock_row_usd, operator_disabled=True)
    coordinator = _coordinator(policy=mock_policy, reference_rates=_reference_rates())
    coordinator._offers.get_by_ref = AsyncMock(return_value=disabled_row)
    coordinator._offers.set_auto_price_if_current = AsyncMock()

    updated = await coordinator._auto_price("leaseweb", _report(("product-us", "loc-us")), [])

    assert updated == 0
    coordinator._offers.set_auto_price_if_current.assert_not_called()


async def test_manual_prices_are_not_repriced(
    mock_row_usd: SellableOffer,
    mock_policy: PricingPolicy,
) -> None:
    """Existing manual prices are left untouched even when the cost changed."""
    manual_row = replace(
        mock_row_usd,
        selling_price_minor=8000,
        selling_currency="USD",
        auto_priced=False,
    )
    coordinator = _coordinator(policy=mock_policy, reference_rates=_reference_rates())
    coordinator._offers.get_by_ref = AsyncMock(return_value=manual_row)
    coordinator._offers.set_auto_price_if_current = AsyncMock()

    updated = await coordinator._auto_price("leaseweb", _report(("product-us", "loc-us")), [])

    assert updated == 0
    coordinator._offers.set_auto_price_if_current.assert_not_called()
