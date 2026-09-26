"""``leaseweb cloud accounts``: read-only capacity diagnostics for the operator.

When the provider account limit is reached (``PC-2031``), the operator has to be
able to answer three questions from the shell, without a customer reporting it
and without leaking a credential:

    which account?  since when?  and how do I re-probe it?

These tests pin the command's contract: safe facts only (account id, lifecycle,
authentication, proven regions, held instances, capacity state, error code,
correlation id), a nonzero exit while an account is out of capacity, and a
``clear`` action that restores eligibility without deleting the evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cloud_platform.cli import leaseweb_cloud_accounts
from cloud_platform.modules.provider_capacity.domain import (
    AccountCapacity,
    AccountCapacityState,
)
from cloud_platform.providers.routing import CredentialAccountState

ACCOUNT = "sales-org-north"


class _Account:
    def __init__(self, account_id: str, priority: int = 100, enabled: bool = True) -> None:
        self.account_id = account_id
        self.priority = priority
        self.enabled = enabled
        self.state = CredentialAccountState.ACTIVE


class _Capability:
    def __init__(self, regions: tuple[tuple[str, int], ...]) -> None:
        self.account_id = ACCOUNT
        self.accessible = True
        self.regions = regions
        self.error_class = None


class _Instance:
    def __init__(self, instance_id: str) -> None:
        self.id = instance_id


class _Provider:
    def __init__(self, instances: dict[str, list[Any]]) -> None:
        self._instances = instances

    async def list_instances(self, region: str) -> list[Any]:
        return list(self._instances.get(region, []))

    async def region_images_verdict(self, region: str) -> Any:
        """The one verdict the doctor, the sync and checkout share."""
        from cloud_platform.providers.leaseweb.cloud import (
            RegionImagesState,
            RegionImageVerdict,
        )

        if region == "eu-west-2":
            return RegionImageVerdict(
                region=region,
                state=RegionImagesState.UNSERVED,
                region_scoped=False,
                detail="region-filter-unsupported",
            )
        return RegionImageVerdict(region=region, state=RegionImagesState.PROVEN, region_scoped=True)


class _Router:
    def __init__(self) -> None:
        self.accounts = [_Account(ACCOUNT)]
        self._provider = _Provider({"eu-central-1": [_Instance("a"), _Instance("b")]})

    async def probe_account(self, account_id: str) -> Any:
        return _Capability((("eu-central-1", 50), ("eu-west-2", 3)))

    def client_for(self, account_id: str) -> Any:
        return self._provider


class _CapacityRepo:
    def __init__(self, records: list[AccountCapacity] | None = None) -> None:
        self.records = list(records or [])
        self.cleared: list[str] = []

    async def list_for_provider(self, provider_key: str) -> list[AccountCapacity]:
        return list(self.records)

    async def record_healthy(self, provider_key: str, account_id: str) -> AccountCapacity:
        self.cleared.append(account_id)
        return AccountCapacity(
            provider_key=provider_key,
            credential_account_id=account_id,
            observations=1,
            observed_at=datetime.now(UTC),
        )

    async def list_events(
        self, provider_key: str, *, credential_account_id: str | None = None, limit: int = 20
    ) -> list[Any]:
        return []


def _limit_reached() -> AccountCapacity:
    observed = datetime.now(UTC) - timedelta(minutes=5)
    return AccountCapacity(
        provider_key="leaseweb",
        credential_account_id=ACCOUNT,
        state=AccountCapacityState.LIMIT_REACHED,
        error_code="PC-2031",
        correlation_id="07376219-7bcd-43d9-a5ea-4128fa57345a",
        location_id="eu-central-1",
        product_id="lsw.m4.large",
        observations=1,
        observed_at=observed,
        expires_at=observed + timedelta(hours=1),
    )


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Wire a fake router + capacity store into the CLI command."""
    import cloud_platform.providers.leaseweb.cloud_accounts as cloud_accounts

    router = _Router()
    monkeypatch.setattr(cloud_accounts, "build_cloud_account_router", lambda settings: router)
    holder: dict[str, Any] = {"repo": _CapacityRepo()}
    monkeypatch.setattr("cloud_platform.cli._cloud_capacity_repository", lambda: holder["repo"])

    async def _sellable() -> int:
        return holder["sellable"]

    holder["sellable"] = 12
    monkeypatch.setattr("cloud_platform.cli._cloud_sellable_offer_count", _sellable)
    return holder


