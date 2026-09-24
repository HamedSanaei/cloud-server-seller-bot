"""Minimal skeleton for pricing_metadata audit field (STOREFRONT-REWORK)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.db.base import Base
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository


def test_domain_dataclass_accepts_pricing_metadata() -> None:
    offer = SellableOffer(
        id=uuid4(),
        provider_key="test",
        product_id="plan-01",
        location_id="loc-01",
        name="Test Offer",
        vcpu=2,
        ram_gb=4,
        disk_gb=20,
        traffic=None,
        provider_cost_minor=1000,
        provider_cost_currency="EUR",
        selling_price_minor=1500,
        selling_currency="USD",
        billing_parameters={},
        provider_available=True,
        enabled=True,
        pricing_metadata={"price_source": "manual", "markup_percent": 50},
    )
    assert offer.pricing_metadata == {"price_source": "manual", "markup_percent": 50}


def test_orm_column_exists() -> None:
    table = Base.metadata.tables.get("sellable_offers")
    assert table is not None
    columns = {c.name for c in table.columns}
    assert "pricing_metadata" in columns


def _offer_row(**overrides: object) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.provider_key = "leaseweb"
    row.product_id = "VPS02_1"
    row.location_id = "AMS-01"
    row.name = "VPS S"
    row.vcpu = 2
    row.ram_gb = 4
    row.disk_gb = 100
    row.traffic = "10 TB"
    row.provider_cost_minor = 999
    row.provider_cost_currency = "EUR"
    row.selling_price_minor = 1299
    row.selling_currency = "EUR"
    row.billing_parameters = {}
    row.technical_metadata = {}
    row.pricing_metadata = {"price_source": "catalog_sync", "markup_percent": 50}
    row.billing_model = "prepaid_monthly_fixed"
    row.provider_available = True
    row.enabled = True
    row.operator_disabled = False
    row.auto_priced = True
    row.created_at = None
    row.updated_at = None
    row.provider_account_id = None
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


def _result(*rows: MagicMock) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.first.return_value = rows[0] if rows else None
    result.scalars.return_value.all.return_value = list(rows)
    result.rowcount = len(rows)
    return result


@pytest.mark.asyncio
async def test_repository_round_trip_preserves_pricing_metadata() -> None:
    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.get.return_value = _offer_row()

    repo = SqlAlchemySellableOfferRepository(lambda: db)
    offer = await repo.get(uuid4())
    assert offer is not None
    assert offer.pricing_metadata == {"price_source": "catalog_sync", "markup_percent": 50}
