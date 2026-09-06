"""Bot UI layer (M08-001..005): renders screens and dispatches signed callbacks.

The domain services (``BuyFlowViewService``, ``OsSelectionService``,
``PurchaseConfirmationService``) build the SCREEN DATA with signed
callbacks; this module is the thin Telegram-facing renderer: it maps
screen data to aiogram text + inline keyboards, and maps
``callback_query.data`` back through :func:`decode_callback` into the right
screen. It is deliberately framework-light so the whole navigation can be
unit-tested without a live Telegram connection or a database.

Callback rules (M08-001 scheme): every button carries a truncated-HMAC
signature; a tampered or expired callback renders the "link expired" notice
instead of acting on it. Confirming an order derives the idempotency key
from the (signed) callback key, so pressing the same button twice replays
the same order instead of charging twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_platform.core.i18n import Translator
from cloud_platform.modules.catalog.domain import CatalogRepository, OfferNotFoundError, OfferRef
from cloud_platform.modules.catalog.service import (
    BuyFlowViewService,
    OsSelectionError,
    OsSelectionService,
    PlanSelectionError,
    PurchaseConfirmationService,
)
from cloud_platform.modules.compute.service import (
    CreateServerResult,
    MaintenanceBlockedError,
    OfferDisabledError,
    QuotaExceededError,
    UserNotActiveError,
)
from cloud_platform.modules.navigation.domain import (
    DONE,
    MAIN,
    Callback,
    CallbackError,
    NavScreen,
    decode_callback,
    encode_callback,
)
from cloud_platform.modules.provider_accounts.domain import NoProviderAccountError
from cloud_platform.modules.users.domain import TermsAcceptanceRequiredError, User
from cloud_platform.modules.wallet.domain import InsufficientHoldBalanceError


@dataclass(frozen=True, slots=True)
class BotScreen:
    """One rendered bot screen: message text + inline keyboard."""

    text: str
    keyboard: InlineKeyboardMarkup


class _CreateCommand(Protocol):
    async def create_server(
        self,
        *,
        user: User,
        offer_ref: OfferRef,
        idempotency_key: str,
        at: datetime | None = None,
    ) -> CreateServerResult: ...


class BotUi:
    """Renders the customer screens and dispatches verified callbacks."""

    def __init__(
        self,
        signing_key: str,
        buy_flow: BuyFlowViewService,
        *,
        os_selection: OsSelectionService,
        confirmation: PurchaseConfirmationService,
        catalog: CatalogRepository,
        create: _CreateCommand,
        translator: Translator | None = None,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        self._key = signing_key
        self._buy = buy_flow
        self._os = os_selection
        self._confirm = confirmation
        self._catalog = catalog
        self._create = create
        self._t = translator or Translator()
        #: Per-chat current screen. The M08-001 callbacks are state-encoded:
        #: the same (flow, screen, args) wire form means "select" on that
        #: screen and "back" from the NEXT screen, so the dispatcher must
        #: know where the user currently is. In-memory (single polling
        #: process); swap for Redis when the bot runs on many instances.
        self._screens: dict[int, NavScreen] = {}

    # -- helpers -----------------------------------------------------------

    def _callback(self, flow: str, screen: str, *args: str) -> str:
        return encode_callback(Callback(flow=flow, screen=screen, args=args), self._key)

    def _menu_button(self) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=self._t.t("nav.cancel"),
            callback_data=self._callback("main", "menu"),
        )

    def _menu_only(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[self._menu_button()]])

    @staticmethod
    def _format_minor(minor: int, currency: str) -> str:
        """Minor units -> \"1.23 EUR\" (no float money)."""
        major, rem = divmod(minor, 100)
        return f"{major}.{rem:02d} {currency}"

    # -- screens -----------------------------------------------------------

    def menu_screen(self) -> BotScreen:
        """The main menu; each entry is a signed callback to a flow entry."""
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("menu.buy"),
                    callback_data=self._callback("buy", "locations"),
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
                    callback_data=self._callback("recharge", "amount"),
                )
            ],
        ]
        return BotScreen(self._t.t("menu.title"), InlineKeyboardMarkup(inline_keyboard=rows))

    async def locations_screen(self) -> BotScreen:
        """buy.locations: sellable datacenters grouped by country (M08-002)."""
        views = await self._buy.locations_screen()
        if not views:
            return BotScreen(self._t.t("buy.empty"), self._menu_only())
        rows = []
        for country in views:
            for option in country.options:
                label = self._t.t(
                    "buy.location_row",
                    name=option.name,
                    count=option.offer_count,
                )
                rows.append(
                    [InlineKeyboardButton(text=label, callback_data=option.select_callback)]
                )
        rows.append([self._menu_button()])
        return BotScreen(
            self._t.t("buy.locations_title"), InlineKeyboardMarkup(inline_keyboard=rows)
        )

    async def plans_screen(self, provider_key: str, location_id: str) -> BotScreen:
        """buy.plans: the enabled plans sold at one location (M08-003)."""
        try:
            view = await self._buy.plans_screen(provider_key, location_id)
        except PlanSelectionError:
            return BotScreen(self._t.t("buy.empty"), self._menu_only())
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t(
                        "buy.plan_row",
                        name=plan.name,
                        vcpu=plan.vcpu,
                        memory=plan.memory_mb,
                        disk=plan.disk_gb,
                        price=self._format_minor(plan.price_per_quantum, plan.currency),
                    ),
                    callback_data=callback_data,
                )
            ]
            for plan, callback_data in zip(view.plans, view.plan_callbacks, strict=True)
        ]
        nav = []
        if view.back_callback:
            nav.append(
                InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=view.back_callback)
            )
        nav.append(
            InlineKeyboardButton(text=self._t.t("nav.cancel"), callback_data=view.cancel_callback)
        )
        rows.append(nav)
        title = self._t.t("buy.plans_title", location=view.city or view.location_name)
        return BotScreen(title, InlineKeyboardMarkup(inline_keyboard=rows))

    async def os_screen(self, provider_key: str, location_id: str, offer_id: UUID) -> BotScreen:
        """buy.os: architecture-gated OS images for the chosen offer (M08-004)."""
        try:
            view = await self._os.os_screen(provider_key, location_id, offer_id)
        except OsSelectionError:
            return BotScreen(self._t.t("buy.os_unavailable"), self._menu_only())
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("buy.os_row", name=option.name, family=option.os_family),
                    callback_data=option.select_callback,
                )
            ]
            for option in view.options
        ]
        nav = [
            InlineKeyboardButton(text=self._t.t("nav.back"), callback_data=view.back_callback),
            InlineKeyboardButton(text=self._t.t("nav.cancel"), callback_data=view.cancel_callback),
        ]
        rows.append(nav)
        title = self._t.t("buy.os_title")
        return BotScreen(title, InlineKeyboardMarkup(inline_keyboard=rows))

    async def confirm_screen(
        self,
        user: User,
        provider_key: str,
        location_id: str,
        offer_id: UUID,
        image_id: str | None,
    ) -> BotScreen:
        """buy.confirm: exact price policy + wallet impact (M08-005)."""
        view = await self._confirm.confirmation(
            user_id=user.id or UUID(int=0),
            provider_key=provider_key,
            location_id=location_id,
            offer_id=offer_id,
            image_id=image_id,
        )
        policy = view.policy
        price = self._format_minor(policy.selling_minor_per_quantum, policy.currency)
        minutes = policy.quantum_seconds // 60
        lines = [
            self._t.t("buy.confirm_title"),
            self._t.t("buy.confirm_offer", name=view.offer.name),
            self._t.t("buy.confirm_price", price=price, minutes=minutes),
        ]
        if view.wallet.has_wallet:
            balance = self._format_minor(view.wallet.balance_minor, policy.currency)
            after = self._format_minor(view.wallet.balance_after_hold_minor, policy.currency)
            line = self._t.t("buy.confirm_wallet", balance=balance, after=after)
            if not view.wallet.sufficient:
                line += self._t.t("buy.confirm_wallet_insufficient")
            lines.append(line)
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("buy.confirm_button"),
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

    def order_screen(self, result: object) -> BotScreen:
        """buy done: the create command's outcome (idempotent by callback key)."""
        replayed = bool(getattr(result, "replayed", False))
        server = getattr(result, "server", None)
        server_id = str(getattr(server, "id", "?"))
        text = self._t.t("buy.order_created", server_id=server_id)
        if replayed:
            text += "\n" + self._t.t("buy.order_replayed")
        return BotScreen(text, self._menu_only())

    def coming_soon_screen(self) -> BotScreen:
        """Placeholder for flows/screens not built yet (servers, wallet...)."""
        return BotScreen(self._t.t("buy.coming_soon"), self._menu_only())

    def expired_screen(self) -> BotScreen:
        """A callback that failed verification (M08-001 tamper guard)."""
        return BotScreen(self._t.t("nav.expired"), self._menu_only())

    def identity_screen(self) -> BotScreen:
        """The acting Telegram user could not be resolved."""
        return BotScreen(self._t.t("buy.no_identity"), self._menu_only())

    def error_screen(self, exc: Exception) -> BotScreen:
        """Map a create-command failure to a stable user-facing message."""
        mapping: dict[type[Exception], str] = {
            UserNotActiveError: "user.frozen",
            TermsAcceptanceRequiredError: "terms.required",
            OfferNotFoundError: "offer.unavailable",
            OfferDisabledError: "offer.unavailable",
            MaintenanceBlockedError: "maintenance.blocked",
            QuotaExceededError: "quota.exceeded",
            NoProviderAccountError: "buy.no_account",
            InsufficientHoldBalanceError: "wallet.insufficient_balance",
        }
        key = mapping.get(type(exc), "error.unknown")
        return BotScreen(self._t.t(key), self._menu_only())

    # -- dispatch ----------------------------------------------------------

    async def handle(
        self, data: str, *, user: User | None = None, chat_id: int | None = None
    ) -> BotScreen:
        """Decode and verify ``data``, then render the target screen.

        The M08-001 callbacks are state-encoded: the same wire form means
        "select an option" when pressed on its own screen and "back" when
        pressed from the next screen. The dispatcher tracks the current
        screen per chat and applies that distinction. Unknown or unbuilt
        flows land on the coming-soon screen; a tampered callback lands on
        the expired screen. Never raises for user input.
        """
        try:
            cb = decode_callback(data, self._key)
        except CallbackError:
            return self.expired_screen()

        if cb.flow == "main" and cb.screen == "menu":
            self._remember(chat_id, MAIN)
            return self.menu_screen()
        if cb.flow != "buy":
            # servers / wallet / recharge flows are not built yet.
            return self.coming_soon_screen()

        # buy.confirm is the terminal action: pressing it again (even after
        # DONE) replays the idempotent order — never a navigation.
        if cb.screen == "confirm":
            return await self._forward(cb, user=user, chat_id=chat_id)

        current = self._screens.get(chat_id or 0)
        # SELECT when the user is on the callback's own screen (or the chat
        # has no tracked state yet — deep links act as forward); BACK when
        # the callback is the previous screen's wire form (M08-001).
        if current is None or current.name == cb.screen:
            return await self._forward(cb, user=user, chat_id=chat_id)
        return await self._back(cb, user=user, chat_id=chat_id)

    def _remember(self, chat_id: int | None, screen: NavScreen) -> None:
        self._screens[chat_id or 0] = screen

    async def _forward(self, cb: Callback, *, user: User | None, chat_id: int | None) -> BotScreen:
        """The user picked an option on ``cb.screen``: advance the flow."""
        if cb.screen == "locations":
            if len(cb.args) >= 2:
                self._remember(chat_id, NavScreen("buy", "plans"))
                return await self.plans_screen(cb.args[0], cb.args[1])
            return await self.locations_screen()
        if cb.screen == "plans" and len(cb.args) >= 3:
            self._remember(chat_id, NavScreen("buy", "os"))
            return await self.os_screen(cb.args[0], cb.args[1], UUID(cb.args[2]))
        if cb.screen == "os" and len(cb.args) >= 4:
            if user is None:
                return self.identity_screen()
            self._remember(chat_id, NavScreen("buy", "confirm"))
            image_id = None if cb.args[3] == "none" else cb.args[3]
            return await self.confirm_screen(
                user, cb.args[0], cb.args[1], UUID(cb.args[2]), image_id
            )
        if cb.screen == "confirm" and len(cb.args) >= 4:
            self._remember(chat_id, DONE)
            return await self._place_order(user, cb)
        return self.coming_soon_screen()

    async def _back(self, cb: Callback, *, user: User | None, chat_id: int | None) -> BotScreen:
        """The user pressed back from the next screen: re-render ``cb``'s view."""
        if cb.screen == "locations":
            self._remember(chat_id, NavScreen("buy", "locations"))
            return await self.locations_screen()
        if cb.screen == "plans" and len(cb.args) >= 2:
            self._remember(chat_id, NavScreen("buy", "plans"))
            return await self.plans_screen(cb.args[0], cb.args[1])
        if cb.screen == "os" and len(cb.args) >= 3:
            self._remember(chat_id, NavScreen("buy", "os"))
            return await self.os_screen(cb.args[0], cb.args[1], UUID(cb.args[2]))
        return self.coming_soon_screen()

    async def _place_order(self, user: User | None, cb: Callback) -> BotScreen:
        """buy.confirm -> the idempotent create command (M08-006 order path)."""
        if user is None or user.id is None:
            return self.identity_screen()
        _, _, offer_id, _ = cb.args[:4]
        try:
            offer = await self._catalog.get_by_id(UUID(offer_id))
        except ValueError:
            return self.error_screen(OfferNotFoundError(f"bad offer id {offer_id!r}"))
        if offer is None:
            return self.error_screen(OfferNotFoundError(f"offer {offer_id} not found"))
        ref = OfferRef(
            provider_key=offer.provider_key,
            plan_id=offer.plan_id,
            location_id=offer.location_id,
        )
        # Deterministic per (signed) callback: a double tap replays, never
        # double-charges.
        idempotency_key = f"bot-create:{cb.key}"
        try:
            result = await self._create.create_server(
                user=user,
                offer_ref=ref,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            return self.error_screen(exc)
        return self.order_screen(result)
