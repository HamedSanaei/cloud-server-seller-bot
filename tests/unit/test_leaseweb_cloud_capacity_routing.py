"""Catalog publication must exclude credential accounts that cannot create.

Production (2026-09-25): the Frankfurt Sales Organization (``sales-org-north``,
priority 100) answered a new hourly create with ``PC-2031`` — "Customer limit
reached" — while the UK account (priority 200) still could NOT prove the
incident's region/image read (``probe_region_images("eu-central-1")`` raises a
validation error there). The storefront therefore has to do three things at
once, and these tests pin each of them:

1. stop publishing NEW hourly orders through the capacity-limited account;
2. NOT hand the offer to an account that never proved the exact pair — an
   inconclusive OR definitively refused probe is not proof;
3. keep the row (and its provenance) so existing resources and reconciliation
   are untouched, and publish it again only after a POSITIVE recovery signal —
   an elapsed cooling window is not one.

The fakes are the ones the multi-account suites already use, so the code under
test is the real syncer and the real publication decision.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_leaseweb_hourly_multi_account import (  # type: ignore[import-not-found]
    _CloudProviderFake,
    _ctype,
    _image,
    _LocationsRepo,
    _OffersRepo,
    _region,
)

from cloud_platform.modules.provider_capacity.domain import (
    AccountCapacity,
    AccountCapacityState,
)
from cloud_platform.providers.errors import ProviderUnavailable
from cloud_platform.providers.leaseweb.errors import LeasewebValidationError

REGION = "eu-central-1"
TYPE = "lsw.m4.large"


class _CapacityStore:
    """Minimal durable-capacity double (the SQL adapter has its own live test)."""

    def __init__(self, records: dict[str, AccountCapacity] | None = None) -> None:
        self.records = dict(records or {})
        self.reads = 0

    async def limit_reached_accounts(self, provider_key: str, *, now: Any = None) -> frozenset[str]:
        self.reads += 1
        return frozenset(
            account_id
            for account_id, record in self.records.items()
            if record.is_limit_reached(now=now)
        )


class _BrokenCapacityStore:
    async def limit_reached_accounts(self, provider_key: str, *, now: Any = None) -> Any:
        raise RuntimeError("capacity store down")


class _OffersRepoWithOwners(_OffersRepo):
    """``_OffersRepo`` plus the read-only ownership map the sync consults."""

    def __init__(self, owners: dict[tuple[str, str], str]) -> None:
        super().__init__([])
        self._owners = dict(owners)

    async def hourly_account_owners(self, provider_key: str) -> dict[tuple[str, str], str]:
        return dict(self._owners)


def _limited(account_id: str, *, expired: bool = False) -> AccountCapacity:
    now = datetime.now(UTC) - (timedelta(hours=2) if expired else timedelta(minutes=5))
    return AccountCapacity(
        provider_key="leaseweb",
        credential_account_id=account_id,
        state=AccountCapacityState.LIMIT_REACHED,
        error_code="PC-2031",
        correlation_id="07376219-7bcd-43d9-a5ea-4128fa57345a",
        location_id=REGION,
        product_id=TYPE,
        observations=1,
        observed_at=now,
        expires_at=now + timedelta(hours=1),
    )


async def _run(
    accounts: dict[str, _CloudProviderFake],
    *,
    capacity: Any = None,
    owners: dict[tuple[str, str], str] | None = None,
    priorities: dict[str, int] | None = None,
) -> tuple[Any, _OffersRepo, _LocationsRepo]:
    from unittest.mock import patch

    import cloud_platform.providers.leaseweb.cloud_sync as mod
    from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

    offers = _OffersRepoWithOwners(owners or {})
    locations = _LocationsRepo()
    syncer = LeasewebHourlyCloudSyncer(
        lambda: None,  # type: ignore[arg-type]
        accounts=accounts,  # type: ignore[arg-type]
        account_priorities=priorities or {},
        capacity=capacity,
    )
    with (
        patch.object(mod, "SqlAlchemySellableOfferRepository", lambda sf: offers),
        patch(
            "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
            lambda sf: locations,
        ),
    ):
        result = await syncer.sync_all()
    return result, offers, locations


def _pair_view(*, proves_images: bool) -> _CloudProviderFake:
    """One account's view of the incident's region/type pair.

    ``proves_images=False`` scripts the read-only refusal production observed:
    the credential answers with a validation error instead of an image list.
    """
    if proves_images:
        return _CloudProviderFake(
            regions=[_region(REGION)],
            types={REGION: [_ctype(TYPE, REGION)]},
            images={REGION: [_image()]},
        )
    return _CloudProviderFake(
        regions=[_region(REGION)],
        types={REGION: [_ctype(TYPE, REGION)]},
        images_error=LeasewebValidationError('The value "eu-central-1" is not valid region.'),
    )


class TestCapacityExcludesNewOrders:
    async def test_a_limited_account_is_not_published_and_its_row_is_kept(self) -> None:
        """The row survives (provenance/reconciliation) but is not for sale."""
        north = _pair_view(proves_images=True)
        result, offers, _ = await _run(
            {"sales-org-north": north},
            capacity=_CapacityStore({"sales-org-north": _limited("sales-org-north")}),
            owners={(TYPE, REGION): "sales-org-north"},
        )
        assert offers.upserts, "the observation is still stored"
        update = offers.upserts[0]["update"]
        assert update.provider_available is False
        # Provenance is preserved: the account that owns the row is unchanged.
        assert offers.upserts[0]["provider_account_id"] == "sales-org-north"
        assert result.verified == frozenset()
        assert result.verified_accounts == frozenset()
        assert any("capacity-limit" in warning for warning in result.warnings)
        # No provider mutation, ever: reads only.
        assert north.posts == 0

    async def test_a_healthy_account_that_proves_the_pair_takes_over(self) -> None:
        """Eligibility changed: publication follows the proven account."""
        north = _pair_view(proves_images=True)
        uk = _pair_view(proves_images=True)
        result, offers, _ = await _run(
            {"sales-org-north": north, "sales-org-uk": uk},
            capacity=_CapacityStore({"sales-org-north": _limited("sales-org-north")}),
            owners={(TYPE, REGION): "sales-org-north"},
            priorities={"sales-org-north": 100, "sales-org-uk": 200},
        )
        update = offers.upserts[0]["update"]
        assert update.provider_available is True
        assert update.provider_account_id == "sales-org-uk"
        assert result.verified_accounts == frozenset({("sales-org-uk", TYPE, REGION)})

    async def test_an_account_that_cannot_prove_the_pair_never_takes_over(self) -> None:
        """The production trap: UK could not prove eu-central-1 images.

        Moving the offer there would advertise a plan the credential cannot
        install (and the earlier incident showed exactly how such a move also
        broke the sync source's persistence).
        """
        north = _pair_view(proves_images=True)
        uk = _pair_view(proves_images=False)
        result, offers, _ = await _run(
            {"sales-org-north": north, "sales-org-uk": uk},
            capacity=_CapacityStore({"sales-org-north": _limited("sales-org-north")}),
            owners={(TYPE, REGION): "sales-org-north"},
            priorities={"sales-org-north": 100, "sales-org-uk": 200},
        )
        update = offers.upserts[0]["update"]
        assert update.provider_available is False
        assert offers.upserts[0]["provider_account_id"] == "sales-org-north"
        assert result.verified_accounts == frozenset()
        # The row is withheld because its own owner is out of capacity — and
        # nothing in the run claims the unproven account took it over.
        assert any("capacity-limit" in warning for warning in result.warnings)
        # The unproven account is never handed the offer in the same run.
        assert all(warning.count("sales-org-uk") == 0 for warning in result.warnings)

    async def test_an_owner_that_is_definitively_refused_is_not_published(self) -> None:
        """The owner's own image read can be definitive: then it is not sellable."""
        refused = _pair_view(proves_images=False)
        result, offers, _ = await _run(
            {"sales-org-uk": refused},
            capacity=_CapacityStore({}),
            owners={(TYPE, REGION): "sales-org-uk"},
        )
        update = offers.upserts[0]["update"]
        assert update.provider_available is False
        assert offers.upserts[0]["provider_account_id"] == "sales-org-uk"
        assert any("account-cannot-serve" in warning for warning in result.warnings)

    async def test_a_fresh_pair_with_only_unproven_accounts_is_not_published(self) -> None:
        """No current owner to preserve: unproven supply is not an offer."""
        uk = _pair_view(proves_images=False)
        result, offers, _ = await _run(
            {"sales-org-uk": uk},
            capacity=_CapacityStore({}),
            owners={},
        )
        assert offers.upserts[0]["update"].provider_available is False
        assert result.verified == frozenset()

    async def test_an_inconclusive_probe_keeps_a_sole_supplier_visible(self) -> None:
        """A timeout is not evidence: inventory must not flap off the storefront."""
        transient = _CloudProviderFake(
            regions=[_region(REGION)],
            types={REGION: [_ctype(TYPE, REGION)]},
            images_error=ProviderUnavailable("read timeout"),
        )
        result, offers, _ = await _run({"solo": transient}, capacity=_CapacityStore({}))
        update = offers.upserts[0]["update"]
        assert update.provider_available is True
        assert update.provider_account_id == "solo"
        assert result.verified_accounts == frozenset({("solo", TYPE, REGION)})

    async def test_an_elapsed_signal_keeps_the_pair_unpublished(self) -> None:
        """An elapsed cooling window is NOT positive proof of recovery.

        This is the exact production shape of 8fc2e573: the refusal (and the
        provider limit) outlived the TTL, the account was treated as eligible
        again, and the next customer order hit the same PC-2031.
        """
        north = _pair_view(proves_images=True)
        result, offers, _ = await _run(
            {"sales-org-north": north},
            capacity=_CapacityStore({"sales-org-north": _limited("sales-org-north", expired=True)}),
            owners={(TYPE, REGION): "sales-org-north"},
        )
        update = offers.upserts[0]["update"]
        assert update.provider_available is False
        assert result.verified_accounts == frozenset()
        assert "capacity-limit" in " ".join(result.warnings)

    async def test_an_operator_clear_republishes_the_pair(self) -> None:
        """A proven recovery (operator clear) is the transition that restores
        NEW-order publication, without waiting for the next full walk."""
        north = _pair_view(proves_images=True)
        cleared = _limited("sales-org-north", expired=True).recovered()
        result, offers, _ = await _run(
            {"sales-org-north": north},
            capacity=_CapacityStore({"sales-org-north": cleared}),
            owners={(TYPE, REGION): "sales-org-north"},
        )
        assert offers.upserts[0]["update"].provider_available is True
        assert result.verified_accounts == frozenset({("sales-org-north", TYPE, REGION)})

    async def test_an_unreadable_capacity_store_never_blinds_the_catalog(self) -> None:
        """Capacity is an extra gate; losing the store is a warning, not an outage."""
        north = _pair_view(proves_images=True)
        result, offers, _ = await _run(
            {"sales-org-north": north},
            capacity=_BrokenCapacityStore(),
            owners={(TYPE, REGION): "sales-org-north"},
        )
        assert offers.upserts[0]["update"].provider_available is True
        assert "capacity state unreadable" in " ".join(result.warnings)

    async def test_an_unreadable_owner_map_is_conservative_not_re_routing(self) -> None:
        """Without the map, only a PROVEN capability publishes (no guessing)."""
        from unittest.mock import patch

        import cloud_platform.providers.leaseweb.cloud_sync as mod
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        offers = _OffersRepo([])  # has no ownership reader at all
        syncer = LeasewebHourlyCloudSyncer(
            lambda: None,  # type: ignore[arg-type]
            accounts={
                "solo": _CloudProviderFake(  # type: ignore[dict-item]
                    regions=[_region(REGION)],
                    types={REGION: [_ctype(TYPE, REGION)]},
                    images={REGION: [_image()]},
                )
            },
            capacity=_CapacityStore({}),
        )
        with (
            patch.object(mod, "SqlAlchemySellableOfferRepository", lambda sf: offers),
            patch(
                "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
                lambda sf: _LocationsRepo(),
            ),
        ):
            result = await syncer.sync_all()
        assert offers.upserts[0]["update"].provider_available is True
        assert result.verified_accounts == frozenset({("solo", TYPE, REGION)})

    async def test_an_owner_keeps_its_row_when_a_transient_probe_hits(self) -> None:
        """Never move a region because one read was inconclusive."""
        north = _CloudProviderFake(
            regions=[_region(REGION)],
            types={REGION: [_ctype(TYPE, REGION)]},
            images_error=ProviderUnavailable("timeout"),
        )
        result, offers, _ = await _run(
            {"sales-org-north": north},
            capacity=_CapacityStore({}),
            owners={(TYPE, REGION): "sales-org-north"},
        )
        update = offers.upserts[0]["update"]
        assert update.provider_available is True
        assert update.provider_account_id == "sales-org-north"
        assert result.verified_accounts == frozenset({("sales-org-north", TYPE, REGION)})

    async def test_no_provider_mutation_ever_happens_during_routing(self) -> None:
        north = _pair_view(proves_images=True)
        uk = _pair_view(proves_images=True)
        await _run(
            {"sales-org-north": north, "sales-org-uk": uk},
            capacity=_CapacityStore({"sales-org-north": _limited("sales-org-north")}),
            owners={(TYPE, REGION): "sales-org-north"},
            priorities={"sales-org-north": 100, "sales-org-uk": 200},
        )
        assert north.posts == 0
        assert uk.posts == 0
        assert north.reads, "the routing decision still needs read evidence"
        assert all(read.startswith(("regions", "types:", "probe:")) for read in north.reads), (
            north.reads
        )


@pytest.mark.parametrize("state", ["healthy", "limit_reached"])
def test_owner_choice_is_deterministic_for_a_given_state(state: str) -> None:
    """Same inputs, same owner: storefront publication must never flip randomly."""
    from cloud_platform.providers.leaseweb.cloud_sync import select_hourly_owner

    ranked = [
        ("sales-org-north", "ok", _ctype(TYPE, REGION)),
        ("sales-org-uk", "ok", _ctype(TYPE, REGION)),
    ]
    limit_reached = frozenset({"sales-org-north"} if state == "limit_reached" else set())
    first = select_hourly_owner(
        ranked, current_owner_id="sales-org-uk", limit_reached=limit_reached
    )
    second = select_hourly_owner(
        ranked, current_owner_id="sales-org-uk", limit_reached=limit_reached
    )
    assert first == second
    # A limited owner never dislodges the current owner in favour of itself;
    # with a healthy owner in the ranking the outcome is the same either way.
    assert first.account_id == "sales-org-uk"
    assert first.published is True
