from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.users.domain import Role, User
from cloud_platform.modules.users.identity import (
    IdentityRequiredError,
    IdentityService,
    IdentityValidationError,
    normalize_iranian_phone,
    normalize_national_id,
    require_verified_identity,
)

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def user() -> User:
    return User(id=uuid4(), username="customer", email="customer@example.com", telegram_user_id=123)


@pytest.mark.parametrize(
    "raw",
    [
        "09123456789",
        "989123456789",
        "+989123456789",
        "0098 912 345 6789",
        "۰۹۱۲۳۴۵۶۷۸۹",
        "+٩٨(٩١٢)٣٤٥-٦٧٨٩",
    ],
)
def test_phone_normalization(raw: str) -> None:
    assert normalize_iranian_phone(raw) == "+989123456789"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "+491234567890",
        "9123456789",
        "009809123456789",
        "02123456789",
        "+98912345678",
        "+9891234567890",
        "09x34567890",
    ],
)
def test_phone_rejection(raw: str) -> None:
    with pytest.raises(IdentityValidationError):
        normalize_iranian_phone(raw)


@pytest.mark.parametrize(
    "raw", ["1000000060", "1000000011", "1000000001", "1234567891", "۱۲۳۴۵۶۷۸۹۱", "١٢٣٤٥٦٧٨٩١"]
)
def test_national_id_checksum_branches(raw: str) -> None:
    assert len(normalize_national_id(raw)) == 10


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "123456789",
        "12345678901",
        "1234567890",
        "0000000000",
        "1111111111",
        "9999999999",
        "12345678x1",
    ],
)
def test_national_id_rejection(raw: str) -> None:
    with pytest.raises(IdentityValidationError):
        normalize_national_id(raw)


@pytest.mark.parametrize(
    "contact_id,actor,private,forwarded,phone",
    [
        (None, 123, True, False, "09123456789"),
        (456, 123, True, False, "09123456789"),
        (456, 456, True, False, "09123456789"),
        (123, 123, False, False, "09123456789"),
        (123, 123, True, True, "09123456789"),
        (123, 123, True, False, "+491234567890"),
    ],
)
async def test_invalid_contact_never_persists(contact_id, actor, private, forwarded, phone) -> None:
    repo = AsyncMock()
    with pytest.raises(IdentityValidationError):
        await IdentityService(repo).verify_contact(
            user(),
            actor_telegram_user_id=actor,
            contact_user_id=contact_id,
            phone_number=phone,
            is_private_chat=private,
            is_forwarded=forwarded,
        )
    repo.update_verified_phone.assert_not_awaited()


async def test_national_id_is_owned_actor_only() -> None:
    repo = AsyncMock()
    service = IdentityService(repo)
    customer = user()
    with pytest.raises(IdentityValidationError):
        await service.collect_national_id(
            customer, actor_telegram_user_id=456, national_id="1234567891"
        )
    repo.update_national_id.assert_not_awaited()


@pytest.mark.parametrize("role", [Role.USER, Role.ADMIN])
def test_purchase_guard_requires_both_facts_for_all_roles(role: Role) -> None:
    customer = user()
    customer.role = role
    with pytest.raises(IdentityRequiredError):
        require_verified_identity(customer)
    customer.phone_number = "+989123456789"
    customer.phone_verified_at = NOW
    with pytest.raises(IdentityRequiredError):
        require_verified_identity(customer)
    customer.national_id = "1234567891"
    require_verified_identity(customer)
    customer.phone_verified_at = None
    with pytest.raises(IdentityRequiredError):
        require_verified_identity(customer)


async def test_naive_verification_time_cannot_persist_an_owned_contact() -> None:
    repo = AsyncMock()
    customer = user()
    with pytest.raises(IdentityValidationError):
        await IdentityService(repo).verify_contact(
            customer,
            actor_telegram_user_id=123,
            contact_user_id=123,
            phone_number="09123456789",
            at=datetime(2026, 10, 4),
        )
    repo.update_verified_phone.assert_not_awaited()
