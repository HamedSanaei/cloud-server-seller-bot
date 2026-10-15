"""Configuration and shared RBAC policy: identities are never notification chats."""

import os
import subprocess
import sys
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cloud_platform.core.config import Settings, load_settings
from cloud_platform.modules.users.domain import PermissionDeniedError, Role, User, UserStatus
from cloud_platform.modules.users.superadmins import is_superadmin, require_superadmin


def test_canonical_toml_multiple_user_ids_and_notification_destination_are_independent(tmp_path):
    config = tmp_path / "configuration.toml"
    config.write_text(
        "[telegram]\nsuperadmin_user_ids = [123456789, 987654321, 123456789]\n"
        "admin_chat_id = -100123456789\n",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.telegram_superadmin_user_ids == [123456789, 987654321]
    assert settings.telegram_admin_chat_id == -100123456789


@pytest.mark.parametrize("value", [None, 42, "42", [True], ["42"], [1.0], [0], [-1], [2**63]])
def test_superadmin_ids_reject_coerced_invalid_or_overflow_values(value):
    with pytest.raises(ValidationError):
        Settings(telegram_superadmin_user_ids=value)


def test_empty_config_is_fail_closed_even_for_legacy_admin():
    user = User(
        id=uuid4(),
        username="admin",
        email="a@example.test",
        role=Role.ADMIN,
        telegram_user_id=123456789,
    )
    assert not is_superadmin(user, [])
    with pytest.raises(PermissionDeniedError):
        require_superadmin(user, [])
    assert is_superadmin(user, [123456789, 987654321])
    assert not is_superadmin(replace(user, telegram_user_id=1), [123456789])
    assert not is_superadmin(replace(user, role=Role.USER), [123456789])
    assert not is_superadmin(replace(user, status=UserStatus.FROZEN), [123456789])
    assert not is_superadmin(replace(user, status=UserStatus.BANNED), [123456789])
    assert not is_superadmin(replace(user, id=None), [123456789])
    assert not is_superadmin(replace(user, telegram_user_id=True), [123456789])
    assert not is_superadmin(
        {"id": user.id, "role": "admin", "telegram_user_id": 123456789}, [123456789]
    )


def test_notification_chat_does_not_implicitly_grant_privileges():
    settings = Settings(telegram_admin_chat_id=123456789)
    assert settings.telegram_superadmin_user_ids == []


def test_cold_entrypoint_imports_with_configured_superadmins_do_not_create_an_import_cycle(
    tmp_path,
):
    config = tmp_path / "configuration.toml"
    config.write_text("[telegram]\nsuperadmin_user_ids = [123456789]\n", encoding="utf-8")
    env = dict(os.environ, CLOUD_PLATFORM_CONFIG_FILE=str(config))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cloud_platform.core.config import get_settings; "
            "assert get_settings().telegram_superadmin_user_ids == [123456789]; "
            "import cloud_platform.api.app, cloud_platform.bot.main, "
            "cloud_platform.worker.settings",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
