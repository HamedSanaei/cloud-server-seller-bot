"""Regression tests for the bot double-initialization incident.

Production crashed the Telegram bot with::

    ValueError: provider already registered: hetzner

because bot startup initialized an already-initialized container:
``get_container()`` initializes exactly once, and ``bot/main.py`` called
``container.initialize()`` a second time.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_platform.core.container import close_container, create_container, get_container
from cloud_platform.providers.registry import ProviderRegistry


def _provider_settings() -> Any:
    """Settings with all three provider credentials configured (fake values)."""
    from cloud_platform.core.config import Settings

    return Settings(
        hetzner_api_token="test-hetzner-token",  # pragma: allowlist secret
        arvancloud_api_key="test-arvan-key",  # pragma: allowlist secret
        leaseweb_api_key="test-leaseweb-key",  # pragma: allowlist secret
    )


@pytest.fixture(autouse=True)
def _patched_provider_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    settings = _provider_settings()
    monkeypatch.setattr("cloud_platform.core.container.get_settings", lambda: settings)
    monkeypatch.setattr("cloud_platform.core.config.get_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
async def _isolated_global_container():
    await close_container()
    yield
    await close_container()


class TestContainerInitializeIdempotency:
    async def test_get_container_returns_an_initialized_container(self) -> None:
        container = await get_container()
        # Initialized means the configured providers are registered exactly once.
        assert container.provider_registry.keys() == ("arvancloud", "hetzner", "leaseweb")

    async def test_double_initialize_registers_nothing_twice(self) -> None:
        container = create_container()
        await container.initialize()
        await container.initialize()
        assert container.provider_registry.keys() == ("arvancloud", "hetzner", "leaseweb")

    async def test_failed_initialize_is_not_marked_and_can_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        container = create_container()
        calls = 0
        real_register = container.provider_registry.register

        def _flaky(provider: Any) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            real_register(provider)

        monkeypatch.setattr(container.provider_registry, "register", _flaky)
        with pytest.raises(RuntimeError, match="boom"):
            await container.initialize()
        # The failure happened on the first of three providers; the retry
        # registers all three, and a further call is a no-op.
        await container.initialize()
        assert calls == 4
        assert container.provider_registry.keys() == ("arvancloud", "hetzner", "leaseweb")
        await container.initialize()
        assert calls == 4

    async def test_close_then_fresh_container_initializes_again(self) -> None:
        first = await get_container()
        await close_container()
        second = await get_container()
        assert second is not first
        assert second.provider_registry.keys() == ("arvancloud", "hetzner", "leaseweb")


class TestRegistryStillRejectsDuplicates:
    def test_two_hetzner_adapters_with_the_same_key_raise(self) -> None:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        registry = ProviderRegistry()
        registry.register(HetznerCloudProvider(token="first"))
        with pytest.raises(ValueError, match="provider already registered: hetzner"):
            registry.register(HetznerCloudProvider(token="second"))

    def test_double_register_providers_without_initialize_guard_raises(self) -> None:
        container = create_container()
        container._register_providers()
        with pytest.raises(ValueError, match="provider already registered: hetzner"):
            container._register_providers()


class TestBotMainLifecycle:
    async def test_bot_main_does_not_initialize_the_container_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bot/main.py must use the get_container() instance as-is."""
        from cloud_platform.bot import main as bot_main

        container = MagicMock()
        container.initialize = AsyncMock()
        container.payment_gateway = MagicMock(return_value=None)
        container.close = AsyncMock()

        async def _get_container() -> MagicMock:
            return container

        monkeypatch.setattr(bot_main, "get_container", _get_container)
        monkeypatch.setattr(bot_main, "Bot", MagicMock())
        monkeypatch.setattr(bot_main, "close_container", AsyncMock())
        settings = MagicMock()
        settings.telegram_bot_token = "123456:" + "A" * 35
        settings.callback_signing_key = "signing"
        settings.support_contact = "support"
        settings.server_management_page_size = 5
        monkeypatch.setattr(bot_main, "get_settings", lambda: settings)
        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock()
        monkeypatch.setattr(bot_main, "dp", dispatcher)

        await bot_main.main()

        container.initialize.assert_not_awaited()
        dispatcher.start_polling.assert_awaited_once()
