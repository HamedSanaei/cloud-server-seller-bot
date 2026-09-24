"""Worker-job operator-surface coverage (overflow for leaseweb worker jobs).

Hermetic wiring tests for the branches not covered in
``test_leaseweb_worker_jobs.py``: credential gating, Telegram token shaping,
notifier teardown and owned-resource teardown. Every test constructs only
fakes — no database, no network, no redis.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_platform.worker.settings as ws


def _ns(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "leaseweb_api_key": "",
        "hetzner_api_token": "",
        "arvancloud_api_key": "",
        "leaseweb_accounts": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestHasOrderProviderCredentials:
    def test_leaseweb_key_counts(self) -> None:
        assert ws._has_order_provider_credentials(_ns(leaseweb_api_key="k")) is True

    def test_hetzner_token_counts(self) -> None:
        assert ws._has_order_provider_credentials(_ns(hetzner_api_token="h")) is True

    def test_arvan_key_counts(self) -> None:
        assert ws._has_order_provider_credentials(_ns(arvancloud_api_key="a")) is True

    def test_usable_account_counts_without_keys(self) -> None:
        usable = SimpleNamespace(usable=True)
        assert ws._has_order_provider_credentials(_ns(leaseweb_accounts=[usable])) is True

    def test_empty_means_no_credentials(self) -> None:
        assert ws._has_order_provider_credentials(_ns()) is False

    def test_non_iterable_accounts_falls_back_to_truthiness(self) -> None:
        assert ws._has_order_provider_credentials(_ns(leaseweb_accounts=123)) is True
        assert ws._has_order_provider_credentials(_ns(leaseweb_accounts=None)) is False


class TestTelegramBotToken:
    def test_empty_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cloud_platform.core.config.get_settings",
            lambda: SimpleNamespace(telegram_bot_token=""),
        )
        assert ws._telegram_bot_token() is None

    def test_malformed_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for bad in ("CHANGE_ME", "abc:def", "123:", "123:bad token"):
            monkeypatch.setattr(
                "cloud_platform.core.config.get_settings",
                lambda bad=bad: SimpleNamespace(telegram_bot_token=bad),
            )
            assert ws._telegram_bot_token() is None

    def test_valid_shape_returns_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cloud_platform.core.config.get_settings",
            lambda: SimpleNamespace(telegram_bot_token="123456:ok-token"),
        )
        assert ws._telegram_bot_token() == "123456:ok-token"


class TestCloseTelegramNotifiers:
    async def test_none_and_botless_are_noops(self) -> None:
        await ws._close_telegram_notifiers(None)
        await ws._close_telegram_notifiers(())
        # An object without ``_bot`` is skipped, never raises.
        await ws._close_telegram_notifiers((SimpleNamespace(),))

    async def test_successful_close_is_awaited(self) -> None:
        close = AsyncMock()
        bot = SimpleNamespace(session=SimpleNamespace(close=close))
        notifier = SimpleNamespace(_bot=bot)
        await ws._close_telegram_notifiers((notifier,))
        close.assert_awaited_once()

    async def test_failing_close_is_swallowed(self) -> None:
        async def _boom() -> None:
            raise RuntimeError("telegram gone")

        bot = SimpleNamespace(session=SimpleNamespace(close=_boom))
        await ws._close_telegram_notifiers((SimpleNamespace(_bot=bot),))


class TestCloseOwnedResources:
    async def test_skips_none_duplicates_and_closeless(self) -> None:
        shared = MagicMock(aclose=AsyncMock())
        # None, a duplicate and a closeless object must not raise.
        await ws._close_owned_resources([None, shared, shared, SimpleNamespace()])
        shared.aclose.assert_awaited_once()

    async def test_sync_and_async_closers_and_failures(self) -> None:
        async_closed: list[str] = []
        sync_closed: list[str] = []

        class _Async:
            async def aclose(self) -> None:
                async_closed.append("a")

        class _Sync:
            def close(self) -> None:
                sync_closed.append("s")

        class _Failing:
            async def aclose(self) -> None:
                raise RuntimeError("close down")

        await ws._close_owned_resources([_Async(), _Sync(), _Failing()])
        assert async_closed == ["a"]
        assert sync_closed == ["s"]
