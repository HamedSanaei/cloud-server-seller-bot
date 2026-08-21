from cloud_platform.providers.base import CloudProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, CloudProvider] = {}

    def register(self, provider: CloudProvider) -> None:
        if provider.key in self._providers:
            raise ValueError(f"provider already registered: {provider.key}")
        self._providers[provider.key] = provider

    def get(self, key: str) -> CloudProvider:
        try:
            return self._providers[key]
        except KeyError as exc:
            raise KeyError(f"unknown provider: {key}") from exc

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))
