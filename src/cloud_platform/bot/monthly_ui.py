"""Monthly prepaid Telegram UI (LEASEWEB-MVP).

The customer-facing screens of the monthly VPS storefront: a MARKET first
(🇮🇷 سرور ایران / 🌍 سرور خارج), then provider -> location -> plan -> OS ->
exact-price confirmation -> idempotent checkout, plus My servers (with
ownership-scoped power controls behind an explicit confirmation), Wallet
(balance + history, with a top-up entry point) and Support.

Design mirrors ``bot/ui.py``: domain services build the SCREEN DATA with
signed callbacks; this module is the thin Telegram renderer, framework-light
enough to unit-test without Telegram or a database.

Callback scheme (extended M08-001). The **store** flow is the storefront's
provider-neutral path; the older ``offers`` flow is kept for compatibility:

- ``main.menu`` — the customer main menu
- ``store.market`` — the market selector
- ``store.providers:{market}`` — providers of a market (Iran / Foreign)
- ``store.locations:{provider}`` — locations of one provider
- ``store.plans:{provider}:{location}`` — plans at that location
- ``store.os:{offer_id}`` — OS options (live, server-side filtered)
- ``store.confirm:{offer_id}:{os_index}`` — exact price + wallet balance
- ``store.buy:{offer_id}:{os_index}`` — terminal: idempotent checkout
- ``recharge.amounts`` — top-up amount selector
- ``recharge.start:{amount_minor}`` — create the pending gateway session
- ``servers.list:{page}`` — My Servers; every further screen lives in
  :mod:`cloud_platform.bot.servers_ui` and carries a short server REFERENCE
  plus, for a destructive action, an opaque pending-action nonce (never a
  UUID, a provider id or a confirmation token: Telegram limits callback data
  to 64 bytes and a signed callback already spends 21 of them)
- ``wallet.balance`` / ``wallet.history``
- ``support.contact``

Back navigation is explicit and walks the flow in reverse: OS -> plan ->
location -> provider -> market -> main menu.

Every money amount is formatted from integer minor units (no float
arithmetic). Every server screen re-loads ownership from the repository —
a callback can never view or control another user's server. The provider
list is built from configuration, so no handler contains a provider name.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, ClassVar, Protocol
from uuid import UUID

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from cloud_platform.bot.servers_ui import ServerManagementUi
from cloud_platform.bot.sessions import ServerSessions
from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.modules.checkout.service import (
    CheckoutError,
    MarketOptionView,
    MonthlyCheckoutResult,
    MonthlyCheckoutService,
    OfferCatalogViewService,
    OfferConfirmView,
    OfferUnavailableError,
    OsUnavailableError,
    UserNotActiveError,
)
from cloud_platform.modules.compute.domain import ServerLifecycleState, ServerRepository
from cloud_platform.modules.hourly.service import HourlyError
from cloud_platform.modules.navigation.domain import (
    Callback,
    CallbackError,
    decode_callback,
    encode_callback,
    encode_offer_ref,
    resolve_offer_id_arg,
)
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    HOURLY_MONTHLY_ESTIMATE_HOURS,
    SellableOfferRepository,
    TechnicalSpec,
)
from cloud_platform.modules.orders.domain import ProviderOrderRepository
from cloud_platform.modules.payments.domain import (
    session_credit_amount,
    session_credit_currency,
)
from cloud_platform.modules.payments.recharge import (
    RechargeAmountError,
    RechargeError,
    RechargeStart,
    WalletRechargeService,
)
from cloud_platform.modules.renewals.domain import RenewalRepository
from cloud_platform.modules.servers.service import ServerManagementService
from cloud_platform.modules.users.domain import User
from cloud_platform.modules.wallet.domain import InsufficientHoldBalanceError, LedgerEntryType
from cloud_platform.modules.wallet.service import WalletBalanceView, WalletHistoryService

logger = logging.getLogger(__name__)

#: Callback flows owned by this UI; other flows fall through to the
#: legacy hourly UI in ``bot/ui.py``.
MONTHLY_FLOWS = frozenset({"main", "offers", "store", "servers", "wallet", "support", "recharge"})

#: Power actions exposed with explicit confirmation (destructive ones).
POWER_ACTIONS = ("power_on", "power_off", "reboot")

#: Top-up amounts offered as buttons, in integer minor units, per currency
#: family. These are UI choices (never a provider price): 2-decimal
#: currencies get 10/25/50, zero-decimal ones get 5,000/10,000/20,000 units.
RECHARGE_PRESETS_MINOR: tuple[int, ...] = (1_000, 2_500, 5_000)
RECHARGE_PRESETS_ZERO_DECIMAL_MINOR: tuple[int, ...] = (500_000, 1_000_000, 2_000_000)
ZERO_DECIMAL_CURRENCIES = frozenset({"IRR", "IRT", "JPY", "KRW"})


def recharge_presets(currency: str) -> tuple[int, ...]:
    """Preset top-up amounts for a currency (integer minor units only)."""
    if currency.upper() in ZERO_DECIMAL_CURRENCIES:
        return RECHARGE_PRESETS_ZERO_DECIMAL_MINOR
    return RECHARGE_PRESETS_MINOR


def build_main_reply_keyboard(t: Translator) -> ReplyKeyboardMarkup:
    """Build the persistent bottom reply keyboard menu."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=t.t("menu.buy")),
                KeyboardButton(text=t.t("menu.servers")),
            ],
            [
                KeyboardButton(text=t.t("menu.wallet")),
                KeyboardButton(text=t.t("menu.recharge")),
            ],
            [
                KeyboardButton(text=t.t("menu.support")),
                KeyboardButton(text=t.t("nav.menu")),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def format_minor(minor: int, currency: str) -> str:
    """Integer-formatted money (no floats).

    Canonical presentation lives in :mod:`cloud_platform.modules.fx.formatting`
    (per-currency exponents: IRT/IRR zero-decimal, EUR/USD 2-decimal); this
    wrapper keeps the existing import path working for callers and tests.
    """
    from cloud_platform.modules.fx.formatting import format_minor as _fx_format

    return _fx_format(minor, currency)


def _country_flag(country_code: str | None) -> str:
    """Regional-indicator flag for an ISO 3166-1 alpha-2 code ("" when unset).

    Purely presentational and provider-neutral: the code comes from the synced
    location row, so any future datacenter gets its flag without a code change.
    """
    code = (country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha() or not code.isascii():
        return ""
    return "".join(chr(0x1F1E6 + (ord(char) - ord("A"))) for char in code) + " "


def _short_id(value: UUID) -> str:
    return str(value)[:8]


def _state_label(t: Translator, state: ServerLifecycleState) -> str:
    key = f"servers.state.{state.value}"
    try:
        return t.t(key)
    except Exception:
        return state.value


class _PowerCommands(Protocol):
    """The ownership-safe power command port (PowerCommandService)."""

    async def power_on(self, user_id: UUID, server_id: UUID, idempotency_key: str) -> object: ...

    async def power_off(self, user_id: UUID, server_id: UUID, idempotency_key: str) -> object: ...

    async def reboot(self, user_id: UUID, server_id: UUID, idempotency_key: str) -> object: ...


class MonthlyBotUi:
    """Renders the monthly storefront screens and dispatches its callbacks."""

    def __init__(
        self,
        signing_key: str,
        *,
        offers_view: OfferCatalogViewService,
        checkout: MonthlyCheckoutService,
        servers: ServerRepository,
        orders: ProviderOrderRepository,
        renewals: RenewalRepository,
        offers_repo: SellableOfferRepository,
        wallet_history: WalletHistoryService,
        power: _PowerCommands | None = None,
        support_contact: str = "",
        translator: Translator | None = None,
        recharge: WalletRechargeService | None = None,
        server_management: ServerManagementService | None = None,
        sessions: ServerSessions | None = None,
        server_page_size: int = 5,
        fx_resolver: object | None = None,
        fx_display_currency: str = "IRT",
        hourly: Any | None = None,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        self._key = signing_key
        self._view = offers_view
        self._checkout = checkout
        self._hourly = hourly
        self._servers = servers
        self._orders = orders
        self._renewals = renewals
        self._offers_repo = offers_repo
        self._wallet = wallet_history
        self._power = power
        self._support_contact = support_contact
        self._recharge = recharge
        self._t = translator or Translator()
        # Platform FX resolver for DISPLAY conversions (catalog equivalents,
        # never a repricing: the DB selling price is authoritative and is
        # always shown; the converted figure is supplementary). Owned by the
        # process like the gateway collection; this UI never closes it.
        self._fx = fx_resolver
        self._fx_display_currency = (fx_display_currency or "IRT").upper()
        # The My Servers flow lives in its own renderer: this module only keeps
        # the storefront and routes the ``servers`` callbacks to it.
        self._servers_ui = (
            ServerManagementUi(
                signing_key,
                server_management,
                sessions=sessions,
                orders=orders,
                renewals=renewals,
                offers_repo=offers_repo,
                page_size=server_page_size,
                extra_lines=self.server_extra_lines,
                translator=self._t,
            )
            if server_management is not None
            else None
        )

    # -- helpers -----------------------------------------------------------

    def _callback(self, flow: str, screen: str, *args: str) -> str:
        return encode_callback(Callback(flow=flow, screen=screen, args=args), self._key)

    def _menu_button(self) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=self._t.t("nav.menu"),
            callback_data=self._callback("main", "menu"),
        )

    def _menu_only(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[self._menu_button()]])

    def _back_button(self, flow: str, screen: str, *args: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=self._t.t("nav.back"),
            callback_data=self._callback(flow, screen, *args),
        )

    def _expired_screen(self) -> BotScreen:
        """Safe dead end for callbacks that decode but resolve to nothing."""
        return BotScreen(self._t.t("nav.expired"), self._menu_only())

    @staticmethod
    def _resolve_offer_id(arg: str) -> UUID | None:
        """Offer UUID from a callback arg: legacy UUID or compact ref, else None."""
        try:
            return resolve_offer_id_arg(arg)
        except CallbackError:
            return None

    # -- main menu ---------------------------------------------------------

    def menu_screen(self) -> BotScreen:
        """The customer main menu; the first entry is the MARKET selector."""
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("menu.buy"),
                    callback_data=self._callback("store", "market"),
                )
            ],
            [
                InlineKeyboardButton(
                    text=self._t.t("menu.servers"),
                    callback_data=self._callback("servers", "list"),
                )
            ],
            [
                InlineKeyboardButton(
                    text=self._t.t("menu.wallet"),
                    callback_data=self._callback("wallet", "balance"),
                )
            ],
            [
                InlineKeyboardButton(
                    text=self._t.t("menu.recharge"),
                    callback_data=self._callback("recharge", "amounts"),
                )
            ],
            [
                InlineKeyboardButton(
                    text=self._t.t("menu.support"),
                    callback_data=self._callback("support", "contact"),
                )
            ],
        ]
        return BotScreen(self._t.t("menu.title"), InlineKeyboardMarkup(inline_keyboard=rows))

    def reply_keyboard(self) -> ReplyKeyboardMarkup:
        """The persistent bottom ReplyKeyboardMarkup for the storefront."""
        return build_main_reply_keyboard(self._t)

    async def servers_screen(self, user: User | None) -> BotScreen:
        """The entry screen for My Servers."""
        if self._servers_ui is None:
            return BotScreen(self._t.t("servers.disabled"), self._menu_only())
        if user is None or user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        return await self._servers_ui.list_screen(user)

    # -- store flow: market -> provider -> location -> plan ----------------

    async def markets_screen(self) -> BotScreen:
        """store.market: ایران or خارج before any provider is shown."""
        options: list[MarketOptionView] = self._view.markets_screen()
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t(option.label_key),
                    callback_data=option.select_callback,
                )
            ]
            for option in options
        ]
        rows.append([self._menu_button()])
        return BotScreen(
            self._t.t("store.market_title"), InlineKeyboardMarkup(inline_keyboard=rows)
        )

    async def providers_screen(self, market: str) -> BotScreen:
        """store.providers:{market}: configured providers of one market."""
        try:
            providers, back_callback = await self._view.providers_screen(market)
        except OfferUnavailableError:
            return BotScreen(self._t.t("store.providers_empty"), self._market_back_only())
        if not providers:
            return BotScreen(self._t.t("store.providers_empty"), self._market_back_only())
        rows: list[list[InlineKeyboardButton]] = []
        for provider in providers:
            label = (
                self._t.t(
                    "store.provider_row",
                    name=provider.display_name,
                    count=provider.offer_count,
                )
                if provider.select_callback
                else self._t.t("store.provider_soon", name=provider.display_name)
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=provider.select_callback or self._callback("store", "market"),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=back_callback),
                self._menu_button(),
            ]
        )
        title_key = f"store.market_title_{market}"
        try:
            title = self._t.t(title_key)
        except Exception:
            title = self._t.t("store.market_title")
        return BotScreen(title, InlineKeyboardMarkup(inline_keyboard=rows))

    def _market_back_only(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [self._back_button("store", "market"), self._menu_button()],
            ]
        )

    async def store_locations_screen(self, provider_key: str) -> BotScreen:
        """store.locations:{provider}: locations of one provider."""
        try:
            locations, back_callback, cancel_callback = await self._view.locations_screen(
                provider_key
            )
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.no_locations"), self._market_back_only())
        if not locations:
            return BotScreen(self._t.t("offers.no_locations"), self._market_back_only())
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t(
                        "store.location_row", code=item.location_id, count=item.offer_count
                    ),
                    callback_data=item.select_callback,
                )
            ]
            for item in locations
        ]
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=back_callback),
                InlineKeyboardButton(text=self._t.t("nav.cancel"), callback_data=cancel_callback),
            ]
        )
        return BotScreen(
            self._t.t(
                "store.locations_title",
                provider=self._provider_name(provider_key),
                location=provider_key,
            ),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def store_plans_screen(self, provider_key: str, location_id: str) -> BotScreen:
        """store.plans:{provider}:{location}: sellable plans at that location."""
        try:
            plans, back_callback, cancel_callback = await self._view.plans_screen(
                location_id, provider_key
            )
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        # The DB selling price is authoritative and never overwritten; the FX
        # equivalent is supplementary display only (native-only when FX is
        # briefly down — availability never depends on presentation).
        rows: list[list[InlineKeyboardButton]] = []
        for plan in plans:
            price = await self._price_label(plan.monthly_price_minor, plan.currency)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._t.t(
                            "offers.plan_row",
                            name=plan.name,
                            vcpu=plan.vcpu,
                            ram=plan.ram_gb,
                            disk=plan.disk_gb,
                            price=price,
                        ),
                        callback_data=self._callback(
                            "store", "os", encode_offer_ref(plan.offer_id)
                        ),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=back_callback),
                InlineKeyboardButton(text=self._t.t("nav.cancel"), callback_data=cancel_callback),
            ]
        )
        return BotScreen(
            self._t.t(
                "store.plans_title",
                location=location_id,
                provider=self._provider_name(provider_key),
            ),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    # -- store flow: families -> locations -> plans -> detail ----------

    _FAMILY_ICONS: ClassVar[dict[str, str]] = {
        "prepaid_monthly_fixed": "\U0001f5a5\ufe0f",
        "hourly": "\U0001f558",
    }

    @staticmethod
    def _family_button_label(
        t: Translator,
        family: Any,
        icons: dict[str, str],
    ) -> str:
        # A configured family is always shown: the row carries its CURRENT
        # sellable count, or an explicit unavailable marker — never faked
        # inventory, never a hidden product line.
        base = {
            "icon": icons.get(family.billing_model, ""),
            "name": family.display_name,
            "billing": t.t(f"store.billing.{family.billing_model}"),
        }
        if getattr(family, "available", True):
            return t.t("store.family_row_count", count=getattr(family, "sellable_count", 0), **base)
        return t.t("store.family_row_unavailable", **base)

    def _family_unavailable_screen(self, provider_key: str) -> BotScreen:
        # A configured family with zero sellable offers stays clickable and
        # lands here: honest "temporarily unavailable" plus Back / Main Menu.
        rows = [
            [self._back_button("store", "families", provider_key), self._menu_button()],
        ]
        return BotScreen(
            self._t.t("store.family_unavailable_text"),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def store_families_screen(self, provider_key: str) -> BotScreen:
        # Commercial product lines of one provider (monthly VPS vs hourly
        # cloud). Auto-forward happens ONLY for a single AVAILABLE family —
        # a provider configured with several families always renders the
        # selector, even when just one of them currently has inventory.
        try:
            families, back_callback, cancel_callback = await self._view.families_screen(
                provider_key
            )
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        if not families:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        if len(families) == 1 and getattr(families[0], "available", True):
            return await self._enter_family(
                provider_key, families[0].family_key, families[0].billing_model
            )
        if len(families) == 1:
            return self._family_unavailable_screen(provider_key)
        rows: list[list[InlineKeyboardButton]] = []
        for family in families:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._family_button_label(self._t, family, self._FAMILY_ICONS),
                        callback_data=family.select_callback,
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=back_callback),
                InlineKeyboardButton(text=self._t.t("nav.cancel"), callback_data=cancel_callback),
            ]
        )
        return BotScreen(
            self._t.t(
                "store.families_title",
                provider=self._provider_name(provider_key),
            ),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def _enter_family(
        self, provider_key: str, family_key: str, billing_model: str
    ) -> BotScreen:
        # Family entry by billing model (never by provider): monthly lists
        # VPS locations, hourly lists cloud locations.
        if billing_model == "hourly":
            return await self.store_cloud_locations_screen(provider_key, family_key)
        return await self.store_vps_locations_screen(provider_key, family_key)

    def _group_price_text(
        self, billing_model: str, min_price_minor: int | None, currency: str | None
    ) -> str | None:
        # Native minimum for a city/hall button (None when the scope mixes
        # currencies or has no priced offer). Hourly minima are per-hour
        # prices, so they always carry the per-hour qualifier.
        if min_price_minor is None or not currency:
            return None
        native = format_minor(min_price_minor, currency)
        if billing_model == BILLING_MODEL_HOURLY:
            return self._t.t("store.price_per_hour", price=native)
        return native

    async def store_cities_screen(
        self, provider_key: str, family_key: str, page: int = 1
    ) -> BotScreen:
        # City groups of one family (monthly and hourly share the screen;
        # the title follows the billing model, never the provider).
        try:
            view = await self._view.cities_screen(provider_key, family_key, page)
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        if not view.items:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        rows: list[list[InlineKeyboardButton]] = []
        for group in view.items:
            price = self._group_price_text(
                view.billing_model, group.min_price_minor, group.currency
            )
            if price is None:
                text = self._t.t(
                    "store.city_row",
                    flag=_country_flag(group.country_code),
                    city=group.city,
                )
            else:
                text = self._t.t(
                    "store.city_row_from",
                    flag=_country_flag(group.country_code),
                    city=group.city,
                    price=price,
                )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=text,
                        callback_data=group.select_callback,
                    )
                ]
            )
        rows.extend(self._pager_rows(view))
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=view.back_callback),
                InlineKeyboardButton(
                    text=self._t.t("nav.cancel"), callback_data=view.cancel_callback
                ),
            ]
        )
        if view.billing_model == BILLING_MODEL_HOURLY:
            title = self._t.t(
                "store.cloud_locations_title",
                provider=self._provider_name(provider_key),
            )
        else:
            title = self._t.t("store.vps_locations_title")
        return BotScreen(title, InlineKeyboardMarkup(inline_keyboard=rows))

    async def store_halls_screen(
        self,
        provider_key: str,
        family_key: str,
        country_arg: str,
        city_slug: str,
        page: int = 1,
    ) -> BotScreen:
        # Exact datacenters/halls of one city, each differentiated with real
        # catalog facts (plan count, minimum native price).
        try:
            view = await self._view.city_locations_screen(
                provider_key, family_key, country_arg, city_slug, page
            )
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        if not view.items:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        rows: list[list[InlineKeyboardButton]] = []
        for hall in view.items:
            price = self._group_price_text(view.billing_model, hall.min_price_minor, hall.currency)
            if price is None:
                text = self._t.t("store.hall_row", code=hall.location_id, count=hall.plan_count)
            else:
                text = self._t.t(
                    "store.hall_row_from",
                    code=hall.location_id,
                    count=hall.plan_count,
                    price=price,
                )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=text,
                        callback_data=hall.select_callback,
                    )
                ]
            )
        rows.extend(self._pager_rows(view))
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=view.back_callback),
                InlineKeyboardButton(
                    text=self._t.t("nav.cancel"), callback_data=view.cancel_callback
                ),
            ]
        )
        return BotScreen(
            self._t.t("store.halls_title", city=view.city),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def store_vps_locations_screen(
        self, provider_key: str, family_key: str, page: int = 1
    ) -> BotScreen:
        # Monthly VPS locations with sellable offers: flag + friendly city,
        # never a raw code as the only label.
        return await self.store_cities_screen(provider_key, family_key, page)

    def _pager_rows(self, view: Any) -> list[list[InlineKeyboardButton]]:
        # Prev | page X/Y | next pager (absent on a single page).
        if view.prev_callback is None and view.next_callback is None:
            return []
        pager: list[InlineKeyboardButton] = []
        if view.prev_callback is not None:
            pager.append(
                InlineKeyboardButton(text=self._t.t("nav.prev"), callback_data=view.prev_callback)
            )
        pager.append(
            InlineKeyboardButton(
                text=self._t.t("store.products_page", page=view.page, pages=view.total_pages),
                callback_data=view.next_callback or view.prev_callback or "",
            )
        )
        if view.next_callback is not None:
            pager.append(
                InlineKeyboardButton(text=self._t.t("nav.next"), callback_data=view.next_callback)
            )
        return [pager]

    async def store_vps_plans_screen(
        self, provider_key: str, family_key: str, location_id: str, page: int = 1
    ) -> BotScreen:
        # Monthly plans at one location: concise provider-sourced specs with
        # the exact native price plus the display equivalent.
        try:
            view = await self._view.family_plans_screen(provider_key, family_key, location_id, page)
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        if not view.items:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        rows: list[list[InlineKeyboardButton]] = []
        for plan in view.items:
            price = await self._price_label(plan.monthly_price_minor, plan.currency)
            storage = TechnicalSpec.from_metadata(plan.technical_metadata).storage_type
            disk = f"{plan.disk_gb}GB" + (f" {storage}" if storage else "")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._t.t(
                            "store.plan_row",
                            vcpu=plan.vcpu,
                            ram=plan.ram_gb,
                            disk=disk,
                            price=price,
                        ),
                        callback_data=plan.select_callback,
                    )
                ]
            )
        rows.extend(self._pager_rows(view))
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=view.back_callback),
                InlineKeyboardButton(
                    text=self._t.t("nav.cancel"), callback_data=view.cancel_callback
                ),
            ]
        )
        return BotScreen(
            self._t.t(
                "store.plans_title",
                location=location_id,
                provider=self._provider_name(provider_key),
            ),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def store_plan_detail_screen(
        self, provider_key: str, location_id: str, product_id: str
    ) -> BotScreen:
        # One exact monthly plan: full spec, proven facts only, then the
        # continue action into OS selection.
        try:
            detail = await self._view.plan_detail_screen(provider_key, location_id, product_id)
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        offer = detail.offer
        spec = TechnicalSpec.from_metadata(detail.technical_metadata)
        unknown = self._t.t("store.detail_unknown")
        disk = f"{offer.disk_gb} GB" + (f" {spec.storage_type}" if spec.storage_type else "")
        lines = [
            self._t.t("store.detail_title", name=offer.name),
            "",
            self._t.t(
                "store.detail_location",
                location=self._location_detail(
                    detail.location_name, offer.location_id, detail.location_country
                ),
            ),
            "",
            self._t.t("store.detail_cpu", vcpu=offer.vcpu),
            self._t.t("store.detail_ram", ram=offer.ram_gb),
            self._t.t("store.detail_disk", disk=disk),
            self._t.t("store.detail_traffic", traffic=offer.traffic or unknown),
            self._t.t("store.detail_arch", arch=spec.architecture or unknown),
            self._t.t("store.detail_ipv4", value=self._present(spec.ipv4)),
            self._t.t("store.detail_ipv6", value=self._present(spec.ipv6)),
            "",
            self._t.t(
                "store.detail_price",
                price=await self._price_label(offer.monthly_price_minor, offer.currency),
            ),
        ]
        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text=self._t.t("store.detail_continue"),
                    callback_data=detail.continue_callback,
                )
            ],
            [
                InlineKeyboardButton(
                    text=self._t.t("nav.back"), callback_data=detail.back_callback
                ),
                InlineKeyboardButton(
                    text=self._t.t("nav.cancel"), callback_data=detail.cancel_callback
                ),
            ],
        ]
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def store_panel_screen(self, offer_ref: str, os_index: int) -> BotScreen:
        # Configuration step between OS and confirmation (free panels only,
        # so the confirmed price never moves).
        try:
            offer_id = resolve_offer_id_arg(offer_ref)
        except CallbackError:
            return self._expired_screen()
        try:
            _offer, options, back_callback, cancel_callback = await self._view.panel_screen(
                offer_id=offer_id, os_index=os_index
            )
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.unavailable"), self._menu_only())
        except OsUnavailableError:
            return BotScreen(self._t.t("offers.os_unavailable"), self._menu_only())
        rows = [
            [
                InlineKeyboardButton(
                    text=option.name if option.name else self._t.t("offers.panel_none"),
                    callback_data=option.select_callback,
                )
            ]
            for option in options
        ]
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=back_callback),
                InlineKeyboardButton(text=self._t.t("nav.cancel"), callback_data=cancel_callback),
            ]
        )
        return BotScreen(
            self._t.t("offers.panel_title"), InlineKeyboardMarkup(inline_keyboard=rows)
        )

    def _present(self, value: bool | None) -> str:
        """YES/NO for a proven technical fact, neutral when the catalog is silent."""
        if value is True:
            return self._t.t("store.detail_yes")
        if value is False:
            return self._t.t("store.detail_no")
        return self._t.t("store.detail_unknown")

    # -- store flow: hourly cloud -----------------------------------------

    async def store_cloud_locations_screen(
        self, provider_key: str, family_key: str, page: int = 1
    ) -> BotScreen:
        """Hourly cloud regions, API-discovered only, with country flags."""
        return await self.store_cities_screen(provider_key, family_key, page)

    async def store_cloud_families_screen(
        self, provider_key: str, family_key: str, location_id: str
    ) -> BotScreen:
        """Instance-type families at one region (provider-classified only)."""
        try:
            families, back_callback, cancel_callback = await self._view.cloud_plan_families_screen(
                provider_key, family_key, location_id
            )
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        if not families:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text=family.display_name, callback_data=family.select_callback)]
            for family in families
        ]
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=back_callback),
                InlineKeyboardButton(text=self._t.t("nav.cancel"), callback_data=cancel_callback),
            ]
        )
        return BotScreen(
            self._t.t("store.cloud_families_title") + "\n" + self._t.t("store.cloud_families_text"),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def store_cloud_plans_screen(
        self,
        provider_key: str,
        location_id: str,
        plan_family: str,
        page: int = 1,
    ) -> BotScreen:
        """Hourly plans of one instance family: specs plus the hourly price."""
        try:
            view = await self._view.cloud_plans_screen(provider_key, location_id, plan_family, page)
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        if not view.items:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        rows: list[list[InlineKeyboardButton]] = []
        for plan in view.items:
            price = await self._price_label(plan.monthly_price_minor, plan.currency)
            storage = TechnicalSpec.from_metadata(plan.technical_metadata).storage_type
            disk = f"{plan.disk_gb}GB" + (f" {storage}" if storage else "")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._t.t(
                            "store.plan_row",
                            vcpu=plan.vcpu,
                            ram=plan.ram_gb,
                            disk=disk,
                            price=self._t.t("store.price_per_hour", price=price),
                        ),
                        callback_data=plan.select_callback,
                    )
                ]
            )
        rows.extend(self._pager_rows(view))
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=view.back_callback),
                InlineKeyboardButton(
                    text=self._t.t("nav.cancel"), callback_data=view.cancel_callback
                ),
            ]
        )
        return BotScreen(
            self._t.t(
                "store.cloud_plans_title",
                location=location_id,
                provider=self._provider_name(provider_key),
            ),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def store_cloud_detail_screen(
        self, provider_key: str, location_id: str, product_id: str
    ) -> BotScreen:
        """One hourly plan: full specs, hourly price plus the monthly
        estimate (display only), then continue into images."""
        try:
            detail = await self._view.cloud_detail_screen(provider_key, location_id, product_id)
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        offer = detail.offer
        spec = TechnicalSpec.from_metadata(detail.technical_metadata)
        unknown = self._t.t("store.detail_unknown")
        disk = f"{offer.disk_gb} GB" + (f" {spec.storage_type}" if spec.storage_type else "")
        hourly = self._t.t(
            "store.price_per_hour",
            price=await self._price_label(offer.monthly_price_minor, offer.currency),
        )
        monthly = self._t.t(
            "store.price_per_month",
            price=await self._price_label(
                offer.monthly_price_minor * HOURLY_MONTHLY_ESTIMATE_HOURS, offer.currency
            ),
        )
        lines = [
            self._t.t("store.cloud_detail_title", name=offer.name),
            "",
            self._t.t(
                "store.detail_location",
                location=self._location_detail(
                    detail.location_name, offer.location_id, detail.location_country
                ),
            ),
            self._t.t(
                "store.cloud_detail_family",
                family=str(detail.technical_metadata.get("plan_family_name") or "?"),
            ),
            "",
            self._t.t("store.detail_cpu", vcpu=offer.vcpu),
            self._t.t("store.detail_ram", ram=offer.ram_gb),
            self._t.t("store.detail_disk", disk=disk),
            self._t.t("store.detail_traffic", traffic=offer.traffic or unknown),
            self._t.t("store.detail_arch", arch=spec.architecture or unknown),
            self._t.t("store.detail_ipv4", value=self._present(spec.ipv4)),
            self._t.t("store.detail_ipv6", value=self._present(spec.ipv6)),
            "",
            self._t.t("store.cloud_detail_price", hourly=hourly, monthly=monthly),
        ]
        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text=self._t.t("store.detail_continue"),
                    callback_data=detail.continue_callback,
                )
            ],
            [
                InlineKeyboardButton(
                    text=self._t.t("nav.back"), callback_data=detail.back_callback
                ),
                InlineKeyboardButton(
                    text=self._t.t("nav.cancel"), callback_data=detail.cancel_callback
                ),
            ],
        ]
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def store_cloud_images_screen(self, offer_ref: str) -> BotScreen:
        """Supported operating systems, read live (label and provider id
        apart), under the summary of the plan the customer just picked."""
        offer_id = self._resolve_offer_id(offer_ref)
        if offer_id is None:
            return self._expired_screen()
        try:
            offer, options, back_callback, cancel_callback = await self._view.cloud_images_screen(
                offer_id
            )
        except OsUnavailableError:
            # The plan is sellable; the provider exposes no usable image for
            # it right now. Say exactly that instead of "unavailable".
            return BotScreen(self._t.t("store.cloud_no_images"), self._menu_only())
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.unavailable"), self._menu_only())
        rows = [
            [
                InlineKeyboardButton(
                    text=option.name or "",
                    callback_data=option.select_callback,
                )
            ]
            for option in options
        ]
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=back_callback),
                InlineKeyboardButton(text=self._t.t("nav.cancel"), callback_data=cancel_callback),
            ]
        )
        # For a hourly offer the offer view's price field IS the hourly
        # selling price (never a monthly estimate).
        hourly = self._t.t(
            "store.price_per_hour",
            price=await self._price_label(offer.monthly_price_minor, offer.currency),
        )
        header = [
            self._t.t("store.cloud_images_title"),
            "",
            self._t.t("store.cloud_selected_plan", name=offer.name),
            self._t.t(
                "store.cloud_selected_specs",
                vcpu=offer.vcpu,
                ram=offer.ram_gb,
                disk=offer.disk_gb,
            ),
            self._t.t("store.cloud_selected_price", price=hourly),
        ]
        return BotScreen("\n".join(header), InlineKeyboardMarkup(inline_keyboard=rows))

    async def store_cloud_buy_screen(
        self, user: User | None, offer_ref: str, image_index: int, cb_key: str
    ) -> BotScreen:
        # Hourly creation intent (terminal action): no provider call and no
        # charge here — the worker POSTs once under the operation ledger and
        # accrual bills per quantum from the price snapshot.
        if user is None or user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        if self._hourly is None:
            return BotScreen(self._t.t("offers.unavailable"), self._menu_only())
        offer_id = self._resolve_offer_id(offer_ref)
        if offer_id is None:
            return self._expired_screen()
        idempotency_key = f"bot-hourly:{cb_key}"
        try:
            image = await self._resolve_cloud_image(offer_id, image_index)
            result = await self._hourly.create_instance(
                user=user,
                offer_id=offer_id,
                image_id=image.id,
                image_label=image.label,
                idempotency_key=idempotency_key,
            )
        except (OfferUnavailableError, OsUnavailableError, HourlyError) as exc:
            logger.warning("hourly create rejected: %s", exc)
            return BotScreen(self._t.t("offers.unavailable"), self._menu_only())
        except Exception as exc:
            logger.warning("hourly create rejected: %s", exc)
            return BotScreen(self._t.t("error.unknown"), self._menu_only())
        text = self._t.t("store.cloud_created", server_id=str(result.server.id)[:8])
        if result.replayed:
            text += "\n" + self._t.t("offers.order_replayed")
        return BotScreen(text, self._menu_only())

    async def _resolve_cloud_image(self, offer_id: UUID, image_index: int) -> Any:
        from cloud_platform.modules.offers.domain import OfferNotFoundError, SellableOffer

        offer: SellableOffer | None = await self._offers_repo.get(offer_id)
        if offer is None:
            raise OfferNotFoundError(f"offer {offer_id} not found")
        return await self._view.cloud_image_by_index(offer, image_index)

    async def store_cloud_confirm_screen(
        self, user: User, offer_ref: str, image_index: int
    ) -> BotScreen:
        """Hourly creation confirmation: per-hour charge basis, explicit
        delete-to-stop-billing warning, never 'stop billing' language."""
        if user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        offer_id = self._resolve_offer_id(offer_ref)
        if offer_id is None:
            return self._expired_screen()
        try:
            view = await self._view.cloud_confirmation(
                user_id=user.id, offer_id=offer_id, image_index=image_index
            )
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.unavailable"), self._menu_only())
        except OsUnavailableError:
            return BotScreen(self._t.t("offers.os_unavailable"), self._menu_only())
        hourly = self._t.t(
            "store.price_per_hour",
            price=await self._price_label(view.hourly_price_minor, view.currency),
        )
        monthly = self._t.t(
            "store.price_per_month",
            price=await self._price_label(view.monthly_estimate_minor, view.currency),
        )
        lines = [
            self._t.t("store.cloud_confirm_title"),
            "",
            self._t.t(
                "store.cloud_confirm_provider",
                provider=self._provider_name(view.offer.provider_key),
            ),
            self._t.t("store.cloud_confirm_plan", plan=view.offer.name),
            self._t.t(
                "store.cloud_confirm_location",
                location=self._location_detail(
                    view.location_name, view.offer.location_id, view.location_country
                ),
            ),
            self._t.t(
                "store.cloud_confirm_specs",
                vcpu=view.offer.vcpu,
                ram=view.offer.ram_gb,
                disk=view.offer.disk_gb,
            ),
            self._t.t("store.cloud_confirm_os", os=view.image_label),
            "",
            self._t.t("store.cloud_confirm_cost", hourly=hourly, monthly=monthly),
            "",
            self._t.t("store.cloud_confirm_warning"),
        ]
        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text=self._t.t("store.cloud_confirm_create"),
                    callback_data=view.confirm_callback,
                )
            ],
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=view.back_callback),
                InlineKeyboardButton(
                    text=self._t.t("nav.cancel"), callback_data=view.cancel_callback
                ),
            ],
        ]
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def store_product_locations_screen(
        self,
        provider_key: str,
        product_id: str,
        price_minor: int | None = None,
        currency: str | None = None,
    ) -> BotScreen:
        """store.product_locations:{provider}:{product}:{price}:{currency}: availability.

        Every row is its own sellable offer (own price, own fulfillment
        credential behind it), so choosing a location is a real choice.
        Currency is part of the card identity: equal minor-unit values in
        different currencies never mix. A legacy callback without currency
        falls back to the product cards instead of guessing a currency.
        """
        try:
            locations, back_callback, cancel_callback = await self._view.product_locations_screen(
                provider_key, product_id, price_minor, currency
            )
        except OfferUnavailableError:
            if currency is None:
                try:
                    return await self.store_families_screen(provider_key)
                except OfferUnavailableError:
                    pass
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        if not locations:
            return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
        rows: list[list[InlineKeyboardButton]] = []
        for item in locations:
            price = await self._price_label(item.monthly_price_minor, item.currency)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._t.t(
                            "store.product_location_row",
                            location=self._location_detail(
                                item.name, item.location_id, item.country_code
                            ),
                            price=price,
                        ),
                        callback_data=item.select_callback,
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=back_callback),
                InlineKeyboardButton(text=self._t.t("nav.cancel"), callback_data=cancel_callback),
            ]
        )
        return BotScreen(
            self._t.t(
                "store.product_locations_title",
                product=locations[0].product_name or product_id,
                count=len(locations),
            ),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @staticmethod
    def _location_display_name(name: str | None, location_id: str) -> str:
        """Friendly name if the catalog row states one, else the raw code."""
        return (name or "").strip() or location_id

    @staticmethod
    def _location_button(
        name: str | None, location_id: str, country_code: str | None, show_code: bool
    ) -> str:
        """Customer-facing location button: flag + friendly name.

        The raw code is appended only when the caller asks (several halls of
        one city would otherwise render identical buttons). The flag comes
        from the synced ISO country code only — never inferred here.
        """
        base = MonthlyBotUi._location_display_name(name, location_id)
        text = f"{_country_flag(country_code)}{base}"
        if show_code and base != location_id:
            text += f" — {location_id}"
        return text.strip()

    @staticmethod
    def _location_detail(name: str | None, location_id: str, country_code: str | None) -> str:
        """Customer-facing location line for message/detail text.

        Always carries the code (``🇩🇪 Frankfurt — FRA-10``) so an exact
        datacenter stays identifiable outside the button list.
        """
        return MonthlyBotUi._location_button(name, location_id, country_code, True)

    @staticmethod
    def _location_button_labels(
        items: list[tuple[str | None, str, str | None]],
    ) -> list[str]:
        """Button labels for one location list, codes only on name collisions.

        ``items`` are ``(name, location_id, country_code)`` triples in display
        order; halls sharing one friendly name keep their code so every
        button stays distinguishable.
        """
        names = [MonthlyBotUi._location_display_name(name, code) for name, code, _ in items]
        crowded = {name for name in names if names.count(name) > 1}
        return [
            MonthlyBotUi._location_button(name, code, country, display in crowded)
            for (name, code, country), display in zip(items, names, strict=True)
        ]

    def _provider_name(self, provider_key: str) -> str:
        """Customer-facing provider name from configuration (never the key)."""
        try:
            listings = self._view.provider_display_name(provider_key)
        except Exception:  # pragma: no cover - view services always provide it
            return provider_key
        return listings or provider_key

    async def _display_equivalent(self, amount_minor: int, currency: str) -> str | None:
        """Supplementary display-currency equivalent (None when unavailable).

        The authoritative native price is always shown by the caller; this
        only adds the ``≈ X`` equivalent. Same-currency needs no FX call; a
        briefly-down FX source degrades to native-only (never a fabricated
        or zero price, never hides the offer).
        """
        target = (self._fx_display_currency or "").upper()
        source = (currency or "").upper()
        if not target or not source or target == source or self._fx is None:
            return None
        try:
            from cloud_platform.modules.fx.domain import FxPurpose

            resolved = await self._fx.resolve(amount_minor, source, target, FxPurpose.DISPLAY)  # type: ignore[attr-defined]
        except Exception:
            return None
        return format_minor(resolved.target_amount_minor, resolved.target_currency)

    async def _price_label(self, amount_minor: int, currency: str) -> str:
        """Native price plus the FX display equivalent when convertible."""
        native = format_minor(amount_minor, currency)
        equivalent = await self._display_equivalent(amount_minor, currency)
        if equivalent:
            return f"{native} (≈ {equivalent})"
        return native

    # -- recharge flow -----------------------------------------------------

    async def recharge_screen(self, user: User | None) -> BotScreen:
        """recharge.amounts: preset top-up amounts for the wallet currency."""
        if user is None or user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        view: WalletBalanceView = await self._wallet.balance(user.id)
        if not view.has_wallet or not view.currency:
            return BotScreen(self._t.t("wallet.no_wallet"), self._menu_only())
        if self._recharge is None:
            lines = [self._t.t("recharge.unavailable")]
            if self._support_contact:
                lines.append(self._t.t("support.contact", contact=self._support_contact))
            return BotScreen("\n".join(lines), self._market_back_only())
        # FX-aware availability: a wallet whose currency needs conversion
        # (e.g. EUR -> Tetraminator IRT) is offered only when the resolver
        # can convert it; sync fast path first, async FX probe second.
        available = self._recharge.supports_currency(view.currency)
        if not available and hasattr(self._recharge, "supports_currency_async"):
            try:
                available = await self._recharge.supports_currency_async(view.currency)
            except Exception:
                available = False
        if not available:
            # No online top-up for this currency: say so and point at support
            # instead of offering a button that cannot work.
            lines = [self._t.t("recharge.unavailable")]
            if self._support_contact:
                lines.append(self._t.t("support.contact", contact=self._support_contact))
            return BotScreen("\n".join(lines), self._market_back_only())
        # Filter presets below every gateway's (converted) minimum so the UI
        # never offers an amount the gateway will reject.
        presets = await self._available_presets(view.currency)
        if not presets:
            lines = [self._t.t("recharge.unavailable")]
            if self._support_contact:
                lines.append(self._t.t("support.contact", contact=self._support_contact))
            return BotScreen("\n".join(lines), self._market_back_only())
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t(
                        "recharge.amount_row",
                        amount=format_minor(amount, view.currency),
                    ),
                    callback_data=self._callback("recharge", "start", str(amount)),
                )
            ]
            for amount in presets
        ]
        rows.append([self._back_button("wallet", "balance"), self._menu_button()])
        return BotScreen(
            self._t.t("recharge.title")
            + "\n"
            + f"{self._t.t('wallet.balance_row', balance=view.formatted)}",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def _available_presets(self, currency: str) -> tuple[int, ...]:
        """Preset amounts that at least one gateway can settle (converted min)."""
        if self._recharge is None:
            return ()
        out: list[int] = []
        for amount in recharge_presets(currency):
            try:
                if hasattr(self._recharge, "compatible_gateways_async"):
                    compatible = await self._recharge.compatible_gateways_async(currency, amount)
                else:
                    compatible = self._recharge.compatible_gateways(currency, amount)
            except Exception:
                continue
            if compatible:
                out.append(amount)
        return tuple(out)

    async def recharge_start_screen(
        self, user: User, amount_text: str, gateway_key: str | None = None
    ) -> BotScreen:
        """recharge.start:{amount}[:{gateway}]: create the pending session, then pay."""
        if user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        if self._recharge is None:
            return BotScreen(self._t.t("recharge.unavailable"), self._menu_only())
        try:
            amount_minor = int(amount_text)
        except (TypeError, ValueError):
            return BotScreen(self._t.t("recharge.invalid_amount"), self._menu_only())
        view: WalletBalanceView = await self._wallet.balance(user.id)
        currency = view.currency or ""
        try:
            if hasattr(self._recharge, "compatible_gateways_async"):
                compatible = await self._recharge.compatible_gateways_async(currency, amount_minor)
            else:
                compatible = self._recharge.compatible_gateways(currency, amount_minor)
        except Exception:
            return BotScreen(self._t.t("recharge.unavailable"), self._menu_only())
        if not compatible:
            return BotScreen(self._t.t("recharge.unavailable"), self._menu_only())
        if gateway_key is None and len(compatible) > 1:
            return self.recharge_gateway_screen(amount_minor, currency, compatible)
        key = gateway_key or compatible[0]
        if key not in compatible:
            return BotScreen(self._t.t("recharge.unavailable"), self._menu_only())
        try:
            start: RechargeStart = await self._recharge.start(
                user=user,
                amount_minor=amount_minor,
                currency=currency,
                # Deterministic per (user, amount): a double tap replays the
                # same session instead of creating a second one.
                idempotency_key=f"bot-recharge:{user.id}:{amount_minor}",
                gateway_key=key,
            )
        except RechargeAmountError:
            return BotScreen(self._t.t("recharge.invalid_amount"), self._menu_only())
        except RechargeError as exc:
            logger.warning("recharge rejected for user %s: %s", user.id, exc)
            return BotScreen(self._t.t("recharge.unavailable"), self._menu_only())
        lines = [
            self._t.t(
                "recharge.created",
                amount=format_minor(
                    session_credit_amount(start.session),
                    session_credit_currency(start.session),
                ),
            )
        ]
        rows: list[list[InlineKeyboardButton]] = []
        if start.redirect_url:
            rows.append(
                [InlineKeyboardButton(text=self._t.t("menu.recharge"), url=start.redirect_url)]
            )
        rows.append([self._back_button("wallet", "balance"), self._menu_button()])
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    def gateway_display_name(self, gateway_key: str) -> str:
        """Customer-facing gateway name (never the technical key alone)."""
        try:
            return self._t.t(f"payments.gateway.{gateway_key}")
        except Exception:
            return gateway_key

    def recharge_gateway_screen(
        self, amount_minor: int, currency: str, gateway_keys: list[str]
    ) -> BotScreen:
        """recharge gateway picker: one button per compatible gateway."""
        del currency
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("recharge.gateway_row", name=self.gateway_display_name(key)),
                    callback_data=self._callback("recharge", "start", str(amount_minor), key),
                )
            ]
            for key in gateway_keys
        ]
        rows.append([self._back_button("recharge", "amounts"), self._menu_button()])
        return BotScreen(
            self._t.t("recharge.gateway_title"), InlineKeyboardMarkup(inline_keyboard=rows)
        )

    async def _recharge_cb(self, cb: Callback, user: User | None) -> BotScreen:
        if user is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        if cb.screen == "amounts":
            return await self.recharge_screen(user)
        if cb.screen == "start" and len(cb.args) == 1:
            return await self.recharge_start_screen(user, cb.args[0])
        if cb.screen == "start" and len(cb.args) == 2:
            return await self.recharge_start_screen(user, cb.args[0], gateway_key=cb.args[1])
        return self._menu_screen()

    # -- offers flow -------------------------------------------------------

    async def locations_screen(self) -> BotScreen:
        """offers.locations (legacy): every location with a sellable offer.

        Provider-neutral: the list is built from the offer repository, not
        from a hardcoded provider. The market-first ``store`` flow is the
        customer path; this screen is kept for callbacks already in flight.
        """
        locations = await self._offers_repo.list_provider_locations()
        if not locations:
            return BotScreen(self._t.t("offers.no_locations"), self._menu_only())
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("offers.location_row", code=location_id, city=location_id),
                    callback_data=self._callback("offers", "plans", location_id),
                )
            ]
            for _provider_key, location_id in dict.fromkeys(locations)
        ]
        if not rows:
            return BotScreen(self._t.t("offers.no_locations"), self._menu_only())
        rows.append([self._menu_button()])
        return BotScreen(
            self._t.t("offers.locations_title"), InlineKeyboardMarkup(inline_keyboard=rows)
        )

    async def plans_screen(self, location_id: str) -> BotScreen:
        """offers.plans:{location}: sellable offers at one location."""
        try:
            plans, back_callback, cancel_callback = await self._view.plans_screen(location_id)
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.no_offers"), self._menu_only())
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t(
                        "offers.plan_row",
                        name=plan.name,
                        vcpu=plan.vcpu,
                        ram=plan.ram_gb,
                        disk=plan.disk_gb,
                        price=format_minor(plan.monthly_price_minor, plan.currency),
                    ),
                    callback_data=self._callback("offers", "os", encode_offer_ref(plan.offer_id)),
                )
            ]
            for plan in plans
        ]
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=back_callback),
                InlineKeyboardButton(text=self._t.t("nav.cancel"), callback_data=cancel_callback),
            ]
        )
        return BotScreen(
            self._t.t("offers.plans_title", location=location_id),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def os_screen(self, offer_id: UUID) -> BotScreen:
        """offers.os:{offer_id}: live OS options for the plan."""
        try:
            _offer, options, back_callback, cancel_callback = await self._view.os_screen(
                offer_id=offer_id
            )
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.unavailable"), self._menu_only())
        except OsUnavailableError:
            return BotScreen(self._t.t("offers.os_unavailable"), self._menu_only())
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("offers.os_row", name=option.name),
                    callback_data=option.select_callback,
                )
            ]
            for option in options
        ]
        rows.append(
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=back_callback),
                InlineKeyboardButton(text=self._t.t("nav.cancel"), callback_data=cancel_callback),
            ]
        )
        return BotScreen(self._t.t("offers.os_title"), InlineKeyboardMarkup(inline_keyboard=rows))

    async def confirm_screen(
        self, user: User, offer_id: UUID, os_index: int, panel_index: int | None = None
    ) -> BotScreen:
        """offers.confirm:{offer_id}:{os_index}[:{panel}]: exact price + wallet."""
        if user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        try:
            view: OfferConfirmView = await self._view.confirmation(
                user_id=user.id,
                offer_id=offer_id,
                os_index=os_index,
                panel_index=panel_index,
            )
        except OfferUnavailableError:
            return BotScreen(self._t.t("offers.unavailable"), self._menu_only())
        except OsUnavailableError:
            return BotScreen(self._t.t("offers.os_unavailable"), self._menu_only())
        lines = [
            self._t.t("offers.confirm_title"),
            self._t.t("offers.confirm_offer", name=view.offer.name),
            self._t.t(
                "offers.confirm_specs",
                vcpu=view.offer.vcpu,
                ram=view.offer.ram_gb,
                disk=view.offer.disk_gb,
                traffic=f" — {view.offer.traffic}" if view.offer.traffic else "",
            ),
            self._t.t("offers.confirm_os", os=view.os_name),
            self._t.t(
                "offers.confirm_panel",
                panel=view.panel_name or self._t.t("offers.panel_none"),
            ),
            self._t.t("offers.confirm_location", location=view.offer.location_id),
            self._t.t(
                "offers.confirm_price",
                price=await self._price_label(view.offer.monthly_price_minor, view.currency),
            ),
            self._t.t("offers.confirm_billing"),
            self._t.t(
                "offers.confirm_wallet",
                balance=format_minor(view.balance_minor, view.currency),
            ),
        ]
        if not view.sufficient:
            lines.append(self._t.t("offers.confirm_insufficient"))
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("offers.confirm_button"), callback_data=view.confirm_callback
                )
            ],
            [
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=view.back_callback),
                InlineKeyboardButton(
                    text=self._t.t("nav.cancel"), callback_data=view.cancel_callback
                ),
            ],
        ]
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def buy_screen(
        self,
        user: User | None,
        offer_id: UUID,
        os_index: int,
        panel_index: int | None,
        cb_key: str,
    ) -> BotScreen:
        """offers.buy: the idempotent checkout command (terminal action)."""
        if user is None or user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        # Deterministic per (signed) callback: a double tap replays the same
        # order instead of double-charging or double-ordering.
        idempotency_key = f"bot-monthly:{cb_key}"
        try:
            result = await self._checkout.create_order(
                user=user,
                offer_id=offer_id,
                os_name=await self._resolve_os_name(offer_id, os_index),
                idempotency_key=idempotency_key,
                panel_name=await self._resolve_panel_name(offer_id, panel_index),
            )
        except InsufficientHoldBalanceError as exc:
            return BotScreen(
                self._t.t("wallet.insufficient_balance", balance=str(exc)), self._menu_only()
            )
        except UserNotActiveError:
            return BotScreen(self._t.t("user.frozen"), self._menu_only())
        except (OfferUnavailableError, OsUnavailableError):
            return BotScreen(self._t.t("offers.unavailable"), self._menu_only())
        except CheckoutError as exc:
            logger.warning("monthly checkout rejected: %s", exc)
            return BotScreen(self._t.t("error.unknown"), self._menu_only())
        return self.order_screen(result)

    async def _resolve_os_name(self, offer_id: UUID, os_index: int) -> str:
        from cloud_platform.modules.offers.domain import OfferNotFoundError, SellableOffer

        offer: SellableOffer | None = await self._offers_repo.get(offer_id)
        if offer is None:
            raise OfferNotFoundError(f"offer {offer_id} not found")
        return await self._view.os_by_index(offer, os_index)

    async def _resolve_panel_name(self, offer_id: UUID, panel_index: int | None) -> str | None:
        from cloud_platform.modules.offers.domain import OfferNotFoundError, SellableOffer

        if panel_index is None:
            return None
        offer: SellableOffer | None = await self._offers_repo.get(offer_id)
        if offer is None:
            raise OfferNotFoundError(f"offer {offer_id} not found")
        return await self._view.panel_name_by_index(offer, panel_index)

    def order_screen(self, result: MonthlyCheckoutResult) -> BotScreen:
        text = self._t.t("offers.order_created", order_id=str(result.order.id)[:8])
        if result.replayed:
            text += "\n" + self._t.t("offers.order_replayed")
        return BotScreen(text, self._menu_only())

    # -- servers flow ------------------------------------------------------

    async def servers_list_screen(self, user: User, *, page: int = 1) -> BotScreen:
        """servers.list: the My Servers entry screen (delegated to the UI)."""
        if self._servers_ui is None:
            return BotScreen(self._t.t("servers.disabled"), self._menu_only())
        return await self._servers_ui.list_screen(user, page)

    async def server_detail_screen(self, user: User, server_id: UUID) -> BotScreen:
        """Backwards-compatible entry point for one owned server's details.

        Only kept so existing callers/tests can address a server by its LOCAL
        id; the bot itself always routes through a signed reference.
        """
        if self._servers_ui is None:
            return BotScreen(self._t.t("servers.disabled"), self._menu_only())
        ref = await self._servers_ui.ref_for(user, server_id)
        if ref is None:
            return BotScreen(self._t.t("servers.not_found"), self._menu_only())
        return await self._servers_ui.handle(Callback("servers", "view", (ref,)), user)

    async def server_extra_lines(self, server_id: UUID) -> list[str]:
        """Storefront facts (plan, price, renewal) for the details screen.

        These come from the LOCAL order/offer/renewal rows, never from the
        provider: the provider snapshot holds infrastructure state only.
        """
        lines: list[str] = []
        order = await self._orders.get_by_server(server_id)
        offer = (
            await self._offers_repo.get(order.offer_id)
            if self._orders is not None and order and order.offer_id
            else None
        )
        if offer is not None:
            lines.append(self._t.t("servers.detail_plan", plan=offer.name))
            lines.append(
                self._t.t(
                    "servers.detail_price",
                    price=format_minor(offer.selling_price_minor, offer.selling_currency),
                )
            )
        renewal = await self._renewals.get(server_id)
        if renewal is not None and renewal.provider_renewal_at is not None:
            date = renewal.provider_renewal_at.strftime("%Y-%m-%d")
            estimated = (
                self._t.t("servers.renewal_estimated") if renewal.renewal_date_estimated else ""
            )
            lines.append(self._t.t("servers.detail_renewal", date=date, estimated=estimated))
        return lines

    # -- wallet flow -------------------------------------------------------

    async def wallet_screen(self, user: User | None) -> BotScreen:
        if user is None or user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        view = await self._wallet.balance(user.id)
        if not view.has_wallet:
            return BotScreen(self._t.t("wallet.no_wallet"), self._menu_only())
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("wallet.history_button"),
                    callback_data=self._callback("wallet", "history"),
                )
            ],
            [self._menu_button()],
        ]
        return BotScreen(
            self._t.t("wallet.balance_title")
            + "\n"
            + self._t.t("wallet.balance_row", balance=view.formatted),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def wallet_history_screen(self, user: User) -> BotScreen:
        if user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        page = await self._wallet.history(user.id, limit=20)
        if not page.items:
            text = self._t.t("wallet.history_title") + "\n" + self._t.t("wallet.history_empty")
        else:
            labels = {
                LedgerEntryType.DEPOSIT.value: "wallet.entry.deposit",
                LedgerEntryType.HOLD.value: "wallet.entry.hold",
                LedgerEntryType.RELEASE.value: "wallet.entry.release",
                LedgerEntryType.CHARGE.value: "wallet.entry.charge",
                LedgerEntryType.REFUND.value: "wallet.entry.refund",
                LedgerEntryType.ADJUSTMENT.value: "wallet.entry.adjustment",
            }
            lines = [self._t.t("wallet.history_title")]
            for item in page.items:
                when = ""
                if item.created_at is not None:
                    when = datetime.fromisoformat(str(item.created_at)).strftime("%Y-%m-%d %H:%M")
                label = labels.get(item.entry_type, item.entry_type)
                lines.append(
                    self._t.t("wallet.history_row", date=when, label=label, amount=item.formatted)
                )
            text = "\n".join(lines)
        rows = [
            [self._back_button("wallet", "balance"), self._menu_button()],
        ]
        return BotScreen(text, InlineKeyboardMarkup(inline_keyboard=rows))

    # -- support -----------------------------------------------------------

    def support_screen(self) -> BotScreen:
        lines = [self._t.t("support.title"), self._t.t("support.text")]
        if self._support_contact:
            lines.append(self._t.t("support.contact", contact=self._support_contact))
        return BotScreen("\n".join(lines), self._menu_only())

    def _menu_screen(self) -> BotScreen:
        # Rendered by the main bot UI; here as a safe fallback.
        return BotScreen(self._t.t("menu.title"), self._menu_only())

    # -- dispatch ----------------------------------------------------------

    async def handle(
        self, data: str, *, user: User | None = None, chat_id: int | None = None
    ) -> BotScreen | None:
        """Handle a callback belonging to a MONTHLY flow.

        Returns None when the callback belongs to another flow (the caller
        routes it to the legacy UI).
        """
        del chat_id
        try:
            cb = decode_callback(data, self._key)
        except CallbackError:
            return None  # let the caller render the tamper notice
        if cb.flow not in MONTHLY_FLOWS:
            return None

        if cb.flow == "main":
            return self.menu_screen()
        if cb.flow == "store":
            return await self._store(cb, user)
        if cb.flow == "recharge":
            return await self._recharge_cb(cb, user)
        if cb.flow == "offers":
            return await self._offers(cb, user)
        if cb.flow == "servers":
            return await self._servers_cb(cb, user)
        if cb.flow == "wallet":
            return await self._wallet_cb(cb, user)
        if cb.flow == "support":
            return self.support_screen()
        return None

    async def _store(self, cb: Callback, user: User | None) -> BotScreen:
        """The provider-neutral storefront: market -> provider -> location."""
        if cb.screen == "market":
            return await self.markets_screen()
        if cb.screen == "providers" and len(cb.args) == 1:
            return await self.providers_screen(cb.args[0])
        if cb.screen == "locations" and len(cb.args) == 1:
            return await self.store_locations_screen(cb.args[0])
        if cb.screen == "plans" and len(cb.args) == 2:
            return await self.store_plans_screen(cb.args[0], cb.args[1])
        if cb.screen == "families" and len(cb.args) == 1:
            return await self.store_families_screen(cb.args[0])
        if cb.screen == "family" and len(cb.args) == 2:
            try:
                families, _back, _cancel = await self._view.families_screen(cb.args[0])
            except OfferUnavailableError:
                return BotScreen(self._t.t("offers.no_offers"), self._market_back_only())
            family = next((f for f in families if f.family_key == cb.args[1]), None)
            if family is None:
                return await self.store_families_screen(cb.args[0])
            if not getattr(family, "available", True):
                return self._family_unavailable_screen(cb.args[0])
            return await self._enter_family(cb.args[0], family.family_key, family.billing_model)
        if cb.screen == "products":
            # Legacy product-first callbacks re-enter through the family
            # screen instead of guessing a removed route.
            if len(cb.args) >= 1:
                return await self.store_families_screen(cb.args[0])
            return self._menu_screen()
        if cb.screen == "vps_locations" and len(cb.args) == 3:
            try:
                numbered = int(cb.args[2])
            except (TypeError, ValueError):
                numbered = 1
            return await self.store_vps_locations_screen(cb.args[0], cb.args[1], numbered)
        if cb.screen == "loc_cities" and len(cb.args) == 3:
            try:
                numbered = int(cb.args[2])
            except (TypeError, ValueError):
                numbered = 1
            return await self.store_cities_screen(cb.args[0], cb.args[1], numbered)
        if cb.screen == "loc_halls" and len(cb.args) == 5:
            try:
                numbered = int(cb.args[4])
            except (TypeError, ValueError):
                numbered = 1
            return await self.store_halls_screen(
                cb.args[0], cb.args[1], cb.args[2], cb.args[3], numbered
            )
        if cb.screen == "vps_plans" and len(cb.args) in (3, 4):
            try:
                numbered = int(cb.args[3]) if len(cb.args) == 4 else 1
            except (TypeError, ValueError):
                numbered = 1
            return await self.store_vps_plans_screen(cb.args[0], cb.args[1], cb.args[2], numbered)
        if cb.screen == "plan_detail" and len(cb.args) == 3:
            return await self.store_plan_detail_screen(cb.args[0], cb.args[1], cb.args[2])
        if cb.screen == "panel" and len(cb.args) == 2:
            try:
                os_index = int(cb.args[1])
            except (TypeError, ValueError):
                return self._menu_screen()
            offer_ref = cb.args[0]
            if self._resolve_offer_id(offer_ref) is None:
                return self._expired_screen()
            return await self.store_panel_screen(offer_ref, os_index)
        if cb.screen == "product_locations" and len(cb.args) == 4:
            try:
                priced: int = int(cb.args[2])
            except (TypeError, ValueError):
                return await self.store_families_screen(cb.args[0])
            currency = cb.args[3]
            if not currency:
                return await self.store_families_screen(cb.args[0])
            return await self.store_product_locations_screen(
                cb.args[0], cb.args[1], priced, currency
            )
        if cb.screen == "product_locations" and len(cb.args) in (2, 3):
            try:
                price: int | None = int(cb.args[2]) if len(cb.args) == 3 else None
            except (TypeError, ValueError):
                return await self.store_families_screen(cb.args[0])
            return await self.store_product_locations_screen(cb.args[0], cb.args[1], price, None)
        if cb.screen == "os" and len(cb.args) == 1:
            offer_id = self._resolve_offer_id(cb.args[0])
            if offer_id is None:
                return self._expired_screen()
            return await self.os_screen(offer_id)
        if cb.screen == "confirm" and len(cb.args) in (2, 3):
            if user is None:
                return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
            offer_id = self._resolve_offer_id(cb.args[0])
            if offer_id is None:
                return self._expired_screen()
            try:
                os_index = int(cb.args[1])
                panel_index = int(cb.args[2]) if len(cb.args) == 3 else None
            except (TypeError, ValueError):
                return self._expired_screen()
            return await self.confirm_screen(user, offer_id, os_index, panel_index)
        if cb.screen == "buy" and len(cb.args) in (2, 3):
            offer_id = self._resolve_offer_id(cb.args[0])
            if offer_id is None:
                return self._expired_screen()
            try:
                os_index = int(cb.args[1])
                panel_index = int(cb.args[2]) if len(cb.args) == 3 else None
            except (TypeError, ValueError):
                return self._expired_screen()
            return await self.buy_screen(user, offer_id, os_index, panel_index, cb.key)
        if cb.screen == "cloud_locations" and len(cb.args) == 3:
            try:
                numbered = int(cb.args[2])
            except (TypeError, ValueError):
                numbered = 1
            return await self.store_cloud_locations_screen(cb.args[0], cb.args[1], numbered)
        if cb.screen == "cloud_families" and len(cb.args) == 3:
            return await self.store_cloud_families_screen(cb.args[0], cb.args[1], cb.args[2])
        if cb.screen == "cloud_plans" and len(cb.args) in (3, 4):
            try:
                numbered = int(cb.args[3]) if len(cb.args) == 4 else 1
            except (TypeError, ValueError):
                numbered = 1
            return await self.store_cloud_plans_screen(cb.args[0], cb.args[1], cb.args[2], numbered)
        if cb.screen == "cloud_detail" and len(cb.args) == 3:
            return await self.store_cloud_detail_screen(cb.args[0], cb.args[1], cb.args[2])
        if cb.screen == "cloud_images" and len(cb.args) == 1:
            if self._resolve_offer_id(cb.args[0]) is None:
                return self._expired_screen()
            return await self.store_cloud_images_screen(cb.args[0])
        if cb.screen == "cloud_confirm" and len(cb.args) == 2:
            if user is None:
                return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
            if self._resolve_offer_id(cb.args[0]) is None:
                return self._expired_screen()
            try:
                image_index = int(cb.args[1])
            except (TypeError, ValueError):
                return self._expired_screen()
            return await self.store_cloud_confirm_screen(user, cb.args[0], image_index)
        if cb.screen == "cloud_buy" and len(cb.args) == 2:
            if self._resolve_offer_id(cb.args[0]) is None:
                return self._expired_screen()
            try:
                image_index = int(cb.args[1])
            except (TypeError, ValueError):
                return self._expired_screen()
            return await self.store_cloud_buy_screen(user, cb.args[0], image_index, cb.key)
        return self._menu_screen()

    async def _offers(self, cb: Callback, user: User | None) -> BotScreen:
        if cb.screen == "locations":
            return await self.locations_screen()
        if cb.screen == "plans" and len(cb.args) == 1:
            return await self.plans_screen(cb.args[0])
        if cb.screen == "os" and len(cb.args) == 1:
            offer_id = self._resolve_offer_id(cb.args[0])
            if offer_id is None:
                return self._expired_screen()
            return await self.os_screen(offer_id)
        if cb.screen == "confirm" and len(cb.args) == 2:
            if user is None:
                return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
            offer_id = self._resolve_offer_id(cb.args[0])
            if offer_id is None:
                return self._expired_screen()
            return await self.confirm_screen(user, offer_id, int(cb.args[1]), None)
        if cb.screen == "buy" and len(cb.args) == 2:
            offer_id = self._resolve_offer_id(cb.args[0])
            if offer_id is None:
                return self._expired_screen()
            return await self.buy_screen(user, offer_id, int(cb.args[1]), None, cb.key)
        return self._menu_screen()

    async def _servers_cb(self, cb: Callback, user: User | None) -> BotScreen:
        """Delegate every My Servers screen to the dedicated management UI."""
        if self._servers_ui is None:
            return BotScreen(self._t.t("servers.disabled"), self._menu_only())
        return await self._servers_ui.handle(cb, user)

    async def handle_text(self, text: str, user: User | None) -> BotScreen | None:
        """Route a free-text message to the My Servers prompts (rename, rDNS).

        Returns None when nothing was waiting for an answer, so the caller can
        keep ignoring ordinary chat messages.
        """
        if self._servers_ui is None:
            return None
        return await self._servers_ui.handle_text(text, user)

    async def _wallet_cb(self, cb: Callback, user: User | None) -> BotScreen:
        if user is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        if cb.screen == "balance":
            return await self.wallet_screen(user)
        if cb.screen == "history":
            return await self.wallet_history_screen(user)
        return self._menu_screen()
