"""Owner-bound identity prompts resumed through the shared bot session store."""

from __future__ import annotations

from aiogram.types import Message, ReplyKeyboardRemove
from sqlalchemy.exc import SQLAlchemyError

from cloud_platform.bot.identity_ui import (
    contact_from_message,
    identity_cancel_screen,
    is_identity_cancel,
    national_id_screen,
    phone_contact_screen,
)
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import BotSessionStore
from cloud_platform.modules.users.domain import User
from cloud_platform.modules.users.identity import (
    IdentityRequiredError,
    IdentityService,
    IdentityValidationError,
    require_verified_identity,
)


class IdentityFlow:
    """Stores only prompt stage and signed continuation, never personal data."""

    def __init__(
        self, service: IdentityService, store: BotSessionStore, *, ttl_seconds: int
    ) -> None:
        self._service = service
        self._store = store
        self._ttl = ttl_seconds
        self._t = Translator()

    async def begin(self, message: Message, user: User, resume: str) -> bool:
        if user.id is None:
            return False
        try:
            require_verified_identity(user)
        except IdentityRequiredError:
            if message.chat.type != "private" or message.chat.id != user.telegram_user_id:
                await message.answer(self._t.t("identity.private_only"))
                return True
            step = "national" if user.phone_number and user.phone_verified_at else "phone"
            await self._store.put(
                "identity", str(user.id), {"step": step, "resume": resume}, ttl_seconds=self._ttl
            )
            screen = (
                national_id_screen(self._t) if step == "national" else phone_contact_screen(self._t)
            )
            await message.answer(screen.text, reply_markup=screen.keyboard)
            return True
        return False

    async def handle(self, message: Message, user: User) -> tuple[bool, str | None]:
        if (
            user.id is None
            or message.chat.type != "private"
            or message.chat.id != user.telegram_user_id
        ):
            return False, None
        pending = await self._store.get("identity", str(user.id))
        if pending is None:
            return False, None
        if is_identity_cancel(message.text):
            await self._store.delete("identity", str(user.id))
            screen = identity_cancel_screen(self._t)
            await message.answer(screen.text, reply_markup=screen.keyboard)
            return True, None
        try:
            if pending.get("step") == "phone":
                actor, contact_id, phone = contact_from_message(message)
                await self._service.verify_contact(
                    user,
                    actor_telegram_user_id=actor,
                    contact_user_id=contact_id,
                    phone_number=phone,
                    is_private_chat=True,
                    is_forwarded=False,
                )
                await self._store.put(
                    "identity", str(user.id), {**pending, "step": "national"}, ttl_seconds=self._ttl
                )
                screen = national_id_screen(self._t)
                await message.answer(screen.text, reply_markup=screen.keyboard)
                return True, None
            if pending.get("step") == "national" and message.from_user and message.text:
                saved = await self._service.collect_national_id(
                    user, actor_telegram_user_id=message.from_user.id, national_id=message.text
                )
                require_verified_identity(saved)
                await self._store.delete("identity", str(user.id))
                await message.answer(
                    self._t.t("identity.complete"), reply_markup=ReplyKeyboardRemove()
                )
                resume = pending.get("resume")
                return True, resume if isinstance(resume, str) else None
        except (IdentityValidationError, IdentityRequiredError):
            key = (
                "identity.invalid_contact"
                if pending.get("step") == "phone"
                else "identity.invalid_national_id"
            )
            await message.answer(self._t.t(key))
            return True, None
        except SQLAlchemyError:
            # SQL parameters are hidden by the engine; do not hand a driver
            # exception (which can itself contain PII) to aiogram's logger.
            await message.answer(self._t.t("servers.err_retry"))
            return True, None
        await message.answer(self._t.t("identity.invalid_national_id"))
        return True, None
