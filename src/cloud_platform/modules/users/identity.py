"""Sender-owned Iranian contact verification and national-ID format collection.

A checksum-valid national ID is not evidence that the phone's owner owns that ID.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cloud_platform.modules.users.domain import User, UserRepository

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_PHONE_SEPARATORS = frozenset("-\u2010\u2011\u2013\u2014()./")


class IdentityValidationError(ValueError):
    """Contact ownership, Iranian mobile format, or national-ID validation failed."""


class IdentityRequiredError(RuntimeError):
    """Server purchase requires a verified Iranian contact and valid national ID."""


def normalize_iranian_phone(phone_number: str) -> str:
    """Accept Adminbot's Iranian mobile forms; return canonical +989xxxxxxxxx."""
    compact: list[str] = []
    for character in phone_number.strip().translate(_DIGITS):
        if character == "+" and not compact:
            compact.append(character)
        elif "0" <= character <= "9":
            compact.append(character)
        elif character.isspace() or character in _PHONE_SEPARATORS:
            continue
        else:
            raise IdentityValidationError("Only an Iranian mobile contact can be verified")
    value = "".join(compact)
    if value.startswith("+98"):
        national = value[3:]
    elif value.startswith("0098"):
        national = value[4:]
    elif value.startswith("98"):
        national = value[2:]
    elif value.startswith("0"):
        national = value[1:]
    else:
        raise IdentityValidationError("Only an Iranian mobile contact can be verified")
    if (
        len(national) != 10
        or not national.startswith("9")
        or not national.isascii()
        or not national.isdigit()
    ):
        raise IdentityValidationError("Only an Iranian mobile contact can be verified")
    return "+98" + national


def normalize_national_id(national_id: str) -> str:
    """Normalize Persian/Arabic digits and validate Iran's ten-digit checksum."""
    value = national_id.strip().translate(_DIGITS)
    if len(value) != 10 or not value.isascii() or not value.isdigit() or len(set(value)) == 1:
        raise IdentityValidationError("National ID must be a valid ten-digit Iranian code")
    remainder = (
        sum(int(digit) * weight for digit, weight in zip(value[:9], range(10, 1, -1), strict=True))
        % 11
    )
    expected = remainder if remainder < 2 else 11 - remainder
    if int(value[-1]) != expected:
        raise IdentityValidationError("National ID checksum is invalid")
    return value


def require_verified_identity(user: User) -> None:
    """Guard purchases for every role, before wallet/provider effects."""
    try:
        phone = normalize_iranian_phone(user.phone_number or "")
        national_id = normalize_national_id(user.national_id or "")
    except IdentityValidationError:
        raise IdentityRequiredError(
            "Verify your own Iranian Telegram contact and provide a valid national ID "
            "before purchasing a server"
        ) from None
    if (
        phone != user.phone_number
        or national_id != user.national_id
        or user.phone_verified_at is None
        or user.phone_verified_at.tzinfo is None
        or user.phone_verified_at.utcoffset() is None
        or user.telegram_user_id is None
    ):
        raise IdentityRequiredError(
            "Verify your own Iranian Telegram contact and provide a valid national ID "
            "before purchasing a server"
        )


class IdentityService:
    """Persist identity facts only for the authenticated platform user's actor."""

    def __init__(self, user_repo: UserRepository) -> None:
        self._users = user_repo

    @staticmethod
    def _require_actor(user: User, actor_telegram_user_id: int) -> None:
        if (
            user.id is None
            or user.telegram_user_id is None
            or user.telegram_user_id != actor_telegram_user_id
        ):
            raise IdentityValidationError(
                "Identity must be submitted by this user's Telegram account"
            )

    async def verify_contact(
        self,
        user: User,
        *,
        actor_telegram_user_id: int,
        contact_user_id: int | None,
        phone_number: str,
        at: datetime | None = None,
        is_private_chat: bool = True,
        is_forwarded: bool = False,
    ) -> User:
        self._require_actor(user, actor_telegram_user_id)
        if (
            not is_private_chat
            or is_forwarded
            or contact_user_id is None
            or contact_user_id != actor_telegram_user_id
        ):
            raise IdentityValidationError(
                "Use the contact button to share your own Telegram contact in a private chat"
            )
        phone = normalize_iranian_phone(phone_number)
        verified_at = at or datetime.now(UTC)
        if verified_at.tzinfo is None or verified_at.utcoffset() is None:
            raise IdentityValidationError("Verification timestamp must be timezone-aware")
        assert user.id is not None
        return await self._users.update_verified_phone(user.id, phone, verified_at.astimezone(UTC))

    async def collect_national_id(
        self,
        user: User,
        *,
        actor_telegram_user_id: int,
        national_id: str,
    ) -> User:
        self._require_actor(user, actor_telegram_user_id)
        value = normalize_national_id(national_id)
        assert user.id is not None
        return await self._users.update_national_id(user.id, value)
