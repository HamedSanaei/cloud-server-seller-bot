"""Tests for the catalog location repository and list_offers (mocked session)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.catalog.repository import (
    SqlAlchemyCatalogRepository,
    SqlAlchemyLocationRepository,
    provider_key_to_uuid,
)


def _provider_row() -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    return row


def _location_row() -> MagicMock:
    row = MagicMock()
    row.location_id = "fsn1"
    row.name = "Falkenstein (Germany)"
    row.country_code = "DE"
    row.city = "Falkenstein"
    row.network_zone = "eu-central"
    return row


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    mock.flush = AsyncMock()
    mock.commit = AsyncMock()
    return mock


def _loc_repo(db: AsyncMock) -> SqlAlchemyLocationRepository:
    return SqlAlchemyLocationRepository(lambda: db)  # type: ignore[arg-type]


class TestLocationUpsert:
    async def test_creates_row_when_missing(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),  # provider missing
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),  # location missing
            ]
        )
        created = await _loc_repo(db).upsert(
            LocationRecord(
                provider_key="hetzner",
                location_id="fsn1",
                name="Falkenstein (Germany)",
                country_code="DE",
            )
        )
        assert created is True
        added = [c.args[0] for c in db.add.call_args_list]
        location_add = next(a for a in added if a.__class__.__name__ == "ProviderLocation")
        assert location_add.location_id == "fsn1"
        assert location_add.country_code == "DE"
        assert location_add.provider_id == provider_key_to_uuid("hetzner")
        db.commit.assert_awaited_once()

    async def test_updates_row_when_present(self, db: AsyncMock) -> None:
        row = _location_row()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: _provider_row())),
                MagicMock(scalars=lambda: MagicMock(first=lambda: row)),
            ]
        )
        created = await _loc_repo(db).upsert(
            LocationRecord(
                provider_key="hetzner",
                location_id="fsn1",
                name="Renamed",
                country_code="DE",
                city="Falkenstein",
            )
        )
        assert created is False
        db.add.assert_not_called()
        assert row.name == "Renamed"

    async def test_deterministic_provider_uuid_when_missing(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),
            ]
        )
        await _loc_repo(db).upsert(
            LocationRecord(provider_key="arvan", location_id="tab1", name="Tehran (Iran)")
        )
        provider_add = next(
            c.args[0] for c in db.add.call_args_list if c.args[0].__class__.__name__ == "Provider"
        )
        assert provider_add.id == provider_key_to_uuid("arvan")
        assert provider_add.name == "arvan"


class TestLocationListForProvider:
    async def test_returns_records_for_provider(self, db: AsyncMock) -> None:
        rows = [_location_row(), _location_row()]
        rows[1].location_id = "nbg1"
        rows[1].name = "Nuremberg (Germany)"
        rows[1].country_code = "DE"
        rows[1].city = "Nuremberg"
        rows[1].network_zone = "eu-central"
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: _provider_row())),
                MagicMock(scalars=lambda: MagicMock(all=lambda: list(rows))),
            ]
        )
        records = await _loc_repo(db).list_for_provider("hetzner")
        assert [r.location_id for r in records] == ["fsn1", "nbg1"]
        assert records[0].city == "Falkenstein"
        assert records[1].provider_key == "hetzner"

    async def test_unknown_provider_yields_empty(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),  # provider missing
                MagicMock(scalars=lambda: MagicMock(all=lambda: [])),
            ]
        )
        assert await _loc_repo(db).list_for_provider("ghost") == []


class TestListOffers:
    async def test_maps_rows_with_provider_name(self, db: AsyncMock) -> None:
        catalog = _location_row()
        catalog.id = uuid4()
        catalog.provider_plan_id = "cx22"
        catalog.provider_location_id = "fsn1"
        catalog.name = "CX22"
        catalog.architecture = "x86"
        catalog.vcpu = 2
        catalog.memory_mb = 4096
        catalog.disk_gb = 40
        catalog.currency = "EUR"
        catalog.price_per_quantum = 219
        catalog.quantum_seconds = 3600
        catalog.enabled = True
        catalog.description = None
        db.execute = AsyncMock(return_value=MagicMock(all=lambda: [(catalog, "hetzner")]))

        offers = await SqlAlchemyCatalogRepository(lambda: db).list_offers()  # type: ignore[arg-type]

        assert len(offers) == 1
        offer = offers[0]
        assert offer.provider_key == "hetzner"
        assert offer.plan_id == "cx22"
        assert offer.location_id == "fsn1"
        assert offer.price_per_quantum == 219
        assert offer.enabled is True

    async def test_empty_catalog(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))
        offers = await SqlAlchemyCatalogRepository(lambda: db).list_offers()  # type: ignore[arg-type]
        assert offers == []
