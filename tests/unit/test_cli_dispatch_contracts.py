"""Routing and exit-code contracts of the operator CLI dispatcher.

``_dispatch`` is the single mapping from parsed argv to a command
implementation. Every branch is exercised with the implementation stubbed, so
the test asserts WHAT the operator's command maps to (arguments included) and
which exit code the process would return — not the internals of the command.

This pins the operator surfaces that matter for USD pricing operations:
``fx doctor`` / ``fx rates --target``, ``offers doctor``,
``offers normalize-selling-currency --dry-run|--execute`` and
``catalog auto-sync doctor``.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import cloud_platform.cli as cli

SENTINEL = 7


@pytest.fixture(autouse=True)
def _quiet(capsys: pytest.CaptureFixture[str]) -> Any:
    yield
    capsys.readouterr()


def _stub(monkeypatch: pytest.MonkeyPatch, name: str) -> AsyncMock:
    """Replace one async CLI command implementation with a recording stub."""
    stub = AsyncMock(return_value=SENTINEL)
    monkeypatch.setattr(cli, name, stub)
    return stub


def _stub_sync(monkeypatch: pytest.MonkeyPatch, name: str) -> Any:
    """Replace one synchronous CLI command implementation."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _record(*args: Any, **kwargs: Any) -> int:
        calls.append((args, kwargs))
        return SENTINEL

    monkeypatch.setattr(cli, name, _record)
    return SimpleNamespace(calls=calls)


def _stub_doctor(monkeypatch: pytest.MonkeyPatch, name: str, *, ok: bool) -> AsyncMock:
    stub = AsyncMock(return_value=SimpleNamespace(ok=ok, lines=["checked"]))
    monkeypatch.setattr(cli, name, stub)
    return stub


async def _dispatch(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["cloud_platform.cli", *argv])
    return await cli._dispatch(cli._parser().parse_args(argv))


class TestFxOperatorSurface:
    async def test_fx_rates_defaults_to_the_configured_catalog_currency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _stub(monkeypatch, "fx_rates")

        assert await _dispatch(monkeypatch, ["fx", "rates"]) == SENTINEL
        stub.assert_awaited_once_with(None)

    async def test_fx_rates_passes_an_explicit_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _stub(monkeypatch, "fx_rates")

        assert await _dispatch(monkeypatch, ["fx", "rates", "--target", "USD"]) == SENTINEL
        stub.assert_awaited_once_with("USD")

    async def test_fx_doctor_success_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub = _stub_doctor(monkeypatch, "fx_doctor", ok=True)

        assert await _dispatch(monkeypatch, ["fx", "doctor"]) == 0
        stub.assert_awaited_once_with()
        assert "checked" in capsys.readouterr().out

    async def test_fx_doctor_failure_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_doctor(monkeypatch, "fx_doctor", ok=False)

        assert await _dispatch(monkeypatch, ["fx", "doctor"]) == 1

    async def test_unknown_fx_subcommand_fails_closed(self) -> None:
        args = SimpleNamespace(command="fx", subcommand="launch")

        assert await cli._dispatch(args) == 2


