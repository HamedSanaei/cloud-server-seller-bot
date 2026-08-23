"""Tests for SqlAlchemyPriceBookRepository (mocked session)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.pricing.domain import (
    DuplicateBookVersionError,
    MarginRule,
    PriceBookVersion,
)
from cloud_platform.modules.pricing.repository import SqlAlchemyPriceBookRepository

T0 = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)


def _row(
    *,
    book: str = "retail-eur",
    version: int = 1,
    effective_at: datetime = T0,
    rules: list[dict[str, object]] | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.book_name = book
    row.version = version
    row.effective_at = effective_at
    row.rules = rules
    if row.rules is None:
        row.rules = [
            {
                "provider": "*",
                "plan": "*",
                "location": "*",
                "margin_factor": "1.1",
                "fixed_minor": 0,
            }
        ]
    row.created_at = T0
    return row


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    return mock


def _repo(db: AsyncMock) -> SqlAlchemyPriceBookRepository:
    return SqlAlchemyPriceBookRepository(lambda: db)  # type: ignore[arg-type]


def _rule() -> MarginRule:
    return MarginRule("*", "cx22", "*", Decimal("1.15"), fixed_minor=5)


def _version_obj() -> PriceBookVersion:
    return PriceBookVersion(book_name="retail-eur", version=1, effective_at=T0, rules=(_rule(),))


class TestCreateVersion:
    async def test_persists_and_returns_domain(self, db: AsyncMock) -> None:
        new_id = uuid4()
        db.refresh = AsyncMock(side_effect=lambda r: setattr(r, "id", new_id))

        created = await _repo(db).create_version(_version_obj())

        assert created.id == new_id
        assert created.book_name == "retail-eur"
        assert created.version == 1
        assert created.rules == (_rule(),)
        db.add.assert_called_once()
        db.commit.assert_awaited_once()
        row = db.add.call_args[0][0]
        # rules are stored as JSON-safe dicts with Decimal-as-string
        assert row.rules == [
            {
                "provider": "*",
                "plan": "cx22",
                "location": "*",
                "margin_factor": "1.15",
                "fixed_minor": 5,
            }
        ]

    async def test_duplicate_version_raises(self, db: AsyncMock) -> None:
        db.commit = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("uq")))

        with pytest.raises(DuplicateBookVersionError, match="already has version"):
            await _repo(db).create_version(_version_obj())
        db.rollback.assert_awaited_once()


class TestListAndGet:
    async def test_list_versions_maps_rows(self, db: AsyncMock) -> None:
        rows = [_row(version=2), _row(version=1)]
        db.execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: rows)))

        versions = await _repo(db).list_versions("retail-eur")

        assert [v.version for v in versions] == [2, 1]
        assert versions[0].rules[0].margin_factor == Decimal("1.1")
        assert versions[0].rules[0].fixed_minor == 0
        # stored naive timestamps are normalized to aware UTC
        assert versions[0].effective_at.tzinfo is not None

    async def test_get_returns_none_when_missing(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: None))
        )
        assert await _repo(db).get("retail-eur", 9) is None

    async def test_get_maps_row(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: _row()))
        )
        version = await _repo(db).get("retail-eur", 1)
        assert version is not None
        assert version.book_name == "retail-eur"
        assert version.version == 1
