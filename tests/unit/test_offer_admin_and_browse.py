"""Offer administration and customer browse (STOREFRONT).

Two boundaries matter here and both are pinned below:

- administration: every price/visibility change is admin-authorized, carries a
  reason, is audited, and never derives a selling price from provider cost;
  hiding records an explicit operator block and showing clears it;
- browse: only rows passing ALL THREE gates (provider-reported, enabled,
  priced) exist, and location display metadata comes from the synced
  provider-location rows when present — never from a platform assumption.

Foreign provider costs sell in the canonical catalog currency (USD);
domestic IRT/IRR costs sell natively. Manual EUR on a foreign row is
refused naming USD, and showing requires valid pricing provenance.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.offers.domain import OfferNotFoundError, SellableOffer
from cloud_platform.modules.offers.service import (
    OfferAdminError,
    OfferAdminService,
    OfferBrowseService,
)
from cloud_platform.modules.users.domain import (
    Permission,
    PermissionDeniedError,
    Role,
    User,
    UserStatus,
)

PROVIDER = "leaseweb"
CATALOG_CURRENCY = "USD"


def _user(*, role: Role = Role.ADMIN) -> User:
    return User(
        id=uuid4(),
        username="ops",
        email="ops@example.test",
        status=UserStatus.ACTIVE,
        role=role,
        telegram_user_id=1,
    )


def _exact_monthly_rate(cost_minor: int) -> str:
    # Decimal-only exact native rate text; never float.
    return str(Decimal(cost_minor) / 100)


def _offer(
    *,
    offer_id: UUID | None = None,
    provider_key: str = PROVIDER,
    product_id: str = "VPS02_1",
    location_id: str = "FRA-01",
    name: str = "VPS 1",
    cost_minor: int = 499,
    cost_currency: str = CATALOG_CURRENCY,
    price_minor: int = 624,
    price_currency: str = CATALOG_CURRENCY,
    currency: str | None = None,
    enabled: bool = True,
    operator_disabled: bool = False,
    auto_priced: bool = False,
    available: bool = True,
    billing_parameters: dict[str, object] | None = None,
    technical_metadata: dict[str, object] | None = None,
    pricing_metadata: dict[str, object] | None = None,
) -> SellableOffer:
    # ``currency`` is a legacy single-currency shortcut for both snapshots;
    # explicit cost/price currencies win when given.
    if currency is not None:
        cost_currency = currency
        price_currency = currency
    if billing_parameters is None:
        billing_parameters = {"provider_monthly_rate": _exact_monthly_rate(cost_minor)}
    return SellableOffer(
        id=offer_id or uuid4(),
        provider_key=provider_key,
        product_id=product_id,
        location_id=location_id,
        name=name,
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic=None,
        provider_cost_minor=cost_minor,
        provider_cost_currency=cost_currency,
        selling_price_minor=price_minor,
        selling_currency=price_currency,
        billing_parameters=dict(billing_parameters),
        technical_metadata=dict(technical_metadata or {}),
        pricing_metadata=dict(pricing_metadata or {}),
        provider_available=available,
        enabled=enabled,
        operator_disabled=operator_disabled,
        auto_priced=auto_priced,
        created_at=None,
    )


class FakeOffersRepo:
    """Price book honoring the operator-owned write boundaries."""

    def __init__(self, offers: list[SellableOffer] | None = None) -> None:
        self.rows: dict[UUID, SellableOffer] = {o.id: o for o in offers or []}
        self.calls: list[str] = []

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return self.rows.get(offer_id)

    async def get_by_ref(
        self,
        provider_key: str,
        product_id: str,
        location_id: str,
        provider_account_id: str | None = None,
    ) -> SellableOffer | None:
        for offer in self.rows.values():
            if (
                offer.provider_key == provider_key
                and offer.product_id == product_id
                and offer.location_id == location_id
            ):
                if provider_account_id is None:
                    return offer
                if offer.provider_account_id == provider_account_id:
                    return offer
        return None

    async def set_manual_price(
        self,
        offer_id: UUID,
        selling_price_minor: int,
        currency: str,
        pricing_metadata: dict[str, object],
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        expected_updated_at: object | None = None,
    ) -> SellableOffer | None:
        self.calls.append("set_manual_price")
        row = self.rows[offer_id]
        if (
            row.provider_cost_minor != expected_cost_minor
            or row.provider_cost_currency.strip().upper() != expected_cost_currency.strip().upper()
        ):
            return None
        if expected_updated_at is not None and row.updated_at != expected_updated_at:
            return None
        # A stored manual price opts out of auto-pricing and clears any
        # pending auto-repricing flags by replacing the metadata snapshot.
        updated = replace(
            row,
            selling_price_minor=selling_price_minor,
            selling_currency=currency,
            auto_priced=False,
            pricing_metadata=dict(pricing_metadata),
        )
        self.rows[offer_id] = updated
        return updated

    async def set_visibility_state(
        self, offer_id: UUID, *, enabled: bool, operator_disabled: bool
    ) -> SellableOffer:
        self.calls.append("set_visibility_state")
        if not isinstance(enabled, bool) or not isinstance(operator_disabled, bool):
            raise ValueError("visibility flags must be boolean")
        if not enabled and not operator_disabled:
            raise ValueError("a disabled offer must carry the operator-disabled block")
        updated = replace(self.rows[offer_id], enabled=enabled, operator_disabled=operator_disabled)
        self.rows[offer_id] = updated
        return updated

    async def set_auto_price_if_current(
        self,
        offer_id: UUID,
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        selling_price_minor: int,
        selling_currency: str,
        pricing_metadata: dict[str, object],
        expected_provider_rate: str | None = None,
    ) -> SellableOffer | None:
        self.calls.append("set_auto_price_if_current")
        row = self.rows[offer_id]
        if (
            not row.auto_priced
            or row.operator_disabled
            or row.provider_cost_minor != expected_cost_minor
            or row.provider_cost_currency.strip().upper() != expected_cost_currency.strip().upper()
        ):
            return None
        updated = replace(
            row,
            selling_price_minor=selling_price_minor,
            selling_currency=selling_currency,
            pricing_metadata=dict(pricing_metadata),
        )
        self.rows[offer_id] = updated
        return updated

    async def publish_if_current(
        self,
        offer_id: UUID,
        *,
        expected_price_minor: int,
        expected_currency: str,
        expected_cost_minor: int,
        expected_cost_currency: str,
        expected_provider_rate: str | None = None,
    ) -> SellableOffer | None:
        self.calls.append("publish_if_current")
        row = self.rows[offer_id]
        if (
            row.operator_disabled
            or row.provider_cost_minor != expected_cost_minor
            or row.selling_price_minor != expected_price_minor
        ):
            return None
        updated = replace(row, enabled=True)
        self.rows[offer_id] = updated
        return updated

    async def record_auto_pricing_failure_if_current(
        self,
        offer_id: UUID,
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        expected_price_minor: int,
        expected_selling_currency: str,
        expected_pricing_metadata: dict[str, object],
        pricing_metadata: dict[str, object],
        preserve_valid_price: bool,
        expected_provider_rate: str | None = None,
    ) -> SellableOffer | None:
        self.calls.append("record_auto_pricing_failure_if_current")
        row = self.rows[offer_id]
        if (
            row.provider_cost_minor != expected_cost_minor
            or row.selling_price_minor != expected_price_minor
        ):
            return None
        updated = replace(row, pricing_metadata=dict(pricing_metadata))
        self.rows[offer_id] = updated
        return updated

    async def list_all(self) -> list[SellableOffer]:
        return list(self.rows.values())

    async def list_sellable(self, provider_key: str | None = None) -> list[SellableOffer]:
        return [
            o
            for o in self.rows.values()
            if o.sellable and (provider_key is None or o.provider_key == provider_key)
        ]


class FakeAuditRepo:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


class FakeLocationRepo:
    def __init__(self, records: list[LocationRecord] | None = None) -> None:
        self.records = list(records or [])

    async def list_for_provider(self, provider_key: str) -> list[LocationRecord]:
        return [r for r in self.records if r.provider_key == provider_key]


def _admin(offers: FakeOffersRepo, audit: FakeAuditRepo | None = None) -> OfferAdminService:
    return OfferAdminService(offers, audit or FakeAuditRepo())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# administration
# ---------------------------------------------------------------------------


class TestSetSellingPrice:
    async def test_a_manual_price_is_stored_and_opts_out_of_auto_pricing(self) -> None:
        # USD-native manual price: same-currency provenance via the exact
        # native rate, auto-pricing cleared by the single manual write.
        offer = _offer(auto_priced=True)
        repo = FakeOffersRepo([offer])
        audit = FakeAuditRepo()
        updated = await _admin(repo, audit).set_selling_price(
            actor=_user(),
            offer_id=offer.id,
            selling_price_minor=780,
            currency="USD",
            reason="operator pricing",
        )
        assert (updated.selling_price_minor, updated.selling_currency) == (780, "USD")
        assert updated.auto_priced is False
        assert repo.calls == ["set_manual_price"]
        assert audit.events

    async def test_provider_cost_is_never_touched_by_a_price_change(self) -> None:
        offer = _offer(cost_minor=499, price_minor=624)
        repo = FakeOffersRepo([offer])
        updated = await _admin(repo).set_selling_price(
            actor=_user(),
            offer_id=offer.id,
            selling_price_minor=1_000,
            currency="USD",
            reason="reprice",
        )
        assert updated.provider_cost_minor == 499
        assert updated.provider_cost_currency == "USD"

    async def test_foreign_manual_eur_is_refused_naming_usd(self) -> None:
        # Foreign EUR cost sells in catalog USD; a manual EUR relabel is
        # refused and names the required USD currency (never weakened).
        offer = _offer(currency="EUR", auto_priced=True)
        repo = FakeOffersRepo([offer])
        with pytest.raises(OfferAdminError, match="USD"):
            await _admin(repo).set_selling_price(
                actor=_user(),
                offer_id=offer.id,
                selling_price_minor=780,
                currency="EUR",
                reason="operator pricing",
            )
        assert repo.calls == []

    async def test_a_missing_reason_is_refused(self) -> None:
        repo = FakeOffersRepo([_offer()])
        with pytest.raises(OfferAdminError):
            await _admin(repo).set_selling_price(
                actor=_user(),
                offer_id=next(iter(repo.rows)),
                selling_price_minor=780,
                currency="USD",
                reason="   ",
            )

    async def test_a_non_positive_price_is_refused(self) -> None:
        repo = FakeOffersRepo([_offer()])
        with pytest.raises(OfferAdminError):
            await _admin(repo).set_selling_price(
                actor=_user(),
                offer_id=next(iter(repo.rows)),
                selling_price_minor=0,
                currency="USD",
                reason="free",
            )

    async def test_an_unknown_offer_is_refused(self) -> None:
        repo = FakeOffersRepo([_offer()])
        with pytest.raises(OfferNotFoundError):
            await _admin(repo).set_selling_price(
                actor=_user(),
                offer_id=uuid4(),
                selling_price_minor=780,
                currency="USD",
                reason="reprice",
            )

    async def test_a_customer_actor_is_refused(self) -> None:
        repo = FakeOffersRepo([_offer()])
        with pytest.raises(PermissionDeniedError):
            await _admin(repo).set_selling_price(
                actor=_user(role=Role.USER),
                offer_id=next(iter(repo.rows)),
                selling_price_minor=780,
                currency="USD",
                reason="reprice",
            )


class TestSetEnabled:
    async def test_hiding_records_an_explicit_operator_block(self) -> None:
        offer = _offer(enabled=True, operator_disabled=False)
        repo = FakeOffersRepo([offer])
        updated = await _admin(repo).set_enabled(
            actor=_user(), offer_id=offer.id, enabled=False, reason="not for sale yet"
        )
        assert updated.enabled is False
        assert updated.operator_disabled is True
        assert repo.calls == ["set_visibility_state"]

    async def test_showing_clears_the_operator_block(self) -> None:
        # USD-valid provenance (exact native rate, priced, available) is
        # required before an offer can be shown.
        offer = _offer(enabled=False, operator_disabled=True)
        repo = FakeOffersRepo([offer])
        updated = await _admin(repo).set_enabled(
            actor=_user(), offer_id=offer.id, enabled=True, reason="launch"
        )
        assert updated.enabled is True
        assert updated.operator_disabled is False

    async def test_showing_foreign_without_normalization_is_refused(self) -> None:
        # Foreign EUR rows must be normalized to USD before they can be shown.
        offer = _offer(currency="EUR", enabled=False, operator_disabled=True)
        repo = FakeOffersRepo([offer])
        with pytest.raises(OfferAdminError, match="USD"):
            await _admin(repo).set_enabled(
                actor=_user(), offer_id=offer.id, enabled=True, reason="launch"
            )

    async def test_showing_without_provenance_is_refused(self) -> None:
        # USD cost without the exact native rate has no valid provenance.
        offer = _offer(enabled=False, operator_disabled=True, billing_parameters={})
        repo = FakeOffersRepo([offer])
        with pytest.raises(OfferAdminError, match="provenance"):
            await _admin(repo).set_enabled(
                actor=_user(), offer_id=offer.id, enabled=True, reason="launch"
            )

    async def test_hiding_an_already_hidden_offer_is_a_no_op(self) -> None:
        offer = _offer(enabled=False, operator_disabled=True)
        repo = FakeOffersRepo([offer])
        returned = await _admin(repo).set_enabled(
            actor=_user(), offer_id=offer.id, enabled=False, reason="still hidden"
        )
        assert returned is offer
        assert repo.calls == []

    async def test_an_unknown_offer_is_refused(self) -> None:
        repo = FakeOffersRepo([_offer()])
        with pytest.raises(OfferNotFoundError):
            await _admin(repo).set_enabled(
                actor=_user(), offer_id=uuid4(), enabled=False, reason="hide"
            )

    async def test_a_missing_reason_is_refused(self) -> None:
        repo = FakeOffersRepo([_offer()])
        with pytest.raises(OfferAdminError):
            await _admin(repo).set_enabled(
                actor=_user(), offer_id=next(iter(repo.rows)), enabled=False, reason=""
            )

    async def test_a_customer_actor_is_refused(self) -> None:
        repo = FakeOffersRepo([_offer()])
        with pytest.raises(PermissionDeniedError):
            await _admin(repo).set_enabled(
                actor=_user(role=Role.USER),
                offer_id=next(iter(repo.rows)),
                enabled=False,
                reason="hide",
            )


class TestAdminReads:
    async def test_list_all_rows_returns_hidden_and_unpriced_rows(self) -> None:
        repo = FakeOffersRepo(
            [
                _offer(enabled=True, price_minor=624),
                _offer(product_id="VPS02_2", enabled=False, price_minor=0),
            ]
        )
        assert len(await _admin(repo).list_all_rows()) == 2

    async def test_list_sellable_returns_only_buyable_rows(self) -> None:
        # Provenance-filtered: only USD-valid (exact rate, priced,
        # available, enabled) rows are buyable.
        repo = FakeOffersRepo(
            [
                _offer(enabled=True, price_minor=624),
                _offer(product_id="VPS02_2", enabled=False, price_minor=0),
                _offer(product_id="VPS02_3", enabled=True, price_minor=624, available=False),
            ]
        )
        sellable = await _admin(repo).list_sellable()
        assert len(sellable) == 1
        assert sellable[0].product_id == "VPS02_1"


# ---------------------------------------------------------------------------
# customer browse
# ---------------------------------------------------------------------------


def _browse(
    offers: list[SellableOffer], records: list[LocationRecord] | None = None
) -> OfferBrowseService:
    return OfferBrowseService(  # type: ignore[arg-type]
        FakeOffersRepo(offers), FakeLocationRepo(records)
    )


class TestBrowseLocations:
    async def test_nothing_sellable_browses_as_empty(self) -> None:
        assert await _browse([_offer(enabled=False, price_minor=0)]).list_locations() == []

    async def test_synced_metadata_supplies_the_display_name_and_flag(self) -> None:
        offer = _offer()
        service = _browse(
            [offer],
            [
                LocationRecord(
                    provider_key=PROVIDER,
                    location_id="FRA-01",
                    name="Frankfurt",
                    country_code="DE",
                    city="Frankfurt",
                )
            ],
        )
        views = await service.list_locations()
        assert len(views) == 1
        assert (views[0].name, views[0].country_code, views[0].city) == (
            "Frankfurt",
            "DE",
            "Frankfurt",
        )
        assert views[0].offers == (offer,)

    async def test_an_unsynced_location_still_browses_by_its_code(self) -> None:
        service = _browse([_offer(location_id="LON-11")], records=[])
        views = await service.list_locations()
        assert views[0].name == "LON-11"
        assert views[0].country_code is None
        assert views[0].city is None

    async def test_each_location_is_its_own_entry(self) -> None:
        service = _browse(
            [
                _offer(product_id="VPS02_1", location_id="FRA-01"),
                _offer(product_id="VPS02_1", location_id="LON-01"),
            ]
        )
        views = await service.list_locations()
        assert sorted(v.location_id for v in views) == ["FRA-01", "LON-01"]

    async def test_offers_in_one_location_are_sorted_by_name(self) -> None:
        service = _browse(
            [
                _offer(product_id="VPS02_2", name="Zeta"),
                _offer(product_id="VPS02_1", name="Alpha"),
            ]
        )
        views = await service.list_locations()
        assert [o.name for o in views[0].offers] == ["Alpha", "Zeta"]

    async def test_several_providers_are_indexed_independently(self) -> None:
        service = _browse(
            [
                _offer(provider_key="leaseweb", location_id="FRA-01"),
                _offer(provider_key="hetzner", location_id="FRA-01"),
            ],
            [
                LocationRecord(
                    provider_key="leaseweb",
                    location_id="FRA-01",
                    name="Frankfurt",
                    country_code="DE",
                ),
                LocationRecord(
                    provider_key="hetzner",
                    location_id="FRA-01",
                    name="Falkenstein",
                    country_code="DE",
                ),
            ],
        )
        views = await service.list_locations()
        by_provider = {v.offers[0].provider_key: v.name for v in views}
        assert by_provider == {"leaseweb": "Frankfurt", "hetzner": "Falkenstein"}


class TestPermissionModel:
    def test_admins_hold_the_offer_management_permission(self) -> None:
        assert _user(role=Role.ADMIN).has_permission(Permission.ADMIN_MANAGE_SETTINGS)

    def test_customers_do_not_hold_it(self) -> None:
        assert not _user(role=Role.USER).has_permission(Permission.ADMIN_MANAGE_SETTINGS)
