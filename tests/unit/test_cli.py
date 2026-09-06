"""Operator CLI tests (LEASEWEB-MVP release hardening).

Every DB-backed command is exercised with patched repository classes (the
CLI imports them lazily inside each command), and the container-backed
commands with a patched ``create_container``. There is intentionally NO
smoke-order command (no untracked billable POST path); order retry and the
manual resolution commands are exercised through the patched container.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import cloud_platform.cli as cli
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.orders.domain import OrderStatus, ProviderOrder
from cloud_platform.modules.orders.service import OrderManualResolutionError
from cloud_platform.modules.renewals.domain import RenewalRecord, RenewalStatus
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.modules.wallet.domain import Wallet

OFFER_ID = uuid4()
SERVER_ID = uuid4()
ORDER_ID = uuid4()
OP_KEY = f"order-create:{SERVER_ID}"


def _offer(**overrides: Any) -> SellableOffer:
    values: dict[str, Any] = dict(
        id=OFFER_ID,
        provider_key="leaseweb",
        product_id="VPS02_1",
        location_id="AMS-01",
        name="VPS S",
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic="10 TB",
        provider_cost_minor=999,
        provider_cost_currency="EUR",
        selling_price_minor=1299,
        selling_currency="EUR",
        billing_parameters={},
        provider_available=True,
        enabled=True,
    )
    values.update(overrides)
    return SellableOffer(**values)


def _order(status: OrderStatus = OrderStatus.SUBMITTED) -> ProviderOrder:
    return ProviderOrder(
        id=ORDER_ID,
        server_id=SERVER_ID,
        operation_key=OP_KEY,
        provider_key="leaseweb",
        offer_id=OFFER_ID,
        status=status,
        provider_order_id="LS-ORD-1" if status is not OrderStatus.PENDING_SUBMIT else None,
        product_id="VPS02_1",
        location_id="AMS-01",
        os_name="Ubuntu 24.04",
        contract_term="1_MONTH",
        billing_cycle="1_MONTH",
        provider_cost_minor=999,
        provider_cost_currency="EUR",
        selling_price_minor=1299,
        selling_currency="EUR",
    )


def _renewal() -> RenewalRecord:
    return RenewalRecord(
        server_id=SERVER_ID,
        provider_contract_id="C-1",
        provider_order_ref="LS-ORD-1",
        purchased_at=datetime.now(UTC),
        provider_renewal_at=datetime.now(UTC) + timedelta(days=20),
        renewal_date_estimated=False,
        customer_price_minor=1299,
        currency="EUR",
        status=RenewalStatus.ACTIVE,
        auto_charge_enabled=True,
    )


@pytest.fixture(autouse=True)
def _quiet(capsys: pytest.CaptureFixture[str]) -> None:
    yield
    capsys.readouterr()  # drain prints


def _fake_repo_class(repo: Any) -> Any:
    """Build a class the CLI lazily imports that returns ``repo``."""
    return lambda *a, **k: repo  # type: ignore[assignment]


class TestLeasewebDoctor:
    async def test_missing_key_fails_with_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = MagicMock()
        settings.leaseweb_api_key = ""
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        result = await cli.leaseweb_doctor()
        assert not result.ok
        assert any("LEASEWEB_API_KEY configured" in line for line in result.lines)
        assert any(line.startswith("\nAction:") for line in result.lines)

    async def test_key_set_runs_read_only_checks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = MagicMock()
        settings.leaseweb_api_key = "lsw-secret-key-1234"
        settings.leaseweb_api_base_url = "https://api.test"
        settings.leaseweb_locations = "AMS-01"
        settings.leaseweb_os_allowlist = ""
        settings.leaseweb_order_os_only_free = True
        settings.redis_url = "redis://localhost:6379/0"
        settings.database_url = "postgresql+asyncpg://x"
        monkeypatch.setattr(cli, "get_settings", lambda: settings)

        class _FakeProduct:
            id = "VPS02_1"

        class _FakeOption:
            name = "Ubuntu 24.04"

        class _FakeDetail:
            product = MagicMock(monthly_price_minor=1299, currency="EUR")
            os_options: ClassVar[list[Any]] = [_FakeOption()]

            def free_os_options(self) -> list[Any]:
                return [_FakeOption()]

        class _FakeProvider:
            def __init__(self, **kw: Any) -> None:
                pass

            async def list_locations(self) -> list[Any]:
                return [MagicMock(id="AMS-01")]

            async def list_products(self, location: str) -> list[Any]:
                return [_FakeProduct()]

            async def get_product(self, location: str, product_id: str) -> Any:
                return _FakeDetail()

            async def verify_credential(self, candidate: str) -> None:
                return None

            async def close(self) -> None:
                return None

        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.ordering.LeaseWebOrderingProvider",
            _FakeProvider,
        )
        offers_repo = AsyncMock()
        offers_repo.list_all = AsyncMock(return_value=[_offer()])
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            _fake_repo_class(offers_repo),
        )
        monkeypatch.setattr(cli, "_check_db", AsyncMock(return_value=(True, "ok")))
        monkeypatch.setattr(cli, "_check_redis", AsyncMock(return_value=(True, "ok")))
        result = await cli.leaseweb_doctor()
        assert result.ok
        text = "\n".join(result.lines)
        # The key must never be printed; only its redacted tail may appear.
        assert "lsw-secret-key-1234" not in text
        assert "1234" in text  # redacted tail only

    async def test_failing_auth_marks_doctor_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = MagicMock()
        settings.leaseweb_api_key = "lsw-secret-key-1234"
        settings.leaseweb_api_base_url = "https://api.test"
        settings.leaseweb_locations = "AMS-01"
        settings.leaseweb_os_allowlist = ""
        settings.leaseweb_order_os_only_free = True
        monkeypatch.setattr(cli, "get_settings", lambda: settings)

        class _FakeProvider:
            def __init__(self, **kw: Any) -> None:
                pass

            async def list_locations(self) -> list[Any]:
                raise RuntimeError("403 Forbidden")

        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.ordering.LeaseWebOrderingProvider",
            _FakeProvider,
        )
        result = await cli.leaseweb_doctor()
        assert not result.ok
        assert any("Ordering API reachable" in line for line in result.lines)

    async def test_redact_never_leaks(self) -> None:
        assert cli._redact("") == "<not set>"
        assert cli._redact("abcd") == "****"
        assert cli._redact("abcdefgh") == "****efgh"


class TestManualOrderResolutionCommands:
    def _container_with(self, service: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        container = MagicMock()
        container.order_manual_resolution = MagicMock(return_value=service)
        container.close = AsyncMock()
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        return container

    async def test_smoke_order_command_does_not_exist(self) -> None:
        """The untracked live-order CLI path was REMOVED: no parser branch
        can place a billable POST outside the durable checkout -> worker
        pipeline (an ambiguous smoke POST had no recovery identity)."""
        with pytest.raises(SystemExit):
            cli._parser().parse_args(["leaseweb", "smoke-order", "--offer", "x", "--yes"])

    async def test_orders_retry_failed_success(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """Definitive FAILED retry reopens the same local operation; the
        output never claims provider-side deduplication."""
        order = _order(OrderStatus.FAILED)
        op = MagicMock(id=uuid4(), status=MagicMock(value="pending"), attempts=2)
        service = MagicMock()
        service.retry_failed = AsyncMock(return_value=(order, op))
        self._container_with(service, monkeypatch)
        assert await cli.orders_retry(str(ORDER_ID), "verified nothing created") == 0
        out = capsys.readouterr().out
        assert "reopened" in out
        assert "SAME local operation key" in out
        assert "NO provider-side idempotency" in out
        assert "duplicate order is possible" not in out
        service.retry_failed.assert_awaited_once()

    async def test_orders_retry_refuses_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """An OUTCOME_UNKNOWN / NEEDS_REVIEW order is refused by `retry` —
        it must be resolved with the dedicated commands, not blindly
        reopened."""
        service = MagicMock()
        service.retry_failed = AsyncMock(
            side_effect=OrderManualResolutionError(
                "order is outcome_unknown; only definitive FAILED orders may be retried. "
                "Ambiguous orders must be resolved with `orders resolve-existing` or "
                "`orders resolve-not-created` after verifying at the provider portal."
            )
        )
        self._container_with(service, monkeypatch)
        assert await cli.orders_retry(str(ORDER_ID), "retry anyway") == 2
        out = capsys.readouterr().out
        assert "REFUSED" in out
        assert "resolve-existing" in out or "resolve-not-created" in out
        service.retry_failed.assert_awaited_once()

    async def test_orders_resolve_existing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        order = _order(OrderStatus.SUBMITTED)  # provider_order_id=LS-ORD-1 attached
        op = MagicMock(id=uuid4(), status=MagicMock(value="completed"), attempts=1)
        service = MagicMock()
        service.resolve_existing = AsyncMock(return_value=(order, op))
        self._container_with(service, monkeypatch)
        args = cli._parser().parse_args(
            [
                "orders",
                "resolve-existing",
                str(ORDER_ID),
                "LS-ORD-9",
                "--reason",
                "verified at portal",
                "--yes",
            ]
        )
        assert await cli._dispatch(args) == 0
        out = capsys.readouterr().out
        assert "LS-ORD-1" in out
        assert "No provider POST" in out
        service.resolve_existing.assert_awaited_once()

    async def test_orders_resolve_existing_refused_without_yes(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        service = MagicMock()
        self._container_with(service, monkeypatch)
        args = cli._parser().parse_args(
            ["orders", "resolve-existing", str(ORDER_ID), "LS-ORD-9", "--reason", "x"]
        )
        assert await cli._dispatch(args) == 2
        assert "REFUSED" in capsys.readouterr().out
        service.resolve_existing.assert_not_called()

    async def test_orders_resolve_not_created(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        order = _order(OrderStatus.PENDING_SUBMIT)
        op = MagicMock(id=uuid4(), status=MagicMock(value="pending"), attempts=1)
        service = MagicMock()
        service.resolve_not_created = AsyncMock(return_value=(order, op))
        self._container_with(service, monkeypatch)
        args = cli._parser().parse_args(
            [
                "orders",
                "resolve-not-created",
                str(ORDER_ID),
                "--reason",
                "verified nothing",
                "--yes",
            ]
        )
        assert await cli._dispatch(args) == 0
        out = capsys.readouterr().out
        assert "re-queued" in out
        assert "WARNING" in out
        assert "NO provider-side idempotency" in out
        service.resolve_not_created.assert_awaited_once()

    async def test_orders_resolve_not_created_refused_without_yes(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        service = MagicMock()
        self._container_with(service, monkeypatch)
        args = cli._parser().parse_args(
            ["orders", "resolve-not-created", str(ORDER_ID), "--reason", "x"]
        )
        assert await cli._dispatch(args) == 2
        assert "REFUSED" in capsys.readouterr().out
        service.resolve_not_created.assert_not_called()


class TestOffersCommands:
    async def test_offers_list_empty(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        repo = AsyncMock()
        repo.list_sellable = AsyncMock(return_value=[])
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            _fake_repo_class(repo),
        )
        assert await cli.offers_list(False) == 0
        assert "no offers" in capsys.readouterr().out

    async def test_offers_list_flags(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        repo = AsyncMock()
        repo.list_sellable = AsyncMock(
            return_value=[
                _offer(),
                _offer(id=uuid4(), enabled=False),
                _offer(id=uuid4(), selling_price_minor=0),
            ]
        )
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            _fake_repo_class(repo),
        )
        assert await cli.offers_list(False) == 0
        out = capsys.readouterr().out
        assert "SALE" in out
        assert "off" in out
        assert "no-price" in out

    async def test_offers_price_and_enable(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        repo = AsyncMock()
        repo.set_selling_price = AsyncMock(return_value=_offer(selling_price_minor=1899))
        repo.set_enabled = AsyncMock(return_value=_offer(enabled=True))
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            _fake_repo_class(repo),
        )
        assert await cli.offers_set(str(OFFER_ID), "price", "1899", "EUR") == 0
        assert "1899 EUR" in capsys.readouterr().out
        assert await cli.offers_set(str(OFFER_ID), "enable", None, None) == 0
        assert "enabled" in capsys.readouterr().out

    async def test_offers_error_path(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        repo = AsyncMock()
        repo.set_enabled = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            _fake_repo_class(repo),
        )
        assert await cli.offers_set(str(OFFER_ID), "enable", None, None) == 1
        assert "boom" in capsys.readouterr().out


class TestUsersAndWallet:
    async def test_users_find_hit_and_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = AsyncMock()
        repo.get_by_telegram_user_id = AsyncMock(
            return_value=User(
                id=uuid4(),
                username="customer",
                email="c@t.me",
                status=UserStatus.ACTIVE,
                role=Role.USER,
            )
        )
        monkeypatch.setattr(
            "cloud_platform.modules.users.repository.SqlAlchemyUserRepository",
            _fake_repo_class(repo),
        )
        assert await cli.users_find(123) == 0
        repo.get_by_telegram_user_id = AsyncMock(return_value=None)
        assert await cli.users_find(999) == 1

    async def test_wallet_balance(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        repo = AsyncMock()
        repo.get = AsyncMock(
            return_value=Wallet(user_id=uuid4(), id=uuid4(), balance=5000, currency="EUR")
        )
        monkeypatch.setattr(
            "cloud_platform.modules.wallet.repository.SqlAlchemyWalletRepository",
            _fake_repo_class(repo),
        )
        assert await cli.wallet_balance(str(uuid4())) == 0
        assert "5000" in capsys.readouterr().out

    async def test_wallet_adjust_requires_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cloud_platform.core.container.create_container", MagicMock(side_effect=AssertionError)
        )
        assert await cli.wallet_adjust(str(uuid4()), 100, "   ") == 2

    async def test_wallet_adjust_via_admin_service(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        wallet = Wallet(user_id=uuid4(), id=uuid4(), balance=6000, currency="EUR")
        service = AsyncMock()
        service.adjust_balance = AsyncMock(return_value=(wallet, MagicMock(id=uuid4())))
        container = AsyncMock()
        container.wallet_admin_service = MagicMock(return_value=service)
        container.close = AsyncMock()
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        assert await cli.wallet_adjust(str(uuid4()), 1000, "manual credit") == 0
        assert "6000" in capsys.readouterr().out
        # The idempotency key is deterministic per (user, amount, reason).
        call_kwargs = service.adjust_balance.await_args.kwargs
        assert call_kwargs["idempotency_key"].startswith("cli-adj:")


class TestOrdersCommands:
    async def test_orders_list_and_attention(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        repo = AsyncMock()
        repo.list_open = AsyncMock(return_value=[_order(OrderStatus.SUBMITTED)])
        repo.list_attention = AsyncMock(return_value=[_order(OrderStatus.NEEDS_REVIEW)])
        monkeypatch.setattr(
            "cloud_platform.modules.orders.repository.SqlAlchemyProviderOrderRepository",
            _fake_repo_class(repo),
        )
        assert await cli.orders_list(False) == 0
        assert "submitted" in capsys.readouterr().out
        assert await cli.orders_list(True) == 0
        assert "needs_review" in capsys.readouterr().out

    async def test_orders_inspect(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        orders_repo = AsyncMock()
        orders_repo.get = AsyncMock(return_value=_order(OrderStatus.OUTCOME_UNKNOWN))
        monkeypatch.setattr(
            "cloud_platform.modules.orders.repository.SqlAlchemyProviderOrderRepository",
            _fake_repo_class(orders_repo),
        )
        op_repo = AsyncMock()
        op = MagicMock(id=uuid4(), status=MagicMock(value="outcome_unknown"), attempts=1)
        op_repo.get_by_key = AsyncMock(return_value=op)
        monkeypatch.setattr(
            "cloud_platform.modules.operations.repository.SqlAlchemyOperationRepository",
            _fake_repo_class(op_repo),
        )
        assert await cli.orders_inspect(str(ORDER_ID)) == 0
        out = capsys.readouterr().out
        assert "outcome_unknown" in out

    async def test_orders_retry_rejects_non_reviewable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = MagicMock()
        service.retry_failed = AsyncMock(
            side_effect=OrderManualResolutionError("order is submitted; only FAILED may be retried")
        )
        container = MagicMock()
        container.order_manual_resolution = MagicMock(return_value=service)
        container.close = AsyncMock()
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        assert await cli.orders_retry(str(ORDER_ID), "retry") == 2


class TestRenewalsCommands:
    async def test_renewals_list(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        repo = AsyncMock()
        repo.list_active = AsyncMock(return_value=[_renewal()])
        monkeypatch.setattr(
            "cloud_platform.modules.renewals.repository.SqlAlchemyRenewalRepository",
            _fake_repo_class(repo),
        )
        assert await cli.renewals_list(False) == 0
        assert "1299 EUR" in capsys.readouterr().out

    async def test_renewals_check(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        outcome = MagicMock(server_id=SERVER_ID, action="charged", balance_minor=None)
        checker = AsyncMock()
        checker.run = AsyncMock(return_value=[outcome])
        container = AsyncMock()
        container.renewal_checker = MagicMock(return_value=checker)
        container.close = AsyncMock()
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        assert await cli.renewals_check() == 0
        assert "charged" in capsys.readouterr().out


class TestDispatch:
    async def test_dispatch_unknown_command(self) -> None:
        assert await cli._dispatch(MagicMock(command="nope")) == 2

    async def test_main_round_trip(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        repo = AsyncMock()
        repo.list_open = AsyncMock(return_value=[_order()])
        monkeypatch.setattr(
            "cloud_platform.modules.orders.repository.SqlAlchemyProviderOrderRepository",
            _fake_repo_class(repo),
        )
        args = cli._parser().parse_args(["orders", "list"])
        assert await cli._dispatch(args) == 0
        assert "LS-ORD-1" in capsys.readouterr().out

    async def test_smoke_order_parser_branch_is_gone(self) -> None:
        """Dispatch has no smoke-order branch: the command cannot exist in
        the parser (covered above), so there is no live-order escape hatch."""
        assert not hasattr(cli, "leaseweb_smoke_order")


class TestHelperPaths:
    async def test_check_db_and_redis_failures_are_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _BrokenSession:
            async def __aenter__(self) -> Any:
                raise RuntimeError("down")

            async def __aexit__(self, *a: Any) -> None:
                return None

        monkeypatch.setattr("cloud_platform.db.session.SessionFactory", lambda: _BrokenSession())
        ok, detail = await cli._check_db()
        assert ok is False
        assert "down" in detail

    async def test_check_redis_catches_connection_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = MagicMock()
        settings.redis_url = "redis://localhost:1/0"
        monkeypatch.setattr(cli, "get_settings", lambda: settings)

        class _FakeRedis:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            async def ping(self) -> None:
                raise ConnectionError("no route")

            async def aclose(self) -> None:
                return None

        monkeypatch.setattr("redis.asyncio.from_url", lambda *a, **k: _FakeRedis())
        ok, _detail = await cli._check_redis()
        assert ok is False
