"""Account sharding allocation outcomes and capacity boundaries."""

import pytest

from cloud_platform.providers.allocator import (
    AccountShard,
    AllocationRequest,
    NoSuitableAccountError,
    ShardedAllocator,
)
from cloud_platform.providers.base import Capability


class _FakeProvider:
    def __init__(self, key: str, capabilities: frozenset[Capability]) -> None:
        self.key = key
        self.capabilities = capabilities


def _shards() -> list[AccountShard]:
    hetzner = _FakeProvider("hetzner", frozenset({Capability.COMPUTE, Capability.POWER}))
    leaseweb = _FakeProvider("leaseweb", frozenset({Capability.COMPUTE, Capability.POWER}))
    return [
        AccountShard(
            provider=hetzner,
            account_id="hetzner-a",
            region="fsn1",
            weight=1.0,
            active_servers=80,
            max_servers=100,
        ),
        AccountShard(
            provider=leaseweb,
            account_id="leaseweb-a",
            region="AMS-01",
            weight=1.0,
            active_servers=10,
            max_servers=100,
        ),
    ]


class TestShardedAllocator:
    async def test_picks_least_loaded_shard(self) -> None:
        allocator = ShardedAllocator(_shards())
        result = await allocator.allocate(
            AllocationRequest(required_capabilities=frozenset({Capability.COMPUTE}))
        )
        assert result.account_id == "leaseweb-a"

    async def test_region_filter(self) -> None:
        allocator = ShardedAllocator(_shards())
        result = await allocator.allocate(
            AllocationRequest(
                required_capabilities=frozenset({Capability.COMPUTE}), preferred_region="fsn1"
            )
        )
        assert result.account_id == "hetzner-a"

    async def test_excluded_and_full_skipped(self) -> None:
        allocator = ShardedAllocator(_shards())
        with pytest.raises(NoSuitableAccountError):
            await allocator.allocate(
                AllocationRequest(
                    required_capabilities=frozenset({Capability.COMPUTE}),
                    excluded_account_ids=frozenset({"hetzner-a", "leaseweb-a"}),
                )
            )

    def test_full_shard_rejected_at_construction(self) -> None:
        hetzner = _FakeProvider("hetzner", frozenset({Capability.COMPUTE}))
        with pytest.raises(ValueError, match="capacity"):
            AccountShard(provider=hetzner, account_id="x", active_servers=100, max_servers=100)
