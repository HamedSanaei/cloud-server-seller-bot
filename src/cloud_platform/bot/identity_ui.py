"""Identity prompts only; the caller owns active-flow persistence and routing."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from cloud_platform.core.i18n import Translator
from cloud_platform.modules.users.identity import IdentityValidationError


@dataclass(frozen=True, slots=True)
class IdentityScreen:
    text: str
    keyboard: ReplyKeyboardMarkup | ReplyKeyboardRemove


def phone_contact_screen(translator: Translator | None = None) -> IdentityScreen:
    translator = translator or Translator()
    return IdentityScreen(
        translator.t("identity.phone_prompt"),
        ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=translator.t("identity.share_contact"), request_contact=True)],
                [KeyboardButton(text=translator.t("identity.cancel"))],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


def national_id_screen(translator: Translator | None = None) -> IdentityScreen:
    translator = translator or Translator()
    return IdentityScreen(
        translator.t("identity.national_id_prompt"),
        ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=translator.t("identity.cancel"))]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


def identity_cancel_screen(translator: Translator | None = None) -> IdentityScreen:
    translator = translator or Translator()
    return IdentityScreen(
        translator.t("identity.cancelled"),
        ReplyKeyboardRemove(),
    )


def is_identity_cancel(text: str | None) -> bool:
    return (text or "").strip().casefold() in {"لغو", "cancel", "/cancel"}


def contact_from_message(message: Message) -> tuple[int, int, str]:
    """Reject text, forwarded/shared contacts, non-private chats and missing IDs."""
    sender = message.from_user
    contact = message.contact
    if (
        message.chat.type != "private"
        or message.forward_origin is not None
        or sender is None
        or contact is None
        or contact.user_id is None
        or contact.user_id != sender.id
    ):
        raise IdentityValidationError(
            "Use the contact button to share your own Telegram contact in a private chat"
        )
    return sender.id, contact.user_id, contact.phone_number
