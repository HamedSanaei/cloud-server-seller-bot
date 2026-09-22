"""Persistence cover for publication fields and sync state (STOREFRONT-V2).

Mocked-session pattern (no database): the SQLAlchemy adapters' mapping and
CRUD logic for ``technical_metadata`` / ``operator_disabled`` /
``auto_priced`` and the per-provider sync-state rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from cloud_platform.modules.offers.domain import OfferSpecUpdate
from cloud_platform.modules.offers.repository import (
    SqlAlchemyCatalogSyncStateRepository,
    SqlAlchemySellableOfferRepository,
)

OFFER_ID = uuid4()


def _db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    mock.refresh = AsyncMock()
    return mock


def _result(*rows: Any) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.first.return_value = rows[0] if rows else None
    result.scalars.return_value.all.return_value = list(rows)
    return result


def _offer_row(**overrides: Any) -> MagicMock:
    row = MagicMock()
    row.id = OFFER_ID
    row.provider_key = "leaseweb"
    row.product_id = "VPS02_1"
    row.location_id = "FRA-01"
    row.name = "VPS 1"
    row.vcpu = 2
    row.ram_gb = 4
    row.disk_gb = 100
    row.traffic = None
    row.provider_cost_minor = 499
    row.provider_cost_currency = "EUR"
    row.selling_price_minor = 0
    row.selling_currency = "EUR"
    row.billing_parameters = {}
    row.technical_metadata = {"architecture": "x86_64"}
    row.provider_available = True
    row.enabled = False
    row.operator_disabled = False
    row.auto_priced = True
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    row.provider_account_id = "default"
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _offers(db: AsyncMock) -> SqlAlchemySellableOfferRepository:
    return SqlAlchemySellableOfferRepository(lambda: db)  # type: ignore[arg-type]


def _state(db: AsyncMock) -> SqlAlchemyCatalogSyncStateRepository:
    return SqlAlchemyCatalogSyncStateRepository(lambda: db)  # type: ignore[arg-type]


class TestPublicationMapping:
    async def test_get_maps_new_fields(self) -> None:
        db = _db()
        db.get.return_value = _offer_row(operator_disabled=True, auto_priced=False)
        offer = await _offers(db).get(OFFER_ID)
        assert offer is not None
        assert offer.technical_metadata == {"architecture": "x86_64"}
        assert offer.operator_disabled is True
        assert offer.auto_priced is False

    async def test_upsert_stores_technical_metadata_on_create(self) -> None:
        db = _db()
        db.execute.return_value = _result()
        offer = await _offers(db).upsert_from_provider(
            provider_key="leaseweb",
            product_id="VPS02_1",
            location_id="FRA-01",
            update=OfferSpecUpdate(
                name="VPS 1",
                vcpu=2,
                ram_gb=4,
                disk_gb=100,
                traffic=None,
                provider_cost_minor=499,
                provider_cost_currency="EUR",
                billing_parameters={},
                technical_metadata={"architecture": "x86_64", "deprecated": False},
                provider_available=True,
            ),
        )
        assert offer.technical_metadata["architecture"] == "x86_64"
        assert offer.enabled is False  # operator-owned, never touched by sync

    async def test_upsert_refreshes_technical_metadata(self) -> None:
        db = _db()
        row = _offer_row()
        db.execute.return_value = _result(row)
        offer = await _offers(db).upsert_from_provider(
            provider_key="leaseweb",
            product_id="VPS02_1",
            location_id="FRA-01",
            update=OfferSpecUpdate(
                name="VPS 1",
                vcpu=4,
                ram_gb=6,
                disk_gb=100,
                traffic=None,
                provider_cost_minor=449,
                provider_cost_currency="EUR",
                billing_parameters={},
                technical_metadata={"storage_type": "NVMe"},
                provider_available=True,
            ),
        )
        assert row.technical_metadata == {"storage_type": "NVMe"}
        assert offer.vcpu == 4

    async def test_upsert_without_technical_metadata_keeps_stored(self) -> None:
        db = _db()
        row = _offer_row()
        db.execute.return_value = _result(row)
        offer = await _offers(db).upsert_from_provider(
            provider_key="leaseweb",
            product_id="VPS02_1",
            location_id="FRA-01",
            update=OfferSpecUpdate(
                name="VPS 1",
                vcpu=2,
                ram_gb=4,
                disk_gb=100,
                traffic=None,
                provider_cost_minor=499,
                provider_cost_currency="EUR",
                billing_parameters={},
                provider_available=True,
            ),
        )
        assert row.technical_metadata == {"architecture": "x86_64"}
        assert offer.technical_metadata == {"architecture": "x86_64"}

    async def test_set_operator_disabled(self) -> None:
        db = _db()
        db.get.return_value = _offer_row()
        disabled = await _offers(db).set_operator_disabled(OFFER_ID, True)
        assert disabled.operator_disabled is True
        cleared = await _offers(db).set_operator_disabled(OFFER_ID, False)
        assert cleared.operator_disabled is False

    async def test_set_auto_priced(self) -> None:
        db = _db()
        db.get.return_value = _offer_row()
        manual = await _offers(db).set_auto_priced(OFFER_ID, False)
        assert manual.auto_priced is False
        auto = await _offers(db).set_auto_priced(OFFER_ID, True)
        assert auto.auto_priced is True

    async def test_unknown_offer_raises(self) -> None:
        import pytest

        from cloud_platform.modules.offers.domain import OfferNotFoundError

        db = _db()
        db.get.return_value = None
        with pytest.raises(OfferNotFoundError):
            await _offers(db).set_operator_disabled(OFFER_ID, True)
        with pytest.raises(OfferNotFoundError):
            await _offers(db).set_auto_priced(OFFER_ID, False)


def _state_row(**overrides: Any) -> MagicMock:
    row = MagicMock()
    row.provider_key = "leaseweb"
    row.last_attempted_at = datetime.now(UTC)
    row.last_success_at = datetime.now(UTC)
    row.discovered = 36
    row.persisted = 35
    row.prices_updated = 35
    row.published = 35
    row.retired = 1
    row.warnings = []
    row.errors = []
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class TestCatalogSyncStateRepository:
    async def test_record_run_creates_row(self) -> None:
        db = _db()
        db.get.return_value = None
        state = await _state(db).record_run(
            provider_key="leaseweb",
            ok=True,
            discovered=36,
            persisted=35,
            prices_updated=35,
            published=35,
            retired=1,
            warnings=(),
            errors=(),
        )
        assert state.provider_key == "leaseweb"
        assert state.last_success_at is not None
        assert state.published == 35
        db.add.assert_called_once()

    async def test_record_run_updates_existing_without_success(self) -> None:
        db = _db()
        row = _state_row()
        db.get.return_value = row
        state = await _state(db).record_run(
            provider_key="leaseweb",
            ok=False,
            discovered=0,
            persisted=0,
            prices_updated=0,
            published=0,
            retired=0,
            warnings=("auth failed",),
            errors=("boom",),
        )
        assert state.last_success_at == row.last_success_at  # preserved on failure
        assert state.warnings == ("auth failed",)
        assert state.errors == ("boom",)
        db.add.assert_not_called()

    async def test_get_hit_and_miss(self) -> None:
        db = _db()
        db.get.return_value = _state_row()
        assert (await _state(db).get("leaseweb")) is not None
        db.get.return_value = None
        assert (await _state(db).get("hetzner")) is None

    async def test_list_all(self) -> None:
        db = _db()
        db.execute.return_value = _result(_state_row(), _state_row(provider_key="hetzner"))
        states = await _state(db).list_all()
        assert [state.provider_key for state in states] == ["leaseweb", "hetzner"]
