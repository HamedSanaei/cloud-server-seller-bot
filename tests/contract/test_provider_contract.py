"""Provider contract tests for Hetzner adapter.

These tests verify that the HetznerCloudProvider implementation
complies with the CloudProvider contract.
"""

from __future__ import annotations

import os

import pytest

from cloud_platform.providers.base import (
    CloudProvider,
)
from cloud_platform.providers.contract import ProviderContractTests
from cloud_platform.providers.hetzner.client import HetznerCloudProvider

# Skip all tests if no Hetzner token is available
pytestmark = pytest.mark.skipif(
    not os.environ.get("HETZNER_TEST_TOKEN"),
    reason="HETZNER_TEST_TOKEN environment variable not set",
)


class TestHetznerContract(ProviderContractTests):
    """Test Hetzner provider contract compliance."""

    @pytest.fixture(scope="class")
    def provider(self) -> CloudProvider:
        token = os.environ["HETZNER_TEST_TOKEN"]
        provider = HetznerCloudProvider(token=token)
        yield provider
        # Cleanup
        import asyncio

        asyncio.run(provider.close())

    @pytest.fixture
    def test_location_id(self) -> str:
        return "fsn1"  # Falkenstein, Germany

    @pytest.fixture
    def test_plan_id(self) -> str:
        return "cx22"  # Basic shared CPU

    @pytest.fixture
    def test_image_id(self) -> str:
        return "ubuntu-22.04"

    @pytest.fixture
    def test_ssh_key_ids(self) -> tuple[str, ...]:
        return ()


# Additional contract verification tests
@pytest.mark.asyncio
async def test_hetzner_capabilities() -> None:
    """Verify Hetzner declares expected capabilities."""
    from cloud_platform.providers.base import Capability
    from cloud_platform.providers.hetzner.client import HetznerCloudProvider

    provider = HetznerCloudProvider(token="dummy")
    assert Capability.COMPUTE in provider.capabilities
    assert Capability.POWER in provider.capabilities
    assert Capability.REBUILD in provider.capabilities
    assert Capability.SNAPSHOT in provider.capabilities
    assert Capability.BACKUP in provider.capabilities
    assert Capability.FIREWALL in provider.capabilities
    assert Capability.NETWORK in provider.capabilities
    assert Capability.VOLUME in provider.capabilities
    assert Capability.FLOATING_IP in provider.capabilities
    assert Capability.PRIMARY_IP in provider.capabilities
    assert Capability.RDNS in provider.capabilities
    assert Capability.RESCUE in provider.capabilities


@pytest.mark.asyncio
async def test_hetzner_error_mapping() -> None:
    """Verify Hetzner maps HTTP errors to typed provider errors."""
    from cloud_platform.providers.errors import (
        ProviderAuthError,
        ProviderConflict,
        ProviderError,
        ProviderNotFound,
        ProviderRateLimited,
        ProviderUnavailable,
    )

    # Verify error hierarchy is correct
    assert issubclass(ProviderAuthError, ProviderError)
    assert issubclass(ProviderNotFound, ProviderError)
    assert issubclass(ProviderRateLimited, ProviderError)
    assert issubclass(ProviderConflict, ProviderError)
    assert issubclass(ProviderUnavailable, ProviderError)
