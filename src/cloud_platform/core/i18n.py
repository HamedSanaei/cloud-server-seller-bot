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
        "menu.servers": "🖥 سرورهای من",
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
        "offers.os_temporarily_unavailable": "دریافت فهرست سیستم‌عامل‌ها در حال حاضر ممکن نیست؛ لطفاً کمی بعد دوباره تلاش کنید.",  # noqa: E501, RUF001
        "offers.products_unavailable": "دریافت اطلاعات پلن ممکن نیست؛ کمی بعد دوباره تلاش کنید.",
        "servers.title": "🖥 سرورهای من:",
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
        # -- My Servers: management experience (customer-facing) --
        "servers.list_header": "برای مدیریت هر سرور، دکمه «⚙️ مدیریت» آن را بزنید.",
        "servers.manage_button": "⚙️ مدیریت",
        "servers.page": "{page} / {pages}",
        "servers.prev": "⬅️ قبلی",
        "servers.next": "بعدی ➡️",
        "servers.refresh_button": "🔄 بروزرسانی",
        "servers.refresh_done": "✅ اطلاعات سرور بروزرسانی شد.",
        "servers.refresh_partial": "⚠️ بروزرسانی از سمت سرویس‌دهنده کامل نشد؛ مقادیر قبلی نمایش داده می‌شود.",  # noqa: E501
        "servers.manage_title": "⚙️ مدیریت سرور",
        "servers.manage_hint": "یکی از گزینه‌های زیر را انتخاب کنید:",
        "servers.info_button": "📋 مشخصات",
        "servers.detail_header": "🖥 مشخصات سرور",
        "servers.spec_location": "🌍 لوکیشن: {value}",
        "servers.spec_ip": "🌐 IP: {value}",
        "servers.spec_os": "💿 سیستم‌عامل: {value}",
        "servers.spec_plan": "⚙️ پلن: {value}",
        "servers.spec_ram": "🧠 RAM: {value}",
        "servers.spec_cpu": "🧮 CPU: {value}",
        "servers.spec_disk": "💾 فضای دیسک: {value}",
        "servers.spec_traffic": "📊 ترافیک: {used} / {limit}",
        "servers.spec_state": "وضعیت: {value}",
        "servers.spec_started": "📅 شروع سرویس: {value}",
        "servers.spec_ends": "📅 پایان قرارداد: {value}",
        "servers.state.starting": "🟡 در حال روشن شدن",
        "servers.state.stopping": "🟡 در حال خاموش شدن",
        "servers.state.rebooting": "🟡 در حال راه‌اندازی مجدد",
        "servers.state.pending_review": "🟠 در انتظار بررسی",
        "servers.state.unknown": "⚪ نامشخص",
        "servers.traffic_button": "📊 مصرف ترافیک",
        "servers.traffic_title": "📊 مصرف ترافیک",
        "servers.traffic_period": "دوره: {start} تا {end}",
        "servers.traffic_down": "دانلود: {value}",
        "servers.traffic_up": "آپلود: {value}",
        "servers.traffic_total": "مجموع: {value}",
        "servers.traffic_limit": "سقف سرویس: {value}",
        "servers.traffic_unavailable": "⚠️ دریافت اطلاعات ترافیک در حال حاضر ممکن نیست.",
        "servers.console_button": "🖥 کنسول",
        "servers.console_title": "🖥 کنسول موقت",
        "servers.console_open_button": "🌐 باز کردن کنسول",
        "servers.console_note": "این لینک موقت است و پس از مدتی منقضی می‌شود. آن را با کسی به اشتراک نگذارید.",  # noqa: E501
        "servers.console_unavailable": "در حال حاضر کنسول در دسترس نیست.",
        "servers.snapshots_button": "📸 Snapshot",
        "servers.snapshots_title": "📸 Snapshotها",  # noqa: RUF001
        "servers.snapshots_empty": "هنوز Snapshotی برای این سرور ساخته نشده است.",
        "servers.snapshot_row": "{name} — {state} — {date}",
        "servers.snapshot_create_button": "➕ ساخت Snapshot",  # noqa: RUF001
        "servers.snapshot_create_prompt": "نام Snapshot را ارسال کنید (مثلاً before-upgrade).",
        "servers.snapshot_restore_button": "♻️ بازیابی",
        "servers.snapshot_delete_button": "🗑 حذف",
        "servers.snapshot_restore_title": "⚠️ بازیابی Snapshot",
        "servers.snapshot_restore_text": "وضعیت فعلی سرور با این Snapshot جایگزین می‌شود و تغییرات بعد از آن از بین می‌رود.",  # noqa: E501
        "servers.snapshot_delete_title": "⚠️ حذف Snapshot",
        "servers.snapshot_delete_text": "این Snapshot برای همیشه حذف می‌شود و قابل بازگردانی نیست.",
        "servers.reinstall_button": "💿 نصب مجدد",
        "servers.reinstall_title": "⚠️ نصب مجدد سیستم‌عامل",
        "servers.reinstall_choose": "💿 انتخاب سیستم‌عامل جدید:",
        "servers.reinstall_text": "این عملیات باعث حذف اطلاعات فعلی سرور می‌شود و قابل بازگشت نیست.",
        "servers.reinstall_confirm_button": "✅ نصب مجدد",
        "servers.reinstall_empty": "سیستم‌عاملی برای نصب مجدد در دسترس نیست.",
        "servers.password_button": "🔑 بازنشانی رمز",
        "servers.password_title": "⚠️ بازنشانی رمز",
        "servers.password_text": "رمز جدید توسط سرویس‌دهنده ساخته می‌شود و رمز فعلی از کار می‌افتد.",
        "servers.ips_button": "🌐 مدیریت IP",
        "servers.ips_title": "🌐 مدیریت IP",
        "servers.ip_row": "{ip} — {kind}",
        "servers.ip_main": "IP اصلی",
        "servers.ip_secondary": "IP فرعی",
        "servers.ip_null_tag": " (Null route)",
        "servers.ip_rdns_button": "🔤 Reverse DNS",
        "servers.ip_rdns_prompt": "مقدار جدید Reverse DNS را ارسال کنید.",
        "servers.ip_rdns_done": "✅ Reverse DNS ثبت شد.",
        "servers.ip_null_button": "🚫 Null route",
        "servers.ip_unnull_button": "✅ برداشتن Null route",
        "servers.ip_null_title": "⚠️ Null route روی IP",
        "servers.ip_null_text": "این IP از دسترس خارج می‌شود و سرویس روی آن قطع خواهد شد.",
        "servers.ip_not_owned": "این IP به سرور شما تعلق ندارد.",
        "servers.iso_button": "📀 ISO",
        "servers.iso_title": "📀 مدیریت ISO",
        "servers.iso_attach_button": "📀 اتصال ISO",
        "servers.iso_detach_button": "⏏️ جدا کردن ISO",
        "servers.iso_text": "تغییر ISO می‌تواند نحوه بوت سرور را تغییر دهد.",
        "servers.monitoring_button": "📡 مانیتورینگ",
        "servers.monitoring_title": "📡 مانیتورینگ",
        "servers.monitoring_on": "فعال",
        "servers.monitoring_off": "غیرفعال",
        "servers.monitoring_enable_button": "✅ فعال کردن مانیتورینگ",
        "servers.rename_button": "✏️ نام سرور",
        "servers.rename_prompt": "نام جدید سرور را ارسال کنید (حداکثر ۶۴ کاراکتر؛ حروف، عدد، خط تیره و فاصله).",  # noqa: E501
        "servers.rename_done": "✅ نام سرور به «{name}» تغییر کرد.",
        "servers.confirm_title": "⚠️ تأیید عملیات",
        "servers.confirm_button": "✅ انجام بده",
        "servers.confirm_target": "سرور: {target}",
        "servers.confirm_expired": "⏳ زمان تأیید این عملیات به پایان رسیده است. لطفاً از ابتدا تلاش کنید.",  # noqa: E501
        "servers.confirm_replayed": "این عملیات قبلاً تأیید و اجرا شده است — دوباره اجرا نشد.",
        "servers.action_done": "✅ درخواست «{operation}» ارسال شد.\nممکن است اعمال تغییر چند لحظه زمان ببرد.",  # noqa: E501
        "servers.action_in_progress": "⏳ این عملیات همین حالا در حال اجراست؛ دوباره ارسال نشد.",
        "servers.outcome_unknown": "🟠 نتیجه عملیات هنوز مشخص نیست.\n\nبرای جلوگیری از اجرای دوباره، درخواست مجدد ارسال نشد و وضعیت سرور به‌صورت خودکار بررسی می‌شود.",  # noqa: E501, RUF001
        "servers.err_unavailable": "❌ در حال حاضر امکان ارتباط با سرویس‌دهنده وجود ندارد.",
        "servers.err_forbidden": "❌ اجازه انجام این عملیات وجود ندارد.",
        "servers.err_retry": "⚠️ سرویس‌دهنده موقتاً در دسترس نیست. لطفاً کمی بعد دوباره تلاش کنید.",
        "servers.err_generic": "❌ انجام این عملیات ممکن نشد. لطفاً کمی بعد دوباره تلاش کنید.",
        "servers.disabled": "این بخش فعلاً برای شما فعال نیست.",
        "servers.op.start": "روشن کردن",
        "servers.op.stop": "خاموش کردن",
        "servers.op.reboot": "ریبوت",
        "servers.op.reinstall": "نصب مجدد",
        "servers.op.password_reset": "بازنشانی رمز",
        "servers.op.snapshot_create": "ساخت Snapshot",
        "servers.op.snapshot_restore": "بازیابی Snapshot",
        "servers.op.snapshot_delete": "حذف Snapshot",
        "servers.op.ip_null_route": "Null route",
        "servers.op.ip_unnull_route": "برداشتن Null route",
        "servers.op.iso_attach": "اتصال ISO",
        "servers.op.iso_detach": "جدا کردن ISO",
        "servers.op.monitoring_enable": "فعال کردن مانیتورینگ",
        "servers.op.rename": "تغییر نام سرور",
        "servers.op.renew_now": "تمدید اکنون",
        "servers.op.unknown": "عملیات",
        # -- commercial lifecycle (§36-§38) ------------------------------ #
        "servers.commercial.status": "💳 وضعیت سرویس: {value}",
        "servers.commercial.valid_until": "📅 اعتبار تا: {value}",
        "servers.commercial.grace_until": "📅 مهلت پرداخت: {value}",
        "servers.commercial.price": "💵 مبلغ تمدید: {value}",
        "servers.commercial.auto_renew": "🔄 تمدید خودکار: {value}",
        "servers.commercial.on": "فعال",
        "servers.commercial.off": "غیرفعال",
        "servers.commercial.active": "فعال",
        "servers.commercial.payment_due": "نیازمند پرداخت",
        "servers.commercial.grace_period": "در مهلت پرداخت",
        "servers.commercial.suspended": "تعلیق‌شده (تجاری)",
        "servers.commercial.expired": "منقضی‌شده",
        "servers.commercial.attention": "نیازمند بررسی پشتیبانی",
        "servers.commercial.cancelled": "لغو‌شده",
        "servers.commercial.recharge_hint": "💳 برای پرداخت، از منوی اصلی کیف پول خود را شارژ کنید.",  # noqa: E501
        "servers.renew_button": "💳 تمدید اکنون",
        "servers.renew_title": "💳 تمدید سرویس",
        "servers.renew_text": "مبلغ دوره بعدی از کیف پول شما کسر می‌شود.",
        "servers.renew_amount": "💵 مبلغ: {amount}",
        "servers.renew_period": "📅 پایان دوره: {value}",
        "servers.renew_note": "مبلغ از قیمت ثبت‌شده سرویس شما محاسبه می‌شود (نه قیمت لحظه‌ای پروایدر).",  # noqa: E501
        "servers.renew_confirm_button": "✅ پرداخت و تمدید",
        "servers.renew_done": "✅ سرویس شما با موفقیت تمدید شد.\n\nمبلغ: {amount}\n📅 دوره جدید تا: {value}",  # noqa: E501
        "servers.renew_already": "ℹ️ این درخواست قبلاً پردازش شده است.\nدوباره از کیف پول شما کسر نشد.",  # noqa: E501, RUF001
        "servers.renew_insufficient": "⚠️ موجودی کیف پول کافی نیست.\n\nابتدا کیف پول خود را شارژ کنید و سپس دوباره تلاش کنید. سرویس شما فعال می‌ماند.",  # noqa: E501, RUF001
        "servers.renew_not_due": "ℹ️ در حال حاضر تمدیدی سررسید نشده است.",  # noqa: RUF001
        "servers.renew_manual": "🔔 این سرویس بیش از یک دوره عقب است و نیاز به بررسی پشتیبانی دارد.",  # noqa: E501
        "servers.renew_unavailable": "ℹ️ اطلاعات مالی این سرور در دسترس نیست. لطفاً با پشتیبانی تماس بگیرید.",  # noqa: E501, RUF001
        "servers.renew_failed": "❌ تمدید انجام نشد. لطفاً کمی بعد دوباره تلاش کنید.",
        "servers.auto_renew_on_button": "🔄 تمدید خودکار",
        "servers.auto_renew_off_button": "🔕 تمدید خودکار",
        "servers.auto_renew_title": "🔄 تمدید خودکار",
        "servers.auto_renew_on": "وضعیت فعلی: فعال",
        "servers.auto_renew_off": "وضعیت فعلی: غیرفعال",
        "servers.auto_renew_note": "در صورت فعال بودن، مبلغ هر دوره به‌طور خودکار از کیف پول کسر می‌شود.",  # noqa: E501
        "servers.auto_renew_unavailable": "ℹ️ تغییر تمدید خودکار برای این سرور در دسترس نیست.",  # noqa: RUF001
        "servers.unnamed": "سرور بدون نام",
        "servers.list_index": "{index}. {title}",
        "servers.list_ip": "🌐 {ip}",
        "servers.list_ip_pending": "🌐 IP هنوز اختصاص نیافته",
        "servers.list_os": "💿 {os}",
        "servers.list_state": "{state}",
        "servers.list_manage_n": "⚙️ مدیریت {index}",
        "servers.snapshots_list_button": "📋 لیست Snapshotها",  # noqa: RUF001
        "servers.snapshot_create_title": "➕ ساخت Snapshot",  # noqa: RUF001
        "servers.snapshot_create_text": "از وضعیت فعلی سرور یک Snapshot ساخته می‌شود.",
        "servers.confirm_irreversible": "در صورت تأیید، این عملیات روی سرور شما اجرا می‌شود.",
        "servers.confirm_generic_text": "آیا از انجام این عملیات مطمئن هستید؟",
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
        # -- storefront: market -> provider -> location (provider-neutral) --
        "store.market_title": "🛒 نوع سرور را انتخاب کنید:",
        "store.market_iran": "🇮🇷 سرور ایران",
        "store.market_foreign": "🌍 سرور خارج",
        "store.market_title_iran": "🇮🇷 سرور ایران — پروایدر را انتخاب کنید:",
        "store.market_title_foreign": "🌍 سرور خارج — پروایدر را انتخاب کنید:",
        "store.providers_empty": "در این بازار فعلاً سروری برای فروش فعال نشده است.",
        "store.provider_row": "{name} — {count} پلن",
        "store.provider_soon": "{name} — به‌زودی",
        "store.locations_title": "🌍 انتخاب لوکیشن — {provider}:",
        "store.location_row": "{code} — {count} پلن",
        "store.plans_title": "📋 پلن‌های {location} — {provider}:",
        # Aggregated catalog: ONE card per product, its availability listed as
        # locations underneath (a product sold in several datacenters, possibly
        # through several credential accounts, is still one product).
        "store.products_title": "🖥 انتخاب سرور — {provider} ({count} محصول، صفحه {page} از {pages}):",  # noqa: E501
        "store.product_row": "{flags}{name} · {vcpu}C/{ram}GB · {price}",
        "store.products_page": "صفحه {page} از {pages}",
        "store.product_locations_title": "📍 {product} — انتخاب لوکیشن ({count}):",
        "store.product_location_row": "{location} — {price}",
        "store.families_title": "\U0001f4ce {provider}\n\u0645\u062d\u0635\u0648\u0644 \u0631\u0627 \u0627\u0646\u062a\u062e\u0627\u0628 \u06a9\u0646\u06cc\u062f:",  # noqa: E501
        "store.family_row": "{icon} {name} ({billing})",
        "store.family_row_count": "{icon} {name} ({billing}) — {count} پلن",
        "store.family_row_unavailable": "{icon} {name} ({billing}) — موقتاً ناموجود",
        "store.family_unavailable_text": "در حال حاضر پلن قابل فروش برای این محصول موجود نیست.",
        "store.cities_title": "\U0001f30d شهر را انتخاب کنید:",
        "store.city_row": "{flag}{city}",
        "store.city_row_from": "{flag}{city} · از {price}",
        "store.halls_title": "\U0001f3e2 دیتاسنتر {city} را انتخاب کنید:",
        "store.hall_row": "{code} · {count} پلن",
        "store.hall_row_from": "{code} · {count} پلن · شروع از {price}",
        "store.billing.prepaid_monthly_fixed": "\u0645\u0627\u0647\u0627\u0646\u0647",
        "store.billing.hourly": "\u0633\u0627\u0639\u062a\u06cc",
        "store.vps_locations_title": "\U0001f4cd \u0644\u0648\u06a9\u06cc\u0634\u0646 \u0648\u06cc\u200c\u067e\u06cc\u200c\u0627\u0633 \u0645\u0627\u0647\u0627\u0646\u0647 \u062e\u0648\u062f \u0631\u0627 \u0627\u0646\u062a\u062e\u0627\u0628 \u06a9\u0646\u06cc\u062f:",  # noqa: E501
        "store.plan_row": "{vcpu} \u0647\u0633\u062a\u0647 | {ram}GB \u0631\u0645 | {disk} | {price}",  # noqa: E501
        "store.detail_location": "\U0001f4cd \u0645\u0648\u0642\u0639\u06cc\u062a:\n{location}",
        "store.detail_continue": "\u2705 \u0627\u062f\u0627\u0645\u0647 \u0648 \u0627\u0646\u062a\u062e\u0627\u0628 \u0633\u06cc\u0633\u062a\u0645\u200c\u0639\u0627\u0645\u0644",  # noqa: E501
        "store.detail_panel": "\U0001f39b \u067e\u0646\u0644 \u0645\u062f\u06cc\u0631\u06cc\u062a\u06cc: {value}",  # noqa: E501
        "offers.panel_title": "\U0001f39b \u067e\u0646\u0644 \u0645\u062f\u06cc\u0631\u06cc\u062a\u06cc \u0631\u0627 \u0627\u0646\u062a\u062e\u0627\u0628 \u06a9\u0646\u06cc\u062f:",  # noqa: E501
        "offers.panel_none": "\u0628\u062f\u0648\u0646 \u067e\u0646\u0644",
        "offers.confirm_panel": "\U0001f39b \u067e\u0646\u0644: {panel}",
        "store.cloud_locations_title": "\U0001f4cd \u06cc\u06a9 \u0644\u0648\u06a9\u06cc\u0634\u0646 \u0628\u0631\u0627\u06cc {provider} \u0627\u0646\u062a\u062e\u0627\u0628 \u06a9\u0646\u06cc\u062f:",  # noqa: E501
        "store.cloud_families_title": "\U0001f9ec \u0646\u0648\u0639 \u067e\u0644\u0646 \u0631\u0627 \u0627\u0646\u062a\u062e\u0627\u0628 \u06a9\u0646\u06cc\u062f:",  # noqa: E501
        "store.cloud_families_text": "\u0647\u0631 \u0646\u0648\u0639 \u06cc\u06a9 \u062e\u0627\u0646\u0648\u0627\u062f\u0647 \u0645\u062a\u0641\u0627\u0648\u062a \u0627\u0632 \u0645\u0627\u0634\u06cc\u0646 \u0627\u0633\u062a.\n\u0628\u0631\u0627\u06cc \u062f\u06cc\u062f\u0646 \u067e\u0644\u0646\u200c\u0647\u0627 \u06cc\u06a9\u06cc \u0631\u0627 \u0627\u0646\u062a\u062e\u0627\u0628 \u06a9\u0646\u06cc\u062f.",  # noqa: E501
        "store.cloud_plans_title": "\U0001f4be \u06cc\u06a9 \u067e\u0644\u0646 \u0627\u0646\u062a\u062e\u0627\u0628 \u06a9\u0646\u06cc\u062f \u2014 {location}:",  # noqa: E501
        "store.cloud_detail_title": "\u2601\ufe0f {name}",
        "store.cloud_detail_family": "\U0001f9e9 {family}",
        "store.cloud_detail_price": "\U0001f4b5 \u0642\u06cc\u0645\u062a:\n{hourly}\n\u0628\u0631\u0622\u0648\u0631\u062f \u0645\u0627\u0647\u0627\u0646\u0647:\n{monthly}",  # noqa: E501
        "store.price_per_hour": "{price} / \u0633\u0627\u0639\u062a",
        "store.price_per_month": "{price} / \u0645\u0627\u0647",
        "store.cloud_images_title": "💿 سیستم‌عامل را انتخاب کنید:",
        "store.cloud_selected_plan": "🖥 پلن انتخابی: {name}",
        "store.cloud_selected_specs": "⚙️ {vcpu} vCPU · 🧠 {ram} GB · 💾 {disk} GB",
        "store.cloud_selected_price": "💵 {price}",
        "store.cloud_no_images": "در حال حاضر سیستم‌عامل قابل نصب برای این پلن در دسترس نیست.",
        "store.cloud_previous_failed": "درخواست قبلی ساخت سرور ناموفق شده است.\nلطفاً یک سفارش جدید ایجاد کنید.",  # noqa: E501, RUF001
        "store.cloud_account_capacity": "ظرفیت ساخت سرور جدید در حساب ارائه‌دهنده تکمیل شده است.\nلطفاً کمی بعد دوباره تلاش کنید یا پلن/موقعیت دیگری را انتخاب کنید.",  # noqa: E501, RUF001
        "store.cloud_retry_later": "ارتباط با تأمین‌کننده سرویس در حال حاضر برقرار نیست.\nلطفاً چند دقیقه دیگر دوباره تلاش کنید.",  # noqa: E501, RUF001
        "store.cloud_confirm_title": "\u2601\ufe0f \u062a\u0627\u06cc\u06cc\u062f \u0633\u0627\u062e\u062a \u0633\u0631\u0648\u0631 \u0633\u0627\u0639\u062a\u06cc",  # noqa: E501
        "store.cloud_confirm_provider": "\u0627\u0631\u0627\u0626\u0647\u200c\u062f\u0647\u0646\u062f\u0647: {provider}",  # noqa: E501
        "store.cloud_confirm_kind": "\u0646\u0648\u0639: {kind}",
        "store.cloud_confirm_plan": "\u067e\u0644\u0646: {plan}",
        "store.cloud_confirm_location": "\u0644\u0648\u06a9\u06cc\u0634\u0646: {location}",
        "store.cloud_confirm_specs": "CPU: {vcpu} vCPU\nRAM: {ram} GB\nDisk: {disk} GB",
        "store.cloud_confirm_os": "OS: {os}",
        "store.cloud_confirm_cost": "\U0001f4b0 \u0647\u0632\u06cc\u0646\u0647:\n{hourly}\n{monthly}",  # noqa: E501
        "store.cloud_confirm_warning": "\u26a0\ufe0f \u062a\u0627 \u0632\u0645\u0627\u0646\u06cc \u06a9\u0647 Resource \u062d\u0630\u0641 \u0646\u0634\u062f\u0647\u060c \u0647\u0632\u06cc\u0646\u0647 Cloud \u0645\u06cc\u200c\u062a\u0648\u0627\u0646\u062f \u0627\u062f\u0627\u0645\u0647 \u062f\u0627\u0634\u062a\u0647 \u0628\u0627\u0634\u062f.",  # noqa: E501
        "store.cloud_confirm_create": "\u2705 \u0633\u0627\u062e\u062a \u0633\u0631\u0648\u0631",
        "store.cloud_created": "\u2601\ufe0f \u0633\u0631\u0648\u0631 \u0633\u0627\u0639\u062a\u06cc \u062f\u0631\u062e\u0648\u0627\u0633\u062a \u0634\u062f! \u0634\u0646\u0627\u0633\u0647: {server_id}",  # noqa: E501
        "store.detail_title": "🖥 {name}",
        "store.detail_cpu": "⚙️ پردازنده: {vcpu} vCPU",
        "store.detail_ram": "🧠 رم: {ram} GB",
        "store.detail_disk": "💾 دیسک: {disk}",
        "store.detail_traffic": "📦 ترافیک: {traffic}",
        "store.detail_arch": "🏗 معماری: {arch}",
        "store.detail_ipv4": "🌐 IPv4: {value}",
        "store.detail_ipv6": "🌐 IPv6: {value}",
        "store.detail_locations": "📍 موقعیت‌های موجود:",
        "store.detail_location_row": "{location}",
        "store.detail_price": "💳 قیمت ماهانه:\n{price}",
        "store.detail_unknown": "اطلاعات در کاتالوگ ارائه نشده",
        "store.detail_yes": "دارد",
        "store.detail_no": "ندارد",
        "store.price_unavailable": "قیمت تبدیل‌شده لحظه‌ای در دسترس نیست؛ قیمت اصلی اعمال می‌شود.",
        "recharge.title": "⬆️ شارژ کیف پول",
        "recharge.amount_row": "{amount}",
        "recharge.gateway_title": "روش پرداخت را انتخاب کنید:",
        "recharge.gateway_row": "پرداخت با {name}",
        "payments.gateway.tetraminator": "تترامیناتور",
        "payments.gateway.zarinpal": "زرین‌پال",
        # The escaped newline + `{amount}` make this literal mixed-script, which
        # the ambiguous-unicode rule flags even though the Persian text is fine.
        "recharge.created": "🧾 درخواست شارژ به مبلغ {amount} ثبت شد.\nبرای پرداخت روی دکمه زیر بزنید.",  # noqa: E501, RUF001
        "recharge.unavailable": "شارژ آنلاین در حال حاضر فعال نیست. لطفاً با پشتیبانی تماس بگیرید.",
        "recharge.invalid_amount": "مبلغ انتخابی معتبر نیست.",
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
        "nav.prev": "⬅️ قبلی",
        "nav.next": "بعدی ➡️",
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
        "menu.servers": "🖥 My servers",
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
        "offers.os_temporarily_unavailable": "The operating-system list cannot be retrieved right now; please try again shortly.",  # noqa: E501
        "offers.products_unavailable": "Plan information is currently unavailable; please try again shortly.",  # noqa: E501
        "servers.title": "🖥 My servers:",
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
        # -- My Servers: management experience (customer-facing) --
        "servers.list_header": "Tap a server's Manage button to control it.",
        "servers.manage_button": "⚙️ Manage",
        "servers.page": "{page} / {pages}",
        "servers.prev": "⬅️ Previous",
        "servers.next": "Next ➡️",
        "servers.refresh_button": "🔄 Refresh",
        "servers.refresh_done": "✅ Server information refreshed.",
        "servers.refresh_partial": "⚠️ The provider did not answer the refresh; showing the last known values.",  # noqa: E501
        "servers.manage_title": "⚙️ Manage server",
        "servers.manage_hint": "Choose one of the options below:",
        "servers.info_button": "📋 Details",
        "servers.detail_header": "🖥 Server details",
        "servers.spec_location": "🌍 Location: {value}",
        "servers.spec_ip": "🌐 IP: {value}",
        "servers.spec_os": "💿 OS: {value}",
        "servers.spec_plan": "⚙️ Plan: {value}",
        "servers.spec_ram": "🧠 RAM: {value}",
        "servers.spec_cpu": "🧮 CPU: {value}",
        "servers.spec_disk": "💾 Disk: {value}",
        "servers.spec_traffic": "📊 Traffic: {used} / {limit}",
        "servers.spec_state": "Status: {value}",
        "servers.spec_started": "📅 Service start: {value}",
        "servers.spec_ends": "📅 Contract end: {value}",
        "servers.state.starting": "🟡 starting",
        "servers.state.stopping": "🟡 stopping",
        "servers.state.rebooting": "🟡 rebooting",
        "servers.state.pending_review": "🟠 under review",
        "servers.state.unknown": "⚪ unknown",
        "servers.traffic_button": "📊 Traffic usage",
        "servers.traffic_title": "📊 Traffic usage",
        "servers.traffic_period": "Period: {start} to {end}",
        "servers.traffic_down": "Downloaded: {value}",
        "servers.traffic_up": "Uploaded: {value}",
        "servers.traffic_total": "Total: {value}",
        "servers.traffic_limit": "Service limit: {value}",
        "servers.traffic_unavailable": "⚠️ Traffic information is currently unavailable.",
        "servers.console_button": "🖥 Console",
        "servers.console_title": "🖥 Temporary console",
        "servers.console_open_button": "🌐 Open console",
        "servers.console_note": "This link is temporary and expires shortly. Do not share it with anyone.",  # noqa: E501
        "servers.console_unavailable": "Console access is currently unavailable.",
        "servers.snapshots_button": "📸 Snapshots",
        "servers.snapshots_title": "📸 Snapshots",
        "servers.snapshots_empty": "This server has no snapshots yet.",
        "servers.snapshot_row": "{name} — {state} — {date}",
        "servers.snapshot_create_button": "➕ Create snapshot",  # noqa: RUF001
        "servers.snapshot_create_prompt": "Send the snapshot name (for example before-upgrade).",
        "servers.snapshot_restore_button": "♻️ Restore",
        "servers.snapshot_delete_button": "🗑 Delete",
        "servers.snapshot_restore_title": "⚠️ Restore snapshot",
        "servers.snapshot_restore_text": "The server is replaced with this snapshot; later changes are lost.",  # noqa: E501
        "servers.snapshot_delete_title": "⚠️ Delete snapshot",
        "servers.snapshot_delete_text": "This snapshot is deleted permanently and cannot be recovered.",  # noqa: E501
        "servers.reinstall_button": "💿 Reinstall",
        "servers.reinstall_title": "⚠️ Reinstall the operating system",
        "servers.reinstall_choose": "💿 Choose the new operating system:",
        "servers.reinstall_text": "This erases the current server content and cannot be undone.",
        "servers.reinstall_confirm_button": "✅ Reinstall",
        "servers.reinstall_empty": "No image is available for reinstall right now.",
        "servers.password_button": "🔑 Reset password",
        "servers.password_title": "⚠️ Reset password",
        "servers.password_text": "The provider generates a new password and the current one stops working.",  # noqa: E501  # pragma: allowlist secret
        "servers.ips_button": "🌐 IP management",
        "servers.ips_title": "🌐 IP management",
        "servers.ip_row": "{ip} — {kind}",
        "servers.ip_main": "main IP",
        "servers.ip_secondary": "secondary IP",
        "servers.ip_null_tag": " (null routed)",
        "servers.ip_rdns_button": "🔤 Reverse DNS",
        "servers.ip_rdns_prompt": "Send the new reverse DNS value.",
        "servers.ip_rdns_done": "✅ Reverse DNS updated.",
        "servers.ip_null_button": "🚫 Null route",
        "servers.ip_unnull_button": "✅ Remove null route",
        "servers.ip_null_title": "⚠️ Null route this IP",
        "servers.ip_null_text": "The address becomes unreachable and service on it stops.",
        "servers.ip_not_owned": "That IP does not belong to your server.",
        "servers.iso_button": "📀 ISO",
        "servers.iso_title": "📀 ISO management",
        "servers.iso_attach_button": "📀 Attach ISO",
        "servers.iso_detach_button": "⏏️ Detach ISO",
        "servers.iso_text": "Changing the ISO can change how the server boots.",
        "servers.monitoring_button": "📡 Monitoring",
        "servers.monitoring_title": "📡 Monitoring",
        "servers.monitoring_on": "enabled",
        "servers.monitoring_off": "disabled",
        "servers.monitoring_enable_button": "✅ Enable monitoring",
        "servers.rename_button": "✏️ Server name",
        "servers.rename_prompt": "Send the new server name (at most 64 characters: letters, digits, dash, dot, space).",  # noqa: E501
        "servers.rename_done": "✅ Server renamed to “{name}”.",
        "servers.confirm_title": "⚠️ Confirm operation",
        "servers.confirm_button": "✅ Do it",
        "servers.confirm_target": "Server: {target}",
        "servers.confirm_expired": "⏳ This confirmation has expired. Please start again.",
        "servers.confirm_replayed": "This operation was already confirmed and executed — it was not run again.",  # noqa: E501
        "servers.action_done": "✅ The “{operation}” request was sent.\nIt may take a moment to apply.",  # noqa: E501
        "servers.action_in_progress": "⏳ This operation is already running; it was not sent again.",  # noqa: E501
        "servers.outcome_unknown": "🟠 The outcome is not confirmed yet.\n\nThe request was NOT re-sent, and the server state will be checked automatically.",  # noqa: E501
        "servers.err_unavailable": "❌ The provider cannot be reached right now.",
        "servers.err_forbidden": "❌ You are not allowed to perform this operation.",
        "servers.err_retry": "⚠️ The provider is temporarily unavailable. Please try again shortly.",
        "servers.err_generic": "❌ That operation could not be completed. Please try again shortly.",  # noqa: E501
        "servers.disabled": "This section is not enabled for you yet.",
        "servers.op.start": "Power on",
        "servers.op.stop": "Power off",
        "servers.op.reboot": "Reboot",
        "servers.op.reinstall": "Reinstall",
        "servers.op.password_reset": "Reset password",  # pragma: allowlist secret
        "servers.op.snapshot_create": "Create snapshot",
        "servers.op.snapshot_restore": "Restore snapshot",
        "servers.op.snapshot_delete": "Delete snapshot",
        "servers.op.ip_null_route": "Null route",
        "servers.op.ip_unnull_route": "Remove null route",
        "servers.op.iso_attach": "Attach ISO",
        "servers.op.iso_detach": "Detach ISO",
        "servers.op.monitoring_enable": "Enable monitoring",
        "servers.op.rename": "Rename server",
        "servers.op.renew_now": "Renew now",
        "servers.op.unknown": "Operation",
        # -- commercial lifecycle (§36-§38) ------------------------------ #
        "servers.commercial.status": "💳 Service status: {value}",
        "servers.commercial.valid_until": "📅 Valid until: {value}",
        "servers.commercial.grace_until": "📅 Payment deadline: {value}",
        "servers.commercial.price": "💵 Renewal amount: {value}",
        "servers.commercial.auto_renew": "🔄 Auto-renew: {value}",
        "servers.commercial.on": "on",
        "servers.commercial.off": "off",
        "servers.commercial.active": "active",
        "servers.commercial.payment_due": "payment due",
        "servers.commercial.grace_period": "grace period",
        "servers.commercial.suspended": "suspended (commercial)",
        "servers.commercial.expired": "expired",
        "servers.commercial.attention": "support review required",
        "servers.commercial.cancelled": "cancelled",
        "servers.commercial.recharge_hint": "💳 To pay, top up your wallet from the main menu.",
        "servers.renew_button": "💳 Renew now",
        "servers.renew_title": "💳 Renew service",
        "servers.renew_text": "The next period is charged to your wallet.",
        "servers.renew_amount": "💵 Amount: {amount}",
        "servers.renew_period": "📅 Period ends: {value}",
        "servers.renew_note": "The amount comes from your service's recorded price, never a live provider quote.",  # noqa: E501
        "servers.renew_confirm_button": "✅ Pay and renew",
        "servers.renew_done": "✅ Your service was renewed.\n\nAmount: {amount}\n📅 New period until: {value}",  # noqa: E501
        "servers.renew_already": "ℹ️ This request was already processed.\nYour wallet was not charged again.",  # noqa: E501, RUF001
        "servers.renew_insufficient": "⚠️ Your wallet balance is not enough.\n\nTop up your wallet and try again — your service stays active.",  # noqa: E501
        "servers.renew_not_due": "ℹ️ Nothing is due for renewal right now.",  # noqa: RUF001
        "servers.renew_manual": "🔔 This service is more than one period behind and needs support review.",  # noqa: E501
        "servers.renew_unavailable": "ℹ️ Billing information is not available for this server. Please contact support.",  # noqa: E501, RUF001
        "servers.renew_failed": "❌ The renewal did not complete. Please try again shortly.",
        "servers.auto_renew_on_button": "🔄 Auto-renew",
        "servers.auto_renew_off_button": "🔕 Auto-renew",
        "servers.auto_renew_title": "🔄 Auto-renew",
        "servers.auto_renew_on": "Current state: on",
        "servers.auto_renew_off": "Current state: off",
        "servers.auto_renew_note": "While enabled, each period is charged to your wallet automatically.",  # noqa: E501
        "servers.auto_renew_unavailable": "ℹ️ Auto-renew cannot be changed for this server.",  # noqa: RUF001
        "servers.unnamed": "Unnamed server",
        "servers.list_index": "{index}. {title}",
        "servers.list_ip": "🌐 {ip}",
        "servers.list_ip_pending": "🌐 no IP assigned yet",
        "servers.list_os": "💿 {os}",
        "servers.list_state": "{state}",
        "servers.list_manage_n": "⚙️ Manage {index}",
        "servers.snapshots_list_button": "📋 List snapshots",
        "servers.snapshot_create_title": "➕ Create snapshot",  # noqa: RUF001
        "servers.snapshot_create_text": "A snapshot of the server's current state is created.",
        "servers.confirm_irreversible": "If you confirm, this runs on your server now.",
        "servers.confirm_generic_text": "Are you sure you want to do this?",
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
        # -- storefront: market -> provider -> location (provider-neutral) --
        "store.market_title": "🛒 Choose the server kind:",
        "store.market_iran": "🇮🇷 Iran server",
        "store.market_foreign": "🌍 Foreign server",
        "store.market_title_iran": "🇮🇷 Iran server — choose a provider:",
        "store.market_title_foreign": "🌍 Foreign server — choose a provider:",
        "store.providers_empty": "No server is on sale in this market yet.",
        "store.provider_row": "{name} — {count} plans",
        "store.provider_soon": "{name} — coming soon",
        "store.locations_title": "🌍 Choose a location — {provider}:",
        "store.location_row": "{code} — {count} plans",
        "store.plans_title": "📋 Plans at {location} — {provider}:",
        # Aggregated catalog: ONE card per product with its availability listed.
        "store.products_title": "🖥 Choose a server — {provider} ({count} products, page {page} of {pages}):",  # noqa: E501
        "store.product_row": "{flags}{name} · {vcpu}C/{ram}GB · {price}",
        "store.products_page": "Page {page} of {pages}",
        "store.product_locations_title": "📍 {product} — choose a location ({count}):",
        "store.product_location_row": "{location} — {price}",
        "store.families_title": "\U0001f4ce {provider}\nChoose a product:",
        "store.family_row": "{icon} {name} ({billing})",
        "store.family_row_count": "{icon} {name} ({billing}) — {count} plans",
        "store.family_row_unavailable": "{icon} {name} ({billing}) — temporarily unavailable",
        "store.family_unavailable_text": "No sellable plans for this product right now.",
        "store.cities_title": "\U0001f30d Choose a city:",
        "store.city_row": "{flag}{city}",
        "store.city_row_from": "{flag}{city} · from {price}",
        "store.halls_title": "\U0001f3e2 Choose a {city} datacenter:",
        "store.hall_row": "{code} · {count} plans",
        "store.hall_row_from": "{code} · {count} plans · from {price}",
        "store.billing.prepaid_monthly_fixed": "monthly",
        "store.billing.hourly": "hourly",
        "store.vps_locations_title": "\U0001f4cd Choose your monthly VPS location:",
        "store.plan_row": "{vcpu} cores | {ram}GB RAM | {disk} | {price}",
        "store.detail_location": "\U0001f4cd Location:\n{location}",
        "store.detail_continue": "\u2705 Continue to OS selection",
        "store.detail_panel": "\U0001f39b Control panel: {value}",
        "offers.panel_title": "\U0001f39b Choose a control panel:",
        "offers.panel_none": "No panel",
        "offers.confirm_panel": "\U0001f39b Panel: {panel}",
        "store.cloud_locations_title": "\U0001f4cd Choose a location for {provider}:",
        "store.cloud_families_title": "\U0001f9ec Choose a plan type:",
        "store.cloud_families_text": "Each type is a different machine family.\nPick one to see its plans.",  # noqa: E501
        "store.cloud_plans_title": "\U0001f4be Choose a plan \u2014 {location}:",
        "store.cloud_detail_title": "\u2601\ufe0f {name}",
        "store.cloud_detail_family": "\U0001f9e9 {family}",
        "store.cloud_detail_price": "\U0001f4b5 Price:\n{hourly}\nMonthly estimate:\n{monthly}",
        "store.price_per_hour": "{price} / hour",
        "store.price_per_month": "{price} / month",
        "store.cloud_images_title": "💿 Choose an operating system:",
        "store.cloud_selected_plan": "🖥 Selected plan: {name}",
        "store.cloud_selected_specs": "⚙️ {vcpu} vCPU · 🧠 {ram} GB · 💾 {disk} GB",
        "store.cloud_selected_price": "💵 {price}",
        "store.cloud_no_images": "No installable operating system is available for this plan right now.",  # noqa: E501
        "store.cloud_previous_failed": "The previous server request failed.\nPlease create a new order.",  # noqa: E501
        "store.cloud_account_capacity": "The provider's capacity for new servers is currently full.\nPlease try again shortly or choose another plan/location.",  # noqa: E501
        "store.cloud_retry_later": "The provider is currently unreachable.\nPlease try again in a few minutes.",  # noqa: E501
        "store.cloud_confirm_title": "\u2601\ufe0f Confirm hourly server creation",
        "store.cloud_confirm_provider": "Provider: {provider}",
        "store.cloud_confirm_kind": "Type: {kind}",
        "store.cloud_confirm_plan": "Plan: {plan}",
        "store.cloud_confirm_location": "Location: {location}",
        "store.cloud_confirm_specs": "CPU: {vcpu} vCPU\nRAM: {ram} GB\nDisk: {disk} GB",
        "store.cloud_confirm_os": "OS: {os}",
        "store.cloud_confirm_cost": "\U0001f4b0 Cost:\n{hourly}\n{monthly}",
        "store.cloud_confirm_warning": "\u26a0\ufe0f Until the resource is deleted, cloud charges can continue.",  # noqa: E501
        "store.cloud_confirm_create": "\u2705 Create server",
        "store.cloud_created": "\u2601\ufe0f Hourly server requested! ID: {server_id}",
        "store.detail_title": "🖥 {name}",
        "store.detail_cpu": "⚙️ CPU: {vcpu} vCPU",
        "store.detail_ram": "🧠 RAM: {ram} GB",
        "store.detail_disk": "💾 Disk: {disk}",
        "store.detail_traffic": "📦 Traffic: {traffic}",
        "store.detail_arch": "🏗 Architecture: {arch}",
        "store.detail_ipv4": "🌐 IPv4: {value}",
        "store.detail_ipv6": "🌐 IPv6: {value}",
        "store.detail_locations": "📍 Available locations:",
        "store.detail_location_row": "{location}",
        "store.detail_price": "💳 Monthly price:\n{price}",
        "store.detail_unknown": "not stated in the provider catalog",
        "store.detail_yes": "yes",
        "store.detail_no": "no",
        "store.price_unavailable": "The live converted price is temporarily unavailable; the native price applies.",  # noqa: E501
        "recharge.title": "⬆️ Wallet top-up",
        "recharge.amount_row": "{amount}",
        "recharge.gateway_title": "Choose a payment method:",
        "recharge.gateway_row": "Pay with {name}",
        "payments.gateway.tetraminator": "Tetraminator",
        "payments.gateway.zarinpal": "ZarinPal",
        "recharge.created": "🧾 Top-up of {amount} created.\nUse the button below to pay.",
        "recharge.unavailable": "Online top-up is currently disabled. Please contact support.",
        "recharge.invalid_amount": "The selected amount is not valid.",
        "support.title": "🎧 Support",
        "support.text": "Contact support for help. Mention your server order id.",
        "support.contact": "Contact channel: {contact}",
        "nav.menu": "🏠 Main menu",
        "nav.prev": "⬅️ Previous",
        "nav.next": "Next ➡️",
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
