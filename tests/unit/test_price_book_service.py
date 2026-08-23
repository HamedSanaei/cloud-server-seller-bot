"""Tests for PriceBookService: audited publish + versioned sell_price (M06-001)."""

from __future__ import annotations

import dataclasses
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.pricing.domain import (
    MarginRule,
    NoActiveVersionError,
    OfferCost,
    PriceBookVersion,
)
from cloud_platform.modules.pricing.service import PriceBookService
from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    Role,
    User,
)

ADMIN_ID = uuid4()
T0 = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def _user(role: Role = Role.ADMIN) -> User:
    return User(
        id=ADMIN_ID if role is Role.ADMIN else uuid4(),
        username="ops",
        email="ops@example.com",
        role=role,
    )


def _rule(factor: str = "1.1") -> MarginRule:
    return MarginRule("*", "*", "*", Decimal(factor))


def _version(version: int, factor: str, effective_at: datetime) -> PriceBookVersion:
    return PriceBookVersion(
        book_name="retail-eur",
        version=version,
        effective_at=effective_at,
        rules=(_rule(factor),),
        id=uuid4(),
    )


def _offer() -> OfferCost:
    return OfferCost("hetzner", "cx22", "fsn1", 100, "EUR")


def _service(books: AsyncMock, audit: AsyncMock) -> PriceBookService:
    return PriceBookService(books, audit)  # type: ignore[arg-type]


def _audit_events(audit: AsyncMock) -> list:
    return [c.args[0] for c in audit.append.call_args_list]


class TestPublishVersion:
    async def test_publish_first_version_audited(self) -> None:
        books = AsyncMock()
        books.list_versions = AsyncMock(return_value=[])
        books.create_version = AsyncMock(side_effect=lambda v: dataclasses.replace(v, id=uuid4()))
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        created = await _service(books, audit).publish_version(
            book_name="retail-eur",
            rules=(_rule("1.15"),),
            effective_at=T0,
            actor=_user(),
            reason="launch pricing",
        )

        assert created.version == 1
        assert created.id is not None
        passed = books.create_version.call_args[0][0]
        assert passed.book_name == "retail-eur"
        assert passed.version == 1
        assert passed.effective_at == T0
        assert passed.rules == (_rule("1.15"),)
        event = _audit_events(audit)[0]
        assert event.action == "pricing.publish_book_version"
        assert event.resource_type == "price_book"
        assert event.resource_id == "retail-eur"
        assert event.actor_type.value == "admin"
        assert event.actor_id == ADMIN_ID
        assert event.reason == "launch pricing"
        assert event.metadata["version"] == 1
        assert event.metadata["rule_count"] == 1

    async def test_publish_increments_version(self) -> None:
        books = AsyncMock()
        books.list_versions = AsyncMock(return_value=[_version(1, "1.1", T0)])
        books.create_version = AsyncMock(side_effect=lambda v: dataclasses.replace(v, id=uuid4()))
        audit = AsyncMock()

        created = await _service(books, audit).publish_version(
            book_name="retail-eur",
            rules=(_rule("1.2"),),
            effective_at=T1,
            actor=_user(),
            reason="margin increase",
        )
        assert created.version == 2
        passed = books.create_version.call_args[0][0]
        assert passed.version == 2
        assert passed.rules == (_rule("1.2"),)

    async def test_non_admin_denied(self) -> None:
        books = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(PermissionDeniedError):
            await _service(books, audit).publish_version(
                book_name="b",
                rules=(_rule(),),
                effective_at=T0,
                actor=_user(Role.USER),
                reason="r",
            )
        books.list_versions.assert_not_awaited()
        audit.append.assert_not_awaited()

    async def test_empty_reason_rejected(self) -> None:
        books = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(ValueError, match="reason"):
            await _service(books, audit).publish_version(
                book_name="b",
                rules=(_rule(),),
                effective_at=T0,
                actor=_user(),
                reason="  ",
            )
        books.list_versions.assert_not_awaited()

    async def test_invalid_rules_rejected_by_domain(self) -> None:
        books = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(ValueError, match="duplicate rule pattern"):
            await _service(books, audit).publish_version(
                book_name="b",
                rules=(_rule(), _rule()),  # two identical wildcards
                effective_at=T0,
                actor=_user(),
                reason="r",
            )
        books.create_version.assert_not_awaited()
        audit.append.assert_not_awaited()

    async def test_naive_effective_at_rejected(self) -> None:
        books = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(ValueError, match="timezone-aware"):
            await _service(books, audit).publish_version(
                book_name="b",
                rules=(_rule(),),
                effective_at=datetime(2026, 8, 23),
                actor=_user(),
                reason="r",
            )


class TestSellPrice:
    async def test_prices_with_version_active_at_instant(self) -> None:
        v1 = _version(1, "1.1", T0)
        v2 = _version(2, "1.2", T1)
        books = AsyncMock()
        books.list_versions = AsyncMock(return_value=[v2, v1])
        audit = AsyncMock()

        # Before T1 only v1 is effective: 100 * 1.1 = 110
        before = await _service(books, audit).sell_price(
            book_name="retail-eur", offer=_offer(), at=T0
        )
        assert before.selling_minor == 110
        assert before.version == 1

        # After T1 the newer version applies: 100 * 1.2 = 120
        after = await _service(books, audit).sell_price(
            book_name="retail-eur", offer=_offer(), at=T1
        )
        assert after.selling_minor == 120
        assert after.version == 2
        audit.append.assert_not_awaited()  # read-only, no audit

    async def test_no_active_version_raises(self) -> None:
        books = AsyncMock()
        books.list_versions = AsyncMock(return_value=[_version(1, "1.1", T1)])
        audit = AsyncMock()

        with pytest.raises(NoActiveVersionError, match=r"no 'retail-eur' version"):
            await _service(books, audit).sell_price(book_name="retail-eur", offer=_offer(), at=T0)

    async def test_unknown_book_raises(self) -> None:
        books = AsyncMock()
        books.list_versions = AsyncMock(return_value=[])
        audit = AsyncMock()

        with pytest.raises(NoActiveVersionError):
            await _service(books, audit).sell_price(book_name="nope", offer=_offer(), at=T0)

    async def test_naive_instant_rejected(self) -> None:
        books = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(ValueError, match="timezone-aware"):
            await _service(books, audit).sell_price(
                book_name="b", offer=_offer(), at=datetime(2026, 8, 23)
            )
        books.list_versions.assert_not_awaited()

    async def test_result_carries_version_and_rule(self) -> None:
        v1 = _version(1, "1.07", T0)
        books = AsyncMock()
        books.list_versions = AsyncMock(return_value=[v1])
        audit = AsyncMock()

        result = await _service(books, audit).sell_price(
            book_name="retail-eur", offer=_offer(), at=T0
        )
        assert result.version == 1
        assert result.rule.margin_factor == Decimal("1.07")
        # 100 * 1.07 = 107
        assert result.selling_minor == 107
        with pytest.raises(FrozenInstanceError):
            result.selling_minor = 1  # type: ignore[misc]
