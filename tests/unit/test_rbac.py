"""Tests for RBAC primitives: Permission, Role, PermissionChecker."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cloud_platform.modules.users.domain import (
    PERMISSIONS,
    Permission,
    PermissionChecker,
    PermissionDeniedError,
    Role,
    User,
    UserStatus,
)


class TestPermissionEnum:
    def test_all_permissions_are_strings(self) -> None:
        for perm in Permission:
            assert isinstance(perm.value, str)
            assert ":" in perm.value

    def test_permission_values_are_unique(self) -> None:
        values = [p.value for p in Permission]
        assert len(set(values)) == len(values)


class TestRoleEnum:
    def test_role_user_has_basic_permissions(self) -> None:
        perms = PERMISSIONS[Role.USER]
        assert Permission.SERVER_CREATE in perms
        assert Permission.SERVER_LIST in perms
        assert Permission.BILLING_VIEW in perms
        assert Permission.BILLING_PAY in perms
        # ADMIN-only permissions not in USER
        assert Permission.ADMIN_MANAGE_USERS not in perms
        assert Permission.ADMIN_MANAGE_SETTINGS not in perms
        assert Permission.SERVER_DESTROY not in perms

    def test_role_admin_has_all_permissions(self) -> None:
        perms = PERMISSIONS[Role.ADMIN]
        for perm in Permission:
            assert perm in perms

    def test_role_user_cannot_manage_users(self) -> None:
        perms = PERMISSIONS[Role.USER]
        assert Permission.ADMIN_MANAGE_USERS not in perms

    def test_roles_are_strict_enums(self) -> None:
        assert Role.USER.value == "user"
        assert Role.ADMIN.value == "admin"

    def test_permission_sets_are_frozensets(self) -> None:
        for role, perms in PERMISSIONS.items():
            assert isinstance(perms, frozenset), f"Role.{role.name} permissions must be frozenset"


class TestUserHasPermission:
    def test_user_has_user_permissions(self) -> None:
        user = User(id=uuid4(), username="alice", email="alice@example.com", role=Role.USER)
        assert user.has_permission(Permission.SERVER_CREATE) is True
        assert user.has_permission(Permission.BILLING_VIEW) is True
        assert user.has_permission(Permission.ADMIN_MANAGE_USERS) is False

    def test_admin_has_all_permissions(self) -> None:
        user = User(id=uuid4(), username="admin", email="admin@example.com", role=Role.ADMIN)
        for perm in Permission:
            assert user.has_permission(perm) is True

    def test_default_role_is_user(self) -> None:
        user = User(id=uuid4(), username="alice", email="alice@example.com")
        assert user.role is Role.USER


class TestPermissionChecker:
    def test_check_returns_true_for_allowed_permission(self) -> None:
        user = User(id=uuid4(), username="alice", email="alice@example.com", role=Role.USER)
        checker = PermissionChecker(user)
        assert checker.check(Permission.SERVER_CREATE) is True

    def test_check_returns_false_for_denied_permission(self) -> None:
        user = User(id=uuid4(), username="alice", email="alice@example.com", role=Role.USER)
        checker = PermissionChecker(user)
        assert checker.check(Permission.ADMIN_MANAGE_USERS) is False

    def test_require_passes_for_allowed_permission(self) -> None:
        user = User(id=uuid4(), username="alice", email="alice@example.com", role=Role.USER)
        checker = PermissionChecker(user)
        # No exception raised
        checker.require(Permission.SERVER_CREATE)

    def test_require_raises_for_denied_permission(self) -> None:
        user = User(id=uuid4(), username="alice", email="alice@example.com", role=Role.USER)
        checker = PermissionChecker(user)
        with pytest.raises(PermissionDeniedError) as exc_info:
            checker.require(Permission.ADMIN_MANAGE_USERS)
        assert "admin:manage_users" in str(exc_info.value)

    def test_require_active_passes_for_active_user(self) -> None:
        user = User(
            id=uuid4(),
            username="alice",
            email="alice@example.com",
            status=UserStatus.ACTIVE,
        )
        checker = PermissionChecker(user)
        checker.require_active()  # no exception

    def test_require_active_raises_for_frozen_user(self) -> None:
        user = User(
            id=uuid4(),
            username="alice",
            email="alice@example.com",
            status=UserStatus.FROZEN,
        )
        checker = PermissionChecker(user)
        with pytest.raises(PermissionDeniedError):
            checker.require_active()

    def test_require_spend_passes_for_active(self) -> None:
        user = User(id=uuid4(), username="alice", email="alice@example.com")
        checker = PermissionChecker(user)
        checker.require_spend()  # no exception

    def test_require_spend_raises_for_banned(self) -> None:
        user = User(
            id=uuid4(),
            username="alice",
            email="alice@example.com",
            status=UserStatus.BANNED,
        )
        checker = PermissionChecker(user)
        with pytest.raises(PermissionDeniedError):
            checker.require_spend()

    def test_checker_exposes_user(self) -> None:
        user = User(id=uuid4(), username="alice", email="alice@example.com")
        checker = PermissionChecker(user)
        assert checker.user is user


class TestWalletAdjustPermission:
    def test_admin_has_wallet_adjust(self) -> None:
        assert Permission.WALLET_ADJUST in PERMISSIONS[Role.ADMIN]

    def test_regular_user_lacks_wallet_adjust(self) -> None:
        assert Permission.WALLET_ADJUST not in PERMISSIONS[Role.USER]

    def test_admin_user_passes_check(self) -> None:
        admin = User(id=uuid4(), username="boss", email="boss@example.com", role=Role.ADMIN)
        assert admin.has_permission(Permission.WALLET_ADJUST) is True

    def test_regular_user_fails_check(self) -> None:
        user = User(id=uuid4(), username="alice", email="alice@example.com", role=Role.USER)
        checker = PermissionChecker(user)
        assert checker.check(Permission.WALLET_ADJUST) is False
