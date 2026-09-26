"""A capacity refusal must republish NEW orders NOW, not at the next sync.

Production (2026-09-25):

    06:19:58  catalog_auto_sync finished, publishing through sales-org-north
    06:20:02  server-create:5e4bf88c-... refused: PC-2031 Customer limit reached
    ...       the hourly offers kept advertising sales-org-north for the rest
              of the 15-minute catalog cycle, and 8fc2e573 was accepted into
              an account that had just refused an instance.

The periodic sync is the backstop; this module is the immediate reaction. These
tests pin what that reaction may and may not do:

* a pair the limited account published is re-pinned to another credential ONLY
  when that credential PROVED the exact pair read-only, through the SAME
  ranking helper (and the same observation write) the periodic sync uses;
* an inconclusive or refused probe never wins the pair, and a known-limited
  account is not even probed;
* every remaining pair of the limited account is unpublished — the row and its
  provenance survive, existing servers keep their pinned account, no accepted
  contract is touched and no provider mutation (no second create) ever happens;
* the whole reaction is best effort: a failing store or write is reported, never
  raised into the operation that produced the refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from test_leaseweb_cloud_capacity_routing import (  # type: ignore[import-not-found]
    _BrokenCapacityStore,
    _CapacityStore,
)
from test_leaseweb_hourly_multi_account import (  # type: ignore[import-not-found]
    _CloudProviderFake,
    _ctype,
    _image,
    _OffersRepo,
    _region,
)

from cloud_platform.providers.errors import ProviderUnavailable
from cloud_platform.providers.leaseweb.errors import LeasewebValidationError

PROVIDER = "leaseweb"
REGION = "eu-central-1"
TYPE = "lsw.m4.large"
LIMITED = "sales-org-north"
ALTERNATE = "sales-org-uk"


class _RepublishOffersRepo(_OffersRepo):
    """The offer repository surface the republisher uses, fully recorded."""

    def __init__(
        self,
        owners: dict[tuple[str, str], str],
        *,
        fail_upsert: bool = False,
        fail_mark: bool = False,
    ) -> None:
        super().__init__([])
        self._owners = dict(owners)
        self.fail_upsert = fail_upsert
        self.fail_mark = fail_mark
        self.mark_calls: list[dict[str, Any]] = []

    async def hourly_account_owners(self, provider_key: str) -> dict[tuple[str, str], str]:
        return dict(self._owners)

    async def upsert_from_provider(self, **kwargs: Any) -> Any:
        if self.fail_upsert:
            raise RuntimeError("offer persistence down")
        self.upserts.append(dict(kwargs))
        return None

    async def mark_unavailable(
        self,
        provider_key: str,
        available: Any,
        billing_model: Any = None,
        provider_account_id: str | None = None,
    ) -> int:
        self.mark_calls.append(
            {
                "provider_key": provider_key,
                "available": tuple(available),
                "billing_model": billing_model,
                "provider_account_id": provider_account_id,
            }
        )
        if self.fail_mark:
            raise RuntimeError("publication write down")
        return 3


@dataclass(frozen=True, slots=True)
class _Account:
    account_id: str
    enabled: bool
    priority: int


class _Router:
    """The read-only account router the republisher consults."""

    def __init__(
        self,
        clients: dict[str, _CloudProviderFake],
        *,
        enabled: dict[str, bool] | None = None,
        priorities: dict[str, int] | None = None,
    ) -> None:
        self._clients = dict(clients)
        self._enabled = dict(enabled or {})
        self._priorities = dict(priorities or {})
        self.looked_up: list[str] = []

    @property
    def accounts(self) -> tuple[_Account, ...]:
        return tuple(
            sorted(
                (
                    _Account(
                        account_id=account_id,
                        enabled=self._enabled.get(account_id, True),
                        priority=self._priorities.get(account_id, index * 100),
                    )
                    for index, account_id in enumerate(self._clients)
                ),
                key=lambda account: (account.priority, account.account_id),
            )
        )

    def client_for(self, account_id: str) -> _CloudProviderFake:
        self.looked_up.append(account_id)
        if self._enabled.get(account_id, True) is False:
            raise KeyError(account_id)
        return self._clients[account_id]


def _proves(region: str = REGION, type_id: str = TYPE) -> _CloudProviderFake:
    return _CloudProviderFake(
        regions=[_region(region)],
        types={region: [_ctype(type_id, region)]},
        images={region: [_image()]},
    )


def _cannot_serve(region: str = REGION, type_id: str = TYPE) -> _CloudProviderFake:
    return _CloudProviderFake(
        regions=[_region(region)],
        types={region: [_ctype(type_id, region)]},
        images_error=LeasewebValidationError(f'The value "{region}" is not valid region.'),
    )


async def _republish(
    clients: dict[str, _CloudProviderFake],
    *,
    owners: dict[tuple[str, str], str] | None = None,
    capacity: Any = None,
    enabled: dict[str, bool] | None = None,
    priorities: dict[str, int] | None = None,
    fail_upsert: bool = False,
    fail_mark: bool = False,
) -> tuple[Any, _RepublishOffersRepo, _Router]:
    """Run the REAL republisher with patched repositories (no provider calls)."""
    import cloud_platform.providers.leaseweb.capacity_republish as mod
    from cloud_platform.providers.leaseweb.capacity_republish import (
        LeasewebCapacityRepublisher,
    )

    offers = _RepublishOffersRepo(
        owners if owners is not None else {(TYPE, REGION): LIMITED},
        fail_upsert=fail_upsert,
        fail_mark=fail_mark,
    )
    router = _Router(clients, enabled=enabled, priorities=priorities)
    republisher = LeasewebCapacityRepublisher(
        session_factory=lambda: None,  # type: ignore[arg-type]
        router=router,  # type: ignore[arg-type]
        capacity_repo=capacity,
    )
    with patch.object(mod, "SqlAlchemySellableOfferRepository", lambda _sf: offers):
        report = await republisher.after_capacity_refusal(
            provider_key=PROVIDER, credential_account_id=LIMITED
        )
    return report, offers, router


class TestTargetedRepublication:
    async def test_a_proven_alternate_takes_the_pair_over_immediately(self) -> None:
        """The incident's pair moves off the limited account at once."""
        north, uk = _proves(), _proves()
        report, offers, router = await _republish(
            {LIMITED: north, ALTERNATE: uk},
            capacity=_CapacityStore({LIMITED: _limited()}),
        )

        assert report.pairs_considered == 1
        assert report.rerouted == ((TYPE, REGION, ALTERNATE),)
        assert report.unproven == ()
        assert report.errors == ()

        # The offer was written through the SAME observation translation the
        # periodic sync uses — cost, currency and billing model included.
        assert len(offers.upserts) == 1
        written = offers.upserts[0]
        assert written["provider_key"] == PROVIDER
        assert written["product_id"] == TYPE
        assert written["location_id"] == REGION
        assert written["provider_account_id"] == ALTERNATE
        update = written["update"]
        assert update.provider_account_id == ALTERNATE
        assert update.provider_available is True
        assert update.billing_model == "hourly"
        assert (update.provider_cost_minor, update.provider_cost_currency) == (2, "EUR")
        assert update.billing_parameters["region"] == REGION
        assert update.billing_parameters["instance_type_id"] == TYPE

        # Everything left of the limited account stops taking NEW orders.
        assert offers.mark_calls == [
            {
                "provider_key": PROVIDER,
                "available": (),
                "billing_model": "hourly",
                "provider_account_id": LIMITED,
            }
        ]
        # Read-only proof, in the periodic sync's own order, and never a
        # provider mutation: the refused POST is NEVER re-sent.
        assert uk.reads == ["regions", f"types:{REGION}", f"probe:{REGION}"]
        assert north.reads == []  # the limited account is not re-probed
        assert north.posts == 0 and uk.posts == 0
        assert router.looked_up == [ALTERNATE]

    async def test_no_proven_alternate_means_unpublished_not_reassigned(self) -> None:
        """An account that cannot prove the pair must never inherit it."""
        north, uk = _proves(), _cannot_serve()
        report, offers, _ = await _republish(
            {LIMITED: north, ALTERNATE: uk},
            capacity=_CapacityStore({LIMITED: _limited()}),
        )

        assert report.rerouted == ()
        # The rejection is the syncer's own reason for that pair: this credential
        # definitively cannot serve it — never "inconclusive", never inherited.
        assert report.unproven == ((TYPE, REGION, "account-cannot-serve"),)
        assert report.unpublished == 3
        assert offers.upserts == []
        assert offers.mark_calls[0]["provider_account_id"] == LIMITED

    async def test_an_inconclusive_probe_never_wins_but_a_later_prover_does(self) -> None:
        """A timeout proves nothing; the next account that DOES prove the pair
        takes it, and the inconclusive read is reported, not guessed away."""
        north = _proves()
        uk = _CloudProviderFake(regions_error=ProviderUnavailable("connect timeout"))
        ap = _proves()
        report, offers, _ = await _republish(
            {LIMITED: north, ALTERNATE: uk, "sales-org-ap": ap},
            capacity=_CapacityStore({LIMITED: _limited()}),
            priorities={LIMITED: 100, ALTERNATE: 200, "sales-org-ap": 300},
        )

        assert report.rerouted == ((TYPE, REGION, "sales-org-ap"),)
        assert report.errors == (f"regions[{ALTERNATE}]: ProviderUnavailable",)
        assert offers.upserts[0]["provider_account_id"] == "sales-org-ap"

    async def test_another_limited_account_is_not_even_probed(self) -> None:
        """Capacity knowledge excludes a candidate before any read."""
        north, uk = _proves(), _proves()
        report, offers, router = await _republish(
            {LIMITED: north, ALTERNATE: uk},
            capacity=_CapacityStore({LIMITED: _limited(), ALTERNATE: _limited(ALTERNATE)}),
        )

        assert report.rerouted == ()
        assert report.unproven == ((TYPE, REGION, "no-other-account"),)
        assert router.looked_up == []
        assert uk.reads == []
        assert offers.upserts == []

    async def test_a_disabled_account_is_never_a_candidate(self) -> None:
        north, uk = _proves(), _proves()
        report, offers, router = await _republish(
            {LIMITED: north, ALTERNATE: uk},
            capacity=_CapacityStore({LIMITED: _limited()}),
            enabled={ALTERNATE: False},
        )

        assert report.rerouted == ()
        assert router.looked_up == []
        assert offers.upserts == []

    async def test_an_unreadable_capacity_store_still_republishes(self) -> None:
        """Losing the capacity read may only cost the extra exclusions: the
        refused account is always excluded, and the refresh still runs."""
        north, uk = _proves(), _proves()
        report, offers, _ = await _republish(
            {LIMITED: north, ALTERNATE: uk},
            capacity=_BrokenCapacityStore(),
        )

        assert report.rerouted == ((TYPE, REGION, ALTERNATE),)
        assert offers.mark_calls[0]["provider_account_id"] == LIMITED

    async def test_a_failing_publication_write_is_reported_not_raised(self) -> None:
        """Best effort by contract: the operation that produced the refusal must
        never fail because the catalog could not be refreshed."""
        north, uk = _proves(), _proves()
        report, offers, _ = await _republish(
            {LIMITED: north, ALTERNATE: uk},
            capacity=_CapacityStore({LIMITED: _limited()}),
            fail_upsert=True,
            fail_mark=True,
        )

        assert report.rerouted == ()
        assert report.unproven == ((TYPE, REGION, "persistence-failed"),)
        assert report.errors == (
            f"reassign {TYPE}/{REGION}: RuntimeError",
            "mark_unavailable: RuntimeError",
        )
        assert offers.upserts == []

    async def test_a_pair_the_limited_account_does_not_publish_is_untouched(self) -> None:
        """Nothing to refresh: the reaction is scoped to the account's own rows."""
        north, uk = _proves(), _proves()
        report, offers, router = await _republish(
            {LIMITED: north, ALTERNATE: uk},
            owners={("lsw.mini", "eu-west-3"): ALTERNATE},
            capacity=_CapacityStore({LIMITED: _limited()}),
        )

        assert report.pairs_considered == 0
        assert report.rerouted == ()
        assert offers.upserts == []
        assert router.looked_up == []

    async def test_a_foreign_provider_or_empty_account_is_a_no_op(self) -> None:
        """The hook is provider-neutral in shape and Leaseweb-specific in fact."""
        from cloud_platform.providers.leaseweb.capacity_republish import (
            LeasewebCapacityRepublisher,
        )

        offers = _RepublishOffersRepo({(TYPE, REGION): LIMITED})
        republisher = LeasewebCapacityRepublisher(
            session_factory=lambda: None,  # type: ignore[arg-type]
            router=_Router({LIMITED: _proves()}),  # type: ignore[arg-type]
        )
        import cloud_platform.providers.leaseweb.capacity_republish as mod

        with patch.object(mod, "SqlAlchemySellableOfferRepository", lambda _sf: offers):
            he = await republisher.after_capacity_refusal(
                provider_key="hetzner", credential_account_id=LIMITED
            )
            blank = await republisher.after_capacity_refusal(
                provider_key=PROVIDER, credential_account_id="  "
            )
        assert (he.rerouted, he.pairs_considered) == ((), 0)
        assert (blank.rerouted, blank.pairs_considered) == ((), 0)
        assert offers.upserts == []

    async def test_a_failing_owner_read_degrades_to_a_no_op(self) -> None:
        """If the current owners cannot be read, the sync stays the backstop."""

        class _BrokenOwners(_RepublishOffersRepo):
            async def hourly_account_owners(self, provider_key: str) -> dict[tuple[str, str], str]:
                raise RuntimeError("owners unreadable")

        from cloud_platform.providers.leaseweb.capacity_republish import (
            LeasewebCapacityRepublisher,
        )

        offers = _BrokenOwners({})
        republisher = LeasewebCapacityRepublisher(
            session_factory=lambda: None,  # type: ignore[arg-type]
            router=_Router({LIMITED: _proves()}),  # type: ignore[arg-type]
        )
        import cloud_platform.providers.leaseweb.capacity_republish as mod

        with patch.object(mod, "SqlAlchemySellableOfferRepository", lambda _sf: offers):
            report = await republisher.after_capacity_refusal(
                provider_key=PROVIDER, credential_account_id=LIMITED
            )
        assert report.rerouted == ()
        assert report.errors == ()


