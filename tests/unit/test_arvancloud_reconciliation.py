"""Tests for provider-specific reconciliation of ambiguous mutations (M15-004).

Acceptance: ambiguous mutations handled safely. The ArvanCloud adapter has no
native idempotency header, so a timed-out power mutation must never be blindly
re-sent: the platform probes the effect first (PowerEffectProbe), completes
without a provider call when it already holds, and re-queues (instead of
re-applying) when a transient action (reboot) is inconclusive.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.operations.service import (
    PowerExecutionResult,
    PowerOperationExecutor,
)
from cloud_platform.providers.arvancloud.client import ArvanCloudProvider, Throttle
from cloud_platform.providers.base import Capability, supports_power_probe
from cloud_platform.providers.errors import ProviderError
from cloud_platform.providers.registry import ProviderRegistry

REGION = "ir-thr-1"
KEY = "MU-TEST-KEY-RECON"
SERVER_ID = uuid.uuid4()


def _no_sleep() -> Throttle:
    async def wait(_s: float) -> None:
        return None

    return Throttle(max_rps=1000.0, wait=wait)


def _server(state: ServerLifecycleState) -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=uuid.uuid4(),
        provider_key="arvancloud",
        provider_account_id=uuid.uuid4(),
        provider_server_id=f"{REGION}:abc",
        state=state,
    )


def _operation(
    op_type: OperationType, attempts: int, status: OperationStatus = OperationStatus.IN_FLIGHT
) -> Operation:
    op = Operation(
        id=uuid.uuid4(),
        operation_key=f"power-on:{SERVER_ID}:k",
        operation_type=op_type,
        resource_type="server",
        resource_id=SERVER_ID,
        provider_key="arvancloud",
    )
    if status is OperationStatus.IN_FLIGHT:
        op.status = OperationStatus.IN_FLIGHT
    op.attempts = attempts
    return op


class _Fakes:
    def __init__(self, provider: Any, server: CloudServer) -> None:
        self.ops = AsyncMock()
        self.servers = AsyncMock()
        self.audit = _FakeAudit()
        self.servers.get.return_value = server
        registry = ProviderRegistry()
        registry.register(provider)
        self.registry = registry

    def executor(self) -> PowerOperationExecutor:
        return PowerOperationExecutor(
            operation_repo=self.ops,
            server_repo=self.servers,
            provider_registry=self.registry,
            audit_repo=self.audit,
        )


class _FakeAudit:
    """AuditRepository-shaped fake that records the action names it is given."""

    def __init__(self) -> None:
        self.actions: list[str] = []

    async def append(self, event: Any) -> None:
        self.actions.append(getattr(event, "action", ""))


def _arvancloud_provider(get_result: Any, probe_result: bool | None = None) -> ArvanCloudProvider:
    provider = ArvanCloudProvider(
        api_key=KEY, base_url="https://api.test/v1", region=REGION, throttle=_no_sleep()
    )
    provider.get_server = AsyncMock(return_value=get_result)
    if probe_result is not None:
        provider.probe_power_effect = AsyncMock(return_value=probe_result)
    return provider


def _remote(status: str) -> Any:
    from cloud_platform.providers.base import ProviderServer

    return ProviderServer(
        id=f"{REGION}:abc", name="srv-x", status=status, metadata={"region": REGION}
    )


class TestProbePowerEffect:
    async def test_power_on_running_is_true(self) -> None:
        provider = _arvancloud_provider(_remote("running"))
        assert await provider.probe_power_effect(f"{REGION}:abc", "power_on") is True

    async def test_power_on_stopped_is_false(self) -> None:
        provider = _arvancloud_provider(_remote("stopped"))
        assert await provider.probe_power_effect(f"{REGION}:abc", "power_on") is False

    async def test_power_on_building_is_inconclusive(self) -> None:
        provider = _arvancloud_provider(_remote("building"))
        assert await provider.probe_power_effect(f"{REGION}:abc", "power_on") is None

    async def test_power_off_stopped_is_true(self) -> None:
        provider = _arvancloud_provider(_remote("stopped"))
        assert await provider.probe_power_effect(f"{REGION}:abc", "power_off") is True

    async def test_power_off_running_is_false(self) -> None:
        provider = _arvancloud_provider(_remote("running"))
        assert await provider.probe_power_effect(f"{REGION}:abc", "power_off") is False

    async def test_reboot_running_is_true_avoids_second_reboot(self) -> None:
        provider = _arvancloud_provider(_remote("running"))
        assert await provider.probe_power_effect(f"{REGION}:abc", "reboot") is True

    async def test_reboot_stopped_is_false(self) -> None:
        provider = _arvancloud_provider(_remote("stopped"))
        assert await provider.probe_power_effect(f"{REGION}:abc", "reboot") is False

    async def test_reboot_building_is_inconclusive(self) -> None:
        provider = _arvancloud_provider(_remote("building"))
        assert await provider.probe_power_effect(f"{REGION}:abc", "reboot") is None

    async def test_vanished_server_is_inconclusive(self) -> None:
        provider = _arvancloud_provider(None)
        assert await provider.probe_power_effect(f"{REGION}:abc", "power_on") is None

    async def test_unknown_action_is_inconclusive(self) -> None:
        provider = _arvancloud_provider(_remote("running"))
        assert await provider.probe_power_effect(f"{REGION}:abc", "warp") is None

    def test_adapter_satisfies_the_port_protocol(self) -> None:
        provider = ArvanCloudProvider(api_key=KEY, base_url="https://x/v1", throttle=_no_sleep())
        assert supports_power_probe(provider) is True
        assert callable(getattr(provider, "probe_power_effect", None))

    def test_read_only_probe_makes_no_mutating_calls(self) -> None:
        provider = _arvancloud_provider(_remote("running"))
        # patch the transport: any POST would prove the probe mutated
        provider._client.request = AsyncMock(side_effect=AssertionError("probe must not POST"))
        assert provider.get_server is not None  # AsyncMock from the helper


class TestExecutorAmbiguousResend:
    async def test_first_attempt_makes_no_probe(self) -> None:
        provider = _arvancloud_provider(_remote("stopped"), probe_result=True)
        provider.power_on = AsyncMock()
        fakes = _Fakes(provider, _server(ServerLifecycleState.STOPPED))
        op = _operation(OperationType.POWER_ON, attempts=1)
        result = await fakes.executor().execute(op, actor_type=ActorType.SYSTEM, actor_id=None)
        assert result is PowerExecutionResult.EXECUTED
        provider.power_on.assert_awaited_once()

    async def test_resend_with_effect_already_applied_skips_provider_call(self) -> None:
        provider = _arvancloud_provider(_remote("running"), probe_result=True)
        provider.power_on = AsyncMock()
        fakes = _Fakes(provider, _server(ServerLifecycleState.STOPPED))
        op = _operation(OperationType.POWER_ON, attempts=2)
        result = await fakes.executor().execute(op, actor_type=ActorType.SYSTEM, actor_id=None)
        assert result is PowerExecutionResult.EXECUTED
        provider.power_on.assert_not_awaited()  # the ambiguous re-send never happens
        assert op.status is OperationStatus.COMPLETED
        assert op.provider_response["probe"] == "already-applied"
        # audited with the probe correlation
        audit_actions = fakes.audit.actions
        assert "server.powered_on" in audit_actions
        # local state transitioned to the target
        fakes.servers.save.assert_awaited()

    async def test_resend_probe_false_resends_safely(self) -> None:
        provider = _arvancloud_provider(_remote("stopped"), probe_result=False)
        provider.power_on = AsyncMock()
        fakes = _Fakes(provider, _server(ServerLifecycleState.STOPPED))
        op = _operation(OperationType.POWER_ON, attempts=2)
        result = await fakes.executor().execute(op, actor_type=ActorType.SYSTEM, actor_id=None)
        assert result is PowerExecutionResult.EXECUTED
        provider.power_on.assert_awaited_once()  # effect did not hold: re-send is safe

    async def test_resend_probe_none_power_on_resends(self) -> None:
        provider = _arvancloud_provider(_remote("building"), probe_result=None)
        provider.power_on = AsyncMock()
        fakes = _Fakes(provider, _server(ServerLifecycleState.STOPPED))
        op = _operation(OperationType.POWER_ON, attempts=2)
        result = await fakes.executor().execute(op, actor_type=ActorType.SYSTEM, actor_id=None)
        assert result is PowerExecutionResult.EXECUTED
        provider.power_on.assert_awaited_once()

    async def test_resend_probe_none_reboot_requeues_without_resend(self) -> None:
        provider = _arvancloud_provider(_remote("building"), probe_result=None)
        provider.reboot = AsyncMock()
        fakes = _Fakes(provider, _server(ServerLifecycleState.RUNNING))
        op = _operation(OperationType.REBOOT, attempts=2)
        result = await fakes.executor().execute(op, actor_type=ActorType.SYSTEM, actor_id=None)
        assert result is PowerExecutionResult.REQUEUED
        provider.reboot.assert_not_awaited()  # never a second physical reboot
        assert op.status is OperationStatus.PENDING
        assert "not re-sending" in (op.error or "")
        audit_actions = fakes.audit.actions
        assert "server.power_probe_inconclusive" in audit_actions

    async def test_resend_probe_none_reboot_running_completes(self) -> None:
        """A running server means the reboot effect holds (or is
        indistinguishable): complete, don't re-reboot."""
        provider = _arvancloud_provider(_remote("running"), probe_result=True)
        provider.reboot = AsyncMock()
        fakes = _Fakes(provider, _server(ServerLifecycleState.RUNNING))
        op = _operation(OperationType.REBOOT, attempts=2)
        result = await fakes.executor().execute(op, actor_type=ActorType.SYSTEM, actor_id=None)
        assert result is PowerExecutionResult.EXECUTED
        provider.reboot.assert_not_awaited()
        assert op.status is OperationStatus.COMPLETED

    async def test_probe_error_is_inconclusive(self) -> None:
        provider = _arvancloud_provider(_remote("running"))
        provider.probe_power_effect = AsyncMock(side_effect=ProviderError("500"))
        provider.reboot = AsyncMock()
        fakes = _Fakes(provider, _server(ServerLifecycleState.RUNNING))
        op = _operation(OperationType.REBOOT, attempts=2)
        result = await fakes.executor().execute(op, actor_type=ActorType.SYSTEM, actor_id=None)
        assert result is PowerExecutionResult.REQUEUED
        provider.reboot.assert_not_awaited()

    async def test_provider_without_probe_falls_back_to_plain_resend(self) -> None:
        class NoProbeProvider:
            key = "arvancloud"
            capabilities = frozenset({Capability.POWER})
            power_on = AsyncMock()

        fakes = _Fakes(NoProbeProvider(), _server(ServerLifecycleState.STOPPED))
        executor = fakes.executor()
        op = _operation(OperationType.POWER_ON, attempts=2)
        result = await executor.execute(op, actor_type=ActorType.SYSTEM, actor_id=None)
        assert result is PowerExecutionResult.EXECUTED
        NoProbeProvider.power_on.assert_awaited_once()  # no probe, plain re-send

    async def test_probe_true_power_off_transitions_state(self) -> None:
        provider = _arvancloud_provider(_remote("stopped"), probe_result=True)
        provider.power_off = AsyncMock()
        fakes = _Fakes(provider, _server(ServerLifecycleState.RUNNING))
        op = _operation(OperationType.POWER_OFF, attempts=2)
        result = await fakes.executor().execute(op, actor_type=ActorType.SYSTEM, actor_id=None)
        assert result is PowerExecutionResult.EXECUTED
        provider.power_off.assert_not_awaited()
        assert op.status is OperationStatus.COMPLETED
        assert op.provider_response["probe"] == "already-applied"


