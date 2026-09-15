"""Telegram «سرورهای من» — the customer-facing VPS management experience.

This module is a *renderer*. Every screen is built from the customer-safe
records of :mod:`cloud_platform.modules.servers.models`; no provider DTO, URL,
key or resource id is ever touched here — the application service
(:class:`~cloud_platform.modules.servers.service.ServerManagementService`) owns
ownership, policy, confirmations, idempotency and audit.

Flow (spec §1)::

    My Servers  ->  details  ->  ⚙️ manage  ->  actions

Callback budget. Telegram allows 64 bytes of ``callback_data``, and a signed
callback spends 21 of them. Every button therefore carries a *reference*
minted by :class:`~cloud_platform.bot.sessions.ServerSessions` (8 characters)
instead of a UUID, plus — for a destructive action — a **deterministic nonce**
and an optional **selection index**. The one-time confirmation token is minted
by the service and kept server-side in that session; it never travels through
Telegram.

Authorization. A callback is never trusted: ``servers.view:<ref>`` resolves the
reference *for the current customer only*, and the service re-loads the row and
re-checks ``user_id`` before anything happens. A foreign reference resolves to
nothing, so the customer sees "not found" and no provider call is made.

Persian wording lives entirely in the i18n catalogue (``servers.*``), never in
this module.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_platform.bot.sessions import (
    PendingAction,
    PendingInput,
    ServerSessions,
)
from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import SessionStoreUnavailable
from cloud_platform.modules.navigation.domain import Callback
from cloud_platform.modules.offers.domain import SellableOfferRepository
from cloud_platform.modules.orders.domain import ProviderOrderRepository
from cloud_platform.modules.renewals.domain import RenewalRepository
from cloud_platform.modules.servers.confirmations import (
    ConfirmationStatus,
    arguments_digest,
)
from cloud_platform.modules.servers.models import (
    CustomerServerPage,
    CustomerServerState,
    CustomerServerView,
    IpAddressView,
    MonitoringView,
    ReinstallImageView,
    ServerActionOutcome,
    ServerConsoleView,
    ServerOperation,
    ServerRenewalView,
    ServerSnapshotView,
    TrafficUsageView,
    format_bytes,
)
from cloud_platform.modules.servers.policies import ServerManagementPolicy
from cloud_platform.modules.servers.service import (
    ServerAmbiguousOutcomeError,
    ServerConfirmationError,
    ServerManagementError,
    ServerManagementService,
    ServerNotFoundError,
    ServerOperationNotAllowedError,
    ServerProviderError,
    ServerUnavailableError,
)
from cloud_platform.modules.users.domain import User

logger = logging.getLogger(__name__)

__all__ = ["ServerManagementUi"]

#: Prefix of the reference handed to the provider for every mutation this UI
#: makes, so a customer action is traceable in the provider's own audit trail.
IDEMPOTENCY_PREFIX = "bot"

#: i18n key of the operation label used in "request sent" wording.
_OPERATION_LABELS: dict[ServerOperation, str] = {
    ServerOperation.START: "servers.op.start",
    ServerOperation.STOP: "servers.op.stop",
    ServerOperation.REBOOT: "servers.op.reboot",
    ServerOperation.REINSTALL: "servers.op.reinstall",
    ServerOperation.PASSWORD_RESET: "servers.op.password_reset",  # pragma: allowlist secret
    ServerOperation.SNAPSHOT_CREATE: "servers.op.snapshot_create",
    ServerOperation.SNAPSHOT_RESTORE: "servers.op.snapshot_restore",
    ServerOperation.SNAPSHOT_DELETE: "servers.op.snapshot_delete",
    ServerOperation.IP_NULL_ROUTE: "servers.op.ip_null_route",
    ServerOperation.IP_UNNULL_ROUTE: "servers.op.ip_unnull_route",
    ServerOperation.ISO_ATTACH: "servers.op.iso_attach",
    ServerOperation.ISO_DETACH: "servers.op.iso_detach",
    ServerOperation.MONITORING_ENABLE: "servers.op.monitoring_enable",
    ServerOperation.RENAME: "servers.op.rename",
}

#: Confirmation screen (title key, body key) per destructive operation.
_CONFIRM_COPY: dict[ServerOperation, tuple[str, str]] = {
    ServerOperation.STOP: ("servers.power_confirm_title", "servers.power_confirm_text"),
    ServerOperation.SNAPSHOT_CREATE: (
        "servers.snapshot_create_title",
        "servers.snapshot_create_text",
    ),
    ServerOperation.SNAPSHOT_RESTORE: (
        "servers.snapshot_restore_title",
        "servers.snapshot_restore_text",
    ),
    ServerOperation.SNAPSHOT_DELETE: (
        "servers.snapshot_delete_title",
        "servers.snapshot_delete_text",
    ),
    ServerOperation.REINSTALL: ("servers.reinstall_title", "servers.reinstall_text"),
    ServerOperation.PASSWORD_RESET: ("servers.password_title", "servers.password_text"),
    ServerOperation.IP_NULL_ROUTE: ("servers.ip_null_title", "servers.ip_null_text"),
    ServerOperation.ISO_ATTACH: ("servers.iso_title", "servers.iso_text"),
    ServerOperation.ISO_DETACH: ("servers.iso_title", "servers.iso_text"),
}

#: Selection screens whose rendered list is remembered per server, so a button
#: can carry an index instead of a provider identifier.
SEL_IMAGES = "images"
SEL_SNAPSHOTS = "snapshots"
SEL_ISOS = "isos"
SEL_IPS = "ips"

#: Power actions, by their short callback token.
_POWER = {"on": ServerOperation.START, "off": ServerOperation.STOP, "rb": ServerOperation.REBOOT}

#: Optional hook the storefront uses to add its own lines (plan, price,
#: renewal) to the details screen. It receives the LOCAL server id and returns
#: ready-to-render lines; it must never return provider data (§2).
ExtraLines = Callable[[UUID], Awaitable[list[str]]]

#: Every screen this UI owns (used to reject unknown ones).
SCREENS = frozenset(
    {
        "list",
        "view",
        "manage",
        "refresh",
        "pwr",
        "exec",
        "console",
        "traffic",
        "snap",
        "snaplist",
        "snapnew",
        "snapres",
        "snapdel",
        "rein",
        "reinpick",
        "pwreset",
        "ips",
        "ipnull",
        "ipunnull",
        "iprdns",
        "iso",
        "isoat",
        "isodet",
        "mon",
        "monon",
        "rename",
        "renew",
        "autorenew",
    }
)

#: The commercial statuses a customer screen names (PROD-HARDENING §36). An
#: unknown/newer value degrades to the raw key rather than guessing.
_COMMERCIAL_STATE_KEYS: dict[str, str] = {
    "active": "servers.commercial.active",
    "payment_due": "servers.commercial.payment_due",
    "grace_period": "servers.commercial.grace_period",
    "suspended": "servers.commercial.suspended",
    "expired": "servers.commercial.expired",
    "insufficient_funds": "servers.commercial.payment_due",
    "manual_cancellation_required": "servers.commercial.attention",
    "cancelled": "servers.commercial.cancelled",
}


class ServerManagementUi:
    """Renders the My Servers screens and dispatches their callbacks."""

    def __init__(
        self,
        signing_key: str,
        management: ServerManagementService,
        *,
        sessions: ServerSessions | None = None,
        orders: ProviderOrderRepository | None = None,
        renewals: RenewalRepository | None = None,
        offers_repo: SellableOfferRepository | None = None,
        page_size: int = 5,
        extra_lines: ExtraLines | None = None,
        translator: Translator | None = None,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        self._key = signing_key
        self._mgmt = management
        self._sessions = sessions or ServerSessions()
        self._orders = orders
        self._renewals = renewals
        self._offers_repo = offers_repo
        self._page_size = page_size
        self._extra_lines = extra_lines
        self._t = translator or Translator()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def owns(self, cb: Callback) -> bool:
        """Whether this UI handles the callback (flow ``servers``)."""
        return cb.flow == "servers" and cb.screen in SCREENS

    async def ref_for(self, user: User, server_id: UUID) -> str | None:
        """The signed-callback reference for one of the customer's servers."""
        if user.id is None:
            return None
        return await self._sessions.ref_for(user.id, server_id)

    async def handle(self, cb: Callback, user: User | None) -> BotScreen:
        """Render the screen a callback addresses."""
        if user is None or user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        if not self._mgmt.policy.enabled:
            return BotScreen(self._t.t("servers.disabled"), self._menu_only())
        try:
            return await self._dispatch(cb, user)
        except ServerManagementError as exc:
            return self._error_screen(exc)
        except SessionStoreUnavailable:
            # Fail closed (§9): without the shared session state we cannot even
            # resolve which server a button addresses, so we refuse the whole
            # request instead of guessing.
            logger.error("telegram session store unavailable during callback")
            return self._unavailable_screen()

    async def list_screen(self, user: User, page: int = 1) -> BotScreen:
        """The My Servers entry screen (never raises at the customer)."""
        if user.id is None:
            return BotScreen(self._t.t("buy.no_identity"), self._menu_only())
        if not self._mgmt.policy.enabled:
            return BotScreen(self._t.t("servers.disabled"), self._menu_only())
        try:
            page_view = await self._mgmt.list_servers(user.id, page=page)
        except ServerManagementError as exc:
            return self._error_screen(exc)
        except SessionStoreUnavailable:
            logger.error("telegram session store unavailable listing servers")
            return self._unavailable_screen()
        if not page_view.items:
            return BotScreen(self._t.t("servers.empty"), self._menu_only())
        return await self._list_screen(user, page_view)

    async def handle_text(self, text: str, user: User | None) -> BotScreen | None:
        """Consume a free-text answer (rename, reverse DNS), else ``None``.

        Returning ``None`` means "this UI did not want the message", so the
        caller can ignore it instead of the bot answering everything.
        """
        if user is None or user.id is None:
            return None
        try:
            pending = await self._sessions.take_input(user.id)
        except SessionStoreUnavailable:
            # The prompt could not be consumed. Refuse rather than drop the
            # message silently — a consumed-elsewhere prompt must not be typed
            # twice into two different actions.
            logger.error("telegram session store unavailable reading a text prompt")
            return self._unavailable_screen()
        if pending is None:
            return None
        try:
            return await self._apply_input(pending, text, user)
        except ServerManagementError as exc:
            return self._error_screen(exc)
        except SessionStoreUnavailable:
            logger.error("telegram session store unavailable applying a text prompt")
            return self._unavailable_screen()

    # ------------------------------------------------------------------
    # Screens
    # ------------------------------------------------------------------

    async def _dispatch(self, cb: Callback, user: User) -> BotScreen:
        args = cb.args
        if cb.screen == "list":
            return await self.list_screen(user, _int(args[0], 1) if args else 1)
        if cb.screen in {"view", "refresh", "manage"} and len(args) == 1:
            ref = args[0]
            if cb.screen == "refresh":
                return await self._refresh_screen(user, ref)
            return await self._view_screen(user, ref, manage=cb.screen == "manage")
        if cb.screen == "pwr" and len(args) == 2:
            return await self._power_screen(user, args[0], args[1])
        if cb.screen == "exec" and len(args) == 2:
            return await self._exec_screen(user, args[0], args[1])
        if cb.screen == "console" and len(args) == 1:
            return await self._console_screen(user, args[0])
        if cb.screen == "traffic" and len(args) == 1:
            return await self._traffic_screen(user, args[0])
        if cb.screen == "snap" and len(args) == 1:
            return await self._snapshot_menu(user, args[0])
        if cb.screen == "snaplist" and len(args) == 1:
            return await self._snapshot_list(user, args[0])
        if cb.screen == "snapnew" and len(args) == 1:
            return await self._snapshot_create_screen(user, args[0])
        if cb.screen in {"snapres", "snapdel"} and len(args) == 2:
            operation = (
                ServerOperation.SNAPSHOT_RESTORE
                if cb.screen == "snapres"
                else ServerOperation.SNAPSHOT_DELETE
            )
            return await self._snapshot_confirm(user, args[0], args[1], operation)
        if cb.screen == "rein" and len(args) == 1:
            return await self._reinstall_list(user, args[0])
        if cb.screen == "reinpick" and len(args) == 2:
            return await self._reinstall_confirm(user, args[0], args[1])
        if cb.screen == "pwreset" and len(args) == 1:
            return await self._password_confirm(user, args[0])
        if cb.screen == "ips" and len(args) == 1:
            return await self._ip_list(user, args[0])
        if cb.screen in {"ipnull", "ipunnull", "iprdns"} and len(args) == 2:
            return await self._ip_screen(user, args[0], args[1], cb.screen)
        if cb.screen == "iso" and len(args) == 1:
            return await self._iso_menu(user, args[0])
        if cb.screen == "isoat" and len(args) == 2:
            return await self._iso_attach(user, args[0], args[1])
        if cb.screen == "isodet" and len(args) == 1:
            return await self._iso_detach(user, args[0])
        if cb.screen == "mon" and len(args) == 1:
            return await self._monitoring_screen(user, args[0])
        if cb.screen == "monon" and len(args) == 1:
            return await self._monitoring_enable(user, args[0])
        if cb.screen == "rename" and len(args) == 1:
            return await self._rename_prompt(user, args[0])
        if cb.screen == "renew" and len(args) == 1:
            return await self._renew_confirm(user, args[0])
        if cb.screen == "autorenew" and len(args) == 2:
            return await self._auto_renew_screen(user, args[0], enabled=args[1] == "on")
        return self._menu_screen()

    async def _list_screen(self, user: User, page: CustomerServerPage) -> BotScreen:
        """The paginated server list: readable blocks + one Manage button each."""
        blocks: list[str] = [self._t.t("servers.title"), ""]
        rows: list[list[InlineKeyboardButton]] = []
        for index, view in enumerate(page.items, start=1):
            ref = await self._sessions.ref_for(user.id, view.server_id)  # type: ignore[arg-type]
            blocks.append(self._list_block(index, view))
            blocks.append("")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._t.t("servers.list_manage_n", index=str(index)),
                        callback_data=self._callback("servers", "manage", ref),
                    )
                ]
            )
        rows.append(self._pager(page))
        rows.append([self._menu_button()])
        blocks.append(self._t.t("servers.list_header"))
        return BotScreen("\n".join(blocks).strip(), InlineKeyboardMarkup(inline_keyboard=rows))

    def _list_block(self, index: int, view: CustomerServerView) -> str:
        """One customer-friendly server block (unknown fields are omitted)."""
        title = view.location_label or view.display_name or self._t.t("servers.unnamed")
        lines = [self._t.t("servers.list_index", index=str(index), title=title)]
        if view.ip:
            lines.append(self._t.t("servers.list_ip", ip=view.ip))
        elif view.state is CustomerServerState.PROVISIONING:
            lines.append(self._t.t("servers.list_ip_pending"))
        if view.operating_system:
            lines.append(self._t.t("servers.list_os", os=view.operating_system))
        lines.append(self._t.t("servers.list_state", state=self._state(view.state)))
        return "\n".join(lines)

    def _pager(self, page: CustomerServerPage) -> list[InlineKeyboardButton]:
        """``[⬅️ قبلی] [1 / 4] [بعدی ➡️]`` (empty for a single page)."""
        if page.pages <= 1:
            return []
        row: list[InlineKeyboardButton] = []
        if page.has_previous:
            row.append(
                InlineKeyboardButton(
                    text=self._t.t("servers.prev"),
                    callback_data=self._callback("servers", "list", str(page.page - 1)),
                )
            )
        row.append(
            InlineKeyboardButton(
                text=self._t.t("servers.page", page=str(page.page), pages=str(page.pages)),
                callback_data=self._callback("servers", "list", str(page.page)),
            )
        )
        if page.has_next:
            row.append(
                InlineKeyboardButton(
                    text=self._t.t("servers.next"),
                    callback_data=self._callback("servers", "list", str(page.page + 1)),
                )
            )
        return row

    async def _view_screen(self, user: User, ref: str, *, manage: bool = False) -> BotScreen:
        """Spec §6: the customer-safe server details page."""
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        view = await self._mgmt.get_server(user.id, server_id)  # type: ignore[arg-type]
        if manage:
            return await self._manage_screen(user, ref, view)
        return await self._detail_screen(ref, view)

    async def _detail_screen(self, ref: str, view: CustomerServerView) -> BotScreen:
        lines = [self._t.t("servers.detail_header")]
        lines.extend(self._spec_lines(view))
        if self._extra_lines is not None:
            lines.extend(part for part in await self._extra_lines(view.server_id) if part)
        if view.refresh_error:
            lines.append(self._t.t("servers.refresh_partial"))
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("servers.manage_button"),
                    callback_data=self._callback("servers", "manage", ref),
                )
            ],
            [
                InlineKeyboardButton(
                    text=self._t.t("servers.refresh_button"),
                    callback_data=self._callback("servers", "refresh", ref),
                ),
                self._back_button("servers", "list"),
            ],
            [self._menu_button()],
        ]
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    def _spec_lines(self, view: CustomerServerView) -> list[str]:
        """Only the fields the provider genuinely returned (§6/§7)."""
        lines: list[str] = []
        if view.location_label:
            lines.append(self._t.t("servers.spec_location", value=view.location_label))
        if view.ip:
            lines.append(self._t.t("servers.spec_ip", value=view.ip))
        if view.operating_system:
            lines.append(self._t.t("servers.spec_os", value=view.operating_system))
        if view.plan:
            lines.append(self._t.t("servers.spec_plan", value=view.plan))
        if view.ram_gb:
            lines.append(self._t.t("servers.spec_ram", value=f"{view.ram_gb} GB"))
        if view.cpu:
            lines.append(self._t.t("servers.spec_cpu", value=f"{view.cpu} vCPU"))
        if view.storage_gb:
            lines.append(self._t.t("servers.spec_disk", value=f"{view.storage_gb} GB"))
        if view.traffic_limit or view.traffic_used_bytes is not None:
            used = format_bytes(view.traffic_used_bytes) or "—"
            limit = view.traffic_limit or format_bytes(view.traffic_limit_bytes) or "—"
            lines.append(self._t.t("servers.spec_traffic", used=used, limit=limit))
        lines.append(self._t.t("servers.spec_state", value=self._state(view.state)))
        if view.contract_started_at:
            lines.append(self._t.t("servers.spec_started", value=view.contract_started_at))
        if view.contract_ends_at:
            lines.append(self._t.t("servers.spec_ends", value=view.contract_ends_at))
        lines.extend(self._commercial_lines(view))
        return lines

    def _commercial_lines(self, view: CustomerServerView) -> list[str]:
        """The COMMERCIAL block, rendered next to the infrastructure state.

        Provider state and billing state are different facts and are shown as
        such (§36): ``وضعیت سرور`` comes from the provider, while
        ``وضعیت سرویس``/``اعتبار تا``/``مهلت پرداخت`` come from the local
        commercial record. When the platform holds no record, the block is
        omitted entirely rather than guessing a status.
        """
        if not view.has_commercial_record:
            return []
        lines: list[str] = []
        status_key = _COMMERCIAL_STATE_KEYS.get(str(view.commercial_status))
        lines.append(
            self._t.t(
                "servers.commercial.status",
                value=self._t.t(status_key) if status_key else str(view.commercial_status),
            )
        )
        if view.next_renewal_at is not None and not view.commercial_payable:
            lines.append(
                self._t.t("servers.commercial.valid_until", value=_format_day(view.next_renewal_at))
            )
        if view.grace_until is not None:
            lines.append(
                self._t.t("servers.commercial.grace_until", value=_format_day(view.grace_until))
            )
        if view.renewal_price_minor:
            lines.append(
                self._t.t(
                    "servers.commercial.price",
                    value=_format_minor(view.renewal_price_minor, view.renewal_currency or ""),
                )
            )
        if view.auto_renew_enabled is not None:
            lines.append(
                self._t.t(
                    "servers.commercial.auto_renew",
                    value=self._t.t(
                        "servers.commercial.on"
                        if view.auto_renew_enabled
                        else "servers.commercial.off"
                    ),
                )
            )
        if view.commercial_payable:
            lines.append(self._t.t("servers.commercial.recharge_hint"))
        return lines

    async def _manage_screen(self, user: User, ref: str, view: CustomerServerView) -> BotScreen:
        """Spec §1: the ⚙️ مدیریت سرور submenu, built from the policy."""
        policy = self._mgmt.policy
        allowed = self._allowed_operations(view, policy)
        rows: list[list[InlineKeyboardButton]] = []

        def add(screen: str, key: str, *extra: str) -> None:
            rows.append([self._action_button(screen, key, ref, *extra)])

        if ServerOperation.SNAPSHOT_LIST in allowed:
            rows.append(
                [
                    self._action_button("view", "servers.info_button", ref),
                    self._action_button("snap", "servers.snapshots_button", ref),
                ]
            )
        else:
            rows.append([self._action_button("view", "servers.info_button", ref)])
        if ServerOperation.START in allowed:
            add("pwr", "servers.power_on", "on")
        if ServerOperation.REBOOT in allowed:
            add("pwr", "servers.reboot", "rb")
        if ServerOperation.STOP in allowed:
            add("pwr", "servers.power_off", "off")
        if ServerOperation.CONSOLE in allowed:
            add("console", "servers.console_button")
        if ServerOperation.TRAFFIC in allowed:
            add("traffic", "servers.traffic_button")
        if ServerOperation.REINSTALL in allowed:
            add("rein", "servers.reinstall_button")
        if ServerOperation.PASSWORD_RESET in allowed:
            add("pwreset", "servers.password_button")
        if ServerOperation.IP_LIST in allowed:
            add("ips", "servers.ips_button")
        if ServerOperation.ISO_LIST in allowed:
            add("iso", "servers.iso_button")
        if ServerOperation.MONITORING in allowed:
            add("mon", "servers.monitoring_button")
        if ServerOperation.RENAME in allowed:
            add("rename", "servers.rename_button")
        if ServerOperation.RENEW_NOW in allowed and view.commercial_payable:
            add("renew", "servers.renew_button")
        if ServerOperation.AUTO_RENEW in allowed and view.auto_renew_enabled is not None:
            add(
                "autorenew",
                "servers.auto_renew_off_button"
                if view.auto_renew_enabled
                else "servers.auto_renew_on_button",
                "off" if view.auto_renew_enabled else "on",
            )

        rows.append([self._back_button("servers", "view", ref), self._menu_button()])
        text = "\n".join(
            [
                self._t.t("servers.manage_title"),
                self._t.t("servers.confirm_target", target=self._target(view)),
                "",
                self._t.t("servers.manage_hint"),
            ]
        )
        return BotScreen(text, InlineKeyboardMarkup(inline_keyboard=rows))

    def _allowed_operations(
        self, view: CustomerServerView, policy: ServerManagementPolicy
    ) -> set[ServerOperation]:
        """The operations the policy exposes for THIS server right now.

        Derived from the customer state (so ``START`` disappears while the
        server runs) plus the operator policy. Provider capability is checked
        again by the service before anything is called, so this is purely
        presentational.
        """
        allowed: set[ServerOperation] = set()
        if view.state is CustomerServerState.STOPPED:
            allowed |= {ServerOperation.START, ServerOperation.REBOOT}
        if view.state is CustomerServerState.RUNNING:
            allowed |= {ServerOperation.STOP, ServerOperation.REBOOT}
        for operation in (
            ServerOperation.CONSOLE,
            ServerOperation.TRAFFIC,
            ServerOperation.SNAPSHOT_LIST,
            ServerOperation.REINSTALL,
            ServerOperation.PASSWORD_RESET,
            ServerOperation.IP_LIST,
            ServerOperation.ISO_LIST,
            ServerOperation.MONITORING,
            ServerOperation.RENAME,
            ServerOperation.RENEW_NOW,
            ServerOperation.AUTO_RENEW,
        ):
            allowed.add(operation)
        return {op for op in allowed if policy.allows(op)}

    async def _refresh_screen(self, user: User, ref: str) -> BotScreen:
        """Spec §27: read-only reconciliation, then the refreshed details."""
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        view = await self._mgmt.refresh_server(user.id, server_id)  # type: ignore[arg-type]
        screen = await self._detail_screen(ref, view)
        banner = (
            self._t.t("servers.refresh_partial")
            if view.refresh_error
            else self._t.t("servers.refresh_done")
        )
        return BotScreen(f"{banner}\n\n{screen.text}", screen.keyboard)

    # -- power ------------------------------------------------------------

    async def _power_screen(self, user: User, ref: str, token: str) -> BotScreen:
        operation = _POWER.get(token)
        if operation is None:
            return self._menu_screen()
        customer = _customer_id(user)
        server_id = await self._sessions.server_id(customer, ref)
        if server_id is None:
            return self._expired_screen()
        if self._mgmt.policy.requires_confirmation(operation):
            return await self._stage_confirmation(user, ref, operation, arguments={})
        outcome = await self._run_power(customer, server_id, operation)
        return self._outcome_screen(ref, outcome)

    async def _run_power(
        self, customer: UUID, server_id: UUID, operation: ServerOperation
    ) -> ServerActionOutcome:
        """One idempotent power action through the shared operation ledger."""
        key = f"{IDEMPOTENCY_PREFIX}-power:{server_id}:{operation.value}"
        if operation is ServerOperation.START:
            return await self._mgmt.start_server(customer, server_id, idempotency_key=key)
        if operation is ServerOperation.STOP:
            return await self._mgmt.stop_server(customer, server_id, idempotency_key=key)
        return await self._mgmt.reboot_server(customer, server_id, idempotency_key=key)

    # -- confirmations ----------------------------------------------------

    async def _stage_confirmation(
        self,
        user: User,
        ref: str,
        operation: ServerOperation,
        *,
        arguments: dict[str, str],
        target: str | None = None,
    ) -> BotScreen:
        """Mint a one-time token, remember the action, render the confirm screen.

        The pending action is keyed by a deterministic nonce derived from the
        operation and its arguments, so confirming the same thing twice can
        never open a second path to the provider.
        """
        customer = _customer_id(user)
        server_id = await self._sessions.server_id(customer, ref)
        if server_id is None:
            return self._expired_screen()
        token = await self._mgmt.issue_confirmation(
            customer, server_id, operation, arguments=arguments
        )
        nonce = _nonce(operation, arguments)
        title_key, body_key = _CONFIRM_COPY.get(
            operation, ("servers.confirm_title", "servers.confirm_generic_text")
        )
        await self._sessions.stash(
            customer,
            ref,
            nonce,
            PendingAction(
                operation=operation,
                arguments=dict(arguments),
                confirmation_token=token,
                body_key=body_key,
                label_key=_OPERATION_LABELS.get(operation, "servers.op.unknown"),
                target=target,
            ),
        )
        return self._confirm_screen_from(title_key, body_key, ref, nonce, target)

    def _confirm_screen_from(
        self,
        title_key: str,
        body_key: str,
        ref: str,
        nonce: str,
        target: str | None,
    ) -> BotScreen:
        lines = [self._t.t(title_key), "", self._t.t(body_key)]
        if target:
            lines.extend(["", self._t.t("servers.confirm_target", target=target)])
        lines.extend(["", self._t.t("servers.confirm_irreversible")])
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("servers.confirm_button"),
                    callback_data=self._callback("servers", "exec", ref, nonce),
                )
            ],
            [
                self._back_button("servers", "manage", ref),
                self._menu_button(),
            ],
        ]
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def _exec_screen(self, user: User, ref: str, nonce: str) -> BotScreen:
        """Run a confirmed action exactly once (the nonce is single-use)."""
        pending = await self._sessions.take(user.id, ref, nonce)  # type: ignore[arg-type]
        if pending is None:
            return BotScreen(
                self._t.t("servers.action_in_progress"),
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [self._back_button("servers", "view", ref)],
                        [self._menu_button()],
                    ]
                ),
            )
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        outcome = await self._execute(user, server_id, pending)
        if isinstance(outcome, ServerRenewalView):
            return await self._renewal_result_screen(ref, outcome)
        return self._outcome_screen(ref, outcome)

    async def _execute(
        self, user: User, server_id: UUID, pending: PendingAction
    ) -> ServerActionOutcome | ServerRenewalView:
        """Dispatch a confirmed action to the application service."""
        customer = user.id
        assert customer is not None  # narrowed by the caller
        token = pending.confirmation_token
        args = pending.arguments
        operation = pending.operation
        if operation is ServerOperation.RENEW_NOW:
            # Commercial: settles one period from the customer's own wallet. It
            # is the SAME application call the worker makes, so the price and
            # the idempotency key can never diverge.
            return await self._mgmt.renew_now(customer, server_id, confirmation_token=token)
        if operation is ServerOperation.STOP:
            return await self._mgmt.stop_server(
                customer,
                server_id,
                confirmation_token=token,
                idempotency_key=f"{IDEMPOTENCY_PREFIX}-power:{server_id}:stop",
            )
        if operation is ServerOperation.SNAPSHOT_CREATE:
            return await self._mgmt.create_snapshot(
                customer, server_id, name=args.get("name", ""), confirmation_token=token
            )
        if operation is ServerOperation.SNAPSHOT_RESTORE:
            return await self._mgmt.restore_snapshot(
                customer,
                server_id,
                snapshot_ref=args.get("snapshot", ""),
                confirmation_token=token,
            )
        if operation is ServerOperation.SNAPSHOT_DELETE:
            return await self._mgmt.delete_snapshot(
                customer,
                server_id,
                snapshot_ref=args.get("snapshot", ""),
                confirmation_token=token,
            )
        if operation is ServerOperation.REINSTALL:
            return await self._mgmt.reinstall(
                customer,
                server_id,
                image_ref=args.get("image", ""),
                confirmation_token=token,
            )
        if operation is ServerOperation.PASSWORD_RESET:
            return await self._mgmt.reset_password(customer, server_id, confirmation_token=token)
        if operation is ServerOperation.IP_NULL_ROUTE:
            return await self._mgmt.null_route_ip(
                customer, server_id, ip=args.get("ip", ""), confirmation_token=token
            )
        if operation is ServerOperation.ISO_ATTACH:
            return await self._mgmt.attach_iso(
                customer, server_id, iso_ref=args.get("iso", ""), confirmation_token=token
            )
        if operation is ServerOperation.ISO_DETACH:
            return await self._mgmt.detach_iso(customer, server_id, confirmation_token=token)
        raise ServerOperationNotAllowedError(
            "unsupported confirmed operation",
            operation=operation,
            reason="not_exposed",
        )

    # -- commercial lifecycle (§36-§38) -----------------------------------

    async def _renew_confirm(self, user: User, ref: str) -> BotScreen:
        """The 💳 renew-now confirmation screen (money moves; confirm first)."""
        customer = _customer_id(user)
        server_id = await self._sessions.server_id(customer, ref)
        if server_id is None:
            return self._expired_screen()
        view = await self._mgmt.get_server(customer, server_id)
        if not view.commercial_payable:
            return BotScreen(
                self._t.t("servers.renew_not_due"),
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [self._back_button("servers", "manage", ref)],
                        [self._menu_button()],
                    ]
                ),
            )
        amount = (
            _format_minor(view.renewal_price_minor, view.renewal_currency or "")
            if view.renewal_price_minor
            else "—"
        )
        lines = [
            self._t.t("servers.renew_title"),
            "",
            self._t.t("servers.confirm_target", target=self._target(view)),
            self._t.t("servers.renew_amount", amount=amount),
        ]
        if view.next_renewal_at is not None:
            lines.append(self._t.t("servers.renew_period", value=_format_day(view.next_renewal_at)))
        lines.append(self._t.t("servers.renew_note"))
        # Reuse the shared one-time confirmation: it is minted with the same
        # atomic, shared store as every destructive action, so a double click
        # across two replicas still settles the period exactly once.
        token = await self._mgmt.issue_confirmation(
            customer, server_id, ServerOperation.RENEW_NOW, arguments={}
        )
        nonce = _nonce(ServerOperation.RENEW_NOW, {})
        await self._sessions.stash(
            customer,
            ref,
            nonce,
            PendingAction(
                operation=ServerOperation.RENEW_NOW,
                arguments={},
                confirmation_token=token,
                body_key="servers.renew_text",
                label_key="servers.op.renew_now",
                target=self._target(view),
            ),
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=self._t.t("servers.renew_confirm_button"),
                    callback_data=self._callback("servers", "exec", ref, nonce),
                )
            ],
            [self._back_button("servers", "manage", ref), self._menu_button()],
        ]
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def _auto_renew_screen(self, user: User, ref: str, *, enabled: bool) -> BotScreen:
        """Flip the durable automatic-renewal preference (ownership-checked)."""
        customer = _customer_id(user)
        server_id = await self._sessions.server_id(customer, ref)
        if server_id is None:
            return self._expired_screen()
        outcome = await self._mgmt.set_auto_renew(customer, server_id, enabled=enabled)
        if outcome.auto_renew_enabled is None:
            return BotScreen(
                self._t.t("servers.auto_renew_unavailable"),
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [self._back_button("servers", "manage", ref)],
                        [self._menu_button()],
                    ]
                ),
            )
        current = outcome.auto_renew_enabled
        text = "\n".join(
            [
                self._t.t("servers.auto_renew_title"),
                "",
                self._t.t("servers.auto_renew_on" if current else "servers.auto_renew_off"),
                self._t.t("servers.auto_renew_note"),
            ]
        )
        return BotScreen(
            text,
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [self._back_button("servers", "manage", ref)],
                    [self._menu_button()],
                ]
            ),
        )

    async def _renewal_result_screen(self, ref: str, result: ServerRenewalView) -> BotScreen:
        """Render the truthful outcome of a manual settlement (no invention)."""
        key = _RENEWAL_RESULT_KEYS.get(result.reason, "servers.renew_failed")
        fields = {
            "amount": (
                _format_minor(result.amount_minor, result.currency)
                if result.amount_minor and result.currency
                else "—"
            ),
            "value": (_format_day(result.period_end) if result.period_end is not None else "—"),
        }
        lines = [self._t.t(key, **fields)]
        rows = [
            [self._back_button("servers", "view", ref)],
            [self._menu_button()],
        ]
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    # -- read-only screens ------------------------------------------------

    async def _console_screen(self, user: User, ref: str) -> BotScreen:
        """Spec §9: a temporary console link, rendered as a URL button."""
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        session: ServerConsoleView = await self._mgmt.console(user.id, server_id)  # type: ignore[arg-type]
        text = "\n".join(
            [
                self._t.t("servers.console_title"),
                "",
                self._t.t("servers.console_note"),
            ]
        )
        rows: list[list[InlineKeyboardButton]] = []
        if session.url.startswith(("https://", "http://")):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._t.t("servers.console_open_button"), url=session.url
                    )
                ]
            )
        else:
            text = f"{text}\n\n{self._t.t('servers.console_unavailable')}"
        rows.append([self._back_button("servers", "manage", ref), self._menu_button()])
        return BotScreen(text, InlineKeyboardMarkup(inline_keyboard=rows))

    async def _traffic_screen(self, user: User, ref: str) -> BotScreen:
        """Spec §10: only the directions the provider actually returned."""
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        usage: TrafficUsageView = await self._mgmt.traffic(user.id, server_id)  # type: ignore[arg-type]
        lines = [self._t.t("servers.traffic_title"), ""]
        if usage.unavailable_reason:
            lines.append(self._t.t("servers.traffic_unavailable"))
        else:
            if usage.period_from and usage.period_to:
                lines.append(
                    self._t.t(
                        "servers.traffic_period",
                        start=str(usage.period_from),
                        end=str(usage.period_to),
                    )
                )
            if usage.separate_directions:
                lines.append(
                    self._t.t(
                        "servers.traffic_down",
                        value=format_bytes(usage.downloaded_bytes) or "0 B",
                    )
                )
                lines.append(
                    self._t.t(
                        "servers.traffic_up", value=format_bytes(usage.uploaded_bytes) or "0 B"
                    )
                )
            lines.append(
                self._t.t("servers.traffic_total", value=format_bytes(usage.total_bytes) or "0 B")
            )
            limit = usage.limit_label or format_bytes(usage.limit_bytes)
            if limit:
                lines.append(self._t.t("servers.traffic_limit", value=limit))
        rows = [
            [
                self._back_button("servers", "manage", ref),
                self._menu_button(),
            ]
        ]
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    # -- snapshots --------------------------------------------------------

    async def _snapshot_menu(self, user: User, ref: str) -> BotScreen:
        rows = [
            [
                self._action_button("snaplist", "servers.snapshots_list_button", ref),
                self._action_button("snapnew", "servers.snapshot_create_button", ref),
            ],
            [self._back_button("servers", "manage", ref), self._menu_button()],
        ]
        return BotScreen(
            self._t.t("servers.snapshots_button"),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def _snapshot_list(self, user: User, ref: str) -> BotScreen:
        customer = _customer_id(user)
        server_id = await self._sessions.server_id(customer, ref)
        if server_id is None:
            return self._expired_screen()
        snapshots = await self._mgmt.snapshots(customer, server_id)
        await self._sessions.remember(customer, ref, SEL_SNAPSHOTS, tuple(snapshots))
        if not snapshots:
            rows = [[self._back_button("servers", "snap", ref), self._menu_button()]]
            return BotScreen(
                self._t.t("servers.snapshots_empty"),
                InlineKeyboardMarkup(inline_keyboard=rows),
            )
        lines = [self._t.t("servers.snapshots_title"), ""]
        rows = []
        for index, snapshot in enumerate(snapshots):
            lines.append(self._snapshot_line(index, snapshot))
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._t.t("servers.snapshot_restore_button"),
                        callback_data=self._callback("servers", "snapres", ref, str(index)),
                    ),
                    InlineKeyboardButton(
                        text=self._t.t("servers.snapshot_delete_button"),
                        callback_data=self._callback("servers", "snapdel", ref, str(index)),
                    ),
                ]
            )
        rows.append([self._back_button("servers", "snap", ref), self._menu_button()])
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    def _snapshot_line(self, index: int, snapshot: ServerSnapshotView) -> str:
        return self._t.t(
            "servers.snapshot_row",
            name=snapshot.name or f"#{index + 1}",
            state=snapshot.state or "—",
            date=snapshot.created_at or "—",
        )

    async def _snapshot_create_screen(self, user: User, ref: str) -> BotScreen:
        """Snapshot creation is confirmed; the name is generated, not typed."""
        return await self._stage_confirmation(
            user,
            ref,
            ServerOperation.SNAPSHOT_CREATE,
            arguments={"name": _snapshot_name()},
        )

    async def _snapshot_confirm(
        self, user: User, ref: str, index: str, operation: ServerOperation
    ) -> BotScreen:
        """Bind the confirmation to the snapshot the CUSTOMER SAW."""
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        snapshot = await self._selection(user, ref, SEL_SNAPSHOTS, index)
        if not isinstance(snapshot, ServerSnapshotView):
            return self._expired_screen()
        return await self._stage_confirmation(
            user,
            ref,
            operation,
            arguments={"snapshot": snapshot.ref},
            target=snapshot.name or snapshot.ref,
        )

    # -- reinstall --------------------------------------------------------

    async def _reinstall_list(self, user: User, ref: str) -> BotScreen:
        """Spec §13: the provider's CURRENT images, never a hard-coded list."""
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        images = await self._mgmt.reinstall_images(user.id, server_id)  # type: ignore[arg-type]
        await self._sessions.remember(user.id, ref, SEL_IMAGES, tuple(images))  # type: ignore[arg-type]
        rows: list[list[InlineKeyboardButton]] = []
        lines = [self._t.t("servers.reinstall_title"), "", self._t.t("servers.reinstall_choose")]
        for index, image in enumerate(images):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=image.name,
                        callback_data=self._callback("servers", "reinpick", ref, str(index)),
                    )
                ]
            )
        if not images:
            lines.append(self._t.t("servers.reinstall_empty"))
        rows.append([self._back_button("servers", "manage", ref), self._menu_button()])
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def _reinstall_confirm(self, user: User, ref: str, index: str) -> BotScreen:
        image = await self._selection(user, ref, SEL_IMAGES, index)
        if not isinstance(image, ReinstallImageView):
            return self._expired_screen()
        return await self._stage_confirmation(
            user,
            ref,
            ServerOperation.REINSTALL,
            arguments={"image": image.ref},
            target=image.name,
        )

    async def _password_confirm(self, user: User, ref: str) -> BotScreen:
        return await self._stage_confirmation(
            user, ref, ServerOperation.PASSWORD_RESET, arguments={}
        )

    # -- IPs --------------------------------------------------------------

    async def _ip_list(self, user: User, ref: str) -> BotScreen:
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        rows_data = await self._mgmt.list_ips(user.id, server_id)  # type: ignore[arg-type]
        await self._sessions.remember(user.id, ref, SEL_IPS, tuple(rows_data))  # type: ignore[arg-type]
        lines = [self._t.t("servers.ips_title"), ""]
        rows: list[list[InlineKeyboardButton]] = []
        for index, ip in enumerate(rows_data):
            lines.append(self._ip_line(ip))
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._t.t("servers.ip_rdns_button"),
                        callback_data=self._callback("servers", "iprdns", ref, str(index)),
                    ),
                    (
                        InlineKeyboardButton(
                            text=self._t.t(
                                "servers.ip_unnull_button"
                                if ip.null_routed
                                else "servers.ip_null_button"
                            ),
                            callback_data=self._callback(
                                "servers",
                                "ipunnull" if ip.null_routed else "ipnull",
                                ref,
                                str(index),
                            ),
                        )
                    ),
                ]
            )
        rows.append([self._back_button("servers", "manage", ref), self._menu_button()])
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    def _ip_line(self, ip: IpAddressView) -> str:
        kind = self._t.t("servers.ip_main" if ip.main_ip else "servers.ip_secondary")
        if ip.null_routed:
            kind += self._t.t("servers.ip_null_tag")
        return self._t.t("servers.ip_row", ip=ip.ip, kind=kind)

    async def _ip_screen(self, user: User, ref: str, index: str, screen: str) -> BotScreen:
        """Reverse DNS (typed), null route (confirmed) and un-null (reversible)."""
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        ip = await self._selection(user, ref, SEL_IPS, index)
        if not isinstance(ip, IpAddressView):
            return self._expired_screen()
        if screen == "iprdns":
            await self._sessions.await_input(
                user.id,  # type: ignore[arg-type]
                ref,
                PendingInput(
                    operation=ServerOperation.IP_SET_RDNS,
                    server_ref=ref,
                    prompt_key="servers.ip_rdns_prompt",
                    argument="reverse_lookup",
                    extra={"ip": ip.ip},
                ),
            )
            return self._prompt_screen("servers.ip_rdns_prompt", ref)
        if screen == "ipnull":
            return await self._stage_confirmation(
                user,
                ref,
                ServerOperation.IP_NULL_ROUTE,
                arguments={"ip": ip.ip},
                target=ip.ip,
            )
        outcome = await self._mgmt.unnull_route_ip(user.id, server_id, ip=ip.ip)  # type: ignore[arg-type]
        return self._outcome_screen(ref, outcome)

    # -- rename + text prompts -------------------------------------------

    async def _rename_prompt(self, user: User, ref: str) -> BotScreen:
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        await self._sessions.await_input(
            user.id,  # type: ignore[arg-type]
            ref,
            PendingInput(
                operation=ServerOperation.RENAME,
                server_ref=ref,
                prompt_key="servers.rename_prompt",
                argument="display_name",
            ),
        )
        return self._prompt_screen("servers.rename_prompt", ref)

    def _prompt_screen(self, prompt_key: str, ref: str) -> BotScreen:
        rows = [[self._back_button("servers", "manage", ref), self._menu_button()]]
        return BotScreen(self._t.t(prompt_key), InlineKeyboardMarkup(inline_keyboard=rows))

    async def _apply_input(self, pending: PendingInput, text: str, user: User) -> BotScreen:
        """Apply a typed answer (rename / reverse DNS) through the service."""
        server_id = await self._sessions.server_id(user.id, pending.server_ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        customer = _customer_id(user)
        value = (text or "").strip()
        if pending.operation is ServerOperation.RENAME:
            result = await self._mgmt.rename(customer, server_id, display_name=value)
            return BotScreen(
                self._t.t("servers.rename_done", name=result.display_name),
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [self._back_button("servers", "view", pending.server_ref)],
                        [self._menu_button()],
                    ]
                ),
            )
        if pending.operation is ServerOperation.IP_SET_RDNS:
            ip = pending.extra.get("ip", "")
            await self._mgmt.set_reverse_dns(customer, server_id, ip=ip, reverse_lookup=value)
            return BotScreen(
                self._t.t("servers.ip_rdns_done"),
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [self._back_button("servers", "ips", pending.server_ref)],
                        [self._menu_button()],
                    ]
                ),
            )
        return self._menu_screen()

    # -- ISO --------------------------------------------------------------

    async def _iso_menu(self, user: User, ref: str) -> BotScreen:
        rows = [
            [self._action_button("isoat", "servers.iso_attach_button", ref)],
            [self._action_button("isodet", "servers.iso_detach_button", ref)],
            [self._back_button("servers", "manage", ref), self._menu_button()],
        ]
        text = "\n".join([self._t.t("servers.iso_title"), "", self._t.t("servers.iso_text")])
        return BotScreen(text, InlineKeyboardMarkup(inline_keyboard=rows))

    async def _iso_attach(self, user: User, ref: str, index: str) -> BotScreen:
        """Attach a provider-catalogue ISO (index-resolved, then confirmed)."""
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        selection = await self._selection(user, ref, SEL_ISOS, index)
        if selection is None:
            isos = await self._mgmt.list_isos(user.id, server_id)  # type: ignore[arg-type]
            await self._sessions.remember(user.id, ref, SEL_ISOS, tuple(isos))  # type: ignore[arg-type]
            rows: list[list[InlineKeyboardButton]] = []
            for idx, (_iso_ref, name) in enumerate(isos):
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=name,
                            callback_data=self._callback("servers", "isoat", ref, str(idx)),
                        )
                    ]
                )
            rows.append([self._back_button("servers", "iso", ref), self._menu_button()])
            return BotScreen(
                self._t.t("servers.iso_attach_button"),
                InlineKeyboardMarkup(inline_keyboard=rows),
            )
        iso_ref, name = selection
        return await self._stage_confirmation(
            user,
            ref,
            ServerOperation.ISO_ATTACH,
            arguments={"iso": str(iso_ref)},
            target=str(name),
        )

    async def _iso_detach(self, user: User, ref: str) -> BotScreen:
        return await self._stage_confirmation(user, ref, ServerOperation.ISO_DETACH, arguments={})

    # -- monitoring -------------------------------------------------------

    async def _monitoring_screen(self, user: User, ref: str) -> BotScreen:
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        view: MonitoringView = await self._mgmt.monitoring(user.id, server_id)  # type: ignore[arg-type]
        status = self._t.t("servers.monitoring_on" if view.enabled else "servers.monitoring_off")
        lines = [self._t.t("servers.monitoring_title"), "", status]
        rows: list[list[InlineKeyboardButton]] = []
        if view.can_enable:
            rows.append([self._action_button("monon", "servers.monitoring_enable_button", ref)])
        rows.append([self._back_button("servers", "manage", ref), self._menu_button()])
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def _monitoring_enable(self, user: User, ref: str) -> BotScreen:
        server_id = await self._sessions.server_id(user.id, ref)  # type: ignore[arg-type]
        if server_id is None:
            return self._expired_screen()
        outcome = await self._mgmt.enable_monitoring(user.id, server_id)  # type: ignore[arg-type]
        return self._outcome_screen(ref, outcome)

    # -- shared rendering -------------------------------------------------

    def _outcome_screen(self, ref: str, outcome: ServerActionOutcome) -> BotScreen:
        """Honest wording: queued, replay, or unprovable — never "done for sure"."""
        if outcome.outcome_unknown:
            text = self._t.t("servers.outcome_unknown")
        elif outcome.replayed:
            text = self._t.t("servers.action_in_progress")
        else:
            text = self._t.t(
                "servers.action_done",
                operation=self._t.t(_OPERATION_LABELS.get(outcome.operation, "servers.op.unknown")),
            )
        rows = [
            [
                self._back_button("servers", "view", ref),
                self._menu_button(),
            ]
        ]
        return BotScreen(text, InlineKeyboardMarkup(inline_keyboard=rows))

    def _error_screen(self, exc: ServerManagementError) -> BotScreen:
        """Map a failure to safe Persian wording (never a raw exception, §26)."""
        key = "servers.err_generic"
        if isinstance(exc, ServerNotFoundError):
            key = "servers.not_found"
        elif isinstance(exc, ServerConfirmationError):
            key = (
                "servers.confirm_replayed"
                if exc.status is ConfirmationStatus.REPLAYED
                else "servers.confirm_expired"
            )
        elif isinstance(exc, ServerAmbiguousOutcomeError):
            key = "servers.outcome_unknown"
        elif isinstance(exc, ServerOperationNotAllowedError):
            if exc.reason == "feature_disabled":
                key = "servers.disabled"
            elif exc.reason == "ip_not_owned":
                key = "servers.ip_not_owned"
            elif exc.reason == "provider_unsupported":
                key = "servers.err_forbidden"
            else:
                key = "servers.err_forbidden"
        elif isinstance(exc, ServerUnavailableError):
            key = "servers.err_retry"
        elif isinstance(exc, ServerProviderError):
            key = "servers.err_unavailable"
        logger.info("server management rejected: %s (%s)", type(exc).__name__, key)
        return BotScreen(self._t.t(key), self._menu_only())

    def _unavailable_screen(self) -> BotScreen:
        """The shared session/confirmation store is unreachable (fail closed).

        The customer sees an honest "try again shortly" and NO provider
        mutation is attempted; the operation is not queued for later either.
        """
        return BotScreen(self._t.t("servers.err_retry"), self._menu_only())

    def _expired_screen(self) -> BotScreen:
        """A reference that expired (restart/timeout) is not an error, just stale."""
        return BotScreen(self._t.t("nav.expired"), self._menu_only())

    def _action_button(
        self, screen: str, label_key: str, ref: str, *extra: str
    ) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=self._t.t(label_key),
            callback_data=self._callback("servers", screen, ref, *extra),
        )

    async def _selection(self, user: User, ref: str, key: str, index: str) -> Any | None:
        """The item the customer actually saw at ``index`` (None when stale)."""
        if user.id is None:
            return None
        items = await self._sessions.selection(user.id, ref, key)
        if not index.isdigit():
            return None
        position = int(index)
        if position < 0 or position >= len(items):
            return None
        return items[position]

    def _state(self, state: CustomerServerState) -> str:
        """Customer wording for a state (unknown values degrade safely)."""
        try:
            return self._t.t(f"servers.state.{state.value}")
        except Exception:  # pragma: no cover - catalogue is complete
            return self._t.t("servers.state.unknown")

    def _target(self, view: CustomerServerView) -> str:
        """A short, non-identifying label for a server (IP, else location)."""
        return view.ip or view.location_label or view.display_name or "—"

    def _callback(self, flow: str, screen: str, *args: str) -> str:
        from cloud_platform.modules.navigation.domain import Callback, encode_callback

        return encode_callback(Callback(flow=flow, screen=screen, args=args), self._key)

    def _back_button(self, flow: str, screen: str, *args: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=self._t.t("nav.back"),
            callback_data=self._callback(flow, screen, *args),
        )

    def _menu_button(self) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=self._t.t("nav.menu"),
            callback_data=self._callback("main", "menu"),
        )

    def _menu_only(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[self._menu_button()]])

    def _menu_screen(self) -> BotScreen:
        return BotScreen(self._t.t("menu.title"), self._menu_only())


