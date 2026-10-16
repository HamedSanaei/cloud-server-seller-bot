"""Bounded user search, profiles, services and confirmed status changes."""

from __future__ import annotations

import secrets

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import BotSessionStore
from cloud_platform.modules.admin_console.users import AdminUserService
from cloud_platform.modules.fx.formatting import format_minor
from cloud_platform.modules.navigation.domain import Callback, encode_telegram_callback
from cloud_platform.modules.users.domain import User, UserStatus


class AdminUsersUi:
    def __init__(
        self,
        key: str,
        service: AdminUserService,
        store: BotSessionStore,
        translator: Translator,
        *,
        ttl_seconds: int = 900,
        wallet_enabled: bool = False,
    ):
        self._key, self._service, self._store, self._t = key, service, store, translator
        self._ttl, self._wallet_enabled = ttl_seconds, wallet_enabled

    def _button(self, key: str, screen: str, *args: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=self._t.t(key),
            callback_data=encode_telegram_callback(Callback("admin", screen, args), self._key),
        )

    def _screen(self, text: str, rows: list[list[InlineKeyboardButton]] | None = None) -> BotScreen:
        rows = rows or []
        rows.append([self._button("nav.back", "menu")])
        return BotScreen(text, InlineKeyboardMarkup(inline_keyboard=rows))

    async def clear_prompt(self, admin: User) -> None:
        active = await self._store.get("admin-user-active", str(admin.id))
        if active is not None:
            await self._store.delete("admin-user-confirm", f"{admin.id}:{active['nonce']}")
        await self._store.delete("admin-user-active", str(admin.id))
        await self._store.delete("admin-user-input", str(admin.id))

    async def _search(self, admin: User, query: str, offset: int = 0) -> BotScreen:
        page = await self._service.search(admin, query, offset=offset, limit=10)
        nonce = secrets.token_urlsafe(6)
        await self._store.put(
            "admin-user-search", f"{admin.id}:{nonce}", {"query": query}, ttl_seconds=self._ttl
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{user.telegram_user_id} · {user.username[:40]}",
                    callback_data=encode_telegram_callback(
                        Callback("admin", "u_profile", (str(user.telegram_user_id), "0")), self._key
                    ),
                )
            ]
            for user in page.items
            if user.telegram_user_id
        ]
        navigation = []
        if offset:
            navigation.append(self._button("nav.prev", "u_page", nonce, str(max(0, offset - 10))))
        if len(page.items) == 10:
            navigation.append(self._button("nav.next", "u_page", nonce, str(offset + 10)))
        if navigation:
            rows.append(navigation)
        rows.append([self._button("admin.users_search", "u_search")])
        return self._screen(self._t.t("admin.users_title", count=len(page.items)), rows)

    async def handle(self, cb: Callback, admin: User) -> BotScreen | None:
        if cb.screen == "users":
            return await self._search(admin, "")
        if cb.screen == "u_search":
            await self._service.search(admin, "", limit=1)
            await self._store.put(
                "admin-user-input", str(admin.id), {"step": "search"}, ttl_seconds=self._ttl
            )
            return self._screen(self._t.t("admin.users_search_prompt"))
        if cb.screen == "u_page" and len(cb.args) == 2:
            record = await self._store.get("admin-user-search", f"{admin.id}:{cb.args[0]}")
            return (
                await self._search(admin, record["query"], int(cb.args[1]))
                if record
                else self._screen(self._t.t("nav.expired"))
            )
        if cb.screen == "u_profile" and len(cb.args) == 2:
            tid, offset = map(int, cb.args)
            profile = await self._service.profile(admin, tid, server_offset=offset, server_limit=10)
            wallet = profile.wallet
            balance = format_minor(wallet.balance, wallet.currency) if wallet else "—"
            lines = [
                self._t.t(
                    "admin.user_profile",
                    tid=tid,
                    username=profile.user.username,
                    status=profile.user.status.value,
                    balance=balance,
                    count=profile.server_total,
                )
            ]
            lines.extend(
                f"{server.server_id} · {server.provider_key} · {server.state}"
                for server in profile.servers
            )
            rows = []
            if offset:
                rows.append(
                    [self._button("nav.prev", "u_profile", str(tid), str(max(0, offset - 10)))]
                )
            if offset + len(profile.servers) < profile.server_total:
                rows.append([self._button("nav.next", "u_profile", str(tid), str(offset + 10))])
            if self._wallet_enabled:
                rows.append([self._button("admin.wallet_adjust", "w_target", str(tid))])
            rows.append(
                [
                    self._button("admin.user_block", "u_status", str(tid), "banned"),
                    self._button("admin.user_unblock", "u_status", str(tid), "active"),
                ]
            )
            return self._screen("\n".join(lines), rows)
        if cb.screen == "u_status" and len(cb.args) == 2:
            await self._service.profile(admin, int(cb.args[0]))
            status = UserStatus(cb.args[1])
            await self._store.put(
                "admin-user-input",
                str(admin.id),
                {"step": "reason", "tid": int(cb.args[0]), "status": status.value},
                ttl_seconds=self._ttl,
            )
            return self._screen(self._t.t("admin.reason_prompt"))
        if cb.screen == "u_apply" and len(cb.args) == 1:
            active = await self._store.get("admin-user-active", str(admin.id))
            record = await self._store.get("admin-user-confirm", f"{admin.id}:{cb.args[0]}")
            if active is None or active.get("nonce") != cb.args[0] or record is None:
                return self._screen(self._t.t("nav.expired"))
            status = await self._service.set_status(
                admin,
                record["tid"],
                UserStatus(record["status"]),
                reason=record["reason"],
                idempotency_key=f"admin-user:{admin.id}:{cb.args[0]}",
            )
            return self._screen(self._t.t("admin.user_status_done", status=status.value))
        return None

    async def handle_text(self, text: str, admin: User) -> BotScreen | None:
        pending = await self._store.get("admin-user-input", str(admin.id))
        if pending is None:
            return None
        if pending["step"] == "search":
            if len(text.strip()) > 100:
                return self._screen(self._t.t("admin.users_search_prompt"))
            screen = await self._search(admin, text.strip())
            await self._store.delete("admin-user-input", str(admin.id))
            return screen
        reason = text.strip()
        if not 1 <= len(reason) <= 500:
            return self._screen(self._t.t("admin.reason_prompt"))
        # Application facade re-authorizes both at preview and at mutation.
        await self._service.profile(admin, pending["tid"])
        nonce = secrets.token_urlsafe(6)
        await self._store.put(
            "admin-user-confirm",
            f"{admin.id}:{nonce}",
            {**pending, "reason": reason},
            ttl_seconds=self._ttl,
        )
        await self._store.put(
            "admin-user-active", str(admin.id), {"nonce": nonce}, ttl_seconds=self._ttl
        )
        await self._store.delete("admin-user-input", str(admin.id))
        return self._screen(
            self._t.t(
                "admin.user_status_confirm",
                tid=pending["tid"],
                status=pending["status"],
                reason=reason,
            ),
            [[self._button("admin.confirm_action", "u_apply", nonce)]],
        )
