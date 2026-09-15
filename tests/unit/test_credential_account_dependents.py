"""Tests for the deleted-credential-account dependency diagnostic.

Deleting an account from ``configuration.toml`` must not silently move the
servers and orders it owns to another key, and it must not be silent either:
the operator gets an explicit, counted warning. These tests pin the counting,
the ordering, the safety of the sentence and the best-effort failure mode.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from cloud_platform.modules.provider_routes.dependents import (
    CredentialAccountDependents,
    missing_credential_account_report,
    scan_credential_account_dependents,
)


def _result(rows: list[tuple[str, int]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    return result


@pytest.fixture
def session() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock


def _factory(session: AsyncMock) -> Any:
    return lambda: session


class TestScan:
    async def test_counts_servers_and_orders_per_account(self, session: AsyncMock) -> None:
        session.execute.side_effect = [
            _result([("lw-2", 14), ("lw-3", 1)]),
            _result([("lw-2", 3)]),
        ]

        report = await scan_credential_account_dependents(
            _factory(session),
            provider_key="leaseweb",
            configured_account_ids=["lw-1"],
        )

        assert [entry.credential_account_id for entry in report] == ["lw-2", "lw-3"]
        assert report[0].servers == 14
        assert report[0].provider_orders == 3
        assert report[0].total == 17
        assert report[1].total == 1

    async def test_configured_accounts_are_not_reported(self, session: AsyncMock) -> None:
        session.execute.side_effect = [
            _result([("lw-1", 5)]),
            _result([("lw-1", 2)]),
        ]

        report = await scan_credential_account_dependents(
            _factory(session),
            provider_key="leaseweb",
            configured_account_ids=["lw-1", "lw-2"],
        )

        assert report == []

    async def test_reports_an_account_that_only_has_orders(self, session: AsyncMock) -> None:
        session.execute.side_effect = [
            _result([]),
            _result([("lw-gone", 4)]),
        ]

        report = await scan_credential_account_dependents(
            _factory(session),
            provider_key="leaseweb",
            configured_account_ids=[],
        )

        assert report[0].servers == 0
        assert report[0].provider_orders == 4
        assert report[0].total == 4

    async def test_accounts_without_a_pin_are_ignored(self, session: AsyncMock) -> None:
        """Legacy rows with no account id are not "missing accounts"."""
        session.execute.side_effect = [_result([]), _result([])]

        report = await scan_credential_account_dependents(
            _factory(session),
            provider_key="leaseweb",
            configured_account_ids=["default"],
        )

        assert report == []

    async def test_ordering_is_by_impact_then_id(self, session: AsyncMock) -> None:
        session.execute.side_effect = [
            _result([("lw-b", 2), ("lw-a", 2), ("lw-c", 9)]),
            _result([]),
        ]

        report = await scan_credential_account_dependents(
            _factory(session),
            provider_key="leaseweb",
            configured_account_ids=[],
        )

        assert [entry.credential_account_id for entry in report] == ["lw-c", "lw-a", "lw-b"]

    async def test_queries_are_scoped_to_the_provider(self, session: AsyncMock) -> None:
        """A missing account of another provider must not be reported here."""
        session.execute.side_effect = [_result([]), _result([])]

        await scan_credential_account_dependents(
            _factory(session),
            provider_key="leaseweb",
            configured_account_ids=[],
        )

        statements = [
            str(
                call.args[0].compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
            for call in session.execute.call_args_list
        ]
        assert all("'leaseweb'" in statement for statement in statements)
        assert any("JOIN providers" in statement for statement in statements)
        assert all("credential_account_id IS NOT NULL" in statement for statement in statements)
        assert all("GROUP BY" in statement for statement in statements)


class TestMessage:
    def test_message_names_the_account_and_the_counts(self) -> None:
        entry = CredentialAccountDependents(
            provider_key="leaseweb",
            credential_account_id="lw-2",
            servers=12,
            provider_orders=2,
        )

        message = entry.message()

        assert "'lw-2'" in message
        assert "14 existing resource(s)" in message
        assert "12 server(s)" in message
        assert "2 provider order(s)" in message
        assert "fail closed" in message

    def test_message_never_carries_a_credential(self) -> None:
        secret = "lsw_test_ACCOUNT_A_SUPER_SECRET"
        entry = CredentialAccountDependents(
            provider_key="leaseweb",
            credential_account_id="lw-2",
            servers=1,
        )

        assert secret not in entry.message()
        assert secret not in repr(entry)


class TestBestEffortWrapper:
    async def test_returns_the_report_on_success(self, session: AsyncMock) -> None:
        session.execute.side_effect = [_result([("lw-2", 1)]), _result([])]

        report, error = await missing_credential_account_report(
            _factory(session),
            provider_key="leaseweb",
            configured_account_ids=[],
        )

        assert error is None
        assert report[0].credential_account_id == "lw-2"

    async def test_unreachable_database_reports_an_error_class_only(self) -> None:
        """Diagnostics never raise, and never echo a connection string."""

        def exploding_factory() -> Any:
            raise RuntimeError("postgresql://user:sekret@db.internal/cloud")

        report, error = await missing_credential_account_report(
            exploding_factory,
            provider_key="leaseweb",
            configured_account_ids=[],
        )

        assert report == []
        assert error == "RuntimeError"
        assert "sekret" not in str(error)


#: Recognizable fake keys: the whole point is that they can never be printed.
KEY_A = "lsw_test_ACCOUNT_A_SUPER_SECRET"
KEY_B = "lsw_test_ACCOUNT_B_SUPER_SECRET"


def _settings() -> Any:
    from cloud_platform.core.config import Settings

    return Settings(
        leaseweb_accounts=[
            {"id": "lw-eu", "api_key": KEY_A, "priority": 100},
            {"id": "lw-asia", "api_key": KEY_B, "priority": 200},
        ]
    )


def _deleted_account_report() -> list[CredentialAccountDependents]:
    return [
        CredentialAccountDependents(
            provider_key="leaseweb",
            credential_account_id="lw-removed",
            servers=14,
            provider_orders=3,
        )
    ]


def _patch_cli(monkeypatch: Any, *, with_routes: bool) -> Any:
    """Wire the CLI to a real router over the fake accounts + fake diagnostics."""
    import cloud_platform.cli as cli_module
    from cloud_platform.providers.leaseweb.accounts import (
        LeasewebAccountHealth,
        LeasewebHealthReport,
        build_leaseweb_account_router,
    )

    settings = _settings()
    router = build_leaseweb_account_router(settings)
    assert router is not None
    router.verify_all = AsyncMock(  # type: ignore[method-assign]
        return_value=LeasewebHealthReport(
            (
                LeasewebAccountHealth("lw-eu", ok=True),
                LeasewebAccountHealth("lw-asia", ok=True),
            )
        )
    )
    monkeypatch.setattr(
        cli_module, "_leaseweb_account_router_factory", lambda: lambda _settings: router
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    if with_routes:
        routes_repo = AsyncMock()
        routes_repo.list_for_provider = AsyncMock(return_value=[])
        monkeypatch.setattr(
            "cloud_platform.modules.provider_routes.repository.SqlAlchemyProviderRouteRepository",
            lambda *a, **k: routes_repo,
        )
    monkeypatch.setattr(
        "cloud_platform.modules.provider_routes.dependents.missing_credential_account_report",
        AsyncMock(return_value=(_deleted_account_report(), None)),
    )
    return router


class TestOperatorDiagnosticsForADeletedAccount:
    """Requirement: a deleted account with dependents must be reported loudly."""

    async def test_accounts_list_reports_the_dependent_resources(
        self, capsys: Any, monkeypatch: Any
    ) -> None:
        from cloud_platform.cli import leaseweb_accounts_list

        _patch_cli(monkeypatch, with_routes=True)

        code = await leaseweb_accounts_list()

        out = capsys.readouterr().out
        assert code == 0
        assert "WARNING" in out
        assert "'lw-removed'" in out
        assert "17 existing resource(s)" in out
        assert KEY_A not in out and KEY_B not in out

    async def test_accounts_doctor_reports_the_dependent_resources(self, monkeypatch: Any) -> None:
        from cloud_platform.cli import leaseweb_accounts_doctor

        _patch_cli(monkeypatch, with_routes=False)

        result = await leaseweb_accounts_doctor()

        text = "\n".join(result.lines)
        assert "[WARN]" in text
        assert "'lw-removed'" in text
        assert "17 existing resource(s)" in text
        assert KEY_A not in text and KEY_B not in text

    async def test_a_failed_dependency_check_is_not_reported_as_clean(
        self, monkeypatch: Any, capsys: Any
    ) -> None:
        """'Could not check' must never be silently read as 'nothing depends on it'."""
        from cloud_platform.cli import leaseweb_accounts_list

        _patch_cli(monkeypatch, with_routes=True)
        monkeypatch.setattr(
            "cloud_platform.modules.provider_routes.dependents.missing_credential_account_report",
            AsyncMock(return_value=([], "ProgrammingError")),
        )

        await leaseweb_accounts_list()

        assert "dependency check unavailable: ProgrammingError" in capsys.readouterr().out
