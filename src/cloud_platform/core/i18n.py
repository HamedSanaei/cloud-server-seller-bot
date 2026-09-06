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
        "menu.title": "منوی اصلی — یکی از گزینهها را انتخاب کنید:",
        "menu.buy": "🖥️ خرید سرور",
        "menu.servers": "📦 سرورهای من",
        "menu.wallet": "💰 کیف پول",
        "menu.recharge": "⬆️ شارژ حساب",
        "menu.support": "🎧 پشتیبانی",
        "offers.locations_title": "🌍 انتخاب لوکیشن:",
        "offers.location_row": "{code} ({city})",
        "offers.plans_title": "📋 پلن‌های {location}:",
        "offers.plan_row": "{name} — {vcpu} vCPU / {ram} GB / {disk} GB — {price}/ماه",
        "offers.os_title": "🖥️ انتخاب سیستم‌عامل:",
        "offers.os_row": "{name}",
        "offers.confirm_title": "🧾 تأیید نهایی خرید",
        "offers.confirm_offer": "پلن: {name}",
        "offers.confirm_specs": "مشخصات: {vcpu} vCPU / {ram} GB RAM / {disk} GB{traffic}",
        "offers.confirm_os": "سیستم‌عامل: {os}",
        "offers.confirm_location": "لوکیشن: {location}",
        "offers.confirm_price": "قیمت ماهانه: {price}",
        "offers.confirm_billing": "پرداخت پیش‌پرداخت ماهانه — تمدید خودکار از کیف پول",
        "offers.confirm_wallet": "موجودی کیف پول: {balance}",
        "offers.confirm_insufficient": "⚠️ موجودی کافی نیست. لطفاً حساب خود را شارژ کنید.",
        "offers.confirm_button": "✅ تأیید و پرداخت",
        "offers.order_created": "✅ سفارش شما ثبت شد! شناسه سفارش: {order_id}\nپس از آماده‌شدن سرور، مشخصات آن همین‌جا به شما اعلام می‌شود.",  # noqa: E501
        "offers.order_replayed": "این سفارش قبلاً ثبت شده بود — پرداخت دوباره انجام نشد.",
        "offers.unavailable": "این آفر در دسترس نیست.",
        "offers.no_offers": "فعلاً پلنی برای فروش در این لوکیشن موجود نیست.",
        "offers.no_locations": "فعلاً هیچ لوکیشنی برای فروش فعال نیست.",
        "offers.os_unavailable": "سیستم‌عامل انتخابی در دسترس نیست.",
        "offers.products_unavailable": "دریافت اطلاعات پلن ممکن نیست؛ کمی بعد دوباره تلاش کنید.",
        "servers.title": "📦 سرورهای من:",
        "servers.empty": "هنوز سروری ندارید. از «خرید سرور» شروع کنید.",
        "servers.row": "{name} — {state}",
        "servers.detail_title": "🖥️ جزئیات سرور",
        "servers.detail_name": "نام: {name}",
        "servers.detail_state": "وضعیت: {state}",
        "servers.detail_ipv4": "IPv4: {ipv4}",
        "servers.detail_ipv6": "IPv6: {ipv6}",
        "servers.detail_os": "سیستم‌عامل: {os}",
        "servers.detail_location": "لوکیشن: {location}",
        "servers.detail_plan": "پلن: {plan}",
        "servers.detail_price": "قیمت ماهانه: {price}",
        "servers.detail_renewal": "تمدید بعدی: {date}{estimated}",
        "servers.renewal_estimated": " (تخمینی)",
        "servers.detail_order_ref": "کد مرجع پشتیبانی: {ref}",
        "servers.power_on": "🟢 روشن کردن",
        "servers.power_off": "🔴 خاموش کردن",
        "servers.reboot": "🔄 ریبوت",
        "servers.power_confirm_title": "⚠️ تأیید عملیات",
        "servers.power_confirm_text": "آیا مطمئن هستید؟ این عملیات روی سرور شما اعمال می‌شود.",
        "servers.power_confirm_button": "✅ بله، انجام بده",
        "servers.power_done": "✅ عملیات «{action}» روی سرور انجام شد.",
        "servers.power_failed": "عملیات ناموفق بود: {detail}",
        "servers.not_found": "سرور پیدا نشد.",
        "servers.no_control": "کنترل برق برای این سرور در دسترس نیست.",
        "servers.state.requested": "در انتظار ثبت",
        "servers.state.provisioning": "در حال ساخت",
        "servers.state.running": "فعال",
        "servers.state.stopped": "خاموش",
        "servers.state.error": "خطا",
        "servers.state.manual_review": "بررسی دستی",
        "servers.state.deleted": "حذف‌شده",
        "servers.state.delete_requested": "در انتظار حذف",
        "servers.state.deleting": "در حال حذف",
        "servers.actions": "عملیات:",
        "wallet.balance_title": "💰 کیف پول شما",
        "wallet.balance_row": "موجودی: {balance}",
        "wallet.no_wallet": "کیف پولی برای شما ثبت نشده است. برای شارژ با پشتیبانی تماس بگیرید.",
        "wallet.history_button": "📜 تاریخچه تراکنش‌ها",  # noqa: RUF001
        "wallet.history_title": "📜 تاریخچه تراکنش‌ها:",  # noqa: RUF001
        "wallet.history_row": "{date} — {label} — {amount}",
        "wallet.history_empty": "تراکنشی ثبت نشده است.",
        "wallet.entry.deposit": "واریز",
        "wallet.entry.hold": "رزرو",
        "wallet.entry.release": "آزادسازی",
        "wallet.entry.charge": "پرداخت",
        "wallet.entry.refund": "بازگشت وجه",
        "wallet.entry.adjustment": "اصلاح دستی",
        "support.title": "🎧 پشتیبانی",
        "support.text": "برای دریافت کمک، با پشتیبانی در ارتباط باشید. شماره سفارش سرور خود را ذکر کنید.",  # noqa: E501
        "support.contact": "راه ارتباطی: {contact}",
        "nav.menu": "🏠 منوی اصلی",
        "buy.locations_title": "انتخاب دیتاسنتر:",
        "buy.location_row": "{name} — {count} پلن",
        "buy.plans_title": "پلنهای {location}:",
        "buy.plan_row": "{name} — {vcpu} vCPU / {memory} MB / {disk} GB — {price}",
        "buy.empty": "فعلاً سروری برای فروش موجود نیست. بعداً دوباره تلاش کنید.",
        "buy.coming_soon": "🚧 این بخش در حال آمادهسازی است. بهزودی فعال میشود.",
        "buy.os_title": "انتخاب سیستمعامل:",
        "buy.os_row": "{name} ({family})",
        "buy.os_unavailable": "سیستمعاملی برای این پلن در دسترس نیست.",
        "buy.confirm_title": "تأیید خرید",
        "buy.confirm_offer": "سرور: {name}",
        "buy.confirm_price": "قیمت: {price} / هر {minutes} دقیقه",
        "buy.confirm_wallet": "موجودی کیف پول: {balance} — پس از رزرو: {after}",
        "buy.confirm_wallet_insufficient": " (موجودی کافی نیست!)",
        "buy.confirm_button": "✅ تأیید و پرداخت",
        "buy.order_created": "✅ سفارش سرور ثبت شد! شناسه: {server_id}",
        "buy.order_replayed": "این سفارش قبلاً ثبت شده بود.",
        "buy.no_identity": "شناسهی تلگرام شما پیدا نشد. /menu را بفرستید.",
        "buy.no_account": "حساب فعالی برای این ارائهدهنده ندارید.",
        "nav.back": "↩️ بازگشت",
        "nav.cancel": "❌ انصراف",
        "nav.expired": "این دکمه معتبر نیست (منقضی یا دستکاری شده). از منوی اصلی دوباره شروع کنید.",
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
        "error.unknown": "خطای ناشناختهای رخ داد. لطفاً دوباره تلاش کنید.",
        "payment.pending": "پرداخت {amount} در انتظار تأیید درگاه است. پس از پرداخت، «بررسی وضعیت» را بزنید.",  # noqa: E501
        "payment.check": "🔄 بررسی وضعیت پرداخت",
        "payment.success": "پرداخت موفق بود! مبلغ {amount} به کیف پول شما اضافه شد.",
        "payment.failed": "پرداخت ناموفق بود. دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.",
        "payment.still_pending": "پرداخت هنوز تأیید نشده است. چند لحظه دیگر دوباره بررسی کنید.",
        "payment.topup_title": "شارژ کیف پول — مبلغ (تومان):",
    }
)

