"""Tests for SqlAlchemyCatalogRepository (mocked session)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.modules.catalog.domain import CatalogEntrySpec
from cloud_platform.modules.catalog.repository import (
    SqlAlchemyCatalogRepository,
    provider_key_to_uuid,
)


def _spec(**overrides: object) -> CatalogEntrySpec:
    defaults: dict[str, object] = {
        "provider_key": "hetzner",
        "plan_id": "cx22",
        "location_id": "fsn1",
        "name": "CX22",
        "architecture": "x86",
        "vcpu": 2,
        "memory_mb": 4096,
        "disk_gb": 40,
        "currency": "EUR",
        "price_per_quantum": 2,
    }
    defaults.update(overrides)
    return CatalogEntrySpec(**defaults)  # type: ignore[arg-type]


def _provider_row() -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.name = "hetzner"
    return row


def _catalog_row() -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.name = "CX22"
    row.provider_plan_id = "cx22"
    row.provider_location_id = "fsn1"
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


def _repo(db: AsyncMock) -> SqlAlchemyCatalogRepository:
    return SqlAlchemyCatalogRepository(lambda: db)  # type: ignore[arg-type]


class TestProviderKeyToUuid:
    def test_deterministic(self) -> None:
        assert provider_key_to_uuid("hetzner") == provider_key_to_uuid("hetzner")

    def test_distinct_keys_distinct_uuids(self) -> None:
        assert provider_key_to_uuid("hetzner") != provider_key_to_uuid("iran-cloud")

    def test_is_valid_uuid(self) -> None:
        value = provider_key_to_uuid("hetzner")
        assert isinstance(value, uuid.UUID)
        assert value.version == 5


class TestUpsertEntry:
    async def test_creates_row_when_missing(self, db: AsyncMock) -> None:
        provider = _provider_row()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: provider)),
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),
            ]
        )

        created = await _repo(db).upsert_entry(_spec())

        assert created is True
        db.add.assert_called_once()
        db.flush.assert_not_awaited()  # provider existed, no flush needed
        db.commit.assert_awaited_once()
        added = db.add.call_args[0][0]
        assert added.provider_plan_id == "cx22"
        assert added.provider_location_id == "fsn1"
        assert added.price_per_quantum == 2
        assert added.provider_id == provider.id

    async def test_updates_row_when_present(self, db: AsyncMock) -> None:
        provider = _provider_row()
        row = _catalog_row()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: provider)),
                MagicMock(scalars=lambda: MagicMock(first=lambda: row)),
            ]
        )

        created = await _repo(db).upsert_entry(_spec(price_per_quantum=7))

        assert created is False
        db.add.assert_not_called()
        db.commit.assert_awaited_once()
        assert row.price_per_quantum == 7
        assert row.name == "CX22"

    async def test_creates_provider_with_deterministic_uuid_when_missing(
        self, db: AsyncMock
    ) -> None:
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),
            ]
        )

        created = await _repo(db).upsert_entry(_spec())

        assert created is True
        db.flush.assert_awaited_once()  # new provider flushed
        added = [c.args[0] for c in db.add.call_args_list]
        provider_add = next(a for a in added if a.__class__.__name__ == "Provider")
        catalog_add = next(a for a in added if a.__class__.__name__ == "Catalog")
        assert provider_add.id == provider_key_to_uuid("hetzner")
        assert provider_add.name == "hetzner"
        assert catalog_add.provider_id == provider_key_to_uuid("hetzner")

    async def test_spec_metadata_forwarded(self, db: AsyncMock) -> None:
        provider = _provider_row()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: provider)),
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),
            ]
        )

        await _repo(db).upsert_entry(_spec(description="desc", extra_metadata={"a": 1}))

        added = db.add.call_args[0][0]
        assert added.description == "desc"
        assert added.extra_metadata == {"a": 1}
