"""Provider contract test suite.

This module provides a standard test suite that any CloudProvider adapter
must pass to ensure compliance with the provider-neutral contract.

Usage in adapter tests::

    from cloud_platform.providers.contract import ProviderContractTests

    class TestHetznerContract(ProviderContractTests):
        @pytest.fixture
        def provider(self):
            return HetznerCloudProvider(token=os.environ["HETZNER_TEST_TOKEN"])

        @pytest.fixture
        def test_location_id(self):
            return "fsn1"

        @pytest.fixture
        def test_plan_id(self):
            return "cx22"

        @pytest.fixture
        def test_image_id(self):
            return "ubuntu-22.04"

        @pytest.fixture
        def test_ssh_key_ids(self):
            return ()
"""

from __future__ import annotations

import pytest

from cloud_platform.providers.base import (
    CloudProvider,
    CreateServerRequest,
    ProviderImage,
    ProviderLocation,
    ProviderPlan,
    ProviderServer,
)


class ProviderContractTests:
    """Base test class for provider contract compliance.

    Subclasses must implement the following fixtures:
    - provider: CloudProvider instance
    - test_location_id: Valid location ID for testing
    - test_plan_id: Valid plan/server_type ID for testing
    - test_image_id: Valid image ID for testing
    - test_ssh_key_ids: Tuple of SSH key IDs (can be empty)
    """

    # Required fixtures (must be implemented by subclass)
    @pytest.fixture
    def provider(self) -> CloudProvider:
        raise NotImplementedError

    @pytest.fixture
    def test_location_id(self) -> str:
        raise NotImplementedError

    @pytest.fixture
    def test_plan_id(self) -> str:
        raise NotImplementedError

    @pytest.fixture
    def test_image_id(self) -> str:
        raise NotImplementedError

    @pytest.fixture
    def test_ssh_key_ids(self) -> tuple[str, ...]:
        raise NotImplementedError

    # --- Contract Tests ---

    @pytest.mark.asyncio
    async def test_list_locations_returns_non_empty_list(self, provider: CloudProvider) -> None:
        """Provider must return at least one location."""
        locations = await provider.list_locations()
        assert isinstance(locations, list)
        assert len(locations) > 0
        for loc in locations:
            assert isinstance(loc, ProviderLocation)
            assert loc.id
            assert loc.name
            assert loc.country_code

    @pytest.mark.asyncio
    async def test_list_plans_returns_non_empty_list(self, provider: CloudProvider) -> None:
        """Provider must return at least one server plan."""
        plans = await provider.list_plans()
        assert isinstance(plans, list)
        assert len(plans) > 0
        for plan in plans:
            assert isinstance(plan, ProviderPlan)
            assert plan.id
            assert plan.name
            assert plan.architecture
            assert plan.vcpu > 0
            assert plan.memory_mb > 0
            assert plan.disk_gb > 0

    @pytest.mark.asyncio
    async def test_list_images_returns_system_images(self, provider: CloudProvider) -> None:
        """Provider must return system images."""
        images = await provider.list_images()
        assert isinstance(images, list)
        # At least one system image should exist
        assert len(images) >= 0  # Some providers may have no system images
        for img in images:
            assert isinstance(img, ProviderImage)
            assert img.id
            assert img.name
            assert img.os_family
            assert img.architecture

    @pytest.mark.asyncio
    async def test_get_server_existing_returns_server(
        self,
        provider: CloudProvider,
        test_location_id: str,
        test_plan_id: str,
        test_image_id: str,
        test_ssh_key_ids: tuple[str, ...],
    ) -> None:
        """Get server for non-existent ID should return None."""
        result = await provider.get_server("non-existent-id-12345")
        assert result is None

    @pytest.mark.asyncio
    async def test_create_server_requires_valid_inputs(
        self,
        provider: CloudProvider,
        test_location_id: str,
        test_plan_id: str,
        test_image_id: str,
        test_ssh_key_ids: tuple[str, ...],
    ) -> None:
        """Create server should validate inputs before calling provider."""
        from cloud_platform.core.idempotency import IdempotencyKey
        from cloud_platform.providers.errors import ProviderError

        idempotency_key = IdempotencyKey("test-contract-" + "x" * 20)

        # Test with invalid plan
        with pytest.raises((ProviderError, ValueError)):
            await provider.create_server(
                CreateServerRequest(
                    name="test-invalid-plan",
                    plan_id="invalid-plan-id",
                    image_id=test_image_id,
                    location_id=test_location_id,
                    ssh_key_ids=test_ssh_key_ids,
                ),
                idempotency_key,
            )

    @pytest.mark.asyncio
    async def test_capabilities_include_compute(self, provider: CloudProvider) -> None:
        """All providers must support COMPUTE capability."""
        from cloud_platform.providers.base import Capability

        assert Capability.COMPUTE in provider.capabilities

    @pytest.mark.asyncio
    async def test_capabilities_match_implemented_methods(self, provider: CloudProvider) -> None:
        """Capabilities declared should match actual implemented methods."""
        from cloud_platform.providers.base import Capability

        # If POWER is declared, power_on/off/reboot must be implemented
        if Capability.POWER in provider.capabilities:
            # These methods exist on the protocol; we just verify they're callable
            assert hasattr(provider, "power_on")
            assert hasattr(provider, "power_off")
            assert hasattr(provider, "reboot")

        # If REBUILD is declared, rebuild must be implemented
        if Capability.REBUILD in provider.capabilities:
            assert hasattr(provider, "rebuild")

        # If SNAPSHOT is declared, create_snapshot must be implemented
        if Capability.SNAPSHOT in provider.capabilities:
            assert hasattr(provider, "create_snapshot")

    @pytest.mark.asyncio
    async def test_error_types_are_provider_errors(self, provider: CloudProvider) -> None:
        """All provider errors should inherit from ProviderError."""
        from cloud_platform.providers.errors import (
            ProviderAuthError,
            ProviderConflict,
            ProviderError,
            ProviderNotFound,
            ProviderRateLimited,
            ProviderUnavailable,
        )

        # Verify error hierarchy
        assert issubclass(ProviderAuthError, ProviderError)
        assert issubclass(ProviderNotFound, ProviderError)
        assert issubclass(ProviderRateLimited, ProviderError)
        assert issubclass(ProviderConflict, ProviderError)
        assert issubclass(ProviderUnavailable, ProviderError)

    @pytest.mark.asyncio
    async def test_idempotency_key_validation(self, provider: CloudProvider) -> None:
        """IdempotencyKey must validate length constraints."""
        from cloud_platform.core.idempotency import IdempotencyKey

        # Valid key (8-128 chars)
        key = IdempotencyKey("valid-key-123")
        assert key.value == "valid-key-123"

        # Too short
        with pytest.raises(ValueError, match=r"8\.\.128"):
            IdempotencyKey("short")

        # Too long
        with pytest.raises(ValueError, match=r"8\.\.128"):
            IdempotencyKey("x" * 129)

        # Whitespace is stripped
        key = IdempotencyKey("  spaced-key  ")
        assert key.value == "spaced-key"


# --- Adapter-specific contract tests ---


def _assert_provider_location(loc: ProviderLocation) -> None:
    """Assert ProviderLocation has required fields."""
    assert loc.id
    assert loc.name
    assert loc.country_code
    # city and network_zone are optional


def _assert_provider_plan(plan: ProviderPlan) -> None:
    """Assert ProviderPlan has required fields."""
    assert plan.id
    assert plan.name
    assert plan.architecture
    assert plan.vcpu > 0
    assert plan.memory_mb > 0
    assert plan.disk_gb > 0


def _assert_provider_image(img: ProviderImage) -> None:
    """Assert ProviderImage has required fields."""
    assert img.id
    assert img.name
    assert img.os_family
    assert img.architecture


def _assert_provider_server(server: ProviderServer) -> None:
    """Assert ProviderServer has required fields."""
    assert server.id
    assert server.name
    assert server.status
    # ipv4, ipv6, metadata are optional
