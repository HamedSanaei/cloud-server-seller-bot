"""Tests for the catalog country/location view (M08-002).

Acceptance: only enabled offers shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from cloud_platform.modules.catalog.domain import (
    CatalogCountryView,
    CatalogLocationView,
    CatalogOffer,
    LocationRecord,
)
from cloud_platform.modules.catalog.service import CatalogViewService


def _offer(
    *,
    provider_key: str = "hetzner",
    plan_id: str = "cx22",
    location_id: str = "fsn1",
    name: str = "CX22",
    enabled: bool = True,
    vcpu: int = 2,
    price: int = 219,
) -> CatalogOffer:
    return CatalogOffer(
        id=uuid4(),
        provider_key=provider_key,
        plan_id=plan_id,
        location_id=location_id,
        name=name,
        architecture="x86",
        vcpu=vcpu,
        memory_mb=4096,
        disk_gb=40,
        currency="EUR",
        price_per_quantum=price,
        quantum_seconds=3600,
        enabled=enabled,
    )


def _loc(
    provider_key: str = "hetzner",
    location_id: str = "fsn1",
    name: str = "Falkenstein (Germany)",
    country_code: str | None = "DE",
    city: str | None = "Falkenstein",
) -> LocationRecord:
    return LocationRecord(
        provider_key=provider_key,
        location_id=location_id,
        name=name,
        country_code=country_code,
        city=city,
    )


@dataclass
class FakeCatalog:
    offers: list[CatalogOffer]

    async def list_offers(self) -> list[CatalogOffer]:
        return list(self.offers)


@dataclass
class FakeLocations:
    records: list[LocationRecord]

    async def list_for_provider(self, provider_key: str) -> list[LocationRecord]:
        return [r for r in self.records if r.provider_key == provider_key]


def _service(offers: list[CatalogOffer], records: list[LocationRecord]) -> CatalogViewService:
    return CatalogViewService(FakeCatalog(offers), FakeLocations(records))


class TestGrouping:
    async def test_groups_by_country_then_location(self) -> None:
        offers = [
            _offer(plan_id="cx22", location_id="fsn1"),
            _offer(plan_id="cx32", location_id="fsn1", name="CX32"),
            _offer(plan_id="cx22", location_id="nbg1", name="CX22"),
            _offer(plan_id="cx22", location_id="hel1", name="CX22"),
        ]
        records = [
            _loc(location_id="fsn1"),
            _loc(location_id="nbg1", name="Nuremberg (Germany)", city="Nuremberg"),
            _loc(location_id="hel1", name="Helsinki (Finland)", country_code="FI", city="Helsinki"),
        ]
        view = await _service(offers, records).list_by_country()

        assert [c.country_code for c in view] == ["DE", "FI"]
        de = view[0]
        assert isinstance(de, CatalogCountryView)
        assert [loc.location_id for loc in de.locations] == ["fsn1", "nbg1"]
        fsn = de.locations[0]
        assert isinstance(fsn, CatalogLocationView)
        assert fsn.city == "Falkenstein"
        assert [o.plan_id for o in fsn.offers] == ["cx22", "cx32"]  # sorted by name
        fi = view[1]
        assert fi.locations[0].city == "Helsinki"
        assert fi.offer_count == 1

    async def test_countries_sorted_other_last(self) -> None:
        offers = [
            _offer(location_id="x1", name="A"),
            _offer(location_id="y1", name="B"),
            _offer(location_id="z1", name="C"),
        ]
        records = [
            _loc(location_id="x1", name="X", country_code="ZZ"),
            _loc(location_id="y1", name="Y", country_code="AA"),
        ]  # z1 has no location record -> "Other" bucket
        view = await _service(offers, records).list_by_country()

        assert [c.country_code for c in view] == ["AA", "ZZ", None]
        assert view[-1].label == "Other"
        assert view[-1].locations[0].offers[0].name == "C"

    async def test_empty_catalog_is_empty(self) -> None:
        assert await _service([], []).list_by_country() == []


class TestOnlyEnabledOffers:
    async def test_disabled_offers_hidden(self) -> None:
        offers = [
            _offer(plan_id="cx22", location_id="fsn1"),
            _offer(plan_id="cx32", location_id="fsn1", name="CX32", enabled=False),
        ]
        view = await _service(offers, [_loc()]).list_by_country()

        assert view[0].offer_count == 1
        assert view[0].locations[0].offers[0].plan_id == "cx22"

    async def test_disabled_only_leaves_country_empty_and_dropped(self) -> None:
        offers = [_offer(plan_id="cx22", location_id="fsn1", enabled=False)]
        view = await _service(offers, [_loc()]).list_by_country()

        assert view == []  # no country with zero sellable offers is shown

    async def test_non_offer_rows_excluded(self) -> None:
        # synthetic rows (bare location markers) carry no specs: vcpu == 0
        offers = [
            _offer(plan_id="cx22", location_id="fsn1"),
            _offer(plan_id="location", location_id="fsn1", name="Falkenstein", vcpu=0, price=0),
        ]
        view = await _service(offers, [_loc()]).list_by_country()

        assert view[0].offer_count == 1

    async def test_only_known_providers_queried(self) -> None:
        queried: list[str] = []

        class RecodingLocations(FakeLocations):
            async def list_for_provider(self, provider_key: str) -> list[LocationRecord]:
                queried.append(provider_key)
                return await super().list_for_provider(provider_key)

        offers = [
            _offer(provider_key="hetzner", location_id="fsn1"),
            _offer(provider_key="arvan", location_id="tab1", name="A1"),
            _offer(provider_key="hetzner", location_id="nbg1", name="CX22"),
        ]
        records = [_loc(), _loc(location_id="nbg1", name="Nuremberg (Germany)")]
        service = CatalogViewService(FakeCatalog(offers), RecodingLocations(records))

        await service.list_by_country()

        assert queried == ["arvan", "hetzner"]  # each provider once, sorted


class TestOfferView:
    async def test_view_carries_specs_and_price(self) -> None:
        offer = _offer(price=219)
        view = await _service([offer], [_loc()]).list_by_country()

        v = view[0].locations[0].offers[0]
        assert v.offer_id == offer.id
        assert v.spec_label == "2 vCPU / 4 GB / 40 GB"
        assert v.price_per_quantum == 219
        assert v.quantum_seconds == 3600

    async def test_render_ascii(self) -> None:
        offers = [
            _offer(price=219),
            _offer(plan_id="cx32", location_id="fsn1", name="CX32", price=439),
        ]
        view = await _service(offers, [_loc()]).list_by_country()

        text = view[0].render()
        assert "[DE] 2 offer(s)" in text
        assert "Falkenstein (Germany)" in text
        assert "2.19 EUR/60min" in text  # integer-formatted, no float
        assert "4.39 EUR/60min" in text


class TestLocationRecordValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            dict(provider_key=""),
            dict(location_id="  "),
            dict(name=""),
        ],
    )
    def test_rejects_empty_fields(self, bad: dict) -> None:
        kwargs: dict = dict(
            provider_key="hetzner", location_id="fsn1", name="Falkenstein (Germany)"
        )
        kwargs.update(bad)
        with pytest.raises(ValueError):
            LocationRecord(**kwargs)  # type: ignore[arg-type]

    def test_country_code_optional(self) -> None:
        record = LocationRecord(provider_key="p", location_id="l", name="L")
        assert record.country_code is None
        assert record.city is None
