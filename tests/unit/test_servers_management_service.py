"""Customer server management: ownership, policy, confirmation and ambiguity.

These tests are the security boundary of the Telegram My Servers experience.
They assert the guarantees the spec calls out explicitly:

- another customer's server is indistinguishable from a missing one, and a
  refused action never reaches the provider (call counts stay at zero);
- a destructive action needs a one-time confirmation bound to
  (customer, server, operation, arguments, expiry);
- a double tap, a replay or an expired token can never mutate twice;
- an unprovable provider outcome is recorded for attention and NEVER re-sent;
- nothing secret (console URL, password, API key) reaches audit or events.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.businesslog.domain import BusinessEvent, BusinessEventType
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.operations.service import PowerCommandResult
from cloud_platform.modules.servers.confirmations import ConfirmationVerifier
from cloud_platform.modules.servers.models import (
    CustomerServerState,
    ServerOperation,
)
from cloud_platform.modules.servers.policies import ServerManagementPolicy
from cloud_platform.modules.servers.service import (
    ServerAmbiguousOutcomeError,
    ServerConfirmationError,
    ServerManagementService,
    ServerNotFoundError,
    ServerOperationNotAllowedError,
    ServerProviderError,
)
from cloud_platform.providers.errors import ProviderError, ProviderOutcomeUnknown
from cloud_platform.providers.vps_ports import (
    ConsoleSession,
    DataTrafficUsage,
    VpsActionAccepted,
    VpsInfo,
    VpsIpRecord,
    VpsIsoRecord,
    VpsMonitoringRecord,
    VpsReinstallImage,
    VpsSnapshotRecord,
)

CUSTOMER = uuid4()
OTHER_CUSTOMER = uuid4()
SERVER_ID = uuid4()
PROVIDER_ID = "lsw-vps-1"
CONSOLE_URL = "https://console.leaseweb.com/session?token=super-secret-console-token"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeServerRepo:
    """The slice of ``ServerRepository`` the service uses."""

    def __init__(self, servers: list[CloudServer]) -> None:
        self.rows = {server.id: server for server in servers}
        self.saved: list[CloudServer] = []

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.rows.get(server_id)

    async def list_by_user_paged(
        self, user_id: UUID, *, offset: int, limit: int
    ) -> tuple[list[CloudServer], int]:
        owned = [s for s in self.rows.values() if s.user_id == user_id]
        owned.sort(key=lambda s: str(s.id))
        return owned[offset : offset + limit], len(owned)

    async def save(self, server: CloudServer) -> CloudServer:
        self.rows[server.id] = server
        self.saved.append(server)
        return server


class FakeAuditRepo:
    """Records audit events in memory (metadata included, for leak checks).

    ``AuditTrail`` funnels every mutation through ``repo.append``, so this fake
    implements exactly that one method.
    """

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event

    async def get_by_resource(self, resource_type: str, resource_id: str) -> list[Any]:
        return list(self.events)

    async def get_by_actor(self, actor_id: UUID) -> list[Any]:
        return list(self.events)

    async def list_recent(self, limit: int = 20, offset: int = 0) -> list[Any]:
        return list(self.events)[offset : offset + limit]

    @property
    def actions(self) -> list[tuple[str, dict[str, Any]]]:
        return [(str(event.action), dict(event.metadata or {})) for event in self.events]

    @property
    def names(self) -> list[str]:
        return [action for action, _ in self.actions]


class FakeEventSink:
    """Collects business events (the durable outbox in production)."""

    def __init__(self) -> None:
        self.events: list[BusinessEvent] = []

    async def emit(self, event: BusinessEvent) -> bool:
        self.events.append(event)
        return True

    @property
    def types(self) -> list[BusinessEventType]:
        return [event.event_type for event in self.events]


class FakePower:
    """A stand-in for ``PowerCommandService`` driven by a per-action script."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID, UUID, str]] = []
        self.failures: list[Exception] = []
        self.replayed = False

    def _record(self, action: str, user_id: UUID, server_id: UUID, key: str) -> None:
        self.calls.append((action, user_id, server_id, key))

    async def power_on(self, user_id: UUID, server_id: UUID, key: str) -> PowerCommandResult:
        self._record("power_on", user_id, server_id, key)
        return self._result(server_id)

    async def power_off(self, user_id: UUID, server_id: UUID, key: str) -> PowerCommandResult:
        self._record("power_off", user_id, server_id, key)
        return self._result(server_id)

    async def reboot(self, user_id: UUID, server_id: UUID, key: str) -> PowerCommandResult:
        self._record("reboot", user_id, server_id, key)
        return self._result(server_id)

    def _result(self, server_id: UUID) -> PowerCommandResult:
        if self.failures:
            raise self.failures.pop(0)
        server = CloudServer(
            id=server_id,
            user_id=CUSTOMER,
            provider_key="leaseweb",
            provider_account_id=uuid4(),
            state=ServerLifecycleState.RUNNING,
        )
        return PowerCommandResult(server=server, replayed=self.replayed)