#: The message key for each manual-settlement reason (never a provider string).
_RENEWAL_RESULT_KEYS: dict[str, str] = {
    "charged": "servers.renew_done",
    "already_charged": "servers.renew_already",
    "insufficient_funds": "servers.renew_insufficient",
    "not_payable": "servers.renew_not_due",
    "manual_review_required": "servers.renew_manual",
    "no_renewal_record": "servers.renew_unavailable",
    "no_due_date": "servers.renew_unavailable",
}


def _format_minor(minor: int, currency: str) -> str:
    """Integer-formatted money (never a float).

    Delegates to the single platform formatter (per-currency exponents);
    kept as a thin wrapper so the management UI needs no FX import at
    module load and no circular import with the storefront renderer.
    """
    from cloud_platform.modules.fx.formatting import format_minor as _fx_format

    return _fx_format(minor, currency)


def _format_day(moment: datetime) -> str:
    """A date-only rendering of a durable timestamp (UTC, no locale guessing)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d")


def _customer_id(user: User) -> UUID:
    """The local customer id of a resolved Telegram user.

    ``handle``/``list_screen`` already reject an unresolved identity, so this
    only re-states that invariant in a form the type checker can follow (mypy
    does not keep attribute narrowing across the awaits in these methods).
    """
    if user.id is None:
        raise ServerNotFoundError("no customer identity")
    return user.id


def _nonce(operation: ServerOperation, arguments: dict[str, str]) -> str:
    """The deterministic single-use key of a confirmed action.

    Derived from the operation and its arguments, so confirming the same thing
    twice (or a double-tapped "confirm" button) occupies ONE slot and can only
    ever reach the provider once.
    """
    return arguments_digest(operation, arguments)[:10]


def _snapshot_name() -> str:
    """A safe, provider-acceptable snapshot name (generated, never typed)."""
    from datetime import UTC, datetime

    return f"snapshot-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"


def _int(value: str, default: int) -> int:
    """Parse a callback integer defensively (a tampered value cannot crash)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default
