"""Mock-session repository tests for the LEASEWEB-MVP tables.

Follows the established mocked-session pattern (test_operations_repository):
the session factory returns an AsyncMock session whose ``execute`` yields
MagicMock result rows, so the SQLAlchemy adapters' mapping/CRUD logic is
covered without a database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.offers.domain import OfferSpecUpdate
from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
from cloud_platform.modules.orders.domain import OrderStatus, ProviderOrder
from cloud_platform.modules.orders.repository import SqlAlchemyProviderOrderRepository
from cloud_platform.modules.renewals.domain import RenewalKind, RenewalRecord, RenewalStatus
from cloud_platform.modules.renewals.repository import (
    SqlAlchemyRenewalNotificationRepository,
    SqlAlchemyRenewalRepository,
)

SERVER_ID = uuid4()
OFFER_ID = uuid4()


@pytest.fixture
def db() -> AsyncMock:
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
    result.rowcount = len(rows)
    return result


def _offer_row(**overrides: Any) -> MagicMock:
    row = MagicMock()
    row.id = OFFER_ID
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
    row.provider_available = True
    row.enabled = True
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


def _order_row(**overrides: Any) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.server_id = SERVER_ID
    row.operation_key = f"order-create:{SERVER_ID}"
    row.provider_key = "leaseweb"
    row.offer_id = OFFER_ID
    row.status = "pending_submit"
    row.provider_order_id = None
    row.delivery_estimate = None
    row.provider_contract_id = None
    row.provider_service_id = None
    row.error = None
    row.attempts = 0
    row.last_polled_at = None
    row.created_at = None
    row.updated_at = None
    row.product_id = "VPS02_1"
    row.location_id = "AMS-01"
    row.os_name = "Ubuntu 24.04"
    row.contract_term = "1_MONTH"
    row.billing_cycle = "1_MONTH"
    row.provider_cost_minor = 999
    row.provider_cost_currency = "EUR"
    row.selling_price_minor = 1299
    row.selling_currency = "EUR"
    row.post_attempted_at = None
    row.settlement_status = "pending"
    row.settlement_attempted_at = None
    row.settlement_attempts = 0
    row.settlement_error = None
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


def _renewal_row(**overrides: Any) -> MagicMock:
    row = MagicMock()
    row.server_id = SERVER_ID
    row.provider_contract_id = "C-1"
    row.provider_order_ref = "LS-ORD-1"
    row.purchased_at = datetime.now(UTC) - timedelta(days=10)
    row.provider_renewal_at = datetime.now(UTC) + timedelta(days=20)
    row.renewal_date_estimated = False
    row.customer_price_minor = 1299
    row.currency = "EUR"
    row.status = "active"
    row.auto_charge_enabled = True
    row.last_checked_at = None
    row.created_at = None
    row.updated_at = None
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


def _offer_repo(db: AsyncMock) -> SqlAlchemySellableOfferRepository:
    return SqlAlchemySellableOfferRepository(lambda: db)  # type: ignore[arg-type]


def _order_repo(db: AsyncMock) -> SqlAlchemyProviderOrderRepository:
    return SqlAlchemyProviderOrderRepository(lambda: db)  # type: ignore[arg-type]


def _renewal_repo(db: AsyncMock) -> SqlAlchemyRenewalRepository:
    return SqlAlchemyRenewalRepository(lambda: db)  # type: ignore[arg-type]


class TestSellableOfferRepository:
    async def test_get_returns_domain_offer(self, db: AsyncMock) -> None:
        db.get.return_value = _offer_row()
        offer = await _offer_repo(db).get(OFFER_ID)
        assert offer is not None
        assert offer.id == OFFER_ID
        assert offer.selling_price_minor == 1299
        assert offer.sellable is True

    async def test_get_missing_returns_none(self, db: AsyncMock) -> None:
        db.get.return_value = None
        assert await _offer_repo(db).get(OFFER_ID) is None

    async def test_list_sellable_filters(self, db: AsyncMock) -> None:
        db.execute.return_value = _result(_offer_row(), _offer_row(enabled=False))
        offers = await _offer_repo(db).list_sellable("leaseweb")
        assert len(offers) == 2

    async def test_upsert_from_provider_updates_existing(self, db: AsyncMock) -> None:
        db.execute.return_value = _result(_offer_row())
        offer = await _offer_repo(db).upsert_from_provider(
            provider_key="leaseweb",
            product_id="VPS02_1",
            location_id="AMS-01",
            update=OfferSpecUpdate(
                name="VPS S",
                vcpu=2,
                ram_gb=4,
                disk_gb=100,
                traffic="10 TB",
                provider_cost_minor=999,
                provider_cost_currency="EUR",
                billing_parameters={},
                provider_available=True,
            ),
        )
        assert offer.id == OFFER_ID
        assert offer.provider_available is True

    async def test_upsert_from_provider_creates_new_row(self, db: AsyncMock) -> None:
        db.execute.return_value = _result()  # no existing row -> create path
        offer = await _offer_repo(db).upsert_from_provider(
            provider_key="leaseweb",
            product_id="VPS02_1",
            location_id="AMS-01",
            update=OfferSpecUpdate(
                name="VPS S",
                vcpu=2,
                ram_gb=4,
                disk_gb=100,
                traffic="10 TB",
                provider_cost_minor=999,
                provider_cost_currency="EUR",
                billing_parameters={},
                provider_available=True,
            ),
        )
        assert offer.product_id == "VPS02_1"
        db.add.assert_called_once()

    async def test_mark_unavailable_returns_count(self, db: AsyncMock) -> None:
        db.execute.return_value = _result(_offer_row())
        # The row is NOT in the available set -> flagged unavailable.
        count = await _offer_repo(db).mark_unavailable("leaseweb", set())
        assert count == 1
        rows = db.execute.return_value.scalars.return_value.all.return_value
        assert rows[0].provider_available is False

    async def test_set_enabled_and_price(self, db: AsyncMock) -> None:
        db.get.return_value = _offer_row()
        db.execute.return_value = _result(_offer_row())
        repo = _offer_repo(db)
        offer = await repo.set_enabled(OFFER_ID, False)
        assert offer is not None
        assert offer.enabled is False
        offer = await repo.set_selling_price(OFFER_ID, 1899, "EUR")
        assert offer is not None

    async def test_set_price_validates_currency(self, db: AsyncMock) -> None:
        repo = _offer_repo(db)
        with pytest.raises(ValueError):
            await repo.set_selling_price(OFFER_ID, 0, "EUR")
        with pytest.raises(ValueError):
            await repo.set_selling_price(OFFER_ID, 100, "eur")


class TestProviderOrderRepository:
    async def test_create_round_trips_snapshots(self, db: AsyncMock) -> None:
        db.execute.return_value = _result(_order_row())
        repo = _order_repo(db)
        order = await repo.create(
            server_id=SERVER_ID,
            operation_key=f"order-create:{SERVER_ID}",
            provider_key="leaseweb",
            offer_id=OFFER_ID,
            product_id="VPS02_1",
            location_id="AMS-01",
            os_name="Ubuntu 24.04",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            provider_cost_minor=999,
            provider_cost_currency="EUR",
            selling_price_minor=1299,
            selling_currency="EUR",
        )
        assert order.status is OrderStatus.PENDING_SUBMIT
        assert order.product_id == "VPS02_1"
        assert order.provider_cost_minor == 999
        assert order.selling_price_minor == 1299

    async def test_save_persists_all_snapshot_fields(self, db: AsyncMock) -> None:
        db.execute.return_value = _result(_order_row())
        repo = _order_repo(db)
        order = ProviderOrder(
            id=uuid4(),
            server_id=SERVER_ID,
            operation_key=f"order-create:{SERVER_ID}",
            provider_key="leaseweb",
            offer_id=OFFER_ID,
            status=OrderStatus.OUTCOME_UNKNOWN,
            error="timeout",
            provider_cost_minor=999,
            post_attempted_at=datetime.now(UTC),
        )
        await repo.save(order)
        row = db.execute.return_value.scalars.return_value.first.return_value
        assert row.status == "outcome_unknown"
        assert row.provider_cost_minor == 999
        assert row.post_attempted_at is not None

    async def test_list_statuses(self, db: AsyncMock) -> None:
        row = _order_row(status="outcome_unknown")
        db.execute.side_effect = [_result(row), _result(row), _result()]
        repo = _order_repo(db)
        unknown = await repo.list_outcome_unknown("leaseweb")
        assert len(unknown) == 1
        attention = await repo.list_attention("leaseweb")
        assert len(attention) == 1  # OUTCOME_UNKNOWN is in the attention queue
        open_orders = await repo.list_open("leaseweb")
        assert open_orders == []  # the reconciler must NOT poll unknown outcomes


class TestRenewalRepository:
    async def test_upsert_creates_record(self, db: AsyncMock) -> None:
        db.execute.return_value = _result()
        db.get.return_value = None  # no existing record: create path
        record = RenewalRecord(
            server_id=SERVER_ID,
            provider_contract_id="C-1",
            provider_order_ref="LS-ORD-1",
            purchased_at=datetime.now(UTC),
            provider_renewal_at=datetime.now(UTC) + timedelta(days=20),
            renewal_date_estimated=False,
            customer_price_minor=1299,
            currency="EUR",
            status=RenewalStatus.ACTIVE,
            auto_charge_enabled=True,
        )
        saved = await _renewal_repo(db).upsert(record)
        assert saved.server_id == SERVER_ID

    async def test_list_active_and_attention(self, db: AsyncMock) -> None:
        db.execute.return_value = _result(_renewal_row())
        repo = _renewal_repo(db)
        active = await repo.list_active()
        assert len(active) == 1
        attention = await repo.list_needing_attention()
        assert len(attention) == 1

    async def test_notification_record_deduplicated(self, db: AsyncMock) -> None:
        db.execute.return_value = _result(_renewal_row())
        notif_repo = SqlAlchemyRenewalNotificationRepository(lambda: db)  # type: ignore[arg-type]
        recorded = await notif_repo.record(SERVER_ID, RenewalKind.WARN_7D, datetime.now(UTC))
        assert recorded is True
        db.commit.side_effect = IntegrityError("x", {}, Exception("dup"))
        recorded = await notif_repo.record(SERVER_ID, RenewalKind.WARN_7D, datetime.now(UTC))
        assert recorded is False  # already sent: deduped
