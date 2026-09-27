"""Owner-only Telegram wallet credit flow; financial mutation belongs to the service."""

from __future__ import annotations

import logging
import secrets
from typing import Protocol

from aiogram.enums import ChatType
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import NS_ACTION, NS_PROMPT, BotSessionStore
from cloud_platform.modules.navigation.domain import Callback, encode_callback
from cloud_platform.modules.users.domain import PermissionDeniedError

logger = logging.getLogger(__name__)
SUPER_ADMIN_TELEGRAM_ID = 85758085
_PROMPT_TTL_SECONDS = 900
_CONFIRM_TTL_SECONDS = 900
_MAX_BIGINT = 9_223_372_036_854_775_807


class AdminWalletCredit(Protocol):
    async def credit_admin_toman(
        self,
        actor_telegram_id: int,
        target_telegram_id: int,
        amount_toman: int,
        idempotency_key: str,
    ) -> object: ...


class AdminUserLookup(Protocol):
    async def get_by_telegram_user_id(self, telegram_user_id: int) -> object | None: ...


class AdminUi:
    """Store prompts and confirmations in shared, TTL-bounded presentation state."""

    def __init__(
        self,
        signing_key: str,
        *,
        sessions: BotSessionStore,
        users: AdminUserLookup,
        credit: AdminWalletCredit,
        translator: Translator | None = None,
    ) -> None:
        self._signing_key = signing_key
        self._sessions = sessions
        self._users = users
        self._credit = credit
        self._t = translator or Translator()

    @staticmethod
    def allowed(telegram_user_id: int | None, chat_id: int | None, chat_type: str | None) -> bool:
        return (
            telegram_user_id == SUPER_ADMIN_TELEGRAM_ID
            and chat_id == telegram_user_id
            and chat_type == ChatType.PRIVATE
        )

    def _button(self, key: str, screen: str, *args: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=self._t.t(key),
            callback_data=encode_callback(Callback("admin", screen, args), self._signing_key),
        )

    def with_menu_button(self, screen: BotScreen) -> BotScreen:
        """Append an owner option without removing any customer menu action."""
        rows = [list(row) for row in screen.keyboard.inline_keyboard]
        rows.append([self._button("admin.menu", "menu")])
        return BotScreen(screen.text, InlineKeyboardMarkup(inline_keyboard=rows), screen.entities)

    def with_reply_button(self, keyboard: ReplyKeyboardMarkup) -> ReplyKeyboardMarkup:
        rows = [list(row) for row in keyboard.keyboard]
        rows.append([KeyboardButton(text=self._t.t("admin.menu"))])
        return keyboard.model_copy(update={"keyboard": rows})

    def menu_screen(self) -> BotScreen:
        return BotScreen(
            self._t.t("admin.title"),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [self._button("admin.credit", "credit")],
                    [self._button("nav.menu", "cancel")],
                ]
            ),
        )

    def _prompt_screen(self, key: str) -> BotScreen:
        return BotScreen(
            self._t.t(key),
            InlineKeyboardMarkup(inline_keyboard=[[self._button("nav.menu", "cancel")]]),
        )

    def _result_screen(self, key: str, **params: object) -> BotScreen:
        return BotScreen(
            self._t.t(key, **params),
            InlineKeyboardMarkup(inline_keyboard=[[self._button("admin.menu", "menu")]]),
        )

    async def clear_prompt(self, actor_id: int) -> None:
        await self._sessions.delete(NS_PROMPT, f"admin:{actor_id}")

    async def cancel_action(self, actor_id: int, nonce: str) -> None:
        await self._sessions.delete(NS_ACTION, f"admin:{actor_id}:{nonce}")

    async def handle_text(self, actor_id: int, text: str) -> BotScreen | None:
        """Handle an active owner prompt only; never interpret another user's text."""
        if actor_id != SUPER_ADMIN_TELEGRAM_ID:
            return None
        key = f"admin:{actor_id}"
        prompt = await self._sessions.get(NS_PROMPT, key)
        if prompt is None:
            return None
        step = prompt.get("step")
        value = text.strip()
        if not value.isascii() or not value.isdecimal() or len(value) > 19:
            return self._prompt_screen("admin.invalid_number")
        number = int(value)
        if not 0 < number <= _MAX_BIGINT:
            return self._prompt_screen("admin.invalid_number")
        if step == "target":
            if number == actor_id or await self._users.get_by_telegram_user_id(number) is None:
                return self._prompt_screen("admin.target_missing")
            await self._sessions.put(
                NS_PROMPT,
                key,
                {"step": "amount", "target": number},
                ttl_seconds=_PROMPT_TTL_SECONDS,
            )
            return self._prompt_screen("admin.enter_amount")
        if step != "amount" or type(prompt.get("target")) is not int:
            await self.clear_prompt(actor_id)
            return self._result_screen("admin.expired")
        target = prompt["target"]
        nonce = secrets.token_urlsafe(12)
        await self._sessions.put(
            NS_ACTION,
            f"admin:{actor_id}:{nonce}",
            {"target": target, "amount": number},
            ttl_seconds=_CONFIRM_TTL_SECONDS,
        )
        await self.clear_prompt(actor_id)
        return BotScreen(
            self._t.t("admin.confirm", target=target, amount=f"{number:,}"),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [self._button("admin.confirm_button", "confirm", nonce)],
                    [self._button("nav.menu", "cancel", nonce)],
                ]
            ),
        )

    async def handle_callback(self, actor_id: int, screen: str, args: tuple[str, ...]) -> BotScreen:
        """Called only after signature and private-chat checks in the handler."""
        if actor_id != SUPER_ADMIN_TELEGRAM_ID:
            return self._result_screen("admin.denied")
        if screen == "menu" and not args:
            await self.clear_prompt(actor_id)
            return self.menu_screen()
        if screen == "credit" and not args:
            await self._sessions.put(
                NS_PROMPT,
                f"admin:{actor_id}",
                {"step": "target"},
                ttl_seconds=_PROMPT_TTL_SECONDS,
            )
            return self._prompt_screen("admin.enter_target")
        if screen == "confirm" and len(args) == 1:
            nonce = args[0]
            action_key = f"admin:{actor_id}:{nonce}"
            action = await self._sessions.get(NS_ACTION, action_key)
            if action is None:
                return self._result_screen("admin.expired")
            target, amount = action.get("target"), action.get("amount")
            if (
                type(target) is not int
                or type(amount) is not int
                or target <= 0
                or target == actor_id
                or amount <= 0
            ):
                return self._result_screen("admin.expired")
            try:
                await self._credit.credit_admin_toman(
                    actor_id, target, amount, f"telegram-admin-{actor_id}-{nonce}"
                )
            except (PermissionDeniedError, ValueError):
                return self._result_screen("admin.failed")
            except Exception:
                logger.exception("admin wallet credit failed")
                return self._result_screen("admin.failed")
            await self._sessions.delete(NS_ACTION, action_key)
            return self._result_screen("admin.success", target=target, amount=f"{amount:,}")
        return self._result_screen("admin.expired")
