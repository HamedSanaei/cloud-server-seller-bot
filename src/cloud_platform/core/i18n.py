"""Locale and message catalog abstraction (M02-007).

User-facing strings live in ONE place — the message catalogs below — instead
of being scattered across bot/API handlers. The platform's default locale is
Persian (``fa``); English (``en``) is available for support staff and tests.

Usage::

    from cloud_platform.core.i18n import Translator

    t = Translator()                 # Persian (the default)
    await message.answer(t.t("greeting.start"))

    t = Translator(Locale.EN)        # explicit locale
    text = t.t("wallet.insufficient_balance", balance="5,00")

Design rules:

- Keys are stable identifiers (``dot.namespace``); they are the public
  contract between handlers and the catalogs and must not change casually.
- A catalog entry missing in the requested locale is a bug: ``t`` raises
  :class:`UnknownMessageKey` rather than silently falling back, so new
  handlers cannot drift back to scattered literals.
- Templates use ``str.format`` placeholders (``{name}``).
- Domain modules keep their English exception messages — those are log and
  operator text. Only user-facing handler output goes through this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Locale(StrEnum):
    """Supported UI locales."""

    FA = "fa"  # Persian — the platform default
    EN = "en"


DEFAULT_LOCALE: Locale = Locale.FA


class UnknownMessageKey(LookupError):
    """Raised when a message key has no translation in the requested locale."""


_PERSIAN: Mapping[str, str] = MappingProxyType(
    {
        "greeting.start": "به پلتفرم سرور ابری خوش آمدید.",
        "greeting.help": "برای دیدن فهرست دستورها، /help را بفرستید.",
        "terms.required": "شما باید از آخرین شرایط و مقررات پذیرش کنید.",
        "terms.accepted": "شرایط و مقررات ثبت شد.",
        "user.frozen": "حساب شما منجمد شده است. با پشتیبانی تماس بگیرید.",
        "user.banned": "حساب شما مسدود شده است.",
        "user.not_found": "کاربر یافت نشد.",
        "wallet.insufficient_balance": "موجودی کیف پول شما کافی نیست. موجودی فعلی: {balance}",
        "wallet.deposited": "مبلغ {amount} به کیف پول شما افزوده شد.",
        "quota.exceeded": "سقف تعداد سرورهای فعال شما تکمیل شده است.",
        "maintenance.blocked": "سفارش جدید برای این منطقه موقتاً مسدود است.",
        "offer.unavailable": "این آفر در دسترس نیست.",
        "offer.show": "آفر {offer} برای فروش فعال شد.",
        "offer.hide": "آفر {offer} از فروش پنهان شد.",
        "server.created": "سرور شما با موفقیت در حال ساخت است. شناسه: {server_id}",
        "server.destroyed": "سرور {server_id} حذف شد.",
        "operation.failed": "عملیات ناموفق بود: {detail}",
        "error.unknown": "خطای ناشناخته‌ای رخ داد. لطفاً دوباره تلاش کنید.",
    }
)

_ENGLISH: Mapping[str, str] = MappingProxyType(
    {
        "greeting.start": "Welcome to the Cloud Server Platform.",
        "greeting.help": "Send /help to see the command list.",
        "terms.required": "You must accept the latest terms before continuing.",
        "terms.accepted": "Terms acceptance recorded.",
        "user.frozen": "Your account is frozen. Please contact support.",
        "user.banned": "Your account has been banned.",
        "user.not_found": "User not found.",
        "wallet.insufficient_balance": (
            "Your wallet balance is insufficient. Current balance: {balance}"
        ),
        "wallet.deposited": "Amount {amount} was added to your wallet.",
        "quota.exceeded": "Your active server quota is full.",
        "maintenance.blocked": "New orders are temporarily blocked for this region.",
        "offer.unavailable": "This offer is not available.",
        "offer.show": "Offer {offer} is now enabled for sale.",
        "offer.hide": "Offer {offer} is now hidden from sale.",
        "server.created": "Your server is being created. Server id: {server_id}",
        "server.destroyed": "Server {server_id} was destroyed.",
        "operation.failed": "The operation failed: {detail}",
        "error.unknown": "An unexpected error occurred. Please try again.",
    }
)

_CATALOGS: Mapping[Locale, Mapping[str, str]] = MappingProxyType(
    {
        Locale.FA: _PERSIAN,
        Locale.EN: _ENGLISH,
    }
)


@dataclass(frozen=True, slots=True)
class MessageCatalog:
    """The complete message table for one locale (immutable)."""

    locale: Locale
    table: Mapping[str, str]

    def render(self, key: str, **params: object) -> str:
        try:
            template = self.table[key]
        except KeyError:
            raise UnknownMessageKey(f"no {self.locale.value} translation for {key!r}") from None
        return template.format(**params)


def get_catalog(locale: Locale | None = None) -> MessageCatalog:
    """Return the built-in catalog for ``locale`` (default: Persian)."""
    loc = locale or DEFAULT_LOCALE
    return MessageCatalog(locale=loc, table=_CATALOGS[loc])


class Translator:
    """A locale-bound translator for user-facing handler text."""

    def __init__(self, locale: Locale | None = None, catalog: MessageCatalog | None = None) -> None:
        self._catalog = catalog or get_catalog(locale)

    @property
    def locale(self) -> Locale:
        return self._catalog.locale

    def t(self, key: str, **params: object) -> str:
        """Render message ``key`` in the bound locale.

        Raises:
            UnknownMessageKey: When the key is missing in this locale.
            KeyError: When a required placeholder has no parameter.
        """
        return self._catalog.render(key, **params)

    __call__ = t
