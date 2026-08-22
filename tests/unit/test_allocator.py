"""Tests for provider allocator."""

from __future__ import annotations

import pytest

from cloud_platform.providers.allocator import (
    AllAccountsExhaustedError,
    AllocationError,
    AllocationPolicy,
    AllocationRequest,
    AllocationResult,
    BaseProviderAllocator,
    Capability,
    CompositeAllocator,
    NoSuitableAccountError,
)


class MockProvider:
    """Mock provider for testing."""

    def __init__(
        self,
        key: str,
        capabilities: frozenset[Capability],
        region: str | None = None,
    ) -> None:
        self.key = key
        self.capabilities = capabilities
        self.region = region


class MockAllocator(BaseProviderAllocator):
    """Test allocator using mock providers."""

    def __init__(self, providers: list[MockProvider]) -> None:
        super().__init__(providers)


def test_allocation_policy_enum() -> None:
    assert AllocationPolicy.ROUND_ROBIN == "round_robin"
    assert AllocationPolicy.LEAST_USED == "least_used"
    assert AllocationPolicy.CAPABILITY_FIRST == "capability_first"
    assert AllocationPolicy.REGION_AWARE == "region_aware"


def test_allocation_request_creation() -> None:
    request = AllocationRequest(
        required_capabilities=frozenset({Capability.COMPUTE}),
        preferred_region="eu-central",
        policy=AllocationPolicy.CAPABILITY_FIRST,
        excluded_account_ids=frozenset({"account-1"}),
    )
    assert request.required_capabilities == frozenset({Capability.COMPUTE})
    assert request.preferred_region == "eu-central"
    assert request.policy == AllocationPolicy.CAPABILITY_FIRST
    assert request.excluded_account_ids == frozenset({"account-1"})


def test_allocation_result_creation() -> None:
    provider = MockProvider(key="test", capabilities=frozenset({Capability.COMPUTE}))
    result = AllocationResult(provider=provider, account_id="acc-1", score=0.9)
    assert result.provider == provider
    assert result.account_id == "acc-1"
    assert result.score == 0.9


def test_allocation_error_hierarchy() -> None:
    assert issubclass(NoSuitableAccountError, AllocationError)
    assert issubclass(AllAccountsExhaustedError, AllocationError)
    assert issubclass(AllocationError, RuntimeError)


@pytest.mark.asyncio
async def test_allocator_basic_allocation() -> None:
    provider = MockProvider(
        key="test",
        capabilities=frozenset({Capability.COMPUTE, Capability.POWER}),
        region="eu-central",
    )
    allocator = MockAllocator([provider])

    request = AllocationRequest(
        required_capabilities=frozenset({Capability.COMPUTE}),
        preferred_region="eu-central",
    )
    result = await allocator.allocate(request)

    assert isinstance(result, AllocationResult)
    assert result.provider.key == "test"
    assert result.account_id == "test-default"


@pytest.mark.asyncio
async def test_allocator_no_suitable_account() -> None:
    provider = MockProvider(
        key="test",
        capabilities=frozenset({Capability.POWER}),  # Missing COMPUTE
    )
    allocator = MockAllocator([provider])

    request = AllocationRequest(
        required_capabilities=frozenset({Capability.COMPUTE}),
    )

    with pytest.raises(NoSuitableAccountError):
        await allocator.allocate(request)


@pytest.mark.asyncio
async def test_allocator_excluded_accounts() -> None:
    provider = MockProvider(
        key="test",
        capabilities=frozenset({Capability.COMPUTE, Capability.POWER}),
    )
    allocator = MockAllocator([provider])

    request = AllocationRequest(
        required_capabilities=frozenset({Capability.COMPUTE}),
        excluded_account_ids=frozenset({"test-default"}),
    )

    with pytest.raises(NoSuitableAccountError):
        await allocator.allocate(request)


@pytest.mark.asyncio
async def test_allocator_capability_first_policy() -> None:
    provider1 = MockProvider(
        key="p1",
        capabilities=frozenset({Capability.COMPUTE, Capability.POWER, Capability.SNAPSHOT}),
    )
    provider2 = MockProvider(
        key="p2",
        capabilities=frozenset({Capability.COMPUTE}),
    )
    allocator = MockAllocator([provider1, provider2])

    request = AllocationRequest(
        required_capabilities=frozenset({Capability.COMPUTE}),
        policy=AllocationPolicy.CAPABILITY_FIRST,
    )
    result = await allocator.allocate(request)

    # CAPABILITY_FIRST prefers exact capability match (fewer extra capabilities)
    assert result.provider.key == "p2"


@pytest.mark.asyncio
async def test_allocator_region_aware_policy() -> None:
    provider1 = MockProvider(
        key="p1",
        capabilities=frozenset({Capability.COMPUTE}),
        region="us-east",
    )
    provider2 = MockProvider(
        key="p2",
        capabilities=frozenset({Capability.COMPUTE}),
        region="eu-central",
    )
    allocator = MockAllocator([provider1, provider2])

    request = AllocationRequest(
        required_capabilities=frozenset({Capability.COMPUTE}),
        preferred_region="eu-central",
        policy=AllocationPolicy.REGION_AWARE,
    )
    result = await allocator.allocate(request)

    assert result.provider.key == "p2"


@pytest.mark.asyncio
async def test_composite_allocator_fallback() -> None:
    """Test composite allocator tries fallback policies."""
    provider = MockProvider(
        key="test",
        capabilities=frozenset({Capability.COMPUTE}),
    )
    allocator = CompositeAllocator([provider])

    request = AllocationRequest(
        required_capabilities=frozenset({Capability.COMPUTE}),
        excluded_account_ids=frozenset(),  # No exclusions
    )
    result = await allocator.allocate(request)

    assert isinstance(result, AllocationResult)
    assert result.provider.key == "test"


@pytest.mark.asyncio
async def test_composite_allocator_exhausts_policies() -> None:
    """Test composite allocator raises error when all policies fail."""
    provider = MockProvider(
        key="test",
        capabilities=frozenset({Capability.COMPUTE}),
    )
    allocator = CompositeAllocator([provider])

    request = AllocationRequest(
        required_capabilities=frozenset({Capability.COMPUTE}),
        excluded_account_ids=frozenset({"test-default"}),
    )

    with pytest.raises(AllocationError):
        await allocator.allocate(request)


@pytest.mark.asyncio
async def test_allocator_multiple_capabilities() -> None:
    provider = MockProvider(
        key="test",
        capabilities=frozenset(
            {
                Capability.COMPUTE,
                Capability.POWER,
                Capability.SNAPSHOT,
                Capability.BACKUP,
            }
        ),
    )
    allocator = MockAllocator([provider])

    request = AllocationRequest(
        required_capabilities=frozenset(
            {Capability.COMPUTE, Capability.POWER, Capability.SNAPSHOT}
        ),
    )
    result = await allocator.allocate(request)

    assert result.provider.key == "test"


def test_allocation_request_defaults() -> None:
    request = AllocationRequest(required_capabilities=frozenset({Capability.COMPUTE}))
    assert request.preferred_region is None
    assert request.policy == AllocationPolicy.CAPABILITY_FIRST
    assert request.excluded_account_ids == frozenset()