def _limited(account_id: str = LIMITED) -> Any:
    """One durably limit-reached account (the refusal 8fc2e573 hit)."""
    from datetime import UTC, datetime, timedelta

    from cloud_platform.modules.provider_capacity.domain import (
        AccountCapacity,
        AccountCapacityState,
    )

    observed = datetime.now(UTC) - timedelta(minutes=5)
    return AccountCapacity(
        provider_key=PROVIDER,
        credential_account_id=account_id,
        state=AccountCapacityState.LIMIT_REACHED,
        error_code="PC-2031",
        correlation_id="07376219-7bcd-43d9-a5ea-4128fa57345a",
        location_id=REGION,
        product_id=TYPE,
        observations=1,
        observed_at=observed,
        expires_at=observed + timedelta(hours=1),
    )


@pytest.mark.parametrize("account_id", [LIMITED, ""])
def test_only_a_real_account_triggers_a_republish(account_id: str) -> None:
    """Shape guard: an empty account id is never a reason to touch the catalog."""
    from cloud_platform.providers.leaseweb.capacity_republish import CapacityRepublishReport

    report = CapacityRepublishReport(provider_key=PROVIDER, credential_account_id=account_id)
    assert report.pairs_considered == 0
    assert str(report)  # dataclass repr stays printable (no secrets, no crash)
