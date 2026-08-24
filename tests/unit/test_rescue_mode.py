"""Tests for rescue mode (M13-003).

Acceptance: temporary credentials protected.

- Key-first design: arming rescue with the user's REGISTERED SSH key ids
  makes the provider issue NO password at all (password_free=True).
- When a provider issues a temporary root password anyway, it is wrapped
  in OneTimeSecret: str/repr are redacted, reveal() consumes it exactly
  once, and the plaintext NEVER reaches audit events, operation rows or
  any persisted payload.
- Ownership + idempotency follow the platform command conventions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.operations.service import (
    OneTimeSecret,
    RescueActionNotAllowedError,
    RescueCommandService,
    RescueNotOwnerError,
    RescueOperationFailedError,
    RescueOperationInProgressError,
    rescue_operation_key,
)
from cloud_platform.providers.errors import ProviderConflict, ProviderUnavailable

NOW = datetime.now(UTC)
SERVER_ID = uuid4()
USER_A = uuid4()
USER_B = uuid4()


class LocalOpRepo:
    def __init__(self) -> None:
        self.ops: dict[str, Operation] = {}

    async def get_or_create(
        self, *, operation_key, operation_type, resource_type, resource_id, provider_key
    ):
        op = self.ops.get(operation_key)
        if op is None:
            op = Operation(
                id=uuid4(),
                operation_key=operation_key,
                operation_type=operation_type,
                resource_type=resource_type,
                resource_id=resource_id,
                provider_key=provider_key,
            )
            self.ops[operation_key] = op
        return op

    async def get(self, operation_id: UUID) -> Operation | None:
        return next((o for o in self.ops.values() if o.id == operation_id), None)

    async def get_by_key(self, operation_key: str) -> Operation | None:
        return self.ops.get(operation_key)

    async def claim(self, operation_id: UUID) -> Operation | None:
        op = await self.get(operation_id)
        if op is None or op.status is not OperationStatus.PENDING:
            return None
        op.mark_in_flight()
        return op

    async def save(self, operation: Operation) -> Operation:
        return operation


class LocalServerRepo:
    def __init__(self, server: CloudServer) -> None:
        self.servers = {server.id: server}

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.servers.get(server_id)

    async def save(self, server: CloudServer) -> CloudServer:
        self.servers[server.id] = server
        return server


class FakeRegistry:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def get(self, _key: str) -> Any:
        return self._provider


class RescueFakeProvider:
    """Fake provider whose enable_rescue mirrors Hetzner's contract."""

    def __init__(self, *, issued_password: str | None = "temp-root-pass") -> None:
        self.issued_password = issued_password
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.disable_calls: list[str] = []
        self.error: Exception | None = None

    async def enable_rescue(
        self,
        provider_server_id: str,
        *,
        ssh_key_ids: tuple[str, ...] = (),
        rescue_type: str = "linux64",
    ) -> dict[str, Any]:
        self.calls.append((provider_server_id, ssh_key_ids))
        if self.error is not None:
            raise self.error
        # Hetzner semantics: no password when ssh_keys are supplied
        password = None if ssh_key_ids else self.issued_password
        return {"status": "success", "root_password": password}

    async def disable_rescue(self, provider_server_id: str) -> str:
        self.disable_calls.append(provider_server_id)
        if self.error is not None:
            raise self.error
        return "success"


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


def make_world(
    state: ServerLifecycleState = ServerLifecycleState.RUNNING,
    *,
    issued_password: str | None = "temp-root-pass",
):
    server = CloudServer(
        id=SERVER_ID,
        user_id=USER_A,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
        provider_server_id=f"srv-{str(SERVER_ID)[:8]}",
        idempotency_key="ik",
        created_at=NOW,
    )
    ops = LocalOpRepo()
    servers = LocalServerRepo(server)
    provider = RescueFakeProvider(issued_password=issued_password)
    audit = RecordingAudit()
    service = RescueCommandService(
        server_repo=servers,  # type: ignore[arg-type]
        operation_repo=ops,  # type: ignore[arg-type]
        provider_registry=FakeRegistry(provider),  # type: ignore[arg-type]
        audit_repo=audit,  # type: ignore[arg-type]
    )
    return service, ops, servers, provider, audit


class TestCredentialProtection:
    async def test_password_free_when_armed_with_ssh_keys(self) -> None:
        service, _ops, _servers, provider, _audit = make_world(issued_password="SHOULD-NOT-ISSUE")
        result = await service.enable(USER_A, SERVER_ID, "k", ssh_key_ids=("key-1", "key-2"))
        assert result.password_free is True
        assert result.credential is None  # NO password exists at all
        # keys were forwarded to the provider
        assert provider.calls[0][1] == ("key-1", "key-2")

    async def test_issued_password_is_wrapped_and_redacted(self) -> None:
        secret_value = "super-secret-rescue-pass"
        service, _ops, _servers, _provider, _audit = make_world(issued_password=secret_value)
        result = await service.enable(USER_A, SERVER_ID, "k")
        credential = result.credential
        assert credential is not None
        # repr/str NEVER leak the material
        assert secret_value not in repr(credential)
        assert secret_value not in str(credential)
        assert repr(result).find(secret_value) == -1
        # one-time reveal
        assert credential.reveal() == secret_value
        assert credential.reveal() is None

    async def test_plaintext_never_reaches_audit_or_operations(self) -> None:
        secret_value = "super-secret-rescue-pass"
        service, ops, _servers, _provider, audit = make_world(issued_password=secret_value)
        await service.enable(USER_A, SERVER_ID, "k")
        blob = repr(audit.events) + repr(ops.ops) + repr(audit.events[-0:])
        assert secret_value not in blob
        # audit records only flags/counts
        enabled = audit.events[-1]
        assert enabled.metadata["password_free"] == "false"  # password path used...
        assert secret_value not in enabled.reason
        completed = next(o for o in ops.ops.values())
        assert "root_password" not in repr(completed.provider_response)

    async def test_one_time_secret_unit_behavior(self) -> None:
        secret = OneTimeSecret("abc")
        assert bool(secret) is True
        assert "abc" not in f"{secret!r} {secret}"
        assert secret.reveal() == "abc"
        assert secret.reveal() is None
        assert bool(secret) is True  # still an issued secret, just consumed


