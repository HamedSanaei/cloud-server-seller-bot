"""Config creation defaults and durable administrator overrides compose."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.payments.gateways import GatewayManagementService
from cloud_platform.modules.payments.recharge import (
    RechargeAdminForbiddenError,
    RechargeError,
    WalletRechargeService,
)
from cloud_platform.modules.users.domain import Role, UserStatus


@pytest.mark.parametrize(
    ("default", "override", "expected"),
    [
        (False, None, False),
        (True, None, True),
        (False, True, True),
        (True, False, False),
        (False, False, False),
    ],
)
async def test_missing_override_uses_config_while_explicit_override_wins(
    default, override, expected
):
    repository = SimpleNamespace(
        get=AsyncMock(return_value=override),
        list=AsyncMock(return_value={} if override is None else {"nowpayments": override}),
    )
    gateway = SimpleNamespace(key="nowpayments", supported_currency="USD")
    recharge = WalletRechargeService(
        payments_repo=AsyncMock(),
        gateways={"nowpayments": gateway},
        gateway_settings=repository,
        gateway_defaults={"nowpayments": default},
    )
    assert await recharge.compatible_gateways_async("USD", 1000) == (
        ["nowpayments"] if expected else []
    )
    management = GatewayManagementService(
        repository, ["nowpayments"], gateway_defaults={"nowpayments": default}
    )
    management._require_admin = AsyncMock(return_value=uuid4())
    assert (await management.list(object()))[0].enabled is expected


async def test_config_default_applies_without_override_repository():
    gateway = SimpleNamespace(key="nowpayments", supported_currency="USD")
    recharge = WalletRechargeService(
        payments_repo=AsyncMock(),
        gateways={"nowpayments": gateway},
        gateway_defaults={"nowpayments": False},
    )
    assert await recharge.compatible_gateways_async("USD", 1000) == []


@pytest.mark.parametrize("change", ["persisted_admin", "configured_admin", "forged", "banned"])
async def test_new_recharge_rechecks_identity_and_denies_superadmins(change):
    actor = SimpleNamespace(id=uuid4(), telegram_user_id=121, role=Role.USER)
    persisted = SimpleNamespace(
        id=actor.id,
        telegram_user_id=121,
        role=Role.USER,
        status=UserStatus.ACTIVE,
    )
    if change == "persisted_admin":
        persisted.role = Role.ADMIN
    if change == "forged":
        actor.telegram_user_id = 122
    if change == "banned":
        persisted.status = UserStatus.BANNED
    gateway = SimpleNamespace(key="fixture", create_payment=AsyncMock())
    service = WalletRechargeService(
        payments_repo=AsyncMock(),
        gateway=gateway,
        user_repo=SimpleNamespace(get=AsyncMock(return_value=persisted)),
        configured_ids=(121,) if change == "configured_admin" else (),
    )
    expected = RechargeAdminForbiddenError if change.endswith("admin") else RechargeError
    with pytest.raises(expected):
        await service.start(
            user=actor, amount_minor=100, currency="USD", idempotency_key="safe-test"
        )
    gateway.create_payment.assert_not_awaited()