class TestOfferOperatorSurface:
    async def test_offers_list_defaults_to_sellable_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _stub(monkeypatch, "offers_list")

        assert await _dispatch(monkeypatch, ["offers", "list"]) == SENTINEL
        stub.assert_awaited_once_with(False)

    async def test_offers_list_all_includes_disabled_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _stub(monkeypatch, "offers_list")

        assert await _dispatch(monkeypatch, ["offers", "list", "--all"]) == SENTINEL
        stub.assert_awaited_once_with(True)

    async def test_offers_doctor_failure_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_doctor(monkeypatch, "offers_doctor", ok=False)

        assert await _dispatch(monkeypatch, ["offers", "doctor"]) == 1

    async def test_offers_preview_routes_the_market(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _stub(monkeypatch, "offers_preview")

        assert await _dispatch(monkeypatch, ["offers", "preview"]) == SENTINEL
        stub.assert_awaited_once_with(None)

    async def test_price_book_requires_a_markup_and_forwards_dry_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _stub(monkeypatch, "offers_price_book")

        rc = await _dispatch(
            monkeypatch,
            ["offers", "price-book", "--markup-percent", "25", "--dry-run"],
        )

        assert rc == SENTINEL
        stub.assert_awaited_once_with("leaseweb", 25, True, False)

    async def test_manual_price_uses_minor_units_and_optional_currency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _stub(monkeypatch, "offers_set")

        assert await _dispatch(monkeypatch, ["offers", "price", "offer-1", "1299"]) == SENTINEL
        stub.assert_awaited_once_with("offer-1", "price", "1299", None)

    async def test_enable_and_disable_route_through_the_generic_setter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _stub(monkeypatch, "offers_set")

        assert await _dispatch(monkeypatch, ["offers", "disable", "offer-1"]) == SENTINEL
        stub.assert_awaited_once_with("offer-1", "disable", None, None)

    async def test_normalize_selling_currency_dry_run_is_the_default_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _stub(monkeypatch, "offers_normalize_selling_currency")

        rc = await _dispatch(monkeypatch, ["offers", "normalize-selling-currency", "--dry-run"])

        assert rc == SENTINEL
        stub.assert_awaited_once_with(True, None)

    async def test_normalize_selling_currency_execute_forwards_the_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _stub(monkeypatch, "offers_normalize_selling_currency")

        rc = await _dispatch(
            monkeypatch,
            ["offers", "normalize-selling-currency", "--execute", "--target", "USD"],
        )

        assert rc == SENTINEL
        stub.assert_awaited_once_with(False, "USD")

    def test_normalize_requires_an_explicit_mode(self) -> None:
        with pytest.raises(SystemExit):
            cli._parser().parse_args(["offers", "normalize-selling-currency"])

    async def test_offers_readiness_routes_without_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The release gate: read-only, no flags, exit code carries the verdict.
        stub = _stub(monkeypatch, "offers_readiness")

        assert await _dispatch(monkeypatch, ["offers", "readiness"]) == SENTINEL
        stub.assert_awaited_once_with()


class TestCatalogAndProviderOperatorSurface:
    async def test_catalog_auto_sync_doctor_routes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _stub(monkeypatch, "catalog_auto_sync_doctor")

        assert await _dispatch(monkeypatch, ["catalog", "auto-sync", "doctor"]) == SENTINEL
        stub.assert_awaited_once_with()

    async def test_catalog_auto_sync_run_routes_without_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The release transition / operator repair path: one complete refresh,
        # bounded by the dedicated catalog budget, verdict in the exit code.
        stub = _stub(monkeypatch, "catalog_auto_sync_run")

        assert await _dispatch(monkeypatch, ["catalog", "auto-sync", "run"]) == SENTINEL
        stub.assert_awaited_once_with()

    async def test_unknown_catalog_subcommand_fails_closed(self) -> None:
        args = SimpleNamespace(command="catalog", subcommand="auto-sync", subsubcommand="go")

        assert await cli._dispatch(args) == 2

    async def test_leaseweb_doctor_and_accounts_doctor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        leaseweb = _stub_doctor(monkeypatch, "leaseweb_doctor", ok=True)
        accounts = _stub_doctor(monkeypatch, "leaseweb_accounts_doctor", ok=False)

        assert await _dispatch(monkeypatch, ["leaseweb", "doctor"]) == 0
        assert await _dispatch(monkeypatch, ["leaseweb", "accounts", "doctor"]) == 1
        leaseweb.assert_awaited_once_with()
        accounts.assert_awaited_once_with()

    async def test_leaseweb_sync_and_auth_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sync_offers = _stub(monkeypatch, "leaseweb_sync_offers")
        auth = _stub(monkeypatch, "leaseweb_auth_check")

        assert await _dispatch(monkeypatch, ["leaseweb", "sync-offers"]) == SENTINEL
        assert await _dispatch(monkeypatch, ["leaseweb", "auth-check"]) == SENTINEL
        sync_offers.assert_awaited_once_with()
        auth.assert_awaited_once_with()

    async def test_leaseweb_coverage_is_synchronous(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _stub_sync(monkeypatch, "leaseweb_coverage")

        assert await _dispatch(monkeypatch, ["leaseweb", "coverage"]) == SENTINEL
        assert stub.calls == [((), {})]

    async def test_leaseweb_cloud_commands_forward_their_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = _stub(monkeypatch, "leaseweb_cloud_catalog")
        preview = _stub(monkeypatch, "leaseweb_cloud_create_preview")
        create = _stub(monkeypatch, "leaseweb_cloud_create")
        doctor = _stub(monkeypatch, "leaseweb_cloud_doctor")

        # The hourly-cloud doctor owns its own reporting, so its exit code is
        # forwarded verbatim instead of being derived from a DoctorResult.
        assert await _dispatch(monkeypatch, ["leaseweb", "cloud", "doctor"]) == SENTINEL
        assert (
            await _dispatch(monkeypatch, ["leaseweb", "cloud", "catalog", "--region", "eu-west-1"])
            == SENTINEL
        )
        assert (
            await _dispatch(
                monkeypatch,
                [
                    "leaseweb",
                    "cloud",
                    "create-preview",
                    "--region",
                    "eu-west-1",
                    "--type",
                    "lsw.c3.large",
                    "--image",
                    "img-1",
                ],
            )
            == SENTINEL
        )
        # A BILLABLE create must never be reachable without --execute-live.
        assert (
            await _dispatch(
                monkeypatch,
                [
                    "leaseweb",
                    "cloud",
                    "create",
                    "--user-id",
                    "u1",
                    "--offer-id",
                    "o1",
                    "--image-id",
                    "img-1",
                ],
            )
            == SENTINEL
        )

        doctor.assert_awaited_once_with()
        catalog.assert_awaited_once_with("eu-west-1")
        preview.assert_awaited_once_with("eu-west-1", "lsw.c3.large", "img-1", "preview-only")
        create.assert_awaited_once_with("u1", "o1", "img-1", False)

    async def test_leaseweb_cloud_create_live_flag_is_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        create = _stub(monkeypatch, "leaseweb_cloud_create")

        await _dispatch(
            monkeypatch,
            [
                "leaseweb",
                "cloud",
                "create",
                "--user-id",
                "u1",
                "--offer-id",
                "o1",
                "--image-id",
                "img-1",
                "--execute-live",
            ],
        )

        create.assert_awaited_once_with("u1", "o1", "img-1", True)

    async def test_unknown_leaseweb_subcommand_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = SimpleNamespace(command="leaseweb", subcommand="launch")

        assert await cli._dispatch(args) == 2
        assert "unknown leaseweb subcommand" in capsys.readouterr().out

    async def test_hetzner_surface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sync_offers = _stub(monkeypatch, "hetzner_sync_offers")
        _stub_doctor(monkeypatch, "hetzner_doctor", ok=True)

        assert await _dispatch(monkeypatch, ["hetzner", "doctor"]) == 0
        assert await _dispatch(monkeypatch, ["hetzner", "sync-offers"]) == SENTINEL
        sync_offers.assert_awaited_once_with()

    async def test_unknown_hetzner_subcommand_fails_closed(self) -> None:
        args = SimpleNamespace(command="hetzner", subcommand="launch")

        assert await cli._dispatch(args) == 2


class TestMoneyAndIdentityOperatorSurface:
    async def test_wallet_credit_is_positive_and_debit_is_negative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adjust = _stub(monkeypatch, "wallet_adjust")
        balance = _stub(monkeypatch, "wallet_balance")

        assert await _dispatch(monkeypatch, ["wallet", "balance", "user-1"]) == SENTINEL
        assert (
            await _dispatch(monkeypatch, ["wallet", "credit", "user-1", "500", "topup"]) == SENTINEL
        )
        assert (
            await _dispatch(monkeypatch, ["wallet", "debit", "user-1", "500", "refund"]) == SENTINEL
        )

        balance.assert_awaited_once_with("user-1")
        assert adjust.await_args_list[0].args == ("user-1", 500, "topup")
        assert adjust.await_args_list[1].args == ("user-1", -500, "refund")

    async def test_users_find_forwards_the_telegram_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _stub(monkeypatch, "users_find")

        assert await _dispatch(monkeypatch, ["users", "find", "12345"]) == SENTINEL
        stub.assert_awaited_once_with(12345)

    async def test_orders_surface_routes_every_subcommand(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        list_orders = _stub(monkeypatch, "orders_list")
        inspect = _stub(monkeypatch, "orders_inspect")
        retry = _stub(monkeypatch, "orders_retry")
        resolve_existing = _stub(monkeypatch, "orders_resolve_existing")
        resolve_vps = _stub(monkeypatch, "orders_resolve_vps")
        resolve_not_created = _stub(monkeypatch, "orders_resolve_not_created")

        await _dispatch(monkeypatch, ["orders", "list"])
        await _dispatch(monkeypatch, ["orders", "attention"])
        await _dispatch(monkeypatch, ["orders", "inspect", "order-1"])
        await _dispatch(monkeypatch, ["orders", "retry", "order-1", "--reason", "because"])
        await _dispatch(
            monkeypatch,
            [
                "orders",
                "resolve-existing",
                "order-1",
                "provider-9",
                "--reason",
                "verified",
                "--yes",
            ],
        )
        await _dispatch(
            monkeypatch, ["orders", "resolve-vps", "order-1", "vps-9", "--reason", "verified"]
        )
        await _dispatch(monkeypatch, ["orders", "resolve-not-created", "order-1", "--reason", "no"])

        assert list_orders.await_args_list[0].args == (False,)
        assert list_orders.await_args_list[1].args == (True,)
        inspect.assert_awaited_once_with("order-1")
        retry.assert_awaited_once_with("order-1", "because")
        resolve_existing.assert_awaited_once_with("order-1", "provider-9", "verified", True)
        resolve_vps.assert_awaited_once_with("order-1", "vps-9", "verified", False)
        resolve_not_created.assert_awaited_once_with("order-1", "no", False)

    async def test_renewals_surface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        renewals_list = _stub(monkeypatch, "renewals_list")
        check = _stub(monkeypatch, "renewals_check")

        await _dispatch(monkeypatch, ["renewals", "list", "--attention"])
        await _dispatch(monkeypatch, ["renewals", "check"])

        assert renewals_list.await_args_list[0].args == (True,)
        check.assert_awaited_once_with()

    async def test_unknown_command_fails_closed(self) -> None:
        args = SimpleNamespace(command="teleport")

        assert await cli._dispatch(args) == 2


class TestMainEntrypoint:
    def test_main_returns_the_dispatch_exit_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dispatched = AsyncMock(return_value=3)
        monkeypatch.setattr(cli, "_dispatch", dispatched)
        monkeypatch.setattr(sys, "argv", ["cloud_platform.cli", "fx", "doctor"])

        assert cli.main() == 3
        assert dispatched.await_args.args[0].command == "fx"