class TestOwnershipAndGates:
    async def test_foreign_server_reads_as_missing(self) -> None:
        service, ops, _servers, _provider, _audit = make_world()
        with pytest.raises(RescueNotOwnerError, match="not found"):
            await service.enable(USER_B, SERVER_ID, "k")
        with pytest.raises(RescueNotOwnerError, match="not found"):
            await service.disable(USER_B, SERVER_ID, "k")
        assert ops.ops == {}

    async def test_non_steady_state_refused(self) -> None:
        service, ops, _servers, _provider, _audit = make_world(state=ServerLifecycleState.DELETING)
        with pytest.raises(RescueActionNotAllowedError):
            await service.enable(USER_A, SERVER_ID, "k")
        with pytest.raises(RescueActionNotAllowedError):
            await service.disable(USER_A, SERVER_ID, "k")
        assert ops.ops == {}

    async def test_missing_or_long_key_rejected(self) -> None:
        service, _ops, _servers, _provider, _audit = make_world()
        with pytest.raises(Exception, match="idempotency_key is required"):
            await service.enable(USER_A, SERVER_ID, "")
        from cloud_platform.modules.operations.service import RescueCommandError

        with pytest.raises(RescueCommandError, match="too long"):
            await service.enable(USER_A, SERVER_ID, "x" * 129)


class TestIdempotencyAndErrors:
    async def test_completed_enable_replays_without_provider_call(self) -> None:
        service, _ops, _servers, provider, _audit = make_world(issued_password=None)
        first = await service.enable(USER_A, SERVER_ID, "k", ssh_key_ids=("key-1",))
        calls_after_first = len(provider.calls)
        second = await service.enable(USER_A, SERVER_ID, "k", ssh_key_ids=("key-1",))
        assert second.replayed is True and first.replayed is False
        assert len(provider.calls) == calls_after_first

    async def test_retryable_error_requeues_same_key(self) -> None:
        service, ops, _servers, provider, _audit = make_world()
        provider.error = ProviderUnavailable("503")
        result = await service.enable(USER_A, SERVER_ID, "k")
        assert result.requeued is True
        op = ops.ops[rescue_operation_key("enable", SERVER_ID, "k")]
        assert op.status is OperationStatus.PENDING

    async def test_permanent_error_fails_and_surfaces_on_replay(self) -> None:
        service, ops, _servers, provider, _audit = make_world()
        provider.error = ProviderConflict("rescue unavailable")
        with pytest.raises(RescueOperationFailedError):
            await service.enable(USER_A, SERVER_ID, "k")
        assert (
            ops.ops[rescue_operation_key("enable", SERVER_ID, "k")].status is OperationStatus.FAILED
        )
        with pytest.raises(RescueOperationFailedError):
            await service.enable(USER_A, SERVER_ID, "k")

    async def test_disable_flow_and_separate_key_space(self) -> None:
        service, ops, _servers, provider, _audit = make_world()
        await service.enable(USER_A, SERVER_ID, "e1", ssh_key_ids=("key-1",))
        result = await service.disable(USER_A, SERVER_ID, "d1")
        assert result.replayed is False and result.requeued is False
        assert len(provider.disable_calls) == 1
        assert rescue_operation_key("enable", SERVER_ID, "e1") in ops.ops
        assert rescue_operation_key("disable", SERVER_ID, "d1") in ops.ops

    async def test_in_progress_second_command_rejected(self) -> None:
        service, ops, servers, provider, _audit = make_world()
        # claim an intent manually so it stays IN_FLIGHT
        op = await ops.get_or_create(
            operation_key=rescue_operation_key("enable", SERVER_ID, "k"),
            operation_type=OperationType.RESCUE_ENABLE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        claimed = await ops.claim(op.id)
        assert claimed is not None
        with pytest.raises(RescueOperationInProgressError):
            await service.enable(USER_A, SERVER_ID, "k")
        _ = servers, provider


class TestAdapterMapping:
    async def test_hetzner_endpoints(self) -> None:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        class Transport:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

            async def request(self, method: str, path: str, **kwargs: Any):
                body = kwargs.get("json")
                self.calls.append((method, path, body))
                if path.endswith("/enable_rescue"):
                    if body and body.get("ssh_keys"):
                        return _Resp(201, {"action": {"status": "success"}})
                    return _Resp(201, {"action": {"status": "success"}, "root_password": "pw"})
                if path.endswith("/disable_rescue"):
                    return _Resp(201, {"action": {"status": "success"}})
                raise AssertionError(path)

        from tests.unit.test_ssh_keys import _Resp

        transport = Transport()
        provider = HetznerCloudProvider(token="t")
        provider._client = transport  # type: ignore[assignment]

        key_only = await provider.enable_rescue("srv-1", ssh_key_ids=("k1",))
        assert key_only == {"status": "success", "root_password": None}
        password_path = await provider.enable_rescue("srv-1")
        assert password_path["root_password"] == "pw"
        assert await provider.disable_rescue("srv-1") == "success"
        paths = [c[1] for c in transport.calls]
        assert "/servers/srv-1/actions/enable_rescue" in paths
        assert "/servers/srv-1/actions/disable_rescue" in paths
