"""Tests for reverse DNS management (M13-007).

Acceptance: **IP ownership and validation.**

- PTR hostnames and IPs are strictly validated before any provider call.
- Ownership is proven twice: the caller owns the platform server AND the
  provider reports the IP as assigned to THAT server.
- Unsupported providers are refused; supported flows audit every change;
  resetting (ptr=None) is a first-class operation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.networking.domain import (
    RdnsIpNotAssignedError,
    RdnsNotOwnerError,
    RdnsUnsupportedError,
    RdnsValidationError,
    ReverseDnsRecord,
    validate_ip,
    validate_ptr,
)
from cloud_platform.modules.networking.service import RdnsService
from cloud_platform.providers.base import rdns_support_of

NOW = datetime.now(UTC)
SERVER_ID = uuid4()
OWNER = uuid4()
OTHER = uuid4()


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


class LocalServerRepo:
    def __init__(self, server: CloudServer | None) -> None:
        self.server = server

    async def get(self, _server_id: UUID) -> CloudServer | None:
        return self.server


class RdnsFakeProvider:
    """Provider WITH reverse-dns support, reporting its own addresses."""

    key = "hetzner"

    def __init__(self, *, ipv4: str | None = "203.0.113.10", ipv6: str | None = None):
        self.ipv4 = ipv4
        self.ipv6 = ipv6
        self.calls: list[tuple[str, str, str | None]] = []

    async def get_server(self, _provider_server_id: str) -> Any:
        return self

    async def set_reverse_dns(self, provider_server_id: str, ip: str, ptr: str | None) -> None:
        self.calls.append((provider_server_id, ip, ptr))


class PlainProvider:
    """Provider WITHOUT reverse-dns support."""

    key = "plain"

    async def get_server(self, _provider_server_id: str) -> None:
        return None


class FakeRegistry:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def get(self, _key: str) -> Any:
        return self._provider


def make_server() -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=OWNER,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.RUNNING,
        provider_server_id="p-1",
    )


def make_service(
    provider: Any, server: CloudServer | None = None
) -> tuple[RdnsService, RecordingAudit]:
    audit = RecordingAudit()
    service = RdnsService(
        server_repo=LocalServerRepo(server or make_server()),  # type: ignore[arg-type]
        provider_registry=FakeRegistry(provider),  # type: ignore[arg-type]
        audit_repo=audit,  # type: ignore[arg-type]
    )
    return service, audit


class TestValidation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("203.0.113.10", "203.0.113.10"),
            (" 2001:db8::1 ", "2001:db8::1"),
        ],
    )
    def test_valid_ips_canonicalized(self, raw: str, expected: str) -> None:
        assert validate_ip(raw) == expected

    @pytest.mark.parametrize("raw", ["", "not-an-ip", "999.1.1.1", "203.0.113.256"])
    def test_invalid_ips_rejected(self, raw: str) -> None:
        with pytest.raises(RdnsValidationError):
            validate_ip(raw)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Host.Example.COM.", "host.example.com"),
            ("ptr.example.com", "ptr.example.com"),
            ("a-b.co", "a-b.co"),
        ],
    )
    def test_valid_ptrs_normalized(self, raw: str, expected: str) -> None:
        assert validate_ptr(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["", "   ", "nodot", "-bad.example.com", "bad-.example.com", "x" * 64 + ".com"]
    )
    def test_invalid_ptrs_rejected(self, raw: str) -> None:
        with pytest.raises(RdnsValidationError):
            validate_ptr(raw)

    def test_record_normalizes_both_sides(self) -> None:
        record = ReverseDnsRecord(ip=" 203.0.113.10 ", ptr="Host.Example.COM.")
        assert record.ip == "203.0.113.10"
        assert record.ptr == "host.example.com"


class TestOwnership:
    async def test_foreign_server_reads_as_missing(self) -> None:
        service, audit = make_service(RdnsFakeProvider())
        with pytest.raises(RdnsNotOwnerError):
            await service.set_ptr(OTHER, SERVER_ID, ReverseDnsRecord("203.0.113.10", "x.co"))
        assert audit.events == []

    async def test_ip_must_belong_to_this_server(self) -> None:
        provider = RdnsFakeProvider(ipv4="203.0.113.10")
        service, audit = make_service(provider)
        # a VALID ip that the provider does not assign to this server
        with pytest.raises(RdnsIpNotAssignedError):
            await service.set_ptr(OWNER, SERVER_ID, ReverseDnsRecord("198.51.100.9", "x.co"))
        assert provider.calls == []
        assert audit.events == []

    async def test_unassigned_when_provider_reports_no_addresses(self) -> None:
        provider = RdnsFakeProvider(ipv4=None, ipv6=None)
        service, _audit = make_service(provider)
        with pytest.raises(RdnsIpNotAssignedError):
            await service.set_ptr(OWNER, SERVER_ID, ReverseDnsRecord("203.0.113.10", "x.co"))

    async def test_ipv6_membership_accepted(self) -> None:
        provider = RdnsFakeProvider(ipv4=None, ipv6="2001:db8::25")
        service, audit = make_service(provider)
        out = await service.set_ptr(OWNER, SERVER_ID, ReverseDnsRecord("2001:db8::25", "v6.co"))
        assert out["ip"] == "2001:db8::25"
        assert len(audit.events) == 1


class TestCapabilityAndFlow:
    def test_probe_finds_setter_only_on_capable_providers(self) -> None:
        assert rdns_support_of(RdnsFakeProvider()) is not None
        assert rdns_support_of(PlainProvider()) is None

    async def test_unsupported_provider_refused(self) -> None:
        service, audit = make_service(PlainProvider())
        with pytest.raises(RdnsUnsupportedError):
            await service.set_ptr(OWNER, SERVER_ID, ReverseDnsRecord("203.0.113.10", "x.co"))
        assert audit.events == []

    async def test_set_and_reset_audited_with_provider_call(self) -> None:
        provider = RdnsFakeProvider()
        service, audit = make_service(provider)
        out = await service.set_ptr(OWNER, SERVER_ID, ReverseDnsRecord("203.0.113.10", "x.co"))
        assert out == {"server_id": str(SERVER_ID), "ip": "203.0.113.10", "ptr": "x.co"}
        assert provider.calls == [("p-1", "203.0.113.10", "x.co")]
        event = audit.events[-1]
        assert event.action == "rdns.set"
        assert event.actor_type is ActorType.USER
        assert event.metadata["ip"] == "203.0.113.10"

        # reset path
        reset = await service.set_ptr(OWNER, SERVER_ID, ReverseDnsRecord("203.0.113.10", None))
        assert reset["ptr"] is None
        assert provider.calls[-1] == ("p-1", "203.0.113.10", None)
        assert audit.events[-1].action == "rdns.reset"


class TestHetznerAdapterMapping:
    async def test_hetzner_set_reverse_dns_maps_endpoint(self) -> None:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        calls: list[tuple[str, str]] = []

        class Transport:
            async def request(self, method: str, path: str, **kwargs: Any):
                calls.append((method, path))
                assert method == "POST"
                assert path == "/servers/srv-9/actions/change_dns_ptr"

                from tests.unit.test_ssh_keys import _Resp

                return _Resp(201, {"action": {"status": "running"}})

        provider = HetznerCloudProvider(token="t")
        provider._client = Transport()  # type: ignore[assignment]
        await provider.set_reverse_dns("srv-9", "203.0.113.10", "ptr.example.com")
        assert calls == [("POST", "/servers/srv-9/actions/change_dns_ptr")]
