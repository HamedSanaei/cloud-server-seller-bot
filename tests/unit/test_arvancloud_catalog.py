"""Tests for the ArvanCloud catalog/pricing mapper (M15-003).

Acceptance: normalized offers require no core-domain change. The tests pin
that the mapper produces ``PlanPricing`` objects the existing
``PricingIngestionService`` ingests without any modification to the catalog
domain, and that money stays in Decimal/integers.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_platform.modules.catalog.domain import PlanPricing, ProviderPriceEntry
from cloud_platform.providers.arvancloud.sync import (
    ArvanCloudCatalogSyncer,
    SyncResult,
    plan_pricing_from_arvancloud,
)

REGION = "ir-thr-1"


def _plan_item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "12",
        "name": "s1-4c8g",
        "type": "standard",
        "category": "general",
        "cpu_count": 4,
        "memory_in_bytes": 8 * 1024**3,
        "disk_in_bytes": 60 * 1024**3,
        "price_per_hour": 0.55,
        "price_per_day": 8,
        "price_per_month": 180,
        "generation": "g3",
        "disk_type": "ssd",
    }
    base.update(overrides)
    return base


class TestPlanPricingMapper:
    def test_maps_all_fields(self) -> None:
        pricing = plan_pricing_from_arvancloud(_plan_item(), location_id=REGION)
        assert isinstance(pricing, PlanPricing)
        assert pricing.plan_id == "12"
        assert pricing.name == "s1-4c8g"
        assert pricing.architecture == "standard"
        assert pricing.vcpu == 4
        assert pricing.memory_mb == 8192
        assert pricing.disk_gb == 60
        assert len(pricing.prices) == 1
        entry = pricing.prices[0]
        assert entry.location_id == REGION
        assert entry.currency == "IRR"
        assert entry.hourly == Decimal("0.55")
        assert entry.monthly == Decimal("180")

    def test_uses_memory_field_when_bytes_absent(self) -> None:
        item = _plan_item()
        del item["memory_in_bytes"]
        item["memory"] = 4.0  # GB
        pricing = plan_pricing_from_arvancloud(item, location_id=REGION)
        assert pricing.memory_mb == 4096

    def test_uses_disk_field_when_bytes_absent(self) -> None:
        item = _plan_item()
        del item["disk_in_bytes"]
        item["disk"] = 30
        pricing = plan_pricing_from_arvancloud(item, location_id=REGION)
        assert pricing.disk_gb == 30

    def test_monthly_only_price_is_accepted(self) -> None:
        item = _plan_item()
        del item["price_per_hour"]
        pricing = plan_pricing_from_arvancloud(item, location_id=REGION)
        assert pricing.prices[0].hourly is None
        assert pricing.prices[0].monthly == Decimal("180")

    def test_missing_hourly_and_monthly_raises(self) -> None:
        item = _plan_item()
        del item["price_per_hour"]
        del item["price_per_month"]
        with pytest.raises(ValueError, match="neither hourly nor monthly price"):
            plan_pricing_from_arvancloud(item, location_id=REGION)

    def test_missing_id_raises(self) -> None:
        item = _plan_item()
        del item["id"]
        with pytest.raises(ValueError, match="missing id"):
            plan_pricing_from_arvancloud(item, location_id=REGION)

    def test_missing_location_raises(self) -> None:
        with pytest.raises(ValueError, match="location_id"):
            plan_pricing_from_arvancloud(_plan_item(), location_id="")

    def test_negative_price_is_rejected(self) -> None:
        item = _plan_item(price_per_hour=-1, price_per_month=-5)
        with pytest.raises(ValueError):  # ProviderPriceEntry validation
            plan_pricing_from_arvancloud(item, location_id=REGION)

    def test_non_numeric_price_is_skipped_to_next_field(self) -> None:
        item = _plan_item(price_per_hour="not-a-number")
        pricing = plan_pricing_from_arvancloud(item, location_id=REGION)
        assert pricing.prices[0].hourly is None
        assert pricing.prices[0].monthly == Decimal("180")

    def test_string_prices_parse_as_decimal(self) -> None:
        item = _plan_item(price_per_hour="1.25", price_per_month="300")
        pricing = plan_pricing_from_arvancloud(item, location_id=REGION)
        assert pricing.prices[0].hourly == Decimal("1.25")
        assert pricing.prices[0].monthly == Decimal("300")

    def test_produces_domain_type_ingestable_by_core_service(self) -> None:
        """Acceptance: no core-domain change. The output is a PlanPricing the
        existing PricingIngestionService.ingest_plan consumes as-is."""
        pricing = plan_pricing_from_arvancloud(_plan_item(), location_id=REGION)
        # The domain invariants hold without any adapter-specific branch.
        for entry in pricing.prices:
            assert isinstance(entry, ProviderPriceEntry)
        assert pricing.prices  # PlanPricing requires >= 1 price
        # to_minor_units (core) works on the mapped value - no float
        from cloud_platform.modules.catalog.domain import to_minor_units

        minor = to_minor_units(pricing.prices[0].hourly or Decimal(0))
        assert minor == 55  # 0.55 IRR -> 55 minor units


class _FakeProviderPlan:
    def __init__(
        self,
        id: str,
        name: str,
        architecture: str,
        vcpu: int,
        memory_mb: int,
        disk_gb: int,
        metadata: dict[str, Any],
    ) -> None:
        self.id = id
        self.name = name
        self.architecture = architecture
        self.vcpu = vcpu
        self.memory_mb = memory_mb
        self.disk_gb = disk_gb
        self.metadata = metadata


class TestSyncer:
    def _provider(self, plans: list[Any]) -> Any:
        provider = AsyncMock()
        provider.list_plans = AsyncMock(return_value=plans)
        return provider

    def _plan_object(self, id: str = "12", **meta: Any) -> Any:
        return _FakeProviderPlan(
            id=id,
            name="s1",
            architecture="standard",
            vcpu=4,
            memory_mb=8192,
            disk_gb=60,
            metadata={"price_per_hour": 0.55, **meta},
        )

    async def test_sync_plans_ingests_each_plan(self) -> None:
        provider = self._provider([self._plan_object()])
        ingested: list[tuple[str, PlanPricing]] = []

        class FakeIngestion:
            def __init__(self, repo: Any) -> None:
                self.repo = repo

            async def ingest_plan(self, provider_key: str, plan: PlanPricing) -> Any:
                ingested.append((provider_key, plan))
                from cloud_platform.modules.catalog.domain import IngestedPrice, IngestedPricing

                return IngestedPricing(
                    plan_id=plan.plan_id,
                    prices=(
                        IngestedPrice(
                            location_id=REGION, currency="IRR", hourly_minor=55, created=True
                        ),
                    ),
                )

        import cloud_platform.providers.arvancloud.sync as sync_mod

        real = sync_mod.PricingIngestionService
        sync_mod.PricingIngestionService = FakeIngestion  # type: ignore[misc]
        try:
            syncer = ArvanCloudCatalogSyncer(
                session_factory=lambda: None,  # type: ignore[arg-type]
                provider=provider,
                region=REGION,
            )
            result = await syncer.sync_plans()
        finally:
            sync_mod.PricingIngestionService = real

        assert isinstance(result, SyncResult)
        assert result.total_fetched == 1
        assert result.total_upserted == 1
        assert len(ingested) == 1
        assert ingested[0][0] == "arvancloud"
        assert ingested[0][1].plan_id == "12"
        assert provider.list_plans.await_count == 1

    async def test_sync_plans_isolates_per_plan_errors(self) -> None:
        class BadPlan:
            id = "bad"
            name = ""
            architecture = "x"
            vcpu = 0
            memory_mb = 0
            disk_gb = 0

            def __init__(self) -> None:
                self.metadata: dict[str, Any] = {}

        provider = self._provider([BadPlan(), self._plan_object()])

        import cloud_platform.providers.arvancloud.sync as sync_mod

        real = sync_mod.PricingIngestionService

        class FailFirst:
            def __init__(self, repo: Any) -> None:
                self._n = 0

            async def ingest_plan(self, provider_key: str, plan: PlanPricing) -> Any:
                self._n += 1
                if plan.plan_id == "bad":
                    raise ValueError("no price")
                from cloud_platform.modules.catalog.domain import IngestedPricing

                return IngestedPricing(
                    plan_id=plan.plan_id,
                    prices=(),
                )

        sync_mod.PricingIngestionService = FailFirst  # type: ignore[misc]
        try:
            syncer = ArvanCloudCatalogSyncer(
                session_factory=lambda: None,  # type: ignore[arg-type]
                provider=provider,
                region=REGION,
            )
            result = await syncer.sync_plans()
        finally:
            sync_mod.PricingIngestionService = real

        assert result.total_fetched == 2
        assert result.total_upserted == 0
        assert len(result.errors) == 1
        assert "bad" in result.errors[0]


class _FakeProviderPlan:
    def __init__(
        self,
        id: str,
        name: str,
        architecture: str,
        vcpu: int,
        memory_mb: int,
        disk_gb: int,
        metadata: dict[str, Any],
    ) -> None:
        self.id = id
        self.name = name
        self.architecture = architecture
        self.vcpu = vcpu
        self.memory_mb = memory_mb
        self.disk_gb = disk_gb
        self.metadata = metadata
