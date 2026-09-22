"""Automatic catalog refresh: sync, price, publish (STOREFRONT-V2).

- A newly discovered eligible product appears priced and published with no
  manual CLI commands.
- A provider-cost refresh reprices auto-priced rows (same currency, integer
  math, cost untouched); manual prices are never overwritten.
- An explicit operator block survives every future sync.
- Partial runs and persistence failures never price, publish or mass-retire.
- One provider failing never breaks another; a held lock skips the run.
- Pricing policy comes from server-owned configuration (validated).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.modules.offers.auto_sync import (
    CatalogAutoSyncCoordinator,
    pricing_policies_from_settings,
)
from cloud_platform.modules.offers.domain import (
    CatalogSyncReport,
    CatalogSyncState,
    OfferCatalogSyncSource,
    OfferSpecUpdate,
    PricingPolicy,
    SellableOffer,
    TechnicalSpec,
    markup_unit_price,
)

PROVIDER = "leaseweb"
SECOND = "second-provider"


def _offer(
    *,
    provider_key: str = PROVIDER,
    product_id: str = "VPS02_1",
    location_id: str = "FRA-01",
    cost_minor: int = 499,
    cost_currency: str = "EUR",
    price_minor: int = 0,
    price_currency: str = "EUR",
    enabled: bool = False,
    operator_disabled: bool = False,
    auto_priced: bool = True,
    available: bool = True,
    technical_metadata: dict[str, object] | None = None,
    name: str = "VPS 1",
) -> SellableOffer:
    return SellableOffer(
        id=uuid4(),
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
        billing_parameters={},
        technical_metadata=dict(technical_metadata or {}),
        provider_available=available,
        enabled=enabled,
        operator_disabled=operator_disabled,
        auto_priced=auto_priced,
        created_at=None,
    )


def _spec(
    *,
    cost_minor: int = 499,
    currency: str = "EUR",
    name: str = "VPS 1",
    technical_metadata: dict[str, object] | None = None,
) -> OfferSpecUpdate:
    return OfferSpecUpdate(
        name=name,
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic=None,
        provider_cost_minor=cost_minor,
        provider_cost_currency=currency,
        billing_parameters={},
        technical_metadata=technical_metadata,
        provider_available=True,
    )


class FakeOffersRepo:
    """In-memory price book honoring the operator-owned write boundaries."""

    def __init__(self, offers: list[SellableOffer] | None = None) -> None:
        self._rows: dict[tuple[str, str, str], SellableOffer] = {}
        for offer in offers or []:
            self._rows[(offer.provider_key, offer.product_id, offer.location_id)] = offer
        self.calls: list[str] = []

    def _key(self, offer: SellableOffer) -> tuple[str, str, str]:
        return (offer.provider_key, offer.product_id, offer.location_id)

    def _replace(self, offer: SellableOffer, **changes: Any) -> SellableOffer:
        from dataclasses import replace

        updated = replace(offer, **changes)
        self._rows[self._key(offer)] = updated
        return updated

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return next((o for o in self._rows.values() if o.id == offer_id), None)

    async def get_by_ref(
        self, provider_key: str, product_id: str, location_id: str
    ) -> SellableOffer | None:
        return self._rows.get((provider_key, product_id, location_id))

    async def list_all(self) -> list[SellableOffer]:
        return list(self._rows.values())

    async def list_sellable(self, provider_key: str | None = None) -> list[SellableOffer]:
        return [
            o
            for o in self._rows.values()
            if o.sellable and (provider_key is None or o.provider_key == provider_key)
        ]

    async def list_provider_locations(self) -> list[tuple[str, str]]:
        return sorted({(o.provider_key, o.location_id) for o in self._rows.values()})

    async def upsert_from_provider(
        self,
        *,
        provider_key: str,
        product_id: str,
        location_id: str,
        update: OfferSpecUpdate,
        provider_account_id: str | None = None,
    ) -> SellableOffer:
        key = (provider_key, product_id, location_id)
        existing = self._rows.get(key)
        if existing is None:
            offer = SellableOffer(
                id=uuid4(),
                provider_key=provider_key,
                product_id=product_id,
                location_id=location_id,
                name=update.name,
                vcpu=update.vcpu,
                ram_gb=update.ram_gb,
                disk_gb=update.disk_gb,
                traffic=update.traffic,
                provider_cost_minor=update.provider_cost_minor,
                provider_cost_currency=update.provider_cost_currency,
                selling_price_minor=0,
                selling_currency=update.provider_cost_currency,
                billing_parameters=dict(update.billing_parameters),
                technical_metadata=dict(update.technical_metadata or {}),
                provider_available=update.provider_available,
                enabled=False,
                created_at=None,
            )
            self._rows[key] = offer
            return offer
        return self._replace(
            existing,
            name=update.name,
            vcpu=update.vcpu,
            ram_gb=update.ram_gb,
            disk_gb=update.disk_gb,
            traffic=update.traffic,
            provider_cost_minor=update.provider_cost_minor,
            provider_cost_currency=update.provider_cost_currency,
            billing_parameters=dict(update.billing_parameters),
            technical_metadata=(
                dict(update.technical_metadata)
                if update.technical_metadata is not None
                else existing.technical_metadata
            ),
            provider_available=update.provider_available,
        )

    async def mark_unavailable(self, provider_key: str, available: set[tuple[str, str]]) -> int:
        changed = 0
        for key, offer in list(self._rows.items()):
            if key[0] == provider_key and (key[1], key[2]) not in available:
                if offer.provider_available:
                    self._replace(offer, provider_available=False)
                    changed += 1
        return changed

    async def set_enabled(self, offer_id: UUID, enabled: bool) -> SellableOffer:
        offer = await self.get(offer_id)
        assert offer is not None
        self.calls.append(f"enabled={enabled}")
        return self._replace(offer, enabled=enabled)

    async def set_operator_disabled(self, offer_id: UUID, disabled: bool) -> SellableOffer:
        offer = await self.get(offer_id)
        assert offer is not None
        return self._replace(offer, operator_disabled=disabled)

    async def set_auto_priced(self, offer_id: UUID, auto_priced: bool) -> SellableOffer:
        offer = await self.get(offer_id)
        assert offer is not None
        return self._replace(offer, auto_priced=auto_priced)

    async def set_selling_price(
        self, offer_id: UUID, selling_price_minor: int, currency: str
    ) -> SellableOffer:
        offer = await self.get(offer_id)
        assert offer is not None
        self.calls.append(f"price={selling_price_minor} {currency}")
        return self._replace(
            offer, selling_price_minor=selling_price_minor, selling_currency=currency
        )


class FakeStateRepo:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    async def record_run(self, **kwargs: Any) -> CatalogSyncState:
        self.runs.append(kwargs)
        return CatalogSyncState(
            provider_key=str(kwargs["provider_key"]),
            last_attempted_at=datetime.now(),
            last_success_at=datetime.now() if kwargs["ok"] else None,
            discovered=int(kwargs["discovered"]),
            persisted=int(kwargs["persisted"]),
            prices_updated=int(kwargs["prices_updated"]),
            published=int(kwargs["published"]),
            retired=int(kwargs["retired"]),
            warnings=tuple(kwargs["warnings"]),
            errors=tuple(kwargs["errors"]),
        )

    async def get(self, provider_key: str) -> CatalogSyncState | None:
        return None

    async def list_all(self) -> list[CatalogSyncState]:
        return []


class FakeLock:
    """CatalogSyncLock double; ``held=True`` simulates another running sync."""

    def __init__(self, held: bool = False) -> None:
        self._held = held
        self.entries = 0

    def guard(self):  # type: ignore[no-untyped-def]
        held = self._held
        entries = self

        @asynccontextmanager
        async def _guard():  # type: ignore[no-untyped-def]
            entries.entries += 1
            yield not held

        return _guard()


class FakeSource:
    """Sync source double: persists scripted observations, returns a report."""

    def __init__(
        self,
        provider_key: str,
        repo: FakeOffersRepo,
        observations: list[tuple[str, str, OfferSpecUpdate]] | None = None,
        report: CatalogSyncReport | None = None,
        error: Exception | None = None,
    ) -> None:
        self._provider_key = provider_key
        self._repo = repo
        self._observations = observations or []
        self._report = report
        self._error = error
        self.calls = 0

    @property
    def provider_key(self) -> str:
        return self._provider_key

    async def sync_catalog(self) -> CatalogSyncReport:
        self.calls += 1
        if self._error is not None:
            raise self._error
        verified: set[tuple[str, str]] = set()
        for product_id, location_id, update in self._observations:
            await self._repo.upsert_from_provider(
                provider_key=self._provider_key,
                product_id=product_id,
                location_id=location_id,
                update=update,
            )
            verified.add((product_id, location_id))
        if self._report is not None:
            return self._report
        return CatalogSyncReport(
            provider_key=self._provider_key,
            ok=True,
            complete=True,
            discovered=len(verified),
            persisted=len(verified),
            verified=frozenset(verified),
        )


def _policies(**entries: dict[str, Any]) -> dict[str, PricingPolicy]:
    return {
        key: PricingPolicy(
            mode=str(value.get("mode", "markup")),
            markup_percent=int(value.get("markup_percent", 0)),
            auto_publish=bool(value.get("auto_publish", True)),
        )
        for key, value in entries.items()
    }


def _coordinator(
    sources: list[OfferCatalogSyncSource],
    repo: FakeOffersRepo,
    state: FakeStateRepo | None = None,
    lock: FakeLock | None = None,
    policies: dict[str, PricingPolicy] | None = None,
) -> CatalogAutoSyncCoordinator:
    return CatalogAutoSyncCoordinator(
        sources=sources,
        offers=repo,  # type: ignore[arg-type]
        state=state or FakeStateRepo(),  # type: ignore[arg-type]
        lock=lock or FakeLock(),  # type: ignore[arg-type]
        pricing_policies=policies
        if policies is not None
        else {"leaseweb": PricingPolicy(markup_percent=25, auto_publish=True)},
    )


class TestNewProductAutoPublication:
    async def test_new_eligible_product_appears_priced_and_published(self) -> None:
        repo = FakeOffersRepo()
        source = FakeSource(
            PROVIDER, repo, [("VPS02_1", "FRA-01", _spec(cost_minor=499, currency="EUR"))]
        )
        report = await _coordinator([source], repo).run()
        assert report.ran
        (provider,) = report.providers
        assert provider.ok
        assert provider.prices_updated == 1
        assert provider.published == 1
        row = await repo.get_by_ref(PROVIDER, "VPS02_1", "FRA-01")
        assert row is not None
        assert row.selling_price_minor == 624  # 499 + 25%
        assert row.selling_currency == "EUR"
        assert row.enabled is True
        assert row.operator_disabled is False
        assert row.provider_cost_minor == 499  # cost untouched by markup

    async def test_cost_refresh_updates_auto_price(self) -> None:
        repo = FakeOffersRepo(
            [
                _offer(
                    price_minor=624,
                    price_currency="EUR",
                    enabled=True,
                    auto_priced=True,
                )
            ]
        )
        source = FakeSource(
            PROVIDER, repo, [("VPS02_1", "FRA-01", _spec(cost_minor=599, currency="EUR"))]
        )
        report = await _coordinator([source], repo).run()
        assert report.providers[0].prices_updated == 1
        row = await repo.get_by_ref(PROVIDER, "VPS02_1", "FRA-01")
        assert row is not None
        assert row.provider_cost_minor == 599
        assert row.selling_price_minor == 749  # 599 * 1.25 = 748.75, rounded up
        assert row.selling_currency == "EUR"

    async def test_unchanged_price_causes_no_write(self) -> None:
        repo = FakeOffersRepo(
            [_offer(price_minor=624, price_currency="EUR", enabled=True, auto_priced=True)]
        )
        source = FakeSource(
            PROVIDER, repo, [("VPS02_1", "FRA-01", _spec(cost_minor=499, currency="EUR"))]
        )
        report = await _coordinator([source], repo).run()
        assert report.providers[0].prices_updated == 0
        assert "price=" not in " ".join(repo.calls)

    async def test_manual_price_is_never_overwritten(self) -> None:
        repo = FakeOffersRepo(
            [
                _offer(
                    price_minor=700,
                    price_currency="EUR",
                    enabled=True,
                    auto_priced=False,
                )
            ]
        )
        source = FakeSource(
            PROVIDER, repo, [("VPS02_1", "FRA-01", _spec(cost_minor=599, currency="EUR"))]
        )
        report = await _coordinator([source], repo).run()
        assert report.providers[0].prices_updated == 0
        row = await repo.get_by_ref(PROVIDER, "VPS02_1", "FRA-01")
        assert row is not None
        assert row.selling_price_minor == 700
        assert row.provider_cost_minor == 599  # cost still refreshed

    async def test_currency_is_preserved_never_converted(self) -> None:
        repo = FakeOffersRepo()
        source = FakeSource(
            SECOND, repo, [("CX-22", "FSN-1", _spec(cost_minor=400, currency="USD"))]
        )
        policies = {SECOND: PricingPolicy(markup_percent=25, auto_publish=True)}
        report = await _coordinator([source], repo, policies=policies).run()
        assert report.providers[0].prices_updated == 1
        row = await repo.get_by_ref(SECOND, "CX-22", "FSN-1")
        assert row is not None
        assert (row.selling_price_minor, row.selling_currency) == (500, "USD")


class TestOperatorBlock:
    async def test_explicitly_disabled_product_stays_disabled(self) -> None:
        repo = FakeOffersRepo(
            [
                _offer(
                    price_minor=624,
                    price_currency="EUR",
                    enabled=False,
                    operator_disabled=True,
                    auto_priced=True,
                )
            ]
        )
        source = FakeSource(
            PROVIDER, repo, [("VPS02_1", "FRA-01", _spec(cost_minor=599, currency="EUR"))]
        )
        report = await _coordinator([source], repo).run()
        assert report.providers[0].published == 0
        assert report.providers[0].prices_updated == 0
        row = await repo.get_by_ref(PROVIDER, "VPS02_1", "FRA-01")
        assert row is not None
        assert row.enabled is False
        assert row.operator_disabled is True

    async def test_reappearing_offer_returns_without_block(self) -> None:
        repo = FakeOffersRepo(
            [
                _offer(
                    price_minor=624,
                    price_currency="EUR",
                    enabled=False,
                    operator_disabled=False,
                    auto_priced=True,
                    available=False,
                )
            ]
        )
        source = FakeSource(
            PROVIDER, repo, [("VPS02_1", "FRA-01", _spec(cost_minor=499, currency="EUR"))]
        )
        report = await _coordinator([source], repo).run()
        assert report.providers[0].published == 1
        row = await repo.get_by_ref(PROVIDER, "VPS02_1", "FRA-01")
        assert row is not None
        assert row.enabled is True
        assert row.provider_available is True

    async def test_no_policy_means_costs_only(self) -> None:
        repo = FakeOffersRepo()
        source = FakeSource(
            PROVIDER, repo, [("VPS02_1", "FRA-01", _spec(cost_minor=499, currency="EUR"))]
        )
        report = await _coordinator([source], repo, policies={}).run()
        assert report.providers[0].prices_updated == 0
        assert report.providers[0].published == 0
        row = await repo.get_by_ref(PROVIDER, "VPS02_1", "FRA-01")
        assert row is not None  # synced, just not priced/published
        assert row.selling_price_minor == 0
        assert row.enabled is False

    async def test_auto_publish_off_prices_without_publishing(self) -> None:
        repo = FakeOffersRepo()
        source = FakeSource(
            PROVIDER, repo, [("VPS02_1", "FRA-01", _spec(cost_minor=499, currency="EUR"))]
        )
        policies = {PROVIDER: PricingPolicy(markup_percent=25, auto_publish=False)}
        report = await _coordinator([source], repo, policies=policies).run()
        assert report.providers[0].prices_updated == 1
        assert report.providers[0].published == 0
        row = await repo.get_by_ref(PROVIDER, "VPS02_1", "FRA-01")
        assert row is not None
        assert row.selling_price_minor == 624
        assert row.enabled is False

    async def test_deprecated_plan_is_neither_priced_nor_published(self) -> None:
        repo = FakeOffersRepo()
        source = FakeSource(
            PROVIDER,
            repo,
            [
                (
                    "CX-22",
                    "FSN-1",
                    _spec(
                        cost_minor=400,
                        currency="EUR",
                        technical_metadata=TechnicalSpec(deprecated=True).to_metadata(),
                    ),
                )
            ],
        )
        report = await _coordinator([source], repo).run()
        assert report.providers[0].prices_updated == 0
        assert report.providers[0].published == 0


class TestPartialFailureSafety:
    async def test_failed_provider_does_not_block_another(self) -> None:
        leaseweb_repo = FakeOffersRepo()
        good = FakeSource(
            PROVIDER,
            leaseweb_repo,
            [("VPS02_1", "FRA-01", _spec(cost_minor=499, currency="EUR"))],
        )
        second_repo = FakeOffersRepo()
        failing = FakeSource(SECOND, second_repo, error=RuntimeError("provider down"))
        coordinator = _coordinator(
            [good, failing],
            leaseweb_repo,
            policies={
                PROVIDER: PricingPolicy(markup_percent=25, auto_publish=True),
                SECOND: PricingPolicy(markup_percent=25, auto_publish=True),
            },
        )
        # The coordinator shares one price book; the failing source raises
        # before touching it, so the healthy provider is unaffected.
        report = await coordinator.run()
        assert report.ran
        assert failing.calls == 1
        by_key = {p.provider_key: p for p in report.providers}
        assert by_key[PROVIDER].ok is True
        assert by_key[PROVIDER].published == 1
        assert by_key[SECOND].ok is False
        assert by_key[SECOND].errors

    async def test_persistence_failure_suppresses_pricing_and_publishing(self) -> None:
        repo = FakeOffersRepo([_offer(price_minor=0, enabled=False, auto_priced=True)])
        report_text = CatalogSyncReport(
            provider_key=PROVIDER,
            ok=True,
            complete=False,
            discovered=1,
            persisted=0,
            persistence_failures=("upsert VPS02_1/FRA-01: boom",),
            warnings=(),
            errors=(),
            verified=frozenset({("VPS02_1", "FRA-01")}),
        )
        source = FakeSource(PROVIDER, repo, report=report_text)
        outcome = await _coordinator([source], repo).run()
        assert outcome.providers[0].ok is False
        assert outcome.providers[0].prices_updated == 0
        assert outcome.providers[0].published == 0

    async def test_unusable_sync_prices_nothing(self) -> None:
        repo = FakeOffersRepo([_offer(price_minor=0, enabled=False, auto_priced=True)])
        source = FakeSource(
            PROVIDER,
            repo,
            report=CatalogSyncReport(
                provider_key=PROVIDER,
                ok=False,
                complete=False,
                errors=("authentication failed",),
            ),
        )
        outcome = await _coordinator([source], repo).run()
        assert outcome.providers[0].ok is False
        row = await repo.get_by_ref(PROVIDER, "VPS02_1", "FRA-01")
        assert row is not None
        assert row.selling_price_minor == 0
        assert row.enabled is False

    async def test_missing_cost_or_currency_is_skipped_with_warning(self) -> None:
        repo = FakeOffersRepo(
            [
                _offer(cost_minor=0, price_minor=0, enabled=False, auto_priced=True),
                _offer(
                    product_id="BAD-CUR",
                    cost_minor=499,
                    cost_currency="EURO",
                    price_minor=0,
                    enabled=False,
                    auto_priced=True,
                ),
            ]
        )
        source = FakeSource(
            PROVIDER,
            repo,
            report=CatalogSyncReport(
                provider_key=PROVIDER,
                ok=True,
                complete=True,
                verified=frozenset({("VPS02_1", "FRA-01"), ("BAD-CUR", "FRA-01")}),
            ),
        )
        outcome = await _coordinator([source], repo).run()
        assert outcome.providers[0].prices_updated == 0
        assert len(outcome.providers[0].warnings) == 2


class TestConcurrencyAndState:
    async def test_held_lock_skips_the_run(self) -> None:
        repo = FakeOffersRepo()
        state = FakeStateRepo()
        lock = FakeLock(held=True)
        source = FakeSource(PROVIDER, repo, [("VPS02_1", "FRA-01", _spec())])
        report = await _coordinator([source], repo, state=state, lock=lock).run()
        assert report.skipped
        assert source.calls == 0
        assert state.runs == []

    async def test_successful_run_records_state(self) -> None:
        repo = FakeOffersRepo()
        state = FakeStateRepo()
        source = FakeSource(
            PROVIDER, repo, [("VPS02_1", "FRA-01", _spec(cost_minor=499, currency="EUR"))]
        )
        report = await _coordinator([source], repo, state=state).run()
        assert report.ran
        assert len(state.runs) == 1
        run = state.runs[0]
        assert run["provider_key"] == PROVIDER
        assert run["ok"] is True
        assert run["prices_updated"] == 1
        assert run["published"] == 1


class TestPricingPolicyConfig:
    def test_valid_policies_parse(self) -> None:
        settings = _settings(
            storefront_pricing={
                "leaseweb": {"mode": "markup", "markup_percent": 25, "auto_publish": True},
                "hetzner": {"mode": "markup", "markup_percent": 10},
            }
        )
        policies = pricing_policies_from_settings(settings)
        assert policies["leaseweb"] == PricingPolicy(
            mode="markup", markup_percent=25, auto_publish=True
        )
        assert policies["hetzner"].auto_publish is True

    def test_invalid_policies_fail_closed(self) -> None:
        settings = _settings(
            storefront_pricing={
                "bad-mode": {"mode": "dynamic", "markup_percent": 25},
                "bad-markup": {"mode": "markup", "markup_percent": -5},
                "bool-markup": {"mode": "markup", "markup_percent": True},
            }
        )
        assert pricing_policies_from_settings(settings) == {}

    def test_non_mapping_section_rejected_at_config_load(self) -> None:
        import pytest as _pytest
        from pydantic import ValidationError as _ValidationError

        with _pytest.raises(_ValidationError):
            _settings(storefront_pricing={"not-a-mapping": "markup"})

    def test_missing_pricing_means_costs_only(self) -> None:
        assert pricing_policies_from_settings(_settings()) == {}

    def test_non_mapping_pricing_config_is_ignored(self) -> None:
        from types import SimpleNamespace

        assert pricing_policies_from_settings(SimpleNamespace(storefront_pricing=["x"])) == {}
        assert (
            pricing_policies_from_settings(
                SimpleNamespace(storefront_pricing={"x": "not-a-mapping"})
            )
            == {}
        )

    async def test_state_write_failure_does_not_fail_work(self) -> None:
        repo = FakeOffersRepo()
        source = FakeSource(
            PROVIDER, repo, [("VPS02_1", "FRA-01", _spec(cost_minor=499, currency="EUR"))]
        )

        class _FailingState(FakeStateRepo):
            async def record_run(self, **kwargs: Any) -> CatalogSyncState:
                raise RuntimeError("status store down")

        report = await _coordinator([source], repo, state=_FailingState()).run()
        assert report.providers[0].published == 1

    async def test_ghost_verified_pair_is_skipped(self) -> None:
        repo = FakeOffersRepo()
        source = FakeSource(
            PROVIDER,
            repo,
            report=CatalogSyncReport(
                provider_key=PROVIDER,
                ok=True,
                complete=True,
                verified=frozenset({("GHOST", "NOWHERE")}),
            ),
        )
        report = await _coordinator([source], repo).run()
        assert report.providers[0].prices_updated == 0
        assert report.providers[0].published == 0

    async def test_invalid_markup_policy_warns_and_skips(self) -> None:
        repo = FakeOffersRepo(
            [_offer(price_minor=0, enabled=False, auto_priced=True)],
        )
        source = FakeSource(
            PROVIDER, repo, [("VPS02_1", "FRA-01", _spec(cost_minor=499, currency="EUR"))]
        )
        policies = {PROVIDER: PricingPolicy(markup_percent=-5, auto_publish=True)}
        report = await _coordinator([source], repo, policies=policies).run()
        assert report.providers[0].prices_updated == 0
        assert report.providers[0].warnings

    async def test_row_missing_identity_is_skipped(self) -> None:
        import dataclasses

        valid = _offer(price_minor=0, enabled=False, auto_priced=True)
        ghost = object.__new__(SellableOffer)
        for field_name in dataclasses.fields(SellableOffer):
            object.__setattr__(ghost, field_name.name, getattr(valid, field_name.name))
        object.__setattr__(ghost, "name", "")
        repo = FakeOffersRepo([ghost])
        source = FakeSource(
            PROVIDER,
            repo,
            report=CatalogSyncReport(
                provider_key=PROVIDER,
                ok=True,
                complete=True,
                verified=frozenset({("VPS02_1", "FRA-01")}),
            ),
        )
        report = await _coordinator([source], repo).run()
        assert report.providers[0].prices_updated == 0
        assert report.providers[0].warnings

    async def test_deprecated_publish_warns(self) -> None:
        repo = FakeOffersRepo(
            [
                _offer(
                    price_minor=624,
                    price_currency="EUR",
                    enabled=False,
                    operator_disabled=False,
                    auto_priced=False,
                    technical_metadata=TechnicalSpec(deprecated=True).to_metadata(),
                )
            ]
        )
        source = FakeSource(
            PROVIDER,
            repo,
            report=CatalogSyncReport(
                provider_key=PROVIDER,
                ok=True,
                complete=True,
                verified=frozenset({("VPS02_1", "FRA-01")}),
            ),
        )
        report = await _coordinator([source], repo).run()
        assert report.providers[0].published == 0
        assert any("deprecated" in w for w in report.providers[0].warnings)

    def test_markup_math_is_integer_only(self) -> None:
        assert markup_unit_price(499, 25) == 624
        assert markup_unit_price(400, 25) == 500
        assert markup_unit_price(599, 25) == 749


class TestOperatorBlockAdministration:
    """offers disable persists a block syncs never undo; enable clears it."""

    def _admin(self) -> Any:
        from cloud_platform.modules.users.domain import Role, User, UserStatus

        return User(
            id=uuid4(),
            username="admin",
            email="admin@example.test",
            status=UserStatus.ACTIVE,
            role=Role.ADMIN,
            telegram_user_id=1,
        )

    def _service(self, repo: FakeOffersRepo) -> Any:
        from cloud_platform.modules.offers.service import OfferAdminService

        class _Audit:
            async def append(self, event: Any) -> Any:
                return event

        return OfferAdminService(repo, _Audit())  # type: ignore[arg-type]

    async def test_disable_records_block_and_enable_clears_it(self) -> None:
        repo = FakeOffersRepo([_offer(price_minor=624, enabled=True, operator_disabled=False)])
        service = self._service(repo)
        actor = self._admin()
        row = (await repo.list_all())[0]
        disabled = await service.set_enabled(
            actor=actor, offer_id=row.id, enabled=False, reason="bad product"
        )
        assert disabled.enabled is False
        assert disabled.operator_disabled is True
        enabled = await service.set_enabled(
            actor=actor, offer_id=row.id, enabled=True, reason="fixed"
        )
        assert enabled.enabled is True
        assert enabled.operator_disabled is False

    async def test_manual_price_opts_out_of_auto_pricing(self) -> None:
        repo = FakeOffersRepo([_offer(price_minor=0, enabled=False, auto_priced=True)])
        service = self._service(repo)
        row = (await repo.list_all())[0]
        updated = await service.set_selling_price(
            actor=self._admin(),
            offer_id=row.id,
            selling_price_minor=700,
            currency="EUR",
            reason="launch offer",
        )
        assert updated.selling_price_minor == 700
        assert updated.auto_priced is False


def _settings(**overrides: Any) -> Any:
    from cloud_platform.core.config import Settings

    return Settings(**overrides)
