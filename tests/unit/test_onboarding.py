"""Tests for the /start onboarding flow (users.onboarding)."""

from __future__ import annotations

from uuid import uuid4

from cloud_platform.modules.users.domain import User
from cloud_platform.modules.users.onboarding import (
    _generate_email,
    handle_start,
)


class TestGenerateEmail:
    def test_deterministic_prefix_from_username(self) -> None:
        email = _generate_email("alice")
        assert email.startswith("alice_")
        assert email.endswith("@t.me")

    def test_unique_suffixes(self) -> None:
        emails = {_generate_email("bob") for _ in range(10)}
        assert len(emails) == 10


class TestHandleStartReturningUser:
    async def test_returns_existing_user(self) -> None:
        from unittest.mock import AsyncMock

        from cloud_platform.modules.users.domain import UserStatus

        existing = User(
            id=uuid4(),
            username="alice",
            email="alice@example.com",
            status=UserStatus.ACTIVE,
        )

        user_repo = AsyncMock()
        user_repo.get_by_telegram_user_id = AsyncMock(return_value=existing)

        result = await handle_start(
            user_repo,
            AsyncMock(),
            telegram_user_id=12345,
            username="alice",
        )

        assert result is existing
        user_repo.get_by_telegram_user_id.assert_awaited_once_with(12345)


class TestHandleStartNewUser:
    async def test_creates_user_and_wallet(
        self,
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        new_user = User(
            id=uuid4(),
            username="bob",
            email="bob@t.me",
        )

        user_repo = AsyncMock()
        user_repo.get_by_telegram_user_id = AsyncMock(return_value=None)
        user_repo.create = AsyncMock(return_value=new_user)

        wallet_repo = AsyncMock()
        wallet_repo.get_or_create = AsyncMock(return_value=MagicMock())

        result = await handle_start(
            user_repo,  # type: ignore[arg-type]
            wallet_repo,  # type: ignore[arg-type]
            telegram_user_id=99999,
            username="bob",
        )

        assert result is new_user
        user_repo.get_by_telegram_user_id.assert_awaited_once_with(99999)
        user_repo.create.assert_awaited_once()
        wallet_repo.get_or_create.assert_awaited_once()

    async def test_no_email_generates_from_username(
        self,
    ) -> None:
        from unittest.mock import AsyncMock

        new_user = User(
            id=uuid4(),
            username="charlie",
            email="charlie_abcdef@t.me",
        )

        user_repo = AsyncMock()
        user_repo.get_by_telegram_user_id = AsyncMock(return_value=None)
        user_repo.create = AsyncMock(return_value=new_user)
        wallet_repo = AsyncMock()

        result = await handle_start(
            user_repo,  # type: ignore[arg-type]
            wallet_repo,  # type: ignore[arg-type]
            telegram_user_id=111,
            username="charlie",
            email=None,
        )

        assert result is new_user
        assert result.email == "charlie_abcdef@t.me"
