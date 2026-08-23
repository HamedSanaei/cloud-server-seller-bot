"""Tests for the SQLAlchemy payment session repository (mocked session)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.payments.domain import (
    DuplicateExternalIdError,
    PaymentSession,
    PaymentSessionStatus,
)
from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository


def _row(session_id=None) -> MagicMock:
    row = MagicMock()
    row.id = session_id or uuid4()
    row.user_id = uuid4()
    row.gateway_key = "zarinpal"
    row.gateway_payment_id = "gw-123"
    row.amount_minor = 50000
    row.currency = "EUR"
    row.status = "pending"
    row.idempotency_key = "dep-key-1"
    row.credited_at = None
    row.created_at = None
    row.updated_at = None
    return row


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    return mock


@pytest.fixture
def repo(db: AsyncMock) -> SqlAlchemyPaymentSessionRepository:
    return SqlAlchemyPaymentSessionRepository(lambda: db)  # type: ignore[arg-type]


def _aggregate() -> PaymentSession:
    return PaymentSession(
        user_id=uuid4(),
        gateway_key="zarinpal",
        amount_minor=50000,
        currency="EUR",
        idempotency_key="dep-key-1",
    )


class TestCreate:
    async def test_adds_commits_and_returns_persisted_session(
        self, repo: SqlAlchemyPaymentSessionRepository, db: AsyncMock
    ) -> None:
        new_id = uuid4()

        def refresh_side_effect(row: object) -> None:
            row.id = new_id  # simulate DB-assigned PK

        db.refresh = AsyncMock(side_effect=refresh_side_effect)

        created = await repo.create(_aggregate())

        db.add.assert_called_once()
        db.commit.assert_awaited_once()
        assert created.id == new_id
        assert created.status is PaymentSessionStatus.PENDING

    async def test_duplicate_external_id_maps_to_domain_error(
        self, repo: SqlAlchemyPaymentSessionRepository, db: AsyncMock
    ) -> None:
        db.commit = AsyncMock(side_effect=IntegrityError("dup", None, Exception()))
        with pytest.raises(DuplicateExternalIdError):
            await repo.create(_aggregate())
        db.rollback.assert_awaited_once()


class TestQueries:
    async def test_get_returns_none_when_missing(
        self, repo: SqlAlchemyPaymentSessionRepository, db: AsyncMock
    ) -> None:
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
        assert await repo.get(uuid4()) is None

    async def test_get_by_external_id_maps_row(
        self, repo: SqlAlchemyPaymentSessionRepository, db: AsyncMock
    ) -> None:
        row = _row()
        row.status = "succeeded"
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: row))

        found = await repo.get_by_external_id("zarinpal", "gw-123")

        assert found is not None
        assert found.status is PaymentSessionStatus.SUCCEEDED
        assert found.gateway_payment_id == "gw-123"
        db.execute.assert_awaited_once()


class TestSave:
    async def test_save_updates_status_and_returns_refreshed(
        self, repo: SqlAlchemyPaymentSessionRepository, db: AsyncMock
    ) -> None:
        row = _row()
        target_id = row.id
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: row))
        db.refresh = AsyncMock(side_effect=lambda r: setattr(r, "status", "succeeded"))

        aggregate = _aggregate()
        object.__setattr__(aggregate, "id", target_id)
        updated = await repo.save(aggregate.mark_succeeded(gateway_payment_id="gw-123"))

        assert updated.id == target_id
        assert updated.status is PaymentSessionStatus.SUCCEEDED
        db.commit.assert_awaited_once()

    async def test_save_missing_session_raises_lookup_error(
        self, repo: SqlAlchemyPaymentSessionRepository, db: AsyncMock
    ) -> None:
        persisted = _aggregate()
        object.__setattr__(persisted, "id", uuid4())
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))

        with pytest.raises(LookupError, match="not found"):
            await repo.save(persisted)
