"""Tests for the plan selection view + buy-flow screens (M08-003).

Acceptance: price/spec visible and callback tamper-resistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from cloud_platform.modules.catalog.domain import CatalogOffer, LocationRecord
from cloud_platform.modules.catalog.service import (
    BuyFlowViewService,
    PlanSelectionError,
)
from cloud_platform.modules.navigation.domain import (
    CallbackError,
    decode_callback,
)

SIGNING_KEY = "test-signing-key"


def _offer(
    *,
    provider_key: str = "hetzner",
    plan_id: str = "cx22",
    location_id: str = "fsn1",
    name: str = "CX22",
    enabled: bool = True,
    vcpu: int = 2,
    price: int = 219,
    offer_id: uuid4 | None = None,
) -> CatalogOffer:
    return CatalogOffer(
        id=offer_id or uuid4(),
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


def _service(offers: list[CatalogOffer], records: list[LocationRecord]) -> BuyFlowViewService:
    return BuyFlowViewService(FakeCatalog(offers), FakeLocations(records), SIGNING_KEY)


class TestPlansScreen:
    async def test_price_and_spec_visible(self) -> None:
        offers = [
            _offer(plan_id="cx32", location_id="fsn1", name="CX32", price=439),
            _offer(plan_id="cx22", location_id="fsn1", price=219),
        ]
        view = await _service(offers, [_loc()]).plans_screen("hetzner", "fsn1")

        assert [p.name for p in view.plans] == ["CX22", "CX32"]  # sorted by name
        assert view.plans[0].spec_label == "2 vCPU / 4 GB / 40 GB"
        assert view.plans[0].price_per_quantum == 219
        assert view.plans[1].price_per_quantum == 439
        assert view.location_name == "Falkenstein (Germany)"
        assert view.city == "Falkenstein"
        assert len(view.plan_callbacks) == len(view.plans)
        text = view.render()
        assert "2.19 EUR/60min" in text
        assert "4.39 EUR/60min" in text

    async def test_only_enabled_offers_of_the_location(self) -> None:
        offers = [
            _offer(plan_id="cx22", location_id="fsn1"),
            _offer(plan_id="cx32", location_id="fsn1", name="CX32", enabled=False),
            _offer(plan_id="cx22", location_id="nbg1", name="CX22@nbg"),  # other location
        ]
        view = await _service(
            offers, [_loc(), _loc(location_id="nbg1", name="Nuremberg")]
        ).plans_screen("hetzner", "fsn1")

        assert [p.plan_id for p in view.plans] == ["cx22"]

    async def test_unknown_location_raises(self) -> None:
        with pytest.raises(PlanSelectionError, match="no sellable offers"):
            await _service([], [_loc()]).plans_screen("hetzner", "nowhere")

    async def test_no_location_record_still_renders(self) -> None:
        offers = [_offer()]
        view = await _service(offers, []).plans_screen("hetzner", "fsn1")
        assert view.location_name == "fsn1"
        assert view.city is None

    async def test_callback_stability(self) -> None:
        offer = _offer()
        first = await _service([offer], [_loc()]).plans_screen("hetzner", "fsn1")
        second = await _service([offer], [_loc()]).plans_screen("hetzner", "fsn1")

        assert first.plan_callbacks == second.plan_callbacks
        assert first.back_callback == second.back_callback
        assert first.cancel_callback == second.cancel_callback


class TestCallbackIntegrity:
    async def test_plan_callback_roundtrips(self) -> None:
        offer = _offer(offer_id=uuid4())
        view = await _service([offer], [_loc()]).plans_screen("hetzner", "fsn1")

        decoded = decode_callback(view.plan_callbacks[0], SIGNING_KEY)

        assert decoded.flow == "buy"
        assert decoded.screen == "plans"
        assert decoded.args == ("hetzner", "fsn1", str(offer.id))

    async def test_plan_callback_selects_the_right_plan(self) -> None:
        a = _offer(plan_id="cx22", name="CX22")
        b = _offer(plan_id="cx32", name="CX32", price=439)
        view = await _service([a, b], [_loc()]).plans_screen("hetzner", "fsn1")

        decoded_b = decode_callback(view.plan_callbacks[1], SIGNING_KEY)
        assert decoded_b.args[2] == str(b.id)

    async def test_tampered_plan_callback_rejected(self) -> None:
        offer = _offer()
        view = await _service([offer], [_loc()]).plans_screen("hetzner", "fsn1")
        data = view.plan_callbacks[0]
        # swap the provider field inside the signed key
        tampered = data.replace("hetzner", "arvan")
        with pytest.raises(CallbackError):
            decode_callback(tampered, SIGNING_KEY)

    async def test_tampered_signature_rejected(self) -> None:
        offer = _offer()
        view = await _service([offer], [_loc()]).plans_screen("hetzner", "fsn1")
        version, key, signature = view.plan_callbacks[0].split("|")
        flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
        with pytest.raises(CallbackError):
            decode_callback(f"{version}|{key}|{flipped}", SIGNING_KEY)

    async def test_wrong_signing_key_rejected(self) -> None:
        offer = _offer()
        view = await _service([offer], [_loc()]).plans_screen("hetzner", "fsn1")
        with pytest.raises(CallbackError):
            decode_callback(view.plan_callbacks[0], "another-key")

    async def test_back_and_cancel_callbacks(self) -> None:
        offer = _offer()
        view = await _service([offer], [_loc()]).plans_screen("hetzner", "fsn1")

        back = decode_callback(view.back_callback, SIGNING_KEY)
        assert back.flow == "buy"
        assert back.screen == "locations"
        assert back.args == ("hetzner", "fsn1")

        cancel = decode_callback(view.cancel_callback, SIGNING_KEY)
        assert cancel.flow == "main"
        assert cancel.screen == "menu"
        assert cancel.args == ()


class TestLocationsScreen:
    async def test_options_grouped_by_country_with_callbacks(self) -> None:
        offers = [
            _offer(plan_id="cx22", location_id="fsn1"),
            _offer(plan_id="cx32", location_id="fsn1", name="CX32"),
            _offer(plan_id="cx22", location_id="hel1", name="CX22"),
        ]
        records = [
            _loc(location_id="fsn1"),
            _loc(location_id="hel1", name="Helsinki (Finland)", country_code="FI", city="Helsinki"),
        ]
        view = await _service(offers, records).locations_screen()

        assert [c.country_code for c in view] == ["DE", "FI"]
        de = view[0]
        assert de.label == "DE"
        assert de.options[0].offer_count == 2
        assert de.options[0].city == "Falkenstein"
        fi = view[1]
        assert fi.options[0].location_id == "hel1"

        decoded = decode_callback(de.options[0].select_callback, SIGNING_KEY)
        assert decoded.flow == "buy"
        assert decoded.screen == "locations"
        assert decoded.args == ("hetzner", "fsn1")

    async def test_disabled_offers_do_not_create_options(self) -> None:
        offers = [_offer(plan_id="cx22", location_id="fsn1", enabled=False)]
        assert await _service(offers, [_loc()]).locations_screen() == []

    async def test_multi_provider_options_are_separate(self) -> None:
        offers = [
            _offer(provider_key="hetzner", location_id="fsn1"),
            _offer(provider_key="arvan", location_id="fsn1", name="A-FSN"),
        ]
        records = [
            _loc(location_id="fsn1"),
            _loc(
                provider_key="arvan",
                location_id="fsn1",
                name="Arvan FSN",
                country_code="IR",
                city="Tehran",
            ),
        ]
        view = await _service(offers, records).locations_screen()

        de = next(c for c in view if c.country_code == "DE")
        ir = next(c for c in view if c.country_code == "IR")
        assert de.options[0].provider_key == "hetzner"
        assert ir.options[0].provider_key == "arvan"


class TestServiceValidation:
    def test_empty_signing_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            BuyFlowViewService(FakeCatalog([]), FakeLocations([]), "")
