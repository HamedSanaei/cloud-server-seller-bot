"""Tests for the OS selection view (M08-004).

Acceptance: architecture compatibility enforced server-side.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.catalog.domain import CatalogOffer
from cloud_platform.modules.catalog.service import (
    OsSelectionError,
    OsSelectionService,
    compatible_os_options,
)
from cloud_platform.modules.navigation.domain import (
    CallbackError,
    decode_callback,
)
from cloud_platform.providers.base import ProviderImage

SIGNING_KEY = "test-signing-key"
OFFER_ID = uuid4()


def _offer(
    *,
    architecture: str = "x86",
    enabled: bool = True,
    vcpu: int = 2,
) -> CatalogOffer:
    return CatalogOffer(
        id=OFFER_ID,
        provider_key="hetzner",
        plan_id="cx22",
        location_id="fsn1",
        name="CX22",
        architecture=architecture,
        vcpu=vcpu,
        memory_mb=4096,
        disk_gb=40,
        currency="EUR",
        price_per_quantum=219,
        quantum_seconds=3600,
        enabled=enabled,
    )


def _image(
    image_id: str, name: str, architecture: str = "x86", family: str = "linux"
) -> ProviderImage:
    return ProviderImage(id=image_id, name=name, os_family=family, architecture=architecture)


class FakeCatalog:
    def __init__(self, offer: CatalogOffer | None) -> None:
        self._offer = offer

    async def get_by_id(self, offer_id: UUID) -> CatalogOffer | None:
        return self._offer if self._offer.id == offer_id else None


class FakeProvider:
    def __init__(self, images: list[ProviderImage]) -> None:
        self._images = images
        self.list_images_calls = 0

    async def list_images(self) -> list[ProviderImage]:
        self.list_images_calls += 1
        return list(self._images)


class FakeRegistry:
    def __init__(self, providers: dict[str, FakeProvider]) -> None:
        self._providers = providers

    def get(self, key: str) -> FakeProvider:
        return self._providers[key]


def _service(
    offer: CatalogOffer | None,
    images: list[ProviderImage],
    providers: dict[str, FakeProvider] | None = None,
) -> OsSelectionService:
    registry = FakeRegistry(providers or {"hetzner": FakeProvider(images)})
    return OsSelectionService(FakeCatalog(offer), registry, SIGNING_KEY)


class TestArchitectureGate:
    async def test_only_matching_architecture_listed(self) -> None:
        images = [
            _image("ubuntu-24.04", "Ubuntu 24.04"),
            _image("debian-12", "Debian 12"),
            _image("grapple", "ARM image", architecture="arm"),
        ]
        view = await _service(_offer(architecture="x86"), images).os_screen(
            "hetzner", "fsn1", OFFER_ID
        )

        assert [o.image_id for o in view.options] == ["debian-12", "ubuntu-24.04"]  # sorted by name

    async def test_arm_offer_gets_arm_images(self) -> None:
        images = [
            _image("ubuntu-x86", "Ubuntu x86", architecture="x86"),
            _image("ubuntu-arm", "Ubuntu arm", architecture="arm"),
        ]
        view = await _service(_offer(architecture="arm"), images).os_screen(
            "hetzner", "fsn1", OFFER_ID
        )

        assert [o.image_id for o in view.options] == ["ubuntu-arm"]

    async def test_architecture_match_case_insensitive(self) -> None:
        images = [_image("img-1", "Image", architecture="X86")]
        view = await _service(_offer(architecture="x86"), images).os_screen(
            "hetzner", "fsn1", OFFER_ID
        )
        assert len(view.options) == 1

    async def test_no_compatible_images_raises(self) -> None:
        images = [_image("arm-only", "ARM only", architecture="arm")]
        with pytest.raises(OsSelectionError, match="no x86 images"):
            await _service(_offer(architecture="x86"), images).os_screen(
                "hetzner", "fsn1", OFFER_ID
            )

    def test_pure_gate_filters_and_sorts(self) -> None:
        images = [
            _image("b", "Beta", architecture="x86"),
            _image("a", "Alpha", architecture="x86"),
            _image("c", "Gamma", architecture="arm"),
        ]
        result = compatible_os_options(images, "x86")
        assert [i.id for i in result] == ["a", "b"]
        assert compatible_os_options(images, "") == []


class TestScreenContext:
    async def test_unknown_offer_raises(self) -> None:
        with pytest.raises(OsSelectionError, match="not found"):
            await _service(_offer(), []).os_screen("hetzner", "fsn1", uuid4())

    async def test_offer_must_match_location(self) -> None:
        # the offer is at fsn1; asking for it at nbg1 must fail server-side
        with pytest.raises(OsSelectionError, match="not found"):
            await _service(_offer(), [_image("ubuntu-24.04", "Ubuntu")]).os_screen(
                "hetzner", "nbg1", OFFER_ID
            )

    async def test_disabled_offer_not_selectable(self) -> None:
        with pytest.raises(OsSelectionError, match="not sellable"):
            await _service(_offer(enabled=False), [_image("ubuntu-24.04", "Ubuntu")]).os_screen(
                "hetzner", "fsn1", OFFER_ID
            )

    async def test_unknown_provider_raises(self) -> None:
        ghost_offer = _offer()
        ghost_offer = CatalogOffer(
            id=ghost_offer.id,
            provider_key="ghost",
            plan_id=ghost_offer.plan_id,
            location_id="fsn1",
            name=ghost_offer.name,
            architecture=ghost_offer.architecture,
            vcpu=ghost_offer.vcpu,
            memory_mb=ghost_offer.memory_mb,
            disk_gb=ghost_offer.disk_gb,
            currency=ghost_offer.currency,
            price_per_quantum=ghost_offer.price_per_quantum,
            quantum_seconds=ghost_offer.quantum_seconds,
            enabled=ghost_offer.enabled,
        )
        with pytest.raises(OsSelectionError, match="unknown provider"):
            await _service(ghost_offer, []).os_screen("ghost", "fsn1", OFFER_ID)

    async def test_empty_signing_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            OsSelectionService(FakeCatalog(None), FakeRegistry({}), "")


class TestCallbacks:
    async def test_option_callback_roundtrips(self) -> None:
        images = [_image("ubuntu-24.04", "Ubuntu 24.04")]
        view = await _service(_offer(), images).os_screen("hetzner", "fsn1", OFFER_ID)

        decoded = decode_callback(view.options[0].select_callback, SIGNING_KEY)
        assert decoded.flow == "buy"
        assert decoded.screen == "os"
        assert decoded.args == ("hetzner", "fsn1", str(OFFER_ID), "ubuntu-24.04")

    async def test_tampered_callback_rejected(self) -> None:
        images = [_image("ubuntu-24.04", "Ubuntu 24.04")]
        view = await _service(_offer(), images).os_screen("hetzner", "fsn1", OFFER_ID)
        tampered = view.options[0].select_callback.replace("ubuntu-24.04", "debian-12")
        with pytest.raises(CallbackError):
            decode_callback(tampered, SIGNING_KEY)

    async def test_back_and_cancel_callbacks(self) -> None:
        images = [_image("ubuntu-24.04", "Ubuntu 24.04")]
        view = await _service(_offer(), images).os_screen("hetzner", "fsn1", OFFER_ID)

        back = decode_callback(view.back_callback, SIGNING_KEY)
        assert (back.flow, back.screen) == ("buy", "plans")
        assert back.args == ("hetzner", "fsn1", str(OFFER_ID))

        cancel = decode_callback(view.cancel_callback, SIGNING_KEY)
        assert (cancel.flow, cancel.screen) == ("main", "menu")

    async def test_callback_stability(self) -> None:
        images = [_image("ubuntu-24.04", "Ubuntu 24.04"), _image("debian-12", "Debian 12")]
        first = await _service(_offer(), images).os_screen("hetzner", "fsn1", OFFER_ID)
        second = await _service(_offer(), images).os_screen("hetzner", "fsn1", OFFER_ID)
        assert [o.select_callback for o in first.options] == [
            o.select_callback for o in second.options
        ]

    async def test_offer_context_in_view(self) -> None:
        images = [_image("ubuntu-24.04", "Ubuntu 24.04")]
        view = await _service(_offer(), images).os_screen("hetzner", "fsn1", OFFER_ID)

        assert view.offer.plan_id == "cx22"
        assert view.offer.architecture == "x86"
        assert view.offer.price_per_quantum == 219
        text = view.render()
        assert "x86" in text
        assert "ubuntu-24.04" in text
