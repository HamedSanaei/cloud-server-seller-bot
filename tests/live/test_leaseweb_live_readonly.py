"""Opt-in READ-ONLY live contract tests against the real Leaseweb API.

These tests are skipped unless BOTH are set:

- ``LEASEWEB_LIVE_TESTS=true``
- ``LEASEWEB_API_KEY=<a real key>``

They exist to answer one question — "does our typed client still agree with
the live provider?" — and for nothing else. Every call performed here is a
READ: authentication, the ordering catalogue, the VPS inventory and its
details, IPs, data-traffic metrics, monitoring status, snapshots, ISOs and
account orders.

Hard rules (enforced by review and by the absence of every mutating method
below):

- no VPS is ever ordered, started, stopped, rebooted, reinstalled, snapshotted
  or deleted — an automated live test must never create or change a real,
  billable resource;
- ``order_vps`` is never called, not even with a fake product id;
- the API key is read from the environment and never printed (a failing
  assertion must not echo it — see ``_safe``).

Run locally with::

    LEASEWEB_LIVE_TESTS=true LEASEWEB_API_KEY=... \\
        .venv/Scripts/python -m pytest tests/live -q
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from cloud_platform.providers.leaseweb.ordering_api import LeaseWebOrderingApi
from cloud_platform.providers.leaseweb.orders_api import LeaseWebAccountOrdersApi
from cloud_platform.providers.leaseweb.transport import LeasewebTransport
from cloud_platform.providers.leaseweb.vps.client import LeaseWebVpsApi

LIVE = os.environ.get("LEASEWEB_LIVE_TESTS", "").strip().lower() in {"1", "true", "yes"}
KEY = os.environ.get("LEASEWEB_API_KEY", "").strip()
BASE_URL = os.environ.get("LEASEWEB_BASE_URL", "https://api.leaseweb.com").strip()

#: Applied to the network classes only: the static guard rails below must
#: run in CI even when the live flag is off.
requires_live = pytest.mark.skipif(
    not (LIVE and KEY),
    reason="live Leaseweb tests require LEASEWEB_LIVE_TESTS=true and LEASEWEB_API_KEY",
)


def _safe(text: str) -> str:
    """Never let an assertion message echo the API key."""
    return text.replace(KEY, "<redacted>") if KEY else text


def _transport() -> LeasewebTransport:
    return LeasewebTransport(KEY, BASE_URL, timeout_seconds=30.0)


@requires_live
class TestLiveReadOnly:
    """Read-only smoke checks, one per API family."""

    async def test_authentication_and_vps_inventory(self) -> None:
        transport = _transport()
        api = LeaseWebVpsApi(transport)
        try:
            page = await api.list_vps(limit=1)
        finally:
            await api.aclose()
        assert page.items is not None, "auth rejected or response unusable"
        for summary in page.items:
            assert summary.id, "every VPS must expose its id"
            assert summary.state, "every VPS must expose its state"

    async def test_vps_detail_ips_and_monitoring(self) -> None:
        transport = _transport()
        api = LeaseWebVpsApi(transport)
        try:
            page = await api.list_vps(limit=1)
            if not page.items:
                pytest.skip("account has no VPS to inspect")
            vps_id = page.items[0].id
            detail = await api.get_vps(vps_id)
            assert detail.id == vps_id
            ips = await api.list_ips(vps_id, limit=1)
            assert ips.items is not None
            status = await api.get_monitoring_status(vps_id)
            assert status is not None
            snapshots = await api.list_snapshots(vps_id, limit=1)
            assert snapshots.items is not None
        finally:
            await api.aclose()

    async def test_data_traffic_metrics(self) -> None:
        transport = _transport()
        api = LeaseWebVpsApi(transport)
        try:
            page = await api.list_vps(limit=1)
            if not page.items:
                pytest.skip("account has no VPS to measure")
            now = datetime.now(UTC)
            metrics = await api.get_data_traffic_metrics(
                page.items[0].id,
                from_=(now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                to=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                granularity="60m",
            )
        finally:
            await api.aclose()
        assert metrics.metrics is not None

    async def test_iso_catalogue(self) -> None:
        transport = _transport()
        api = LeaseWebVpsApi(transport)
        try:
            isos = await api.list_isos(limit=1)
        finally:
            await api.aclose()
        assert isos.items is not None

    async def test_ordering_catalogue_is_readable_and_priced(self) -> None:
        transport = _transport()
        api = LeaseWebOrderingApi(transport)
        try:
            products = await api.list_products(limit=1)
            if not products.items:
                pytest.skip("no VPS product is sellable for this account")
            product = products.items[0]
            assert product.id
            assert product.vcpu_count is None or product.vcpu_count > 0
        finally:
            await api.aclose()

    async def test_account_orders_are_readable(self) -> None:
        transport = _transport()
        api = LeaseWebAccountOrdersApi(transport)
        try:
            orders = await api.list_orders(limit=1)
        finally:
            await api.aclose()
        assert orders.items is not None


class TestLiveSuiteGuardRails:
    """Static checks that run even when the live suite is disabled."""

    def test_no_mutating_helper_exists_in_this_module(self) -> None:
        """Guard rail: this suite must never reach a mutating client method."""
        with open(__file__, encoding="utf-8") as handle:
            text = handle.read()
        # Only the network-using class is inspected; the guard-rail class
        # necessarily contains the forbidden names themselves.
        body = text.split("class TestLiveReadOnly", 1)[1].split("class TestLiveSuiteGuardRails", 1)[
            0
        ]
        for forbidden in (
            ".order_vps(",
            ".start_vps(",
            ".stop_vps(",
            ".reboot_vps(",
            ".reinstall(",
            ".reset_password(",
            ".create_snapshot(",
            ".restore_snapshot(",
            ".delete_snapshot(",
            ".attach_iso(",
            ".detach_iso(",
            ".null_route_ip(",
            ".store_credential(",
            ".update_credential(",
            ".delete_credential(",
            ".delete_credentials(",
            ".create_data_traffic_notification_setting(",
            ".update_data_traffic_notification_setting(",
            ".delete_data_traffic_notification_setting(",
            ".update_vps(",
        ):
            assert forbidden not in body, _safe(f"mutating call {forbidden} in the live suite")


def test_live_suite_never_uses_a_private_key_or_password() -> None:
    # Assembled from pieces so this assertion does not trip on its own source.
    pem_marker = "BEGIN RSA " + "PRIVATE KEY"
    password_marker = "super" + "-secret-root-password"  # pragma: allowlist secret
    ssh_marker = "PRIVATE-SSH-KEY" + "-CONTENT"
    with open(__file__, encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in (pem_marker, password_marker, ssh_marker):
        assert forbidden not in text


def test_httpx_is_available_for_the_live_suite() -> None:
    """The live suite shares the repository's HTTP stack (no extra dependency)."""
    assert httpx.__version__
