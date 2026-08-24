"""Tests for the User domain aggregate."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from cloud_platform.modules.users.domain import (
    InvalidUserTransition,
    User,
    UserStatus,
)


def _make_user(status: UserStatus = UserStatus.ACTIVE) -> User:
    return User(id=uuid.uuid4(), username="alice", email="alice@example.com", status=status)


class TestUserConstruction:
    def test_defaults_to_active(self) -> None:
        user = _make_user()
        assert user.status is UserStatus.ACTIVE
        assert user.is_active is True

    def test_empty_username_rejected(self) -> None:
        with pytest.raises(ValueError, match="username"):
            User(username="  ", email="a@example.com")

    def test_invalid_email_rejected(self) -> None:
        with pytest.raises(ValueError, match="email"):
            User(username="alice", email="not-an-email")


class TestTransitions:
    def test_freeze_from_active(self) -> None:
        user = _make_user()
        user.freeze()
        assert user.status is UserStatus.FROZEN

    def test_freeze_from_frozen_or_banned_rejected(self) -> None:
        frozen = _make_user(UserStatus.FROZEN)
        banned = _make_user(UserStatus.BANNED)
        with pytest.raises(InvalidUserTransition):
            frozen.freeze()
        with pytest.raises(InvalidUserTransition):
            banned.freeze()

    def test_unfreeze_from_frozen(self) -> None:
        user = _make_user(UserStatus.FROZEN)
        user.unfreeze()
        assert user.is_active

    def test_unfreeze_from_active_rejected(self) -> None:
        user = _make_user()
        with pytest.raises(InvalidUserTransition):
            user.unfreeze()

    def test_ban_from_active_and_frozen(self) -> None:
        active = _make_user()
        active.ban()
        assert active.status is UserStatus.BANNED
        frozen = _make_user(UserStatus.FROZEN)
        frozen.ban()
        assert frozen.status is UserStatus.BANNED

    def test_double_ban_rejected(self) -> None:
        banned = _make_user(UserStatus.BANNED)
        with pytest.raises(InvalidUserTransition):
            banned.ban()


class TestTermsAcceptance:
    def test_accept_terms_records_version_and_timestamp(self) -> None:
        user = _make_user()
        at = datetime(2026, 8, 22, tzinfo=UTC)
        user.accept_terms(3, at)
        assert user.terms_version == 3
        assert user.terms_accepted_at == at

    def test_accept_terms_defaults_to_now(self) -> None:
        user = _make_user()
        before = datetime.now(UTC)
        user.accept_terms(1)
        after = datetime.now(UTC)
        assert user.terms_version == 1
        assert user.terms_accepted_at is not None
        assert before <= user.terms_accepted_at <= after


class TestStatusGuards:
    """Frozen/banned users cannot spend or provision."""

    def test_active_user_can_spend(self) -> None:
        user = _make_user(UserStatus.ACTIVE)
        assert user.can_spend is True

    def test_frozen_user_cannot_spend(self) -> None:
        user = _make_user(UserStatus.FROZEN)
        assert user.can_spend is False

    def test_banned_user_cannot_spend(self) -> None:
        user = _make_user(UserStatus.BANNED)
        assert user.can_spend is False

    def test_active_user_can_provision(self) -> None:
        user = _make_user(UserStatus.ACTIVE)
        assert user.can_provision is True

    def test_frozen_user_cannot_provision(self) -> None:
        user = _make_user(UserStatus.FROZEN)
        assert user.can_provision is False

    def test_banned_user_cannot_provision(self) -> None:
        user = _make_user(UserStatus.BANNED)
        assert user.can_provision is False

    def test_can_spend_and_is_active_are_always_equal(self) -> None:
        for status in UserStatus:
            user = _make_user(status)
            assert user.can_spend is user.is_active


class TestEnumValues:
    def test_status_values_match_schema(self) -> None:
        assert UserStatus.ACTIVE.value == "active"
        assert UserStatus.FROZEN.value == "frozen"
        assert UserStatus.BANNED.value == "banned"
