"""Tests for PricingIngestionService (M04-005)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from cloud_platform.modules.catalog.domain import (
    CatalogEntrySpec,
    PlanPricing,
    PricingError,
    ProviderPriceEntry,
)
from cloud_platform.modules.catalog.service import PricingIngestionService


def _entry(location: str, hourly: str, monthly: str | None = None) -> ProviderPriceEntry:
    return ProviderPriceEntry(
        location_id=location,
        currency="EUR",
        hourly=Decimal(hourly),
        monthly=Decimal(monthly) if monthly is not None else None,
    )


def _plan() -> PlanPricing:
    return PlanPricing(
        plan_id="cx22",
        name="CX22",
        architecture="x86",
        vcpu=2,
        memory_mb=4096,
        disk_gb=40,
        prices=(
            _entry("fsn1", "0.0219", "15.87"),
            _entry("nbg1", "0.0225"),
            _entry("hel1", hourly="0.999", monthly=None),
        ),
        cpu_type="shared",
        storage_type="ssd",
    )


def _repo(results: list[bool]) -> AsyncMock:
    repo = AsyncMock()
    repo.upsert_entry = AsyncMock(side_effect=lambda spec: results.pop(0))
    return repo


class TestIngestPlan:
    async def test_ingests_one_row_per_location_with_decimal_math(self) -> None:
        repo = _repo([True, False, True])
        service = PricingIngestionService(repo)  # type: ignore[arg-type]

        result = await service.ingest_plan("hetzner", _plan())

        assert result.plan_id == "cx22"
        assert [p.location_id for p in result.prices] == ["fsn1", "nbg1", "hel1"]
        # fsn1: hourly 0.0219 -> 2.19 -> 2 (hourly wins over monthly)
        assert result.prices[0].hourly_minor == 2
        # nbg1: hourly 0.0225 -> 2.25 -> 2
        assert result.prices[1].hourly_minor == 2
        # hel1: hourly 0.999 -> 99.9 -> 100 (half-up)
        assert result.prices[2].hourly_minor == 100
        # created flags echo the repository
        assert [p.created for p in result.prices] == [True, False, True]
        assert repo.upsert_entry.await_count == 3

    async def test_specs_and_metadata_forwarded_to_repository(self) -> None:
        captured: list[CatalogEntrySpec] = []

        async def record(spec: CatalogEntrySpec) -> bool:
            captured.append(spec)
            return True

        repo = AsyncMock()
        repo.upsert_entry = AsyncMock(side_effect=record)
        service = PricingIngestionService(repo)  # type: ignore[arg-type]

        await service.ingest_plan("hetzner", _plan())

        first = captured[0]
        assert first.provider_key == "hetzner"
        assert first.plan_id == "cx22"
        assert first.location_id == "fsn1"
        assert first.name == "CX22"
        assert first.vcpu == 2
        assert first.memory_mb == 4096
        assert first.disk_gb == 40
        assert first.currency == "EUR"
        assert first.quantum_seconds == 3600
        assert first.extra_metadata == {"cpu_type": "shared", "storage_type": "ssd"}

    async def test_monthly_only_entry_derives_hourly(self) -> None:
        plan = PlanPricing(
            plan_id="cax21",
            name="CAX21",
            architecture="x86",
            vcpu=2,
            memory_mb=4096,
            disk_gb=40,
            prices=(
                ProviderPriceEntry(location_id="fsn1", currency="EUR", monthly=Decimal("15.87")),
            ),
        )
        repo = _repo([True])
        service = PricingIngestionService(repo)  # type: ignore[arg-type]

        result = await service.ingest_plan("hetzner", plan)
        assert result.prices[0].hourly_minor == 2

    async def test_empty_provider_key_rejected(self) -> None:
        repo = _repo([])
        service = PricingIngestionService(repo)  # type: ignore[arg-type]

        with pytest.raises(PricingError, match="provider_key"):
            await service.ingest_plan("  ", _plan())
        repo.upsert_entry.assert_not_awaited()

    async def test_invalid_plan_data_propagates(self) -> None:
        repo = _repo([])
        service = PricingIngestionService(repo)  # type: ignore[arg-type]

        # Domain validation rejects the plan at construction (before any I/O).
        with pytest.raises(ValueError, match="plan_id"):
            bad = PlanPricing(
                plan_id="",
                name="X",
                architecture="x86",
                vcpu=1,
                memory_mb=1024,
                disk_gb=10,
                prices=(_entry("fsn1", "0.01"),),
            )
            await service.ingest_plan("hetzner", bad)
        repo.upsert_entry.assert_not_awaited()
