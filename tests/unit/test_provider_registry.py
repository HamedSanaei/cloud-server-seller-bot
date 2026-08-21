import pytest

from cloud_platform.providers.registry import ProviderRegistry


class DummyProvider:
    key = "dummy"
    capabilities = frozenset()


def test_registry_rejects_duplicate_provider_keys() -> None:
    registry = ProviderRegistry()
    provider = DummyProvider()
    registry.register(provider)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(provider)  # type: ignore[arg-type]
