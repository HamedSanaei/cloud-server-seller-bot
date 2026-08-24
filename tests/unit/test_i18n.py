"""Tests for the locale/message catalog abstraction (M02-007).

Acceptance: Persian default; strings not scattered in handlers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cloud_platform.core.i18n import (
    DEFAULT_LOCALE,
    Locale,
    MessageCatalog,
    Translator,
    UnknownMessageKey,
    get_catalog,
)

ALL_KEYS = [
    "greeting.start",
    "greeting.help",
    "terms.required",
    "terms.accepted",
    "user.frozen",
    "user.banned",
    "user.not_found",
    "wallet.insufficient_balance",
    "wallet.deposited",
    "quota.exceeded",
    "maintenance.blocked",
    "offer.unavailable",
    "offer.show",
    "offer.hide",
    "server.created",
    "server.destroyed",
    "operation.failed",
    "error.unknown",
]


class TestDefaults:
    def test_default_locale_is_persian(self) -> None:
        assert DEFAULT_LOCALE is Locale.FA
        assert get_catalog().locale is Locale.FA
        assert Translator().locale is Locale.FA

    def test_default_text_is_persian(self) -> None:
        t = Translator()
        text = t.t("greeting.start")
        assert text == "به پلتفرم سرور ابری خوش آمدید."
        assert "Welcome" not in text

    def test_english_locale_available(self) -> None:
        t = Translator(Locale.EN)
        assert t.t("greeting.start") == "Welcome to the Cloud Server Platform."


class TestRendering:
    def test_parameter_substitution(self) -> None:
        fa = Translator(Locale.FA).t("wallet.insufficient_balance", balance="5,00")
        assert "{balance}" not in fa
        assert "5,00" in fa
        en = Translator(Locale.EN).t("wallet.insufficient_balance", balance="5,00")
        assert "Current balance: 5,00" in en

    def test_call_alias(self) -> None:
        t = Translator(Locale.EN)
        assert t("greeting.start") == t.t("greeting.start")

    def test_missing_parameter_raises(self) -> None:
        with pytest.raises(KeyError):
            Translator().t("wallet.insufficient_balance")

    def test_unknown_key_raises_in_every_locale(self) -> None:
        for locale in (Locale.FA, Locale.EN):
            with pytest.raises(UnknownMessageKey, match="no"):
                Translator(locale).t("handler.scatter.literal")

    def test_catalog_immutable(self) -> None:
        catalog = get_catalog(Locale.FA)
        with pytest.raises(TypeError):
            catalog.table["greeting.start"] = "x"  # type: ignore[index]


class TestCatalogCompleteness:
    """Both locales must translate every key exactly once (no drift)."""

    def test_fa_and_en_have_identical_key_sets(self) -> None:
        fa = get_catalog(Locale.FA).table
        en = get_catalog(Locale.EN).table
        assert set(fa) == set(en)

    def test_every_documented_key_present(self) -> None:
        fa = get_catalog(Locale.FA).table
        en = get_catalog(Locale.EN).table
        for key in ALL_KEYS:
            assert key in fa
            assert key in en

    def test_placeholder_parity_between_locales(self) -> None:
        """A placeholder in one locale must exist in the other (same params)."""
        import string

        fa = get_catalog(Locale.FA).table
        en = get_catalog(Locale.EN).table
        fmt = string.Formatter()
        for key in fa:
            fa_fields = {f for _, f, _, _ in fmt.parse(fa[key]) if f}
            en_fields = {f for _, f, _, _ in fmt.parse(en[key]) if f}
            assert fa_fields == en_fields, f"placeholder drift in {key}"


class TestBotHandlerUsesCatalog:
    """The Telegram start handler must render from the catalog (Persian)."""

    async def test_start_handler_renders_persian_greeting(self) -> None:
        from cloud_platform.bot import main as bot_main

        message = AsyncMock()
        await bot_main.start(message)  # type: ignore[arg-type]

        message.answer.assert_awaited_once()
        sent = message.answer.call_args.args[0]
        assert sent == get_catalog(Locale.FA).table["greeting.start"]
        assert "starter is running" not in sent  # old scattered literal is gone


class TestCustomCatalog:
    def test_injected_catalog_wins(self) -> None:
        custom = MessageCatalog(
            locale=Locale.FA,
            table={"greeting.start": "سلام"},
        )
        t = Translator(catalog=custom)
        assert t.t("greeting.start") == "سلام"
        with pytest.raises(UnknownMessageKey):
            t.t("greeting.help")
