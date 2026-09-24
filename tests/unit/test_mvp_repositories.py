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
    result.scalar_one_or_none.return_value = rows[0] if rows else None
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
    row.billing_model = "prepaid_monthly_fixed"
    row.technical_metadata = {}
    row.pricing_metadata = {}
    row.provider_available = True
    row.enabled = True
    row.operator_disabled = False
    row.auto_priced = True
    row.provider_account_id = None
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
        # Manual same-currency rows carry exact-rate provenance, so the
        # provenance gate passes and only the sellable filter applies: the
        # disabled row is filtered, the enabled row is returned.
        db.execute.return_value = _result(
            _offer_row(
                auto_priced=False,
                billing_parameters={"provider_monthly_rate": "9.99"},
            ),
            _offer_row(
                enabled=False,
                auto_priced=False,
                billing_parameters={"provider_monthly_rate": "9.99"},
            ),
        )
        offers = await _offer_repo(db).list_sellable("leaseweb")
        assert len(offers) == 1
        assert offers[0].sellable is True

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

    async def test_provider_sync_preserves_the_operator_price_and_currency(
        self, db: AsyncMock
    ) -> None:
        """SYNC != PRICE: a refresh updates COST, never the storefront.

        Production risk this pins down: a re-sync of a GBP Sales Organization
        must not rewrite the operator's GBP selling price, must not change the
        selling currency, and must not flip enable/disable.
        """
        row = _offer_row(
            provider_cost_minor=999,
            provider_cost_currency="GBP",
            selling_price_minor=1499,
            selling_currency="GBP",
            enabled=True,
            provider_available=True,
        )
        db.execute.return_value = _result(row)
        offer = await _offer_repo(db).upsert_from_provider(
            provider_key="leaseweb",
            product_id="VPS02_1",
            location_id="LON-01",
            update=OfferSpecUpdate(
                name="VPS S",
                vcpu=2,
                ram_gb=4,
                disk_gb=100,
                traffic="10 TB",
                provider_cost_minor=1099,
                provider_cost_currency="GBP",
                billing_parameters={},
                provider_available=True,
            ),
        )
        assert row.provider_cost_minor == 1099
        assert row.provider_cost_currency == "GBP"
        assert row.selling_price_minor == 1499
        assert row.selling_currency == "GBP"
        assert row.enabled is True
        assert offer.id == OFFER_ID

    async def test_a_refresh_repins_legacy_provenance_to_the_supplying_account(
        self, db: AsyncMock
    ) -> None:
        """Production: 36 offers carried migration 0037's legacy `default` pin.

        Current account-scoping: a scoped observation for a different account
        does not mutate the existing differently-pinned row; it creates its
        own account-scoped row. The original `default` pin is left untouched.
        """
        row = _offer_row(provider_account_id="default", provider_cost_currency="GBP")
        db.execute.return_value = _result(row)
        await _offer_repo(db).upsert_from_provider(
            provider_key="leaseweb",
            product_id="VPS02_1",
            location_id="LON-01",
            provider_account_id="sales-org-uk",
            update=OfferSpecUpdate(
                name="VPS S",
                vcpu=2,
                ram_gb=4,
                disk_gb=100,
                traffic="10 TB",
                provider_cost_minor=1099,
                provider_cost_currency="GBP",
                billing_parameters={},
                provider_available=True,
            ),
        )
        # The pre-existing differently-pinned row is not overwritten.
        assert row.provider_account_id == "default"
        # A separate account-scoped row is created for the supplying account.
        created = db.add.call_args.args[0]
        assert created.provider_account_id == "sales-org-uk"
        assert created.provider_cost_currency == "GBP"

    async def test_a_caller_without_account_knowledge_cannot_erase_provenance(
        self, db: AsyncMock
    ) -> None:
        # Account-scoped provenance is enforced: an unscoped observation must
        # not overwrite an account-pinned row.
        row = _offer_row(provider_account_id="sales-org-north")
        db.execute.return_value = _result(row)
        with pytest.raises(ValueError, match="already belongs to a credential account"):
            await _offer_repo(db).upsert_from_provider(
                provider_key="leaseweb",
                product_id="VPS02_1",
                location_id="FRA-01",
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
        assert row.provider_account_id == "sales-org-north"
        db.add.assert_not_called()

    async def test_a_new_offer_is_unpriced_and_disabled(self, db: AsyncMock) -> None:
        """A discovered product must never go on sale by itself."""
        db.execute.return_value = _result()
        await _offer_repo(db).upsert_from_provider(
            provider_key="leaseweb",
            product_id="VPS02_1",
            location_id="LON-01",
            update=OfferSpecUpdate(
                name="VPS S",
                vcpu=2,
                ram_gb=4,
                disk_gb=100,
                traffic="10 TB",
                provider_cost_minor=1099,
                provider_cost_currency="GBP",
                billing_parameters={},
                provider_available=True,
            ),
        )
        created = db.add.call_args.args[0]
        # No selling price is set (the column defaults to 0) and the offer is
        # not enabled; both remain explicit operator decisions.
        assert created.selling_price_minor is None
        assert created.enabled is None
        assert created.provider_cost_currency == "GBP"

    async def test_an_observation_without_a_currency_is_refused(self, db: AsyncMock) -> None:
        """Fail closed: an observation without a currency never reaches storage.

        The hardened domain rejects the empty currency at the OfferSpecUpdate
        boundary ("audited currency"); the repository's own "without a
        provider currency" guard remains as defense-in-depth for any path
        that bypasses the domain constructor.
        """
        db.execute.return_value = _result()
        with pytest.raises(ValueError, match="audited currency"):
            OfferSpecUpdate(
                name="VPS S",
                vcpu=2,
                ram_gb=4,
                disk_gb=100,
                traffic="10 TB",
                provider_cost_minor=1099,
                provider_cost_currency="",
                billing_parameters={},
                provider_available=True,
            )
        db.add.assert_not_called()

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
        # Disabling requires the operator-disabled block under the current
        # visibility contract; direct price writes are idempotent-only (same
        # price/currency succeeds, a different price must go through the
        # audited CAS pipeline).
        db.get.return_value = _offer_row(operator_disabled=True)
        db.execute.return_value = _result(_offer_row(operator_disabled=True))
        repo = _offer_repo(db)
        offer = await repo.set_enabled(OFFER_ID, False)
        assert offer is not None
        assert offer.enabled is False
        assert offer.operator_disabled is True
        db.execute.return_value = _result(_offer_row(operator_disabled=True))
        offer = await repo.set_selling_price(OFFER_ID, 1299, "EUR")
        assert offer is not None
        assert offer.selling_price_minor == 1299
        with pytest.raises(ValueError, match="audited pricing provenance"):
            await repo.set_selling_price(OFFER_ID, 1899, "EUR")

    async def test_set_price_validates_currency(self, db: AsyncMock) -> None:
        repo = _offer_repo(db)
        with pytest.raises(ValueError):
            await repo.set_selling_price(OFFER_ID, 0, "EUR")
        with pytest.raises(ValueError):
            await repo.set_selling_price(OFFER_ID, 100, "XXX")


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
