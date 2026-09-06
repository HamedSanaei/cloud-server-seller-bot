"""Monthly prepaid Telegram UI (LEASEWEB-MVP).

The customer-facing screens of the monthly VPS storefront: locations ->
plans -> OS -> exact-price confirmation -> idempotent checkout, plus My
servers (with ownership-scoped power controls behind an explicit
confirmation), Wallet (balance + history) and Support.

Design mirrors ``bot/ui.py``: domain services build the SCREEN DATA with
signed callbacks; this module is the thin Telegram renderer, framework-light
enough to unit-test without Telegram or a database.

Callback scheme (extended M08-001):

- ``offers.locations`` — entry: list sellable locations
- ``offers.plans:{location}`` — plans at one location
- ``offers.os:{offer_id}`` — OS options (live, server-side filtered)
- ``offers.confirm:{offer_id}:{os_index}`` — exact price + wallet balance
- ``offers.buy:{offer_id}:{os_index}`` — terminal: idempotent checkout
- ``servers.list`` / ``servers.detail:{server_id}``
- ``servers.power_confirm:{server_id}:{action}`` / ``servers.power:{server_id}:{action}``
- ``wallet.balance`` / ``wallet.history``
- ``support.contact``

Every money amount is formatted from integer minor units (no float
arithmetic). Every server screen re-loads ownership from the repository —
a callback can never view or control another user's server.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.modules.checkout.service import (
    CheckoutError,
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
from cloud_platform.modules.renewals.domain import RenewalRepository
from cloud_platform.modules.users.domain import User
from cloud_platform.modules.wallet.domain import InsufficientHoldBalanceError, LedgerEntryType
from cloud_platform.modules.wallet.service import WalletHistoryService

logger = logging.getLogger(__name__)

#: Callback flows owned by this UI; other flows fall through to the
#: legacy hourly UI in ``bot/ui.py``.
MONTHLY_FLOWS = frozenset({"offers", "servers", "wallet", "support"})

#: Power actions exposed with explicit confirmation (destructive ones).
POWER_ACTIONS = ("power_on", "power_off", "reboot")


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
        self._t = translator or Translator()

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

    # -- offers flow -------------------------------------------------------

    async def locations_screen(self) -> BotScreen:
        """offers.locations: every location with at least one sellable offer."""
        locations = await self._offers_repo.list_provider_locations()
        if not locations:
            return BotScreen(self._t.t("offers.no_locations"), self._menu_only())
        rows = []
        for provider_key, location_id in locations:
            if provider_key != "leaseweb":
                continue
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._t.t("offers.location_row", code=location_id, city=location_id),
                        callback_data=self._callback("offers", "plans", location_id),
                    )
                ]
            )
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

    async def servers_list_screen(self, user: User) -> BotScreen:
        """servers.list: only servers owned by the current user."""
        if user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        servers = await self._servers.list_by_user(user.id)
        monthly = [s for s in servers if s.is_prepaid_monthly]
        if not monthly:
            return BotScreen(self._t.t("servers.empty"), self._menu_only())
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t(
                        "servers.row",
                        name=s.os or _short_id(s.id),
                        state=_state_label(self._t, s.state),
                    ),
                    callback_data=self._callback("servers", "detail", str(s.id)),
                )
            ]
            for s in sorted(monthly, key=lambda s: s.created_at or datetime.min, reverse=True)
        ]
        rows.append([self._menu_button()])
        return BotScreen(self._t.t("servers.title"), InlineKeyboardMarkup(inline_keyboard=rows))

    async def server_detail_screen(self, user: User, server_id: UUID) -> BotScreen:
        """servers.detail:{server_id}: details + power controls (ownership)."""
        if user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        server = await self._servers.get(server_id)
        if server is None or server.user_id != user.id:
            return BotScreen(self._t.t("servers.not_found"), self._menu_only())

        order = await self._orders.get_by_server(server.id)
        offer = await self._offers_repo.get(order.offer_id) if order and order.offer_id else None
        renewal = await self._renewals.get(server.id)

        lines = [
            self._t.t("servers.detail_title"),
            self._t.t("servers.detail_name", name=server.os or _short_id(server.id)),
            self._t.t("servers.detail_state", state=_state_label(self._t, server.state)),
        ]
        if server.ipv4:
            lines.append(self._t.t("servers.detail_ipv4", ipv4=server.ipv4))
        if server.ipv6:
            lines.append(self._t.t("servers.detail_ipv6", ipv6=server.ipv6))
        if server.os:
            lines.append(self._t.t("servers.detail_os", os=server.os))
        if offer:
            lines.append(self._t.t("servers.detail_location", location=offer.location_id))
            lines.append(self._t.t("servers.detail_plan", plan=offer.name))
            lines.append(
                self._t.t(
                    "servers.detail_price",
                    price=format_minor(offer.selling_price_minor, offer.selling_currency),
                )
            )
        if renewal is not None and renewal.provider_renewal_at is not None:
            date = renewal.provider_renewal_at.strftime("%Y-%m-%d")
            estimated = (
                self._t.t("servers.renewal_estimated") if renewal.renewal_date_estimated else ""
            )
            lines.append(self._t.t("servers.detail_renewal", date=date, estimated=estimated))
        if order is not None and order.provider_order_id:
            lines.append(
                self._t.t(
                    "servers.detail_order_ref",
                    ref=f"{server.provider_key}-{order.provider_order_id}",
                )
            )

        rows: list[list[InlineKeyboardButton]] = []
        if server.state in (ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED):
            actions = []
            if server.state is ServerLifecycleState.STOPPED:
                actions.append(
                    InlineKeyboardButton(
                        text=self._t.t("servers.power_on"),
                        callback_data=self._callback(
                            "servers", "power", str(server.id), "power_on"
                        ),
                    )
                )
            else:
                actions.append(
                    InlineKeyboardButton(
                        text=self._t.t("servers.reboot"),
                        callback_data=self._callback("servers", "power", str(server.id), "reboot"),
                    )
                )
                actions.append(
                    InlineKeyboardButton(
                        text=self._t.t("servers.power_off"),
                        callback_data=self._callback(
                            "servers", "power_confirm", str(server.id), "power_off"
                        ),
                    )
                )
            if actions:
                rows.append(actions)
        rows.append(
            [
                self._back_button("servers", "list"),
                self._menu_button(),
            ]
        )
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def power_confirm_screen(self, server_id: UUID, action: str) -> BotScreen:
        """servers.power_confirm:{server_id}:{action}: explicit confirmation."""
        if action not in POWER_ACTIONS:
            return self._menu_screen()
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("servers.power_confirm_button"),
                    callback_data=self._callback("servers", "power", str(server_id), action),
                )
            ],
            [
                self._back_button("servers", "detail", str(server_id)),
                self._menu_button(),
            ],
        ]
        text = (
            self._t.t("servers.power_confirm_title")
            + "\n"
            + self._t.t("servers.power_confirm_text")
        )
        return BotScreen(text, InlineKeyboardMarkup(inline_keyboard=rows))

    async def power_screen(self, user: User, server_id: UUID, action: str) -> BotScreen:
        """servers.power:{server_id}:{action}: ownership-scoped execution."""
        if user.id is None or self._power is None:
            return BotScreen(self._t.t("servers.no_control"), self._menu_only())
        server = await self._servers.get(server_id)
        if server is None or server.user_id != user.id:
            return BotScreen(self._t.t("servers.not_found"), self._menu_only())
        try:
            method = {
                "power_on": self._power.power_on,
                "power_off": self._power.power_off,
                "reboot": self._power.reboot,
            }[action]
            idempotency_key = f"bot-power:{server_id}:{action}"
            await method(user.id, server.id, idempotency_key)
        except Exception as exc:
            logger.warning("power %s failed for server %s: %s", action, server_id, exc)
            return BotScreen(self._t.t("servers.power_failed", detail=str(exc)), self._menu_only())
        return BotScreen(
            self._t.t("servers.power_done", action=action),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=self._t.t("nav.back"),
                            callback_data=self._callback("servers", "detail", str(server_id)),
                        )
                    ],
                    [self._menu_button()],
                ]
            ),
        )

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

        if cb.flow == "offers":
            return await self._offers(cb, user)
        if cb.flow == "servers":
            return await self._servers_cb(cb, user)
        if cb.flow == "wallet":
            return await self._wallet_cb(cb, user)
        if cb.flow == "support":
            return self.support_screen()
        return None

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
        if user is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        if cb.screen == "list":
            return await self.servers_list_screen(user)
        if cb.screen == "detail" and len(cb.args) == 1:
            return await self.server_detail_screen(user, UUID(cb.args[0]))
        if cb.screen == "power_confirm" and len(cb.args) == 2:
            return await self.power_confirm_screen(UUID(cb.args[0]), cb.args[1])
        if cb.screen == "power" and len(cb.args) == 2:
            return await self.power_screen(user, UUID(cb.args[0]), cb.args[1])
        return self._menu_screen()

    async def _wallet_cb(self, cb: Callback, user: User | None) -> BotScreen:
        if user is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        if cb.screen == "balance":
            return await self.wallet_screen(user)
        if cb.screen == "history":
            return await self.wallet_history_screen(user)
        return self._menu_screen()