_ENGLISH: Mapping[str, str] = MappingProxyType(
    {
        "greeting.start": "Welcome to the Cloud Server Platform.",
        "greeting.help": "Send /help to see the command list.",
        "menu.title": "Main menu — pick an option:",
        "menu.buy": "🖥️ Buy a server",
        "menu.servers": "📦 My servers",
        "menu.wallet": "💰 Wallet",
        "menu.recharge": "⬆️ Top up",
        "menu.support": "🎧 Support",
        "offers.locations_title": "🌍 Choose a location:",
        "offers.location_row": "{code} ({city})",
        "offers.plans_title": "📋 Plans at {location}:",
        "offers.plan_row": "{name} — {vcpu} vCPU / {ram} GB / {disk} GB — {price}/month",
        "offers.os_title": "🖥️ Choose an operating system:",
        "offers.os_row": "{name}",
        "offers.confirm_title": "🧾 Final purchase confirmation",
        "offers.confirm_offer": "Plan: {name}",
        "offers.confirm_specs": "Specs: {vcpu} vCPU / {ram} GB RAM / {disk} GB{traffic}",
        "offers.confirm_os": "Operating system: {os}",
        "offers.confirm_location": "Location: {location}",
        "offers.confirm_price": "Monthly price: {price}",
        "offers.confirm_billing": "Prepaid monthly — auto renewal from wallet",
        "offers.confirm_wallet": "Wallet balance: {balance}",
        "offers.confirm_insufficient": "⚠️ Insufficient balance. Please top up first.",
        "offers.confirm_button": "✅ Confirm and pay",
        "offers.order_created": "✅ Your order is registered! Order id: {order_id}\nServer details will be delivered here once it is ready.",  # noqa: E501
        "offers.order_replayed": "This order was already placed — you were not charged twice.",
        "offers.unavailable": "This offer is not available.",
        "offers.no_offers": "No plans are for sale at this location yet.",
        "offers.no_locations": "No locations are enabled for sale yet.",
        "offers.os_unavailable": "The selected operating system is not available.",
        "offers.products_unavailable": "Plan information is currently unavailable; please try again shortly.",  # noqa: E501
        "servers.title": "📦 My servers:",
        "servers.empty": "You have no servers yet. Start with Buy a server.",
        "servers.row": "{name} — {state}",
        "servers.detail_title": "🖥️ Server details",
        "servers.detail_name": "Name: {name}",
        "servers.detail_state": "Status: {state}",
        "servers.detail_ipv4": "IPv4: {ipv4}",
        "servers.detail_ipv6": "IPv6: {ipv6}",
        "servers.detail_os": "OS: {os}",
        "servers.detail_location": "Location: {location}",
        "servers.detail_plan": "Plan: {plan}",
        "servers.detail_price": "Monthly price: {price}",
        "servers.detail_renewal": "Next renewal: {date}{estimated}",
        "servers.renewal_estimated": " (estimated)",
        "servers.detail_order_ref": "Support reference: {ref}",
        "servers.power_on": "🟢 Power on",
        "servers.power_off": "🔴 Power off",
        "servers.reboot": "🔄 Reboot",
        "servers.power_confirm_title": "⚠️ Confirm operation",
        "servers.power_confirm_text": "Are you sure? This operation will be applied to your server.",  # noqa: E501
        "servers.power_confirm_button": "✅ Yes, do it",
        "servers.power_done": "✅ Operation '{action}' completed on your server.",
        "servers.power_failed": "The operation failed: {detail}",
        "servers.not_found": "Server not found.",
        "servers.no_control": "Power control is not available for this server.",
        "servers.state.requested": "pending submission",
        "servers.state.provisioning": "provisioning",
        "servers.state.running": "active",
        "servers.state.stopped": "stopped",
        "servers.state.error": "error",
        "servers.state.manual_review": "manual review",
        "servers.state.deleted": "deleted",
        "servers.state.delete_requested": "pending deletion",
        "servers.state.deleting": "deleting",
        "servers.actions": "Actions:",
        "wallet.balance_title": "💰 Your wallet",
        "wallet.balance_row": "Balance: {balance}",
        "wallet.no_wallet": "No wallet is registered for you. Contact support to top up.",
        "wallet.history_button": "📜 Transaction history",
        "wallet.history_title": "📜 Transaction history:",
        "wallet.history_row": "{date} — {label} — {amount}",
        "wallet.history_empty": "No transactions yet.",
        "wallet.entry.deposit": "deposit",
        "wallet.entry.hold": "hold",
        "wallet.entry.release": "release",
        "wallet.entry.charge": "charge",
        "wallet.entry.refund": "refund",
        "wallet.entry.adjustment": "manual adjustment",
        "support.title": "🎧 Support",
        "support.text": "Contact support for help. Mention your server order id.",
        "support.contact": "Contact channel: {contact}",
        "nav.menu": "🏠 Main menu",
        "buy.locations_title": "Choose a datacenter:",
        "buy.location_row": "{name} — {count} plans",
        "buy.plans_title": "Plans at {location}:",
        "buy.plan_row": "{name} — {vcpu} vCPU / {memory} MB / {disk} GB — {price}",
        "buy.empty": "No servers available yet. Please try again later.",
        "buy.coming_soon": "🚧 This section is being prepared. It will be enabled soon.",
        "buy.os_title": "Choose an operating system:",
        "buy.os_row": "{name} ({family})",
        "buy.os_unavailable": "No operating system is available for this plan.",
        "buy.confirm_title": "Confirm purchase",
        "buy.confirm_offer": "Server: {name}",
        "buy.confirm_price": "Price: {price} / per {minutes} minutes",
        "buy.confirm_wallet": "Wallet balance: {balance} — after hold: {after}",
        "buy.confirm_wallet_insufficient": " (insufficient balance!)",
        "buy.confirm_button": "✅ Confirm and pay",
        "buy.order_created": "✅ Server order created! Id: {server_id}",
        "buy.order_replayed": "This order was already placed.",
        "buy.no_identity": "Your Telegram identity was not found. Send /menu.",
        "buy.no_account": "You have no active account with this provider.",
        "nav.back": "↩️ Back",
        "nav.cancel": "❌ Cancel",
        "nav.expired": (
            "This button is not valid (expired or tampered). Please start again from the main menu."
        ),
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
        "payment.pending": "Payment of {amount} is waiting at the gateway. After paying, tap Check status.",  # noqa: E501
        "payment.check": "🔄 Check payment status",
        "payment.success": "Payment succeeded! {amount} was added to your wallet.",
        "payment.failed": "Payment failed. Please try again or contact support.",
        "payment.still_pending": "Payment is not confirmed yet. Please check again shortly.",
        "payment.topup_title": "Top up wallet — amount (Toman):",
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
