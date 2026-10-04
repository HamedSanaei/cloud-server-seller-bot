from datetime import UTC, datetime

import pytest
from aiogram.types import Chat, Contact, Message, MessageOriginUser, User

from cloud_platform.bot.identity_ui import (
    contact_from_message,
    identity_cancel_screen,
    is_identity_cancel,
    national_id_screen,
    phone_contact_screen,
)
from cloud_platform.modules.users.identity import IdentityValidationError


def message(**overrides) -> Message:
    values = dict(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=123, type="private"),
        from_user=User(id=123, is_bot=False, first_name="Customer"),
        contact=Contact(phone_number="09123456789", first_name="Customer", user_id=123),
    )
    values.update(overrides)
    return Message(**values)


def test_contact_request_and_cancel_prompts() -> None:
    screen = phone_contact_screen()
    assert screen.keyboard.keyboard[0][0].request_contact is True
    assert is_identity_cancel(screen.keyboard.keyboard[1][0].text)
    assert is_identity_cancel(national_id_screen().keyboard.keyboard[0][0].text)
    assert identity_cancel_screen().keyboard.remove_keyboard is True


def test_own_contact_extraction() -> None:
    assert contact_from_message(message()) == (123, 123, "09123456789")


@pytest.mark.parametrize(
    "overrides",
    [
        {"contact": None, "text": "09123456789"},
        {"contact": Contact(phone_number="09123456789", first_name="Other")},
        {"contact": Contact(phone_number="09123456789", first_name="Other", user_id=456)},
        {"chat": Chat(id=-123, type="group")},
        {"from_user": None},
        {
            "forward_origin": MessageOriginUser(
                date=datetime.now(UTC),
                sender_user=User(id=123, is_bot=False, first_name="Customer"),
            )
        },
    ],
)
def test_copied_missing_foreign_forwarded_contact_rejected(overrides) -> None:
    with pytest.raises(IdentityValidationError):
        contact_from_message(message(**overrides))