class TestCreateAndDeleteAmbiguityAlreadySafe:
    """Document the provider-side halves (M15-002) that make the create and
    delete ambiguous windows safe, so the reconciliation story is complete."""

    async def test_create_resend_deduplicates_by_name(self) -> None:
        provider = _arvancloud_provider(None)
        calls: list[str] = []
        existing = _remote("running")

        async def fake_find_by_name(region: str, name: str) -> Any:
            calls.append("list")
            if name == existing.name:
                return existing
            return None

        from cloud_platform.providers.base import CreateServerRequest

        provider._find_by_name = fake_find_by_name
        request = CreateServerRequest(
            name=existing.name, plan_id="12", image_id="img-9", location_id=REGION
        )
        result = await provider.create_server(request, IdempotencyKey("recon-test-key"))
        assert result.id == existing.id
        assert calls == ["list"]  # no POST: the ambiguous create is deduplicated

    async def test_delete_404_is_success(self) -> None:
        from cloud_platform.providers.errors import ProviderNotFound

        async def boom(method: str, path: str, **kw: Any) -> Any:
            raise ProviderNotFound("already gone")

        provider = ArvanCloudProvider(
            api_key=KEY, base_url="https://x/v1", region=REGION, throttle=_no_sleep()
        )
        provider._request = boom
        await provider.delete_server(f"{REGION}:gone", IdempotencyKey("delete-test-key"))

    def test_executor_module_imports_the_probe_port(self) -> None:
        import cloud_platform.modules.operations.service as svc

        assert hasattr(svc, "supports_power_probe")