class TestCloudAccountsDoctor:
    async def test_a_healthy_account_reports_capacity_and_census(
        self, patched: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await leaseweb_cloud_accounts("doctor") == 0
        out = capsys.readouterr().out
        assert f"account {ACCOUNT}: state=active priority=100" in out
        assert "authentication: ok" in out
        assert "cloud regions proven accessible: 2" in out
        assert "instances currently held: 2" in out
        assert "capacity: healthy (never observed)" in out
        assert "may receive NEW hourly orders" in out

    async def test_a_limit_reached_account_is_reported_with_safe_evidence(
        self, patched: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        patched["repo"] = _CapacityRepo([_limit_reached()])
        assert await leaseweb_cloud_accounts("doctor") == 1
        out = capsys.readouterr().out
        assert "capacity: NOT ELIGIBLE for NEW orders (limit-reached, PC-2031)" in out
        assert "correlationId: 07376219-7bcd-43d9-a5ea-4128fa57345a" in out
        assert "affected: eu-central-1 / lsw.m4.large" in out
        # Normal recovery is automatic; the manual clear is only the override.
        assert "NO operator action is required" in out
        assert "override-clear --account" in out
        assert "emergency override only" in out

    async def test_storefront_capacity_metrics_are_reported(
        self, patched: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The doctor exposes the same six metrics the readiness gate prints."""
        patched["repo"] = _CapacityRepo([_limit_reached()])
        assert await leaseweb_cloud_accounts("doctor") == 1
        out = capsys.readouterr().out
        assert "cloud_sellable_offers: 12" in out
        assert "capacity_blocked_accounts: 1" in out
        assert "capacity_unknown_accounts: 0" in out
        assert "recovery_candidate_accounts: 0" in out
        assert "cloud_storefront_available: True" in out
        assert "cloud_storefront_outage_seconds:" in out

    async def test_an_elapsed_window_is_reported_as_unproven_not_healthy(
        self, patched: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The TTL elapsing must never read as "eligible again"."""
        observed = datetime.now(UTC) - timedelta(hours=3)
        expired = _limit_reached()
        expired = AccountCapacity(
            provider_key=expired.provider_key,
            credential_account_id=expired.credential_account_id,
            state=AccountCapacityState.LIMIT_REACHED,
            error_code="PC-2031",
            correlation_id=expired.correlation_id,
            location_id=expired.location_id,
            product_id=expired.product_id,
            observations=1,
            observed_at=observed,
            expires_at=observed + timedelta(hours=1),
        )
        patched["repo"] = _CapacityRepo([expired])
        assert await leaseweb_cloud_accounts("doctor") == 1
        out = capsys.readouterr().out
        assert "NOT ELIGIBLE for NEW orders (unknown-after-limit, PC-2031)" in out
        assert "recovery unproven" in out
        assert "capacity: healthy" not in out

    async def test_the_region_image_verdict_explains_the_global_fallback(
        self, patched: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Doctor, sync and checkout must agree about what the same evidence
        proves: the global image catalog is display-only."""
        assert await leaseweb_cloud_accounts("doctor") == 0
        out = capsys.readouterr().out
        assert "region eu-central-1: image capability proven" in out
        assert "region eu-west-2: image capability unserved" in out
        assert "region filter rejected for this credential" in out
        assert "proves display only" in out

    async def test_the_report_never_prints_a_secret(
        self,
        patched: Any,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LEASEWEB_API_KEY", "super-secret-api-key")
        patched["repo"] = _CapacityRepo([_limit_reached()])
        await leaseweb_cloud_accounts("doctor")
        out = capsys.readouterr().out
        assert "super-secret-api-key" not in out

    async def test_an_unreadable_capacity_store_is_a_warning_not_a_crash(
        self, patched: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        patched["repo"] = None
        assert await leaseweb_cloud_accounts("doctor") == 0
        out = capsys.readouterr().out
        assert "capacity store unavailable; capacity reported as UNKNOWN" in out


class TestCloudAccountsReconcile:
    async def test_reconcile_without_a_store_fails_closed(
        self, patched: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        patched["repo"] = None
        assert await leaseweb_cloud_accounts("reconcile") == 1
        assert "capacity store unavailable" in capsys.readouterr().out

    async def test_a_dry_run_lists_historical_evidence_without_applying_it(
        self, patched: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cloud_platform.modules.provider_capacity.domain import HistoricalCapacityEvidence
        from cloud_platform.providers.leaseweb.capacity_evidence import (
            SqlAlchemyHistoricalCapacityEvidenceSource,
        )

        evidence = (
            HistoricalCapacityEvidence(
                provider_key="leaseweb",
                credential_account_id=ACCOUNT,
                source_ref="server-create:5e4bf88c-cabe-4ab1-9eb4-cf448e046382",
                error_code="PC-2031",
                correlation_id="07376219-7bcd-43d9-a5ea-4128fa57345a",
                observed_at=datetime.now(UTC) - timedelta(days=1),
            ),
        )

        async def fake_failed(self: Any, provider_key: str, *, limit: int = 200) -> Any:
            return evidence

        monkeypatch.setattr(
            SqlAlchemyHistoricalCapacityEvidenceSource, "failed_capacity_evidence", fake_failed
        )
        patched["repo"] = _CapacityRepo()
        assert await leaseweb_cloud_accounts("reconcile", dry_run=True) == 0
        out = capsys.readouterr().out
        assert "capacity reconcile (dry run): 1 provable refusal(s)" in out
        assert "server-create:5e4bf88c" in out
        assert patched["repo"].records == []


class TestCloudAccountsClear:
    async def test_clear_restores_eligibility_for_a_configured_account(
        self, patched: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        patched["repo"] = _CapacityRepo([_limit_reached()])
        assert await leaseweb_cloud_accounts("clear", ACCOUNT) == 0
        out = capsys.readouterr().out
        assert f"account {ACCOUNT}: capacity state healthy" in out
        assert patched["repo"].cleared == [ACCOUNT]

    async def test_clear_requires_a_configured_account(
        self, patched: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await leaseweb_cloud_accounts("clear", None) == 2
        assert "clear requires --account" in capsys.readouterr().out
        assert await leaseweb_cloud_accounts("clear", "ghost-account") == 2
        assert "is not configured" in capsys.readouterr().out
        assert patched["repo"].cleared == []

    async def test_clear_without_a_store_fails_closed(
        self, patched: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        patched["repo"] = None
        assert await leaseweb_cloud_accounts("clear", ACCOUNT) == 1
        assert "capacity store unavailable" in capsys.readouterr().out
