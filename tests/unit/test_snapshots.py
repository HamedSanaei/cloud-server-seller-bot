"""Tests for snapshots (M13-004).

Acceptance: snapshot cost/capability represented.

- CAPABILITY: the optional ``snapshot_support_of`` probe on the provider
  port; Hetzner implements create/list/delete against the real API shapes
  (tested through fake transports).
- COST: snapshot storage is priced through an EXPLICIT operator rate card
  (minor units per GB per month) - never a hardcoded provider price. The
  monthly->hourly->quantum derivation mirrors the catalog's convention
  (Decimal /720, ROUND_HALF_UP), all integer in and out.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.pricing.snapshots import (
    SnapshotPricingError,
    SnapshotRateCard,
    snapshot_hourly_quantum_minor,
    snapshot_monthly_minor,
)
from cloud_platform.providers.base import ProviderSnapshot, snapshot_support_of


class _Resp:
    """Minimal httpx.Response stand-in (same shape as other test suites)."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        import json

        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._body = body
        self.content = json.dumps(body).encode()
        self.is_error = status_code >= 400
        self.text = json.dumps(body)

    def json(self) -> dict[str, Any]:
        return self._body


class TestRateCard:
    def test_valid_card(self) -> None:
        card = SnapshotRateCard(currency="EUR", per_gb_month_minor=100)
        assert card.per_gb_month_minor == 100

    @pytest.mark.parametrize(
        ("currency", "rate"),
        [("", 100), ("EURO", 100), ("EUR", -1)],
    )
    def test_invalid_cards_rejected(self, currency: str, rate: int) -> None:
        with pytest.raises(SnapshotPricingError):
            SnapshotRateCard(currency=currency, per_gb_month_minor=rate)

    def test_zero_rate_is_a_valid_operator_decision(self) -> None:
        card = SnapshotRateCard(currency="EUR", per_gb_month_minor=0)
        assert snapshot_monthly_minor(card, 500) == 0


class TestCostMath:
    CARD = SnapshotRateCard(currency="EUR", per_gb_month_minor=100)

    def test_monthly_is_linear_in_gb(self) -> None:
        assert snapshot_monthly_minor(self.CARD, 40) == 4000
        assert snapshot_monthly_minor(self.CARD, 0) == 0
        assert snapshot_monthly_minor(self.CARD, 1) == 100

    def test_negative_size_rejected(self) -> None:
        with pytest.raises(SnapshotPricingError):
            snapshot_monthly_minor(self.CARD, -5)

    def test_hourly_matches_catalog_convention(self) -> None:
        # monthly 7200 minor / 720 h = exactly 10 minor per hour
        assert snapshot_hourly_quantum_minor(self.CARD, 72, quantum_seconds=3600) == 10
        # reference computation the same way catalog/domain does it
        monthly = Decimal(snapshot_monthly_minor(self.CARD, 72))
        expected = int((monthly / Decimal(720)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        assert expected == 10

    def test_half_up_rounding_is_deterministic(self) -> None:
        # monthly 7560 minor / 720 = 10.5 -> ROUND_HALF_UP -> 11
        card = SnapshotRateCard(currency="EUR", per_gb_month_minor=7560)
        assert snapshot_hourly_quantum_minor(card, 1) == 11

    def test_sub_quantum_prorating_rounds_half_up(self) -> None:
        # hourly 10 -> a 900 s quantum is 10 * 900/3600 = 2.5 -> 3
        card = SnapshotRateCard(currency="EUR", per_gb_month_minor=7200)
        assert snapshot_hourly_quantum_minor(card, 1, quantum_seconds=900) == 3

    def test_invalid_quantum_rejected(self) -> None:
        with pytest.raises(SnapshotPricingError):
            snapshot_hourly_quantum_minor(self.CARD, 10, quantum_seconds=0)


class TestCapabilityProbe:
    def test_hetzner_advertises_snapshot_support(self) -> None:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        provider = HetznerCloudProvider(token="t")
        assert CapabilityCheck.has(provider)

    def test_plain_provider_lacks_snapshot_support(self) -> None:
        class Plain:
            pass

        assert not CapabilityCheck.has(Plain())


class CapabilityCheck:
    @staticmethod
    def has(provider: Any) -> bool:
        return snapshot_support_of(provider) is not None


class TestHetznerAdapterMapping:
    def make_provider(self, transport: Any) -> Any:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        provider = HetznerCloudProvider(token="t")
        provider._client = transport  # type: ignore[assignment]
        return provider

    async def test_create_maps_endpoint_and_reads_image_resource(self) -> None:
        calls: list[tuple[str, str]] = []

        class Transport:
            async def request(self, method: str, path: str, **kwargs: Any):
                calls.append((method, path))
                assert kwargs["json"] == {"type": "snapshot", "description": "pre-upgrade"}
                return _Resp(
                    201,
                    {
                        "action": {
                            "status": "running",
                            "resources": [
                                {"id": 77, "type": "server"},
                                {"id": 88, "type": "image"},
                            ],
                        }
                    },
                )

        provider = self.make_provider(Transport())
        snapshot_id = await provider.create_snapshot(
            "srv-1", "pre-upgrade", IdempotencyKey("snap-key")
        )
        assert snapshot_id == "88"
        assert calls == [("POST", "/servers/srv-1/actions/create_image")]

    async def test_list_filters_by_server_and_maps_fields(self) -> None:
        class Transport:
            async def request(self, method: str, path: str, **kwargs: Any):
                assert method == "GET" and path == "/images"
                params = kwargs["params"]
                assert params["type"] == "snapshot"
                assert params["bound_to"] == "srv-9"
                return _Resp(
                    200,
                    {
                        "images": [
                            {
                                "id": 42,
                                "description": "daily",
                                "image_size": 38.4,
                                "created_from": {"id": 9},
                                "created": "2026-08-24T00:00:00+00:00",
                            }
                        ]
                    },
                )

        provider = self.make_provider(Transport())
        snaps = await provider.list_snapshots("srv-9")
        assert len(snaps) == 1
        snap = snaps[0]
        assert isinstance(snap, ProviderSnapshot)
        assert snap.id == "42" and snap.description == "daily"
        assert snap.size_gb == 38.4
        assert snap.server_provider_id == "9"

    async def test_delete_is_idempotent_on_404(self) -> None:
        class Transport:
            def __init__(self) -> None:
                self.calls = 0

            async def request(self, method: str, path: str, **kwargs: Any):
                self.calls += 1
                if self.calls == 1:
                    return _Resp(204, {})
                from cloud_platform.providers.errors import ProviderNotFound

                raise ProviderNotFound("gone")

        transport = Transport()
        provider = self.make_provider(transport)
        await provider.delete_snapshot("55")  # 204 fine
        await provider.delete_snapshot("55")  # 404 -> success, no raise
        assert transport.calls == 2
