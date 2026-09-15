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
from typing import Protocol
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
from cloud_platform.modules.navigation.domain import (
    Callback,
    CallbackError,
    decode_callback,
    encode_callback,
)
from cloud_platform.modules.offers.domain import SellableOfferRepository
from cloud_platform.modules.orders.domain import ProviderOrderRepository
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


def format_minor(minor: int, currency: str) -> str:
    """Integer-formatted money (no floats)."""
    major, rem = divmod(int(minor), 100)
    return f"{major}.{rem:02d} {currency}"


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
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        self._key = signing_key
        self._view = offers_view
        self._checkout = checkout
        self._servers = servers
        self._orders = orders
        self._renewals = renewals
        self._offers_repo = offers_repo
        self._wallet = wallet_history
        self._power = power
        self._support_contact = support_contact
        self._recharge = recharge
        self._t = translator or Translator()
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
                    callback_data=self._callback("store", "os", str(plan.offer_id)),
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
            self._t.t(
                "store.plans_title",
                location=location_id,
                provider=self._provider_name(provider_key),
            ),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    def _provider_name(self, provider_key: str) -> str:
        """Customer-facing provider name from configuration (never the key)."""
        try:
            listings = self._view.provider_display_name(provider_key)
        except Exception:  # pragma: no cover - view services always provide it
            return provider_key
        return listings or provider_key

    # -- recharge flow -----------------------------------------------------

    async def recharge_screen(self, user: User) -> BotScreen:
        """recharge.amounts: preset top-up amounts for the wallet currency."""
        if user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        view: WalletBalanceView = await self._wallet.balance(user.id)
        if not view.has_wallet or not view.currency:
            return BotScreen(self._t.t("wallet.no_wallet"), self._menu_only())
        if self._recharge is None or not self._recharge.supports_currency(view.currency):
            # No online top-up for this currency: say so and point at support
            # instead of offering a button that cannot work.
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
            for amount in recharge_presets(view.currency)
        ]
        rows.append([self._back_button("wallet", "balance"), self._menu_button()])
        return BotScreen(
            self._t.t("recharge.title")
            + "\n"
            + f"{self._t.t('wallet.balance_row', balance=view.formatted)}",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

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
        compatible = self._recharge.compatible_gateways(currency, amount_minor)
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
                amount=format_minor(start.session.amount_minor, start.session.currency),
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
                    callback_data=self._callback("offers", "os", str(plan.offer_id)),
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

    async def confirm_screen(self, user: User, offer_id: UUID, os_index: int) -> BotScreen:
        """offers.confirm:{offer_id}:{os_index}: exact monthly price + wallet."""
        if user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        try:
            view: OfferConfirmView = await self._view.confirmation(
                user_id=user.id, offer_id=offer_id, os_index=os_index
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
            self._t.t("offers.confirm_location", location=view.offer.location_id),
            self._t.t(
                "offers.confirm_price",
                price=format_minor(view.offer.monthly_price_minor, view.currency),
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
        self, user: User | None, offer_id: UUID, os_index: int, cb_key: str
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

    async def wallet_screen(self, user: User) -> BotScreen:
        if user.id is None:
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
        if cb.screen == "os" and len(cb.args) == 1:
            return await self.os_screen(UUID(cb.args[0]))
        if cb.screen == "confirm" and len(cb.args) == 2:
            if user is None:
                return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
            return await self.confirm_screen(user, UUID(cb.args[0]), int(cb.args[1]))
        if cb.screen == "buy" and len(cb.args) == 2:
            return await self.buy_screen(user, UUID(cb.args[0]), int(cb.args[1]), cb.key)
        return self._menu_screen()

    async def _offers(self, cb: Callback, user: User | None) -> BotScreen:
        if cb.screen == "locations":
            return await self.locations_screen()
        if cb.screen == "plans" and len(cb.args) == 1:
            return await self.plans_screen(cb.args[0])
        if cb.screen == "os" and len(cb.args) == 1:
            return await self.os_screen(UUID(cb.args[0]))
        if cb.screen == "confirm" and len(cb.args) == 2:
            if user is None:
                return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
            return await self.confirm_screen(user, UUID(cb.args[0]), int(cb.args[1]))
        if cb.screen == "buy" and len(cb.args) == 2:
            return await self.buy_screen(user, UUID(cb.args[0]), int(cb.args[1]), cb.key)
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
