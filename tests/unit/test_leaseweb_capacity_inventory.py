"""Read-only instance census used by automatic capacity recovery.

The recovery controller learns a refusal against a BASELINE instance count and
treats a later LOWER count as evidence that capacity may have been freed. That
makes the census safety-critical in both directions:

* it must be complete, because a truncated list is a lower bound and would look
  exactly like "instances were freed" (opening a window that costs a real
  customer order);
* it must be an ACCOUNT-scoped read, because Leaseweb Public Cloud credentials
  are region-scoped: the ``region`` query parameter is rejected for every region
  outside the credential's own one, so a per-region walk reports every region
  unreadable and the census is permanently unknown.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from cloud_platform.modules.provider_capacity.recovery import inventory_ids_hash
from cloud_platform.providers.errors import ProviderUnavailable
from cloud_platform.providers.leaseweb.capacity_inventory import LeasewebCloudInventorySource
from cloud_platform.providers.leaseweb.cloud import CloudInstance, CloudInstancesRead

ACCOUNT = "sales-org-north"
OTHER = "sales-org-uk"


class _Definition:
    """The slice of a configured credential account the source reads."""

    def __init__(self, account_id: str, *, enabled: bool = True) -> None:
        self.account_id = account_id
        self.enabled = enabled


class _FakeProvider:
    """Stands in for ``LeasewebHourlyCloudProvider``.

    Deliberately exposes ONLY the account-scoped census read: a source that fell
    back to a region-filtered walk would fail here the same way it fails against
    the live API for a non-entitled region.
    """

    def __init__(self, *, read: CloudInstancesRead | None = None, error: Exception | None = None):
        self._read = read
        self._error = error
        self.calls: list[str] = []

    async def read_all_instances(self) -> CloudInstancesRead:
        self.calls.append("read_all_instances")
        if self._error is not None:
            raise self._error
        assert self._read is not None
        return self._read


class _FakeRouter:
    """The slice of ``LeasewebCloudAccountRouter`` the source reads."""

    def __init__(
        self,
        providers: Mapping[str, _FakeProvider],
        *,
        disabled: Iterable[str] = (),
    ) -> None:
        excluded = set(disabled)
        self._providers = dict(providers)
        self.accounts = tuple(
            _Definition(account_id, enabled=account_id not in excluded) for account_id in providers
        )

    def client_for(self, account_id: str) -> _FakeProvider:
        return self._providers[account_id]


def _instance(instance_id: str, region: str = "eu-central-1") -> CloudInstance:
    return CloudInstance(
        id=instance_id,
        reference=instance_id,
        state="RUNNING",
        region=region,
        instance_type="lsw.m4.large",
    )


def _read(
    instances: Iterable[CloudInstance],
    *,
    raw_items: int | None = None,
    total_count: int | None = None,
    complete: bool = True,
) -> CloudInstancesRead:
    items = tuple(instances)
    return CloudInstancesRead(
        instances=items,
        raw_items=len(items) if raw_items is None else raw_items,
        total_count=total_count,
        complete=complete,
    )


def _source(
    provider: _FakeProvider, *, disabled: Iterable[str] = ()
) -> LeasewebCloudInventorySource:
    return LeasewebCloudInventorySource(
        router=_FakeRouter({ACCOUNT: provider}, disabled=disabled)  # type: ignore[arg-type]
    )


class TestCensuses:
    async def test_a_single_entitled_region_is_a_complete_census(self) -> None:
        """The production regression: one entitled region, one full census."""
        provider = _FakeProvider(read=_read([_instance("i-1"), _instance("i-2")], total_count=2))
        census = (await _source(provider).inventories())[ACCOUNT]
        assert census is not None
        assert census.instance_count == 2
        assert census.regions_read == 1
        assert census.ids_hash == inventory_ids_hash(["i-1", "i-2"])

    async def test_the_census_never_walks_regions(self) -> None:
        """A region-filtered walk cannot be exhaustive for a scoped credential."""
        provider = _FakeProvider(read=_read([]))
        assert not hasattr(provider, "list_instances")
        await _source(provider).inventories()
        assert provider.calls == ["read_all_instances"]

    async def test_an_account_holding_nothing_is_a_census_not_an_unknown(self) -> None:
        """Zero instances is a FACT (one live account holds none), not a failure."""
        provider = _FakeProvider(read=_read([], total_count=0))
        census = (await _source(provider).inventories())[ACCOUNT]
        assert census is not None
        assert census.instance_count == 0
        assert census.regions_read == 0
        assert census.ids_hash == inventory_ids_hash([])

    async def test_instances_in_several_regions_are_counted_once(self) -> None:
        provider = _FakeProvider(
            read=_read(
                [_instance("i-1", "eu-central-1"), _instance("i-2", "eu-west-2")],
                total_count=2,
            )
        )
        census = (await _source(provider).inventories())[ACCOUNT]
        assert census is not None
        assert census.instance_count == 2
        assert census.regions_read == 2

    async def test_the_census_is_order_proof(self) -> None:
        """Two reads of the same instances must hash identically.

        Otherwise a reshuffled page would look like "the set changed" and the
        decrease test could fire on nothing.
        """
        forward = _FakeProvider(read=_read([_instance("i-1"), _instance("i-2")]))
        backward = _FakeProvider(read=_read([_instance("i-2"), _instance("i-1")]))
        first = (await _source(forward).inventories())[ACCOUNT]
        second = (await _source(backward).inventories())[ACCOUNT]
        assert first is not None and second is not None
        assert first.ids_hash == second.ids_hash


class TestFailClosed:
    async def test_a_truncated_census_is_never_a_census(self) -> None:
        """A lower bound would masquerade as freed capacity."""
        provider = _FakeProvider(read=_read([_instance("i-1")], total_count=9, complete=False))
        assert (await _source(provider).inventories())[ACCOUNT] is None

    async def test_unreadable_entries_are_schema_drift_not_an_absence(self) -> None:
        """A row the parser drops would silently shrink the count."""
        provider = _FakeProvider(read=_read([_instance("i-1")], raw_items=2, total_count=2))
        assert (await _source(provider).inventories())[ACCOUNT] is None

    async def test_a_provider_failure_is_unknown_not_empty(self) -> None:
        provider = _FakeProvider(error=ProviderUnavailable("timed out"))
        assert (await _source(provider).inventories())[ACCOUNT] is None

    async def test_a_disabled_account_needs_no_recovery(self) -> None:
        """A disabled credential receives no orders, so it is not censused."""
        provider = _FakeProvider(read=_read([_instance("i-1")]))
        result = await _source(provider, disabled=[ACCOUNT]).inventories()
        assert result == {}
        assert provider.calls == []

    async def test_every_enabled_account_is_censused_independently(self) -> None:
        """One unknown account must not hide another account's real census."""
        healthy = _FakeProvider(read=_read([_instance("i-1")]))
        unknown = _FakeProvider(error=ProviderUnavailable("timed out"))
        source = LeasewebCloudInventorySource(
            router=_FakeRouter({ACCOUNT: healthy, OTHER: unknown})  # type: ignore[arg-type]
        )
        result = await source.inventories()
        assert result[ACCOUNT] is not None
        assert result[ACCOUNT].instance_count == 1  # type: ignore[union-attr]
        assert result[OTHER] is None


class TestPortContract:
    def test_the_read_result_defaults_to_complete_and_envelope_free(self) -> None:
        """``complete`` must default to True only for an explicitly whole read."""
        read = CloudInstancesRead(instances=(), raw_items=0)
        assert read.complete is True
        assert read.total_count is None

    def test_the_source_exposes_only_the_read_only_census_port(self) -> None:
        provider: Any = _FakeProvider(read=_read([]))
        source = _source(provider)
        assert callable(source.inventories)
        assert not hasattr(source, "create_instance")