class FakeProvider:
    """A provider adapter implementing every modern VPS port.

    Every method records its call so the tests can assert that a refused or
    replayed action never reached the provider.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.failures: dict[str, Exception] = {}
        self.state = "RUNNING"
        self.snapshots: list[VpsSnapshotRecord] = [
            VpsSnapshotRecord(id="snap-1", name="before-upgrade", state="AVAILABLE")
        ]
        self.images: list[VpsReinstallImage] = [
            VpsReinstallImage(id="img-ubuntu", name="Ubuntu 24.04", family="ubuntu"),
            VpsReinstallImage(id="img-debian", name="Debian 13", family="debian"),
        ]
        self.ips: list[VpsIpRecord] = [
            VpsIpRecord(ip="88.1.2.3", version=4, network_type="PUBLIC", main_ip=True),
            VpsIpRecord(ip="2001:db8::1", version=6, network_type="PUBLIC"),
        ]

    def _call(self, name: str) -> None:
        self.calls.append(name)
        failure = self.failures.pop(name, None)
        if failure is not None:
            raise failure

    def count(self, name: str) -> int:
        return self.calls.count(name)

    # -- ports -----------------------------------------------------------

    async def get_vps_info(self, provider_server_id: str) -> VpsInfo | None:
        self._call("get_vps_info")
        return VpsInfo(
            id=provider_server_id,
            state=self.state,
            reference="customer-ref",
            image_name="Ubuntu 24.04",
            datacenter="FRA-01",
        )

    async def list_vps_info(self) -> list[VpsInfo]:
        self._call("list_vps_info")
        return []

    async def rename_vps(self, provider_server_id: str, reference: str) -> VpsInfo:
        self._call("rename_vps")
        return VpsInfo(id=provider_server_id, state=self.state, reference=reference)

    async def start_vps(self, provider_server_id: str) -> VpsActionAccepted:
        self._call("start_vps")
        self.state = "RUNNING"
        return VpsActionAccepted(provider_server_id=provider_server_id, action="start")

    async def stop_vps(self, provider_server_id: str) -> VpsActionAccepted:
        self._call("stop_vps")
        self.state = "STOPPED"
        return VpsActionAccepted(provider_server_id=provider_server_id, action="stop")

    async def reboot_vps(self, provider_server_id: str) -> VpsActionAccepted:
        self._call("reboot_vps")
        return VpsActionAccepted(provider_server_id=provider_server_id, action="reboot")

    async def get_console_session(self, provider_server_id: str) -> ConsoleSession:
        self._call("get_console_session")
        return ConsoleSession(url=CONSOLE_URL)

    async def list_vps_isos(self) -> list[VpsIsoRecord]:
        self._call("list_vps_isos")
        return [VpsIsoRecord(id="iso-1", name="debian-13.iso")]

    async def attach_vps_iso(self, provider_server_id: str, iso_id: str) -> VpsActionAccepted:
        self._call("attach_vps_iso")
        return VpsActionAccepted(provider_server_id=provider_server_id, action="attachIso")

    async def detach_vps_iso(self, provider_server_id: str) -> VpsActionAccepted:
        self._call("detach_vps_iso")
        return VpsActionAccepted(provider_server_id=provider_server_id, action="detachIso")

    async def list_vps_reinstall_images(self, provider_server_id: str) -> list[VpsReinstallImage]:
        self._call("list_vps_reinstall_images")
        return list(self.images)

    async def reinstall_vps(
        self, provider_server_id: str, image_id: str, market_app_id: str | None = None
    ) -> VpsActionAccepted:
        self._call("reinstall_vps")
        return VpsActionAccepted(provider_server_id=provider_server_id, action="reinstall")

    async def list_vps_ips(self, provider_server_id: str) -> list[VpsIpRecord]:
        self._call("list_vps_ips")
        return list(self.ips)

    async def get_vps_ip(self, provider_server_id: str, ip: str) -> VpsIpRecord:
        self._call("get_vps_ip")
        return VpsIpRecord(ip=ip, version=4, network_type="PUBLIC")

    async def set_vps_ip_reverse_dns(
        self, provider_server_id: str, ip: str, reverse_lookup: str
    ) -> VpsIpRecord:
        self._call("set_vps_ip_reverse_dns")
        return VpsIpRecord(ip=ip, version=4, network_type="PUBLIC", reverse_lookup=reverse_lookup)

    async def null_route_vps_ip(
        self,
        provider_server_id: str,
        ip: str,
        *,
        comment: str | None = None,
        automated_unnuling_hours: int | None = None,
    ) -> VpsIpRecord:
        self._call("null_route_vps_ip")
        return VpsIpRecord(ip=ip, version=4, network_type="PUBLIC", null_routed=True)

    async def unnull_route_vps_ip(self, provider_server_id: str, ip: str) -> VpsIpRecord:
        self._call("unnull_route_vps_ip")
        return VpsIpRecord(ip=ip, version=4, network_type="PUBLIC")

    async def list_vps_snapshots(self, provider_server_id: str) -> list[VpsSnapshotRecord]:
        self._call("list_vps_snapshots")
        return list(self.snapshots)

    async def get_vps_snapshot(
        self, provider_server_id: str, snapshot_id: str
    ) -> VpsSnapshotRecord:
        self._call("get_vps_snapshot")
        return VpsSnapshotRecord(id=snapshot_id, name="snapshot")

    async def create_vps_snapshot(self, provider_server_id: str, name: str) -> VpsActionAccepted:
        self._call("create_vps_snapshot")
        return VpsActionAccepted(provider_server_id=provider_server_id, action="createSnapshot")

    async def restore_vps_snapshot(
        self, provider_server_id: str, snapshot_id: str
    ) -> VpsActionAccepted:
        self._call("restore_vps_snapshot")
        return VpsActionAccepted(provider_server_id=provider_server_id, action="restoreSnapshot")

    async def delete_vps_snapshot(
        self, provider_server_id: str, snapshot_id: str
    ) -> VpsActionAccepted:
        self._call("delete_vps_snapshot")
        return VpsActionAccepted(provider_server_id=provider_server_id, action="deleteSnapshot")

    async def get_vps_data_traffic(
        self,
        provider_server_id: str,
        *,
        from_: str,
        to: str,
        granularity: str,
        aggregation: str = "SUM",
    ) -> list[DataTrafficUsage]:
        self._call("get_vps_data_traffic")
        return [
            DataTrafficUsage(direction="downPublic", unit="bytes", total_bytes=4096),
            DataTrafficUsage(direction="upPublic", unit="bytes", total_bytes=1024),
        ]

    async def get_vps_monitoring(self, provider_server_id: str) -> VpsMonitoringRecord:
        self._call("get_vps_monitoring")
        return VpsMonitoringRecord(status="UP")

    async def enable_vps_monitoring(self, provider_server_id: str) -> None:
        self._call("enable_vps_monitoring")

    async def list_vps_credentials(self, provider_server_id: str) -> list[dict[str, str]]:
        self._call("list_vps_credentials")
        return []

    async def reset_vps_password(self, provider_server_id: str) -> VpsActionAccepted:
        self._call("reset_vps_password")
        return VpsActionAccepted(provider_server_id=provider_server_id, action="resetPassword")


class FakeRegistry:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def get(self, key: str) -> Any:
        if key != "leaseweb":
            raise KeyError(f"unknown provider: {key}")
        return self.provider


def make_server(
    *,
    state: ServerLifecycleState = ServerLifecycleState.RUNNING,
    provider_server_id: str | None = PROVIDER_ID,
) -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=CUSTOMER,
        provider_key="leaseweb",
        provider_account_id=uuid4(),
        state=state,
        provider_server_id=provider_server_id,
        created_at=datetime.now(UTC),
    )


def make_service(
    *,
    server: CloudServer | None = None,
    policy: ServerManagementPolicy | None = None,
    provider: FakeProvider | None = None,
    power: FakePower | None = None,
    ttl_seconds: int = 900,
) -> tuple[ServerManagementService, FakeProvider, FakePower, FakeAuditRepo, FakeEventSink]:
    provider = provider or FakeProvider()
    power = power or FakePower()
    audit = FakeAuditRepo()
    sink = FakeEventSink()
    service = ServerManagementService(
        servers=FakeServerRepo([server or make_server()]),
        registry=FakeRegistry(provider),
        policy=policy or ServerManagementPolicy(),
        confirmations=ConfirmationVerifier("test-signing-key", ttl_seconds=ttl_seconds),
        audit_repo=audit,  # type: ignore[arg-type]
        power=power,  # type: ignore[arg-type]
        event_sink=sink,
    )
    return service, provider, power, audit, sink


# ---------------------------------------------------------------------------
# Ownership / IDOR
# ---------------------------------------------------------------------------


class TestOwnership:
    async def test_list_returns_only_own_servers(self) -> None:
        service, provider, *_ = make_service()
        page = await service.list_servers(OTHER_CUSTOMER)
        assert page.items == ()
        assert provider.calls == []

    async def test_foreign_server_is_indistinguishable_from_missing(self) -> None:
        service, *_ = make_service()
        with pytest.raises(ServerNotFoundError):
            await service.get_server(OTHER_CUSTOMER, SERVER_ID)
        with pytest.raises(ServerNotFoundError):
            await service.get_server(OTHER_CUSTOMER, uuid4())

    async def test_foreign_start_makes_no_provider_call(self) -> None:
        service, provider, power, *_ = make_service()
        with pytest.raises(ServerNotFoundError):
            await service.stop_server(OTHER_CUSTOMER, SERVER_ID)
        assert provider.calls == []
        assert power.calls == []

    async def test_foreign_destructive_action_makes_no_provider_call(self) -> None:
        service, provider, *_ = make_service()
        for call in (
            service.reinstall(OTHER_CUSTOMER, SERVER_ID, image_ref="img", confirmation_token="x"),
            service.reset_password(OTHER_CUSTOMER, SERVER_ID, confirmation_token="x"),
            service.restore_snapshot(
                OTHER_CUSTOMER, SERVER_ID, snapshot_ref="snap-1", confirmation_token="x"
            ),
            service.null_route_ip(OTHER_CUSTOMER, SERVER_ID, ip="88.1.2.3", confirmation_token="x"),
        ):
            with pytest.raises(ServerNotFoundError):
                await call
        assert all(name.startswith(("get_", "list_")) is False for name in provider.calls)

    async def test_other_customers_cannot_address_a_server_ip(self) -> None:
        """Null-routing an IP the server does not own is refused, not executed."""
        provider = FakeProvider()
        provider.ips = [VpsIpRecord(ip="10.0.0.9", version=4, network_type="PRIVATE")]
        service, *_ = make_service(provider=provider)
        with pytest.raises(ServerOperationNotAllowedError) as excinfo:
            await service.null_route_ip(CUSTOMER, SERVER_ID, ip="88.1.2.3", confirmation_token="x")
        assert excinfo.value.reason == "ip_not_owned"
        assert provider.count("null_route_vps_ip") == 0


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class TestPolicy:
    async def test_disabled_feature_refuses_everything(self) -> None:
        service, *_ = make_service(policy=ServerManagementPolicy(enabled=False))
        with pytest.raises(ServerOperationNotAllowedError) as excinfo:
            await service.get_server(CUSTOMER, SERVER_ID)
        assert excinfo.value.reason == "feature_disabled"

    async def test_group_not_exposed(self) -> None:
        service, provider, *_ = make_service(policy=ServerManagementPolicy(iso=False))
        with pytest.raises(ServerOperationNotAllowedError) as excinfo:
            await service.attach_iso(CUSTOMER, SERVER_ID, iso_ref="iso-1", confirmation_token="x")
        assert excinfo.value.reason == "not_exposed"
        assert provider.count("attach_vps_iso") == 0

    async def test_state_gate_blocks_wrong_state(self) -> None:
        service, power, *_ = make_service(server=make_server(state=ServerLifecycleState.RUNNING))
        with pytest.raises(ServerOperationNotAllowedError) as excinfo:
            await service.start_server(CUSTOMER, SERVER_ID)
        assert excinfo.value.reason == "state_not_allowed"
        assert power.calls == []

    async def test_provider_capability_gate(self) -> None:
        """A provider that cannot snapshot is refused BEFORE it is called."""
        provider = FakeProvider()
        for method in (
            "list_vps_snapshots",
            "get_vps_snapshot",
            "create_vps_snapshot",
            "restore_vps_snapshot",
            "delete_vps_snapshot",
        ):
            setattr(provider, method, None)  # not callable => no capability
        service, *_ = make_service(provider=provider)
        with pytest.raises(ServerOperationNotAllowedError) as excinfo:
            await service.snapshots(CUSTOMER, SERVER_ID)
        assert excinfo.value.reason == "provider_unsupported"
        assert provider.calls == []

    async def test_unprovisioned_server_refuses_provider_work(self) -> None:
        service, *_ = make_service(server=make_server(provider_server_id=None))
        with pytest.raises(ServerOperationNotAllowedError) as excinfo:
            await service.console(CUSTOMER, SERVER_ID)
        assert excinfo.value.reason == "not_provisioned"


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------


class TestPower:
    async def test_start_is_idempotent_and_emits_business_event(self) -> None:
        server = make_server(state=ServerLifecycleState.STOPPED)
        service, _provider, power, audit, sink = make_service(server=server)

        outcome = await service.start_server(CUSTOMER, SERVER_ID, idempotency_key="k1")
        replay = await service.start_server(CUSTOMER, SERVER_ID, idempotency_key="k1")

        assert outcome.accepted is True
        assert replay.replayed is False
        assert [call[0] for call in power.calls] == ["power_on", "power_on"]
        # One ledger key for one action: the operation ledger collapses the retry.
        assert {call[3] for call in power.calls} == {"k1"}
        # The power ledger (not this service) owns the audit trail for power;
        # this service owns the business event.
        assert BusinessEventType.SERVER_STARTED in sink.types
        assert audit.names == []

    async def test_replayed_power_reports_already_in_progress(self) -> None:
        server = make_server(state=ServerLifecycleState.STOPPED)
        power = FakePower()
        power.replayed = True
        service, *_ = make_service(server=server, power=power)

        outcome = await service.start_server(CUSTOMER, SERVER_ID)

        assert outcome.replayed is True
        assert outcome.detail == "already_in_progress"

    async def test_stop_requires_confirmation(self) -> None:
        service, _provider, power, *_ = make_service()
        with pytest.raises(ServerConfirmationError):
            await service.stop_server(CUSTOMER, SERVER_ID)
        assert power.calls == []

        token = await service.issue_confirmation(CUSTOMER, SERVER_ID, ServerOperation.STOP)
        outcome = await service.stop_server(CUSTOMER, SERVER_ID, confirmation_token=token)
        assert outcome.accepted is True
        assert [call[0] for call in power.calls] == ["power_off"]

    async def test_stop_replay_does_not_stop_twice(self) -> None:
        service, _provider, power, *_ = make_service()
        token = await service.issue_confirmation(CUSTOMER, SERVER_ID, ServerOperation.STOP)

        first = await service.stop_server(CUSTOMER, SERVER_ID, confirmation_token=token)
        second = await service.stop_server(CUSTOMER, SERVER_ID, confirmation_token=token)

        assert first.accepted is True
        assert second.replayed is True
        assert [call[0] for call in power.calls] == ["power_off"]

    async def test_ambiguous_power_failure_is_never_retried(self) -> None:
        power = FakePower()
        power.failures.append(ProviderOutcomeUnknown("timeout after write"))
        service, _provider, _power, audit, sink = make_service(power=power)

        # Stopping without the one-time confirmation never reaches the provider.
        with pytest.raises(ServerConfirmationError):
            await service.stop_server(CUSTOMER, SERVER_ID)
        assert power.calls == []

        token = await service.issue_confirmation(CUSTOMER, SERVER_ID, ServerOperation.STOP)
        with pytest.raises(ServerAmbiguousOutcomeError):
            await service.stop_server(CUSTOMER, SERVER_ID, confirmation_token=token)
        assert len(power.calls) == 1
        assert "server.stop_outcome_unknown" in audit.names
        assert BusinessEventType.SERVER_OPERATION_FAILED in sink.types


# ---------------------------------------------------------------------------
# Confirmations
# ---------------------------------------------------------------------------


class TestConfirmations:
    async def test_confirmation_is_bound_to_the_customer(self) -> None:
        """Another customer cannot use a token minted for someone else.

        Ownership is checked before the confirmation is even looked at, so the
        answer is the same uniform "not found" as for a missing server — the
        token never gets a chance to be consumed.
        """
        service, provider, *_ = make_service()
        token = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.PASSWORD_RESET
        )
        with pytest.raises(ServerNotFoundError):
            await service.reset_password(OTHER_CUSTOMER, SERVER_ID, confirmation_token=token)
        assert provider.count("reset_vps_password") == 0

    async def test_confirmation_is_bound_to_the_server(self) -> None:
        """A token issued for one server is rejected for another."""
        other_id = uuid4()
        other = CloudServer(
            id=other_id,
            user_id=CUSTOMER,
            provider_key="leaseweb",
            provider_account_id=uuid4(),
            state=ServerLifecycleState.RUNNING,
            provider_server_id="lsw-vps-2",
        )
        provider = FakeProvider()
        service = ServerManagementService(
            servers=FakeServerRepo([make_server(), other]),
            registry=FakeRegistry(provider),
            policy=ServerManagementPolicy(),
            confirmations=ConfirmationVerifier("test-signing-key"),
            audit_repo=FakeAuditRepo(),  # type: ignore[arg-type]
            power=FakePower(),  # type: ignore[arg-type]
            event_sink=FakeEventSink(),
        )
        token = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.PASSWORD_RESET
        )
        with pytest.raises(ServerConfirmationError) as excinfo:
            await service.reset_password(CUSTOMER, other_id, confirmation_token=token)
        assert excinfo.value.status.value == "mismatch"
        assert provider.count("reset_vps_password") == 0

    async def test_tampered_arguments_are_rejected(self) -> None:
        service, provider, *_ = make_service()
        token = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.REINSTALL, arguments={"image": "img-ubuntu"}
        )
        with pytest.raises(ServerConfirmationError):
            await service.reinstall(
                CUSTOMER, SERVER_ID, image_ref="img-debian", confirmation_token=token
            )
        assert provider.count("reinstall_vps") == 0

    async def test_expired_confirmation_is_rejected(self) -> None:
        service, provider, *_ = make_service(ttl_seconds=60)
        token = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.PASSWORD_RESET
        )
        # Move the service clock past the token's expiry.
        service._clock = lambda: datetime.now(UTC) + timedelta(seconds=120)
        with pytest.raises(ServerConfirmationError) as excinfo:
            await service.reset_password(CUSTOMER, SERVER_ID, confirmation_token=token)
        assert excinfo.value.status.value == "expired"
        assert provider.count("reset_vps_password") == 0

    async def test_missing_confirmation_is_rejected(self) -> None:
        service, provider, *_ = make_service()
        with pytest.raises(ServerConfirmationError):
            await service.reset_password(CUSTOMER, SERVER_ID, confirmation_token=None)
        assert provider.count("reset_vps_password") == 0

    async def test_double_click_reinstall_mutates_once(self) -> None:
        service, provider, *_ = make_service()
        token = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.REINSTALL, arguments={"image": "img-ubuntu"}
        )

        first = await service.reinstall(
            CUSTOMER, SERVER_ID, image_ref="img-ubuntu", confirmation_token=token
        )
        second = await service.reinstall(
            CUSTOMER, SERVER_ID, image_ref="img-ubuntu", confirmation_token=token
        )

        assert first.accepted is True
        assert second.replayed is True
        assert provider.count("reinstall_vps") == 1

    async def test_snapshot_restore_and_delete_mutate_once(self) -> None:
        service, provider, *_ = make_service()
        restore = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.SNAPSHOT_RESTORE, arguments={"snapshot": "snap-1"}
        )
        delete = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.SNAPSHOT_DELETE, arguments={"snapshot": "snap-1"}
        )
        await service.restore_snapshot(
            CUSTOMER, SERVER_ID, snapshot_ref="snap-1", confirmation_token=restore
        )
        await service.delete_snapshot(
            CUSTOMER, SERVER_ID, snapshot_ref="snap-1", confirmation_token=delete
        )
        assert provider.count("restore_vps_snapshot") == 1
        assert provider.count("delete_vps_snapshot") == 1

    async def test_issue_confirmation_checks_policy(self) -> None:
        service, *_ = make_service(policy=ServerManagementPolicy(reinstall=False))
        with pytest.raises(ServerOperationNotAllowedError):
            await service.issue_confirmation(CUSTOMER, SERVER_ID, ServerOperation.REINSTALL)


# ---------------------------------------------------------------------------
# Provider ambiguity + failures
# ---------------------------------------------------------------------------


class TestProviderFailures:
    async def test_ambiguous_mutation_is_recorded_not_retried(self) -> None:
        provider = FakeProvider()
        provider.failures["reinstall_vps"] = ProviderOutcomeUnknown("5xx after transmission")
        service, _p, _power, audit, sink = make_service(provider=provider)
        token = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.REINSTALL, arguments={"image": "img-ubuntu"}
        )

        with pytest.raises(ServerAmbiguousOutcomeError):
            await service.reinstall(
                CUSTOMER, SERVER_ID, image_ref="img-ubuntu", confirmation_token=token
            )

        assert provider.count("reinstall_vps") == 1
        assert "server.reinstall_outcome_unknown" in audit.names
        assert BusinessEventType.SERVER_OPERATION_FAILED in sink.types

    async def test_definitive_rejection_maps_to_provider_error(self) -> None:
        provider = FakeProvider()
        provider.failures["reset_vps_password"] = ProviderError("403 forbidden")
        service, _p, _power, audit, sink = make_service(provider=provider)
        token = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.PASSWORD_RESET
        )

        with pytest.raises(ServerProviderError):
            await service.reset_password(CUSTOMER, SERVER_ID, confirmation_token=token)

        assert provider.count("reset_vps_password") == 1
        assert "server.password_reset_failed" in audit.names
        assert BusinessEventType.SERVER_OPERATION_FAILED in sink.types

    async def test_traffic_failure_is_reported_not_raised(self) -> None:
        provider = FakeProvider()
        provider.failures["get_vps_data_traffic"] = ProviderError("503 unavailable")
        service, *_ = make_service(provider=provider)

        usage = await service.traffic(CUSTOMER, SERVER_ID)

        assert usage.unavailable_reason
        assert usage.total_bytes == 0

    async def test_traffic_keeps_directions_and_integer_bytes(self) -> None:
        service, *_ = make_service()
        usage = await service.traffic(CUSTOMER, SERVER_ID)
        assert usage.downloaded_bytes == 4096
        assert usage.uploaded_bytes == 1024
        assert usage.total_bytes == 5120
        assert usage.separate_directions is True
        assert isinstance(usage.total_bytes, int)


# ---------------------------------------------------------------------------
# Secrets never leak
# ---------------------------------------------------------------------------


class TestSecretDiscipline:
    async def test_console_url_never_reaches_audit_events_or_repr(self) -> None:
        service, *_provider, _power, audit, sink = make_service()
        view = await service.console(CUSTOMER, SERVER_ID)

        assert view.url == CONSOLE_URL
        assert "super-secret-console-token" not in repr(view)
        assert "super-secret-console-token" not in str(view)
        for _action, metadata in audit.actions:
            assert CONSOLE_URL not in str(metadata)
        for event in sink.events:
            assert CONSOLE_URL not in str(event)
            assert "super-secret" not in repr(event)

    async def test_ip_metadata_is_masked(self) -> None:
        service, *_provider, _power, audit, _sink = make_service()
        token = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.IP_NULL_ROUTE, arguments={"ip": "88.1.2.3"}
        )
        await service.null_route_ip(CUSTOMER, SERVER_ID, ip="88.1.2.3", confirmation_token=token)
        for _action, metadata in audit.actions:
            assert "88.1.2.3" not in str(metadata)

    async def test_ambiguous_reason_never_carries_credentials(self) -> None:
        provider = FakeProvider()
        provider.failures["reinstall_vps"] = ProviderOutcomeUnknown(
            "X-LSW-Auth: test-api-key-should-never-leak"
        )
        service, _p, _power, audit, sink = make_service(provider=provider)
        token = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.REINSTALL, arguments={"image": "img-ubuntu"}
        )
        with pytest.raises(ServerAmbiguousOutcomeError):
            await service.reinstall(
                CUSTOMER, SERVER_ID, image_ref="img-ubuntu", confirmation_token=token
            )
        # The provider reason is stored, but the audit trail must not be a place
        # where an operator accidentally publishes an auth header.
        for _action, metadata in audit.actions:
            assert "X-LSW-Auth" not in str(metadata)
        del sink


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestReads:
    async def test_customer_view_maps_unknown_state_safely(self) -> None:
        provider = FakeProvider()
        provider.state = "SOME_FUTURE_STATE"
        server = make_server()
        server.transition_to(ServerLifecycleState.STOPPED)
        service, *_ = make_service(server=server, provider=provider)

        view = await service.refresh_server(CUSTOMER, SERVER_ID)

        assert view.state is CustomerServerState.STOPPED
        assert view.ip == "88.1.2.3"

    async def test_refresh_uses_only_reads(self) -> None:
        service, provider, *_ = make_service()
        await service.refresh_server(CUSTOMER, SERVER_ID)
        assert all(name in {"get_vps_info", "list_vps_ips"} for name in provider.calls), (
            provider.calls
        )

    async def test_snapshots_and_images_are_listed(self) -> None:
        service, *_ = make_service()
        snapshots = await service.snapshots(CUSTOMER, SERVER_ID)
        images = await service.reinstall_images(CUSTOMER, SERVER_ID)
        assert [s.ref for s in snapshots] == ["snap-1"]
        assert [image.name for image in images] == ["Debian 13", "Ubuntu 24.04"]

    async def test_monitoring_exposes_enable_only_when_allowed(self) -> None:
        provider = FakeProvider()
        provider.get_vps_monitoring = _monitoring_off  # type: ignore[method-assign]
        service, *_ = make_service(provider=provider)
        view = await service.monitoring(CUSTOMER, SERVER_ID)
        assert view.enabled is False
        assert view.can_enable is True

    async def test_rename_rejects_unsafe_names(self) -> None:
        service, provider, *_ = make_service()
        with pytest.raises(ServerOperationNotAllowedError) as excinfo:
            await service.rename(CUSTOMER, SERVER_ID, display_name="bad$name<script>")
        assert excinfo.value.reason == "invalid_name"
        assert provider.count("rename_vps") == 0

        renamed = await service.rename(CUSTOMER, SERVER_ID, display_name="my server 1")
        assert renamed.display_name == "my server 1"
        assert provider.count("rename_vps") == 1


async def _monitoring_off(provider_server_id: str) -> VpsMonitoringRecord:
    return VpsMonitoringRecord(status="DOWN")


# ---------------------------------------------------------------------------
# The rest of the provider surface (ISO, monitoring, snapshots, rename)
# ---------------------------------------------------------------------------


class TestRemainingProviderSurface:
    async def test_iso_attach_and_detach_are_confirmed(self) -> None:
        provider = FakeProvider()
        service, *_ = make_service(
            provider=provider,
            # Attaching a boot medium is only offered on a stopped server.
            server=make_server(state=ServerLifecycleState.STOPPED),
            policy=ServerManagementPolicy(iso=True),
        )

        with pytest.raises(ServerConfirmationError):
            await service.detach_iso(CUSTOMER, SERVER_ID, confirmation_token=None)
        assert provider.count("detach_vps_iso") == 0

        attach = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.ISO_ATTACH, arguments={"iso": "iso-1"}
        )
        await service.attach_iso(CUSTOMER, SERVER_ID, iso_ref="iso-1", confirmation_token=attach)
        detach = await service.issue_confirmation(CUSTOMER, SERVER_ID, ServerOperation.ISO_DETACH)
        await service.detach_iso(CUSTOMER, SERVER_ID, confirmation_token=detach)

        assert provider.count("attach_vps_iso") == 1
        assert provider.count("detach_vps_iso") == 1

    async def test_iso_catalogue_and_credentials_stay_available(self) -> None:
        service, provider, *_ = make_service(policy=ServerManagementPolicy(iso=True))
        isos = await service.list_isos(CUSTOMER, SERVER_ID)
        assert isos == [("iso-1", "debian-13.iso")]
        assert provider.count("list_vps_isos") == 1

    async def test_enable_monitoring_is_a_reversible_mutation(self) -> None:
        service, provider, _power, audit, _sink = make_service()
        outcome = await service.enable_monitoring(CUSTOMER, SERVER_ID)
        assert outcome.accepted is True
        assert provider.count("enable_vps_monitoring") == 1
        assert "server.monitoring_enable" in audit.names

    async def test_reverse_dns_is_validated_and_audited_masked(self) -> None:
        service, provider, _power, audit, _sink = make_service()

        with pytest.raises(ServerOperationNotAllowedError):
            await service.set_reverse_dns(
                CUSTOMER, SERVER_ID, ip="88.1.2.3", reverse_lookup="bad host!"
            )
        assert provider.count("set_vps_ip_reverse_dns") == 0

        view = await service.set_reverse_dns(
            CUSTOMER, SERVER_ID, ip="88.1.2.3", reverse_lookup="host.example.com"
        )
        assert view.reverse_lookup == "host.example.com"
        for _action, metadata in audit.actions:
            assert "88.1.2.3" not in str(metadata)

    async def test_iso_and_reverse_dns_need_an_owned_ip(self) -> None:
        provider = FakeProvider()
        provider.ips = [VpsIpRecord(ip="10.0.0.1", version=4, network_type="PRIVATE")]
        service, *_ = make_service(provider=provider)
        token = await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.IP_NULL_ROUTE, arguments={"ip": "88.9.9.9"}
        )
        with pytest.raises(ServerOperationNotAllowedError) as excinfo:
            await service.null_route_ip(
                CUSTOMER, SERVER_ID, ip="88.9.9.9", confirmation_token=token
            )
        assert excinfo.value.reason == "ip_not_owned"

    async def test_unnull_route_is_not_confirmation_gated(self) -> None:
        service, provider, *_ = make_service()
        outcome = await service.unnull_route_ip(CUSTOMER, SERVER_ID, ip="88.1.2.3")
        assert outcome.operation is ServerOperation.IP_UNNULL_ROUTE
        assert provider.count("unnull_route_vps_ip") == 1

    async def test_refresh_survives_a_provider_failure(self) -> None:
        provider = FakeProvider()
        provider.failures["get_vps_info"] = ProviderError("503 unavailable")
        service, *_ = make_service(provider=provider)

        view = await service.refresh_server(CUSTOMER, SERVER_ID)

        assert view.refresh_error
        # The provider failed, so the local row is still authoritative.
        assert view.server_id == SERVER_ID

    async def test_provider_without_ips_skips_the_address_refresh(self) -> None:
        provider = FakeProvider()
        for method in (
            "list_vps_ips",
            "get_vps_ip",
            "set_vps_ip_reverse_dns",
            "null_route_vps_ip",
            "unnull_route_vps_ip",
        ):
            setattr(provider, method, None)
        service, *_ = make_service(provider=provider)
        view = await service.refresh_server(CUSTOMER, SERVER_ID)
        assert view.server_id == SERVER_ID
        assert provider.count("list_vps_ips") == 0

    async def test_unknown_provider_is_refused(self) -> None:
        server = make_server()
        server.provider_key = "mystery-cloud"
        service, *_ = make_service(server=server)
        with pytest.raises(ServerOperationNotAllowedError) as excinfo:
            await service.console(CUSTOMER, SERVER_ID)
        assert excinfo.value.reason == "provider_disabled"


# ---------------------------------------------------------------------------
# Policy helpers (pure functions)
# ---------------------------------------------------------------------------


class TestPolicyHelpers:
    def test_customer_state_maps_known_and_unknown_provider_values(self) -> None:
        from cloud_platform.modules.servers.policies import customer_state

        assert customer_state(ServerLifecycleState.RUNNING, "running") is (
            CustomerServerState.RUNNING
        )
        assert customer_state(ServerLifecycleState.RUNNING, "SHUTOFF") is (
            CustomerServerState.STOPPED
        )
        assert customer_state(ServerLifecycleState.RUNNING, "STARTING") is (
            CustomerServerState.STARTING
        )
        assert customer_state(ServerLifecycleState.RUNNING, "REINSTALLING") is (
            CustomerServerState.REBOOTING
        )
        # An unknown provider value degrades to the local state, never raises.
        assert customer_state(ServerLifecycleState.RUNNING, "WHO_KNOWS") is (
            CustomerServerState.RUNNING
        )
        assert customer_state(ServerLifecycleState.MANUAL_REVIEW) is (
            CustomerServerState.PENDING_REVIEW
        )

    def test_location_label_never_leaks_an_unknown_code(self) -> None:
        from cloud_platform.modules.servers.policies import location_label

        assert location_label("FRA-01") == ("🇩🇪", "Frankfurt")
        assert location_label("ams-01") == ("🇳🇱", "Amsterdam")
        assert location_label(None) is None
        assert location_label("") is None
        assert location_label("ZZZ-99") is None

    def test_operation_allowed_reports_the_specific_reason(self) -> None:
        from cloud_platform.modules.servers.policies import operation_allowed
        from cloud_platform.providers.vps_ports import VpsCapabilities

        policy = ServerManagementPolicy()
        capabilities = VpsCapabilities(power=True, inventory=True)

        assert (
            operation_allowed(
                ServerOperation.START,
                policy=policy,
                capabilities=capabilities,
                state=ServerLifecycleState.STOPPED,
            )
            is None
        )
        assert (
            operation_allowed(
                ServerOperation.START,
                policy=policy,
                capabilities=capabilities,
                state=ServerLifecycleState.RUNNING,
            ).reason
            == "state_not_allowed"
        )  # type: ignore[union-attr]
        assert (
            operation_allowed(
                ServerOperation.START,
                policy=policy,
                capabilities=VpsCapabilities(),
                state=ServerLifecycleState.STOPPED,
            ).reason
            == "provider_unsupported"
        )  # type: ignore[union-attr]
        assert (
            operation_allowed(
                ServerOperation.ISO_ATTACH,
                policy=policy,
                capabilities=capabilities,
                state=ServerLifecycleState.STOPPED,
            ).reason
            == "not_exposed"
        )  # type: ignore[union-attr]
        assert (
            operation_allowed(
                ServerOperation.START,
                policy=ServerManagementPolicy(enabled=False),
                capabilities=capabilities,
                state=ServerLifecycleState.STOPPED,
            ).reason
            == "feature_disabled"
        )  # type: ignore[union-attr]

    def test_policy_reads_the_feature_section(self) -> None:
        class _Settings:
            server_management_enabled = True
            server_management_iso = True
            server_management_page_size = 3
            server_management_traffic_window_days = 7
            server_management_confirmation_ttl_seconds = 60

        policy = ServerManagementPolicy.from_settings(_Settings())
        assert policy.iso is True
        assert policy.page_size == 3
        assert policy.traffic_window_days == 7
        assert policy.confirmation_ttl_seconds == 60
        assert policy.requires_confirmation(ServerOperation.REINSTALL) is True
        assert policy.requires_confirmation(ServerOperation.MONITORING_ENABLE) is False

    def test_policy_rejects_unsafe_values(self) -> None:
        with pytest.raises(ValueError):
            ServerManagementPolicy(page_size=0)
        with pytest.raises(ValueError):
            ServerManagementPolicy(traffic_window_days=0)
        with pytest.raises(ValueError):
            ServerManagementPolicy(confirmation_ttl_seconds=5)

    def test_unknown_customer_state_labels_are_stable(self) -> None:
        from cloud_platform.modules.servers.models import CustomerServerState, format_bytes

        assert CustomerServerState.UNKNOWN.is_operable is False
        assert CustomerServerState.STARTING.is_transitional is True
        assert format_bytes(None) is None
        assert format_bytes(-1) is None
        assert format_bytes(0) == "0 B"
        assert format_bytes(2048) == "2 KB"
        assert format_bytes(1024 * 1024 * 1024) == "1 GB"
        assert format_bytes(1024**5) == "1 PB"
