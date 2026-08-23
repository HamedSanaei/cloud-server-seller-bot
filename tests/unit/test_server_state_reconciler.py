"""Tests for ServerStateReconciler (M07-005).

Acceptance: drift maps to a safe state or manual review.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.operations.domain import (
    ProviderServerState,
    StateAction,
    normalize_provider_status,
    plan_state_repair,
)
from cloud_platform.modules.operations.service import (
    ServerStateReconciler,
    StateReconciliationOutcome,
)
from cloud_platform.providers.base import (
    Capability,
    ProviderServer,
)
from cloud_platform.providers.errors import ProviderUnavailable
from cloud_platform.providers.registry import ProviderRegistry

USER_ID = uuid4()


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("running", ProviderServerState.RUNNING),
            ("Running", ProviderServerState.RUNNING),
            ("shutdown", ProviderServerState.STOPPED),
            ("stopped", ProviderServerState.STOPPED),
            ("creating", ProviderServerState.CREATING),
            ("initial", ProviderServerState.CREATING),
            ("pending", ProviderServerState.CREATING),
            ("deleting", ProviderServerState.DELETING),
            ("terminating", ProviderServerState.DELETING),
            ("weird-status", ProviderServerState.UNKNOWN),
            (None, ProviderServerState.UNKNOWN),
            ("", ProviderServerState.UNKNOWN),
            ("   ", ProviderServerState.UNKNOWN),
        ],
    )
    def test_normalization(self, raw: str | None, expected: ProviderServerState) -> None:
        assert normalize_provider_status(raw) is expected


class TestPlan:
    @pytest.mark.parametrize(
        ("local", "remote", "action", "target"),
        [
            # consistent
            (ServerLifecycleState.RUNNING, ProviderServerState.RUNNING, StateAction.NONE, None),
            (ServerLifecycleState.STOPPED, ProviderServerState.STOPPED, StateAction.NONE, None),
            # in progress
            (
                ServerLifecycleState.PROVISIONING,
                ProviderServerState.CREATING,
                StateAction.IN_PROGRESS,
                None,
            ),
            # repairs
            (
                ServerLifecycleState.PROVISIONING,
                ProviderServerState.RUNNING,
                StateAction.REPAIR,
                ServerLifecycleState.RUNNING,
            ),
            (
                ServerLifecycleState.RUNNING,
                ProviderServerState.STOPPED,
                StateAction.REPAIR,
                ServerLifecycleState.STOPPED,
            ),
            (
                ServerLifecycleState.STOPPED,
                ProviderServerState.RUNNING,
                StateAction.REPAIR,
                ServerLifecycleState.RUNNING,
            ),
            # contained
            (ServerLifecycleState.RUNNING, ProviderServerState.DELETING, StateAction.CONTAIN, None),
            (ServerLifecycleState.STOPPED, ProviderServerState.DELETING, StateAction.CONTAIN, None),
            (
                ServerLifecycleState.PROVISIONING,
                ProviderServerState.DELETING,
                StateAction.CONTAIN,
                None,
            ),
            (ServerLifecycleState.RUNNING, ProviderServerState.UNKNOWN, StateAction.CONTAIN, None),
            (
                ServerLifecycleState.RUNNING,
                ProviderServerState.NOT_FOUND,
                StateAction.CONTAIN,
                None,
            ),
            (
                ServerLifecycleState.PROVISIONING,
                ProviderServerState.STOPPED,
                StateAction.CONTAIN,
                None,
            ),
            # out-of-scope local states
            (ServerLifecycleState.DELETING, ProviderServerState.RUNNING, StateAction.NONE, None),
        ],
    )
    def test_plan_table(
        self,
        local: ServerLifecycleState,
        remote: ProviderServerState,
        action: StateAction,
        target: ServerLifecycleState | None,
    ) -> None:
        plan = plan_state_repair(local, remote)
        assert plan.action is action
        assert plan.target is target


class FakeServerRepo:
    def __init__(self, servers: list[CloudServer]) -> None:
        self.servers = {s.id: s for s in servers}
        self.saved: list[CloudServer] = []

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.servers.get(server_id)

    async def list_requested(self) -> list[CloudServer]:
        return []

    async def list_provisioning(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.PROVISIONING]

    async def list_running(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.RUNNING]

    async def list_stopped(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.STOPPED]

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server


class FakeProvider:
    key = "hetzner"
    capabilities = frozenset({Capability.COMPUTE})

    def __init__(self, status: str | None = "running", remote: bool = True) -> None:
        self.status = status
        self.remote = remote
        self.get_error: Exception | None = None
        self.calls: list[str] = []

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        self.calls.append(provider_server_id)
        if self.get_error is not None:
            raise self.get_error
        if not self.remote:
            return None
        return ProviderServer(id=provider_server_id, name="srv-x", status=self.status or "running")


def _server(
    state: ServerLifecycleState,
    provider_server_id: str | None = "prov-1",
    provider_key: str = "hetzner",
) -> CloudServer:
    return CloudServer(
        id=uuid4(),
        user_id=USER_ID,
        provider_key=provider_key,
        provider_account_id=uuid4(),
        state=state,
        provider_server_id=provider_server_id,
    )


class _Deps:
    def __init__(
        self,
        servers: list[CloudServer],
        provider: FakeProvider | None = None,
        register: bool = True,
    ) -> None:
        self.server_repo = FakeServerRepo(servers)
        self.registry = ProviderRegistry()
        if provider is None:
            provider = FakeProvider()
        if register:
            self.registry.register(provider)  # type: ignore[arg-type]
        self.audit = AsyncMock()
        self.audit.append = AsyncMock(side_effect=lambda e: e)
        self.reconciler = ServerStateReconciler(
            server_repo=self.server_repo,  # type: ignore[arg-type]
            provider_registry=self.registry,
            audit_repo=self.audit,  # type: ignore[arg-type]
        )

    def audit_events(self) -> list:
        return [c.args[0] for c in self.audit.append.call_args_list]


class TestReconciler:
    async def test_running_matches_provider(self) -> None:
        server = _server(ServerLifecycleState.RUNNING)
        deps = _Deps([server], FakeProvider("running"))

        counts = await deps.reconciler.reconcile()

        assert counts == {StateReconciliationOutcome.CONSISTENT: 1}
        assert deps.server_repo.saved == []
        assert deps.audit_events() == []

    async def test_running_repaired_to_stopped(self) -> None:
        server = _server(ServerLifecycleState.RUNNING)
        deps = _Deps([server], FakeProvider("shutdown"))

        counts = await deps.reconciler.reconcile()

        assert counts == {StateReconciliationOutcome.REPAIRED: 1}
        assert server.state is ServerLifecycleState.STOPPED
        assert len(deps.server_repo.saved) == 1
        event = deps.audit_events()[0]
        assert event.action == "server.state_reconciled"
        assert event.actor_type.value == "system"
        assert event.metadata["from"] == "running"
        assert event.metadata["to"] == "stopped"

    async def test_stopped_repaired_to_running(self) -> None:
        server = _server(ServerLifecycleState.STOPPED)
        deps = _Deps([server], FakeProvider("running"))

        counts = await deps.reconciler.reconcile()

        assert counts == {StateReconciliationOutcome.REPAIRED: 1}
        assert server.state is ServerLifecycleState.RUNNING
        assert deps.audit_events()[0].action == "server.state_reconciled"

    async def test_provisioning_in_progress(self) -> None:
        server = _server(ServerLifecycleState.PROVISIONING)
        deps = _Deps([server], FakeProvider("creating"))

        counts = await deps.reconciler.reconcile()

        assert counts == {StateReconciliationOutcome.IN_PROGRESS: 1}
        assert server.state is ServerLifecycleState.PROVISIONING
        assert deps.server_repo.saved == []

    async def test_provisioning_finished_repaired(self) -> None:
        server = _server(ServerLifecycleState.PROVISIONING)
        deps = _Deps([server], FakeProvider("running"))

        counts = await deps.reconciler.reconcile()

        assert counts == {StateReconciliationOutcome.REPAIRED: 1}
        assert server.state is ServerLifecycleState.RUNNING

    async def test_provider_deleting_is_contained(self) -> None:
        server = _server(ServerLifecycleState.RUNNING)
        deps = _Deps([server], FakeProvider("deleting"))

        counts = await deps.reconciler.reconcile()

        assert counts == {StateReconciliationOutcome.CONTAINED: 1}
        assert server.state is ServerLifecycleState.MANUAL_REVIEW
        assert server.contained_from is ServerLifecycleState.RUNNING
        event = deps.audit_events()[0]
        assert event.action == "server.state_review"
        assert "unexpected provider state deleting" in event.reason

    async def test_vanished_resource_is_contained_not_deleted(self) -> None:
        server = _server(ServerLifecycleState.RUNNING)
        deps = _Deps([server], FakeProvider(remote=False))

        counts = await deps.reconciler.reconcile()

        # contained for a human - never auto-deleted or marked DELETED
        assert counts == {StateReconciliationOutcome.CONTAINED: 1}
        assert server.state is ServerLifecycleState.MANUAL_REVIEW
        assert "no longer found" in deps.audit_events()[0].reason

    async def test_unknown_provider_status_is_contained(self) -> None:
        server = _server(ServerLifecycleState.RUNNING)
        deps = _Deps([server], FakeProvider("frobnicating"))

        counts = await deps.reconciler.reconcile()

        assert counts == {StateReconciliationOutcome.CONTAINED: 1}
        assert server.state is ServerLifecycleState.MANUAL_REVIEW

    async def test_provider_error_is_inconclusive(self) -> None:
        server = _server(ServerLifecycleState.RUNNING)
        provider = FakeProvider("running")
        provider.get_error = ProviderUnavailable("503")
        deps = _Deps([server], provider)

        counts = await deps.reconciler.reconcile()

        assert counts == {StateReconciliationOutcome.INCONCLUSIVE: 1}
        assert server.state is ServerLifecycleState.RUNNING
        assert deps.server_repo.saved == []

    async def test_no_provider_id_is_skipped(self) -> None:
        server = _server(ServerLifecycleState.RUNNING, provider_server_id=None)
        provider = FakeProvider("running")
        deps = _Deps([server], provider)

        counts = await deps.reconciler.reconcile()

        assert counts == {StateReconciliationOutcome.SKIPPED: 1}
        assert provider.calls == []

    async def test_unknown_provider_is_skipped(self) -> None:
        server = _server(ServerLifecycleState.RUNNING, provider_key="unknown")
        deps = _Deps([server], FakeProvider("running"), register=False)

        counts = await deps.reconciler.reconcile()

        assert counts == {StateReconciliationOutcome.SKIPPED: 1}

    async def test_deletion_flow_is_out_of_scope(self) -> None:
        # DELETE_REQUESTED / DELETING servers are owned by the deletion flow
        deleting = _server(ServerLifecycleState.DELETING)
        provider = FakeProvider("deleting")
        deps = _Deps([deleting], provider)

        counts = await deps.reconciler.reconcile()

        assert counts == {}  # not scanned at all
        assert provider.calls == []
        assert deleting.state is ServerLifecycleState.DELETING

    async def test_mixed_batch_counts(self) -> None:
        servers = [
            _server(ServerLifecycleState.RUNNING),  # consistent (provider running)
            _server(ServerLifecycleState.STOPPED),  # repaired (provider running)
        ]
        deps = _Deps(servers, FakeProvider("running"))

        counts = await deps.reconciler.reconcile()

        assert counts == {
            StateReconciliationOutcome.CONSISTENT: 1,
            StateReconciliationOutcome.REPAIRED: 1,
        }
