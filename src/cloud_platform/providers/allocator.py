"""Provider account allocator interface.

This module provides the allocator abstraction for selecting a provider account
based on policy, without coupling to compute operations.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from cloud_platform.providers.base import Capability, CloudProvider


class AllocationPolicy(StrEnum):
    """Policy for selecting a provider account."""

    ROUND_ROBIN = "round_robin"
    LEAST_USED = "least_used"
    CAPABILITY_FIRST = "capability_first"
    REGION_AWARE = "region_aware"


@dataclass(frozen=True, slots=True)
class AllocationRequest:
    """Request for provider account allocation."""

    required_capabilities: frozenset[Capability]
    preferred_region: str | None = None
    policy: AllocationPolicy = AllocationPolicy.CAPABILITY_FIRST
    excluded_account_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """Result of a provider account allocation."""

    provider: CloudProvider
    account_id: str
    score: float


class ProviderAllocator(Protocol):
    """Protocol for provider account allocation."""

    async def allocate(self, request: AllocationRequest) -> AllocationResult:
        """Allocate a provider account based on the request.

        Args:
            request: Allocation request with capabilities, region, and policy

        Returns:
            AllocationResult with selected provider and account

        Raises:
            AllocationError: If no suitable account is available
        """
        ...


class AllocationError(RuntimeError):
    """Raised when no suitable provider account can be allocated."""

    pass


class NoSuitableAccountError(AllocationError):
    """Raised when no account matches the required capabilities."""

    pass


class AllAccountsExhaustedError(AllocationError):
    """Raised when all matching accounts are excluded or exhausted."""

    pass


@dataclass(frozen=True, slots=True)
class ScoredAccount:
    """A provider account with its allocation score."""

    provider: CloudProvider
    account_id: str
    score: float
    capabilities: frozenset[Capability]
    region: str | None


class BaseProviderAllocator:
    """Base implementation of provider allocator with common logic."""

    def __init__(self, providers: Sequence[CloudProvider]) -> None:
        self._providers = list(providers)

    def _get_provider_region(self, provider: CloudProvider) -> str | None:
        """Extract region from provider if available.

        This is a placeholder - real implementations would have region metadata
        on the provider or provider account.
        """
        # Try to get region from provider metadata if available
        if hasattr(provider, "region"):
            return getattr(provider, "region", None)
        return None

    def _filter_accounts(
        self,
        required_capabilities: frozenset[Capability],
        excluded_account_ids: frozenset[str],
        preferred_region: str | None = None,
    ) -> list[ScoredAccount]:
        """Filter and score provider accounts based on capabilities and region.

        Args:
            required_capabilities: Required capabilities for the workload
            excluded_account_ids: Account IDs to exclude
            preferred_region: Preferred region for allocation

        Returns:
            List of scored accounts meeting requirements, sorted by score desc
        """
        scored: list[ScoredAccount] = []

        for provider in self._providers:
            # Check if provider has required capabilities
            if not required_capabilities.issubset(provider.capabilities):
                continue

            provider_region = self._get_provider_region(provider)

            # Score based on capability match:
            # - Providers with exactly the required capabilities get highest score (1.0)
            # - Providers with extra capabilities get penalized
            required_count = len(required_capabilities)
            provider_count = len(provider.capabilities)
            extra_capabilities = max(0, provider_count - required_count)
            # Score: 1.0 for exact match, decreasing for each extra capability
            capability_score = 1.0 / (1.0 + extra_capabilities * 0.2)

            # Region preference: boost score if provider matches preferred region
            region_score = 0.0
            provider_region = self._get_provider_region(provider)
            if preferred_region and provider_region:
                region_score = 1.0 if provider_region == preferred_region else 0.0
            elif preferred_region and not provider_region:
                region_score = 0.0  # Can't verify region match
            else:
                region_score = 0.5  # No region preference

            # Load score (placeholder - would integrate with actual load metrics)
            load_score = 1.0

            # Weight: capability 50%, region 30%, load 20%
            total_score = capability_score * 0.5 + region_score * 0.3 + load_score * 0.2

            scored.append(
                ScoredAccount(
                    provider=provider,
                    account_id=f"{provider.key}-default",
                    score=total_score,
                    capabilities=provider.capabilities,
                    region=provider_region,
                )
            )

        # Remove excluded accounts
        scored = [a for a in scored if a.account_id not in excluded_account_ids]

        # Sort by score descending
        scored.sort(key=lambda a: a.score, reverse=True)
        return scored

    async def allocate(self, request: AllocationRequest) -> AllocationResult:
        """Allocate a provider account based on the request."""
        scored = self._filter_accounts(
            required_capabilities=request.required_capabilities,
            excluded_account_ids=request.excluded_account_ids,
            preferred_region=request.preferred_region,
        )

        if not scored:
            raise NoSuitableAccountError(
                f"No accounts with capabilities: {request.required_capabilities}"
            )

        # Apply policy-specific selection
        if request.policy == AllocationPolicy.CAPABILITY_FIRST:
            selected = scored[0]
        elif request.policy == AllocationPolicy.LEAST_USED:
            # Would use load metrics in real implementation
            selected = scored[0]
        elif request.policy == AllocationPolicy.REGION_AWARE:
            # Filter by region first
            regional = [a for a in scored if a.region == request.preferred_region]
            selected = regional[0] if regional else scored[0]
        else:  # ROUND_ROBIN
            # Would use round-robin state in real implementation
            selected = scored[0]

        return AllocationResult(
            provider=selected.provider,
            account_id=selected.account_id,
            score=selected.score,
        )


class CompositeAllocator(BaseProviderAllocator):
    """Allocator that tries multiple policies in sequence."""

    def __init__(
        self,
        providers: Sequence[CloudProvider],
        fallback_policies: Sequence[AllocationPolicy] | None = None,
    ) -> None:
        super().__init__(providers)
        self._fallback_policies = fallback_policies or [
            AllocationPolicy.CAPABILITY_FIRST,
            AllocationPolicy.REGION_AWARE,
            AllocationPolicy.LEAST_USED,
            AllocationPolicy.ROUND_ROBIN,
        ]

    async def allocate(self, request: AllocationRequest) -> AllocationResult:
        """Try multiple policies in sequence until one succeeds."""
        last_error: Exception | None = None

        for policy in self._fallback_policies:
            try:
                modified_request = AllocationRequest(
                    required_capabilities=request.required_capabilities,
                    preferred_region=request.preferred_region,
                    policy=policy,
                    excluded_account_ids=request.excluded_account_ids,
                )
                return await super().allocate(modified_request)
            except AllocationError as e:
                last_error = e
                continue

        if last_error:
            raise last_error
        raise AllocationError("All allocation policies failed")


@dataclass(frozen=True, slots=True)
class AccountShard:
    """One provider account shard with load/limit awareness (M16-005)."""

    provider: CloudProvider
    account_id: str
    region: str | None = None
    weight: float = 1.0
    active_servers: int = 0
    max_servers: int = 100

    def __post_init__(self) -> None:
        if self.weight <= 0:
            raise ValueError("shard weight must be > 0")
        if self.active_servers < 0 or self.max_servers <= 0:
            raise ValueError("shard load/limit values are invalid")
        if self.active_servers >= self.max_servers:
            raise ValueError("shard is at capacity")

    @property
    def load_ratio(self) -> float:
        return self.active_servers / self.max_servers


class ShardedAllocator:
    """Load/limit-aware provider-account sharding (M16-005).

    Picks the eligible shard with the lowest ``load_ratio / weight`` (least
    loaded relative to its capacity, adjusted by operator weight). Full
    shards are skipped; excluded ids are skipped; capability mismatches are
    skipped. Deterministic: ties break by account_id.
    """

    def __init__(self, shards: Sequence[AccountShard]) -> None:
        self._shards = list(shards)

    async def allocate(self, request: AllocationRequest) -> AllocationResult:
        candidates: list[tuple[float, AccountShard]] = []
        for shard in self._shards:
            if shard.account_id in request.excluded_account_ids:
                continue
            if shard.active_servers >= shard.max_servers:
                continue
            if not request.required_capabilities.issubset(shard.provider.capabilities):
                continue
            if request.preferred_region and shard.region != request.preferred_region:
                continue
            candidates.append((shard.load_ratio / shard.weight, shard))
        if not candidates:
            raise NoSuitableAccountError(
                f"No shard with capabilities: {request.required_capabilities}"
            )
        candidates.sort(key=lambda item: (item[0], item[1].account_id))
        _, shard = candidates[0]
        return AllocationResult(
            provider=shard.provider, account_id=shard.account_id, score=1.0 - shard.load_ratio
        )
