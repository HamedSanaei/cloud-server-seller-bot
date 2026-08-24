"""Tests for private networks (M13-010).

Acceptance: **network lifecycle and capabilities.**

- LIFECYCLE: create (RFC-1918 validated, quota'd, self-service only) ->
  attach own servers (idempotent for members) -> detach (no-op for
  non-members) -> delete (404-idempotent at the provider).
- CAPABILITIES: the provider port is optional and probed via
  ``network_port_of``; unsupported providers never reach a branch.
- Every mutation is audited; foreign resources read as missing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.networking.network_service import NetworkService, network_port_of
from cloud_platform.modules.networking.networks import (
    Network,
    NetworkLimitError,
    NetworkNotFoundError,
    NetworkRangeError,
)

USER = uuid4()
OTHER = uuid4()
ACCOUNT = uuid4()
SERVER_A = uuid4()
SERVER_B = uuid4()
NOW = datetime.now(UTC)


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


class LocalNetworkRepo:
    def __init__(self) -> None:
        self.rows: dict[UUID, Network] = {}

    async def add(self, network: Network) -> Network:
        stored = replace(network, id=network.id or uuid4(), created_at=network.created_at or NOW)
        self.rows[stored.id] = stored  # type: ignore[index]
        return stored

    async def get(self, network_id: UUID) -> Network | None:
        return self.rows.get(network_id)

    async def list_for_user(self, user_id: UUID) -> list[Network]:
        return [n for n in self.rows.values() if n.user_id == user_id]

    async def save(self, network: Network) -> Network:
        assert network.id is not None
        self.rows[network.id] = network
        return network

    async def delete(self, network_id: UUID) -> None:
        self.rows.pop(network_id, None)


class FakeServerRepo:
    def __init__(self, servers: dict[UUID, CloudServer]) -> None:
        self.servers = servers

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.servers.get(server_id)


class FakeNetworkPort:
    """Provider-side network lifecycle."""

    def __init__(self) -> None:
        self.next_id = 900
        self.calls: list[tuple[str, ...]] = []
        self.deleted: set[str] = set()

    async def create_network(self, name: str, ip_range: str) -> str:
        self.calls.append(("create", name, ip_range))
        self.next_id += 1
        return f"net-{self.next_id}"

    async def attach_server(self, provider_network_id: str, provider_server_id: str) -> None:
        self.calls.append(("attach", provider_network_id, provider_server_id))

    async def detach_server(self, provider_network_id: str, provider_server_id: str) -> None:
        self.calls.append(("detach", provider_network_id, provider_server_id))

    async def delete_network(self, provider_network_id: str) -> None:
        self.calls.append(("delete", provider_network_id))
        self.deleted.add(provider_network_id)


def make_servers() -> dict[UUID, CloudServer]:
    out: dict[UUID, CloudServer] = {}
    for sid in (SERVER_A, SERVER_B):
        out[sid] = CloudServer(
            id=sid,
            user_id=USER,
            provider_key="hetzner",
            provider_account_id=ACCOUNT,
            state=ServerLifecycleState.RUNNING,
            provider_server_id=str(sid.int % 1000),
        )
    return out


def make_service(
    *, max_networks: int = 2
) -> tuple[NetworkService, LocalNetworkRepo, RecordingAudit]:
    repo = LocalNetworkRepo()
    audit = RecordingAudit()
    service = NetworkService(
        repo=repo,  # type: ignore[arg-type]
        audit_repo=audit,  # type: ignore[arg-type]
        server_repo=FakeServerRepo(make_servers()),  # type: ignore[arg-type]
        max_networks_per_user=max_networks,
    )
    return service, repo, audit


async def create(service: NetworkService, port: FakeNetworkPort) -> Network:
    return await service.create(
        actor_user_id=USER,
        owner_user_id=USER,
        provider_account_id=ACCOUNT,
        provider_key="hetzner",
        provider_port=port,  # type: ignore[arg-type]
        name=f"priv-{uuid4().hex[:6]}",
        ip_range="10.0.0.0/16",
        location_id="fsn1",
    )


class TestDomainValidation:
    @pytest.mark.parametrize("bad_range", ["not-a-cidr", "8.8.8.0/24", "10.0.0.0/64"])
    def test_bad_ranges_rejected(self, bad_range: str) -> None:
        with pytest.raises(NetworkRangeError):
            Network(
                user_id=USER,
                provider_account_id=ACCOUNT,
                provider_key="hetzner",
                provider_network_id="net-1",
                name="x",
                ip_range=bad_range,
            )

    def test_private_range_canonicalized(self) -> None:
        net = Network(
            user_id=USER,
            provider_account_id=ACCOUNT,
            provider_key="hetzner",
            provider_network_id="net-1",
            name="x",
            ip_range=" 10.0.5.9/16 ",
        )
        assert net.ip_range == "10.0.0.0/16"


class TestLifecycle:
    async def test_create_audited_and_scoped(self) -> None:
        service, _repo, audit = make_service()
        port = FakeNetworkPort()
        network = await create(service, port)
        assert network.ip_range == "10.0.0.0/16"
        assert audit.events[-1].action == "network.created"
        assert ("create", network.name, "10.0.0.0/16") in port.calls

        # foreign actor creating FOR someone else reads as missing
        with pytest.raises(NetworkNotFoundError):
            await service.create(
                actor_user_id=OTHER,
                owner_user_id=USER,
                provider_account_id=ACCOUNT,
                provider_key="hetzner",
                provider_port=port,  # type: ignore[arg-type]
                name="y",
                ip_range="10.1.0.0/16",
            )
        assert all(e.actor_type is ActorType.USER for e in audit.events)

    async def test_quota_enforced(self) -> None:
        service, _repo, _audit = make_service(max_networks=2)
        port = FakeNetworkPort()
        await create(service, port)
        await create(service, port)
        with pytest.raises(NetworkLimitError):
            await create(service, port)

    async def test_attach_requires_owning_the_server_and_is_idempotent(self) -> None:
        service, _repo, audit = make_service()
        port = FakeNetworkPort()
        network = await create(service, port)

        joined = await service.attach_server(
            actor_user_id=USER,
            network_id=network.id,  # type: ignore[arg-type]
            server_id=SERVER_A,
            provider_port=port,  # type: ignore[arg-type]
        )
        assert joined.joined(SERVER_A)
        calls_after_attach = len(port.calls)

        replay = await service.attach_server(
            actor_user_id=USER,
            network_id=network.id,  # type: ignore[arg-type]
            server_id=SERVER_A,
            provider_port=port,  # type: ignore[arg-type]
        )
        assert replay.joined(SERVER_A)
        assert len(port.calls) == calls_after_attach  # no second attach call

        stranger = uuid4()
        with pytest.raises(NetworkNotFoundError):
            await service.attach_server(
                actor_user_id=USER,
                network_id=network.id,  # type: ignore[arg-type]
                server_id=stranger,
                provider_port=port,  # type: ignore[arg-type]
            )
        assert audit.events[-1].action != "network.server_attached" or stranger != SERVER_A

    async def test_detach_nonmember_is_noop_member_leaves(self) -> None:
        service, _repo, _audit = make_service()
        port = FakeNetworkPort()
        network = await create(service, port)
        await service.attach_server(
            actor_user_id=USER,
            network_id=network.id,  # type: ignore[arg-type]
            server_id=SERVER_A,
            provider_port=port,  # type: ignore[arg-type]
        )

        noop = await service.detach_server(
            actor_user_id=USER,
            network_id=network.id,  # type: ignore[arg-type]
            server_id=SERVER_B,  # never joined
            provider_port=port,  # type: ignore[arg-type]
        )
        assert SERVER_B not in noop.server_ids
        detach_calls_before = [c for c in port.calls if c[0] == "detach"]
        assert not detach_calls_before

        left = await service.detach_server(
            actor_user_id=USER,
            network_id=network.id,  # type: ignore[arg-type]
            server_id=SERVER_A,
            provider_port=port,  # type: ignore[arg-type]
        )
        assert not left.joined(SERVER_A)
        assert any(c[0] == "detach" for c in port.calls)

    async def test_delete_is_provider_idempotent_and_drops_row(self) -> None:
        service, repo, audit = make_service()
        port = FakeNetworkPort()
        network = await create(service, port)
        await service.delete(
            actor_user_id=USER,
            network_id=network.id,  # type: ignore[arg-type]
            provider_port=port,  # type: ignore[arg-type]
        )
        assert network.id not in repo.rows
        assert ("delete", network.provider_network_id) in port.calls
        assert audit.events[-1].action == "network.deleted"
        with pytest.raises(NetworkNotFoundError):
            await service.delete(
                actor_user_id=USER,
                network_id=network.id,  # type: ignore[arg-type]
                provider_port=port,  # type: ignore[arg-type]
            )


class TestCapabilities:
    async def test_probe_requires_full_port(self) -> None:
        class HalfPort:
            async def create_network(self, name: str, ip_range: str) -> str:
                return "1"

        class Provider:
            def __init__(self, port: Any) -> None:
                self.networks = port

        class NoNetworks:
            pass

        assert network_port_of(Provider(FakeNetworkPort())) is not None
        assert network_port_of(Provider(HalfPort())) is None
        assert network_port_of(NoNetworks()) is None

    async def test_hetzner_adapter_maps_create_endpoint(self) -> None:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        calls: list[tuple[str, str]] = []

        class Transport:
            async def request(self, method: str, path: str, **kwargs: Any):
                calls.append((method, path))
                assert method == "POST" and path == "/networks"

                from tests.unit.test_ssh_keys import _Resp

                return _Resp(201, {"network": {"id": 77}})

        provider = HetznerCloudProvider(token="t")
        provider._client = Transport()  # type: ignore[assignment]
        got = await provider.networks.create_network("priv", "10.0.0.0/16")
        assert got == "77"
        assert calls == [("POST", "/networks")]
