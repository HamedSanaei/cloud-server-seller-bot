"""New-wallet defaults agree; historical financial facts are not migrations."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from cloud_platform.db.base import Wallet as WalletRow
from cloud_platform.modules.payments.recharge import RechargeError, WalletRechargeService
from cloud_platform.modules.wallet.domain import DEFAULT_WALLET_CURRENCY, Wallet
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository


def test_domain_and_database_new_wallet_defaults_are_toman():
    assert DEFAULT_WALLET_CURRENCY == "IRT"
    assert Wallet(user_id=uuid4()).currency == "IRT"
    assert str(WalletRow.__table__.c.currency.server_default.arg) == "IRT"


async def test_repository_default_is_toman_but_never_relabels_existing_money():
    repository = SqlAlchemyWalletRepository(Mock())
    repository.get = AsyncMock(return_value=None)
    repository._create = AsyncMock()
    user_id = uuid4()
    await repository.get_or_create(user_id)
    repository._create.assert_awaited_once_with(user_id, "IRT")
    legacy = Wallet(user_id=user_id, id=uuid4(), balance=123, currency="USD")
    repository.get.return_value = legacy
    repository._create.reset_mock()
    assert await repository.get_or_create(user_id) is legacy
    assert legacy.balance == 123 and legacy.currency == "USD"
    repository._create.assert_not_awaited()


def test_0060_changes_only_the_insert_default_without_updating_existing_rows():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0060_toman_wallet_default.py"
    spec = importlib.util.spec_from_file_location("toman_default_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    operations = SimpleNamespace(alter_column=Mock())
    module.op = operations
    module.upgrade()
    operations.alter_column.assert_called_once()
    args, kwargs = operations.alter_column.call_args
    assert args == ("wallets", "currency")
    assert kwargs["server_default"] == "IRT" and kwargs["nullable"] is False


@pytest.mark.parametrize(
    "wallet_currency,input_currency", [("USD", "USD"), ("IRT", "USD"), ("IRT", "IRR")]
)
async def test_production_recharge_cannot_select_a_dollar_or_rial_wallet(
    wallet_currency, input_currency
):
    user = SimpleNamespace(id=uuid4(), role="user")
    gateway = SimpleNamespace(key="fixture", create_payment=AsyncMock())
    service = WalletRechargeService(
        payments_repo=AsyncMock(),
        gateway=gateway,
        wallet_repo=SimpleNamespace(
            get=AsyncMock(return_value=Wallet(user_id=user.id, currency=wallet_currency))
        ),
    )
    with pytest.raises(RechargeError):
        await service.start(
            user=user,
            amount_minor=100000,
            currency=input_currency,
            idempotency_key="toman-policy-test",
        )
    gateway.create_payment.assert_not_awaited()
