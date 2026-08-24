"""Tests for firewall management (M13-006).

Acceptance: reusable rules and ownership.

- Rules are validated first-class values (direction/protocol/port/CIDR).
- A firewall is a NAMED rulebook owned by exactly one user; another user's
  firewalls are indistinguishable from nonexistent ones.
- Provider sync is idempotent BY NAME: the same rulebook updates the same
  remote firewall instead of ever duplicating it - that reusability across
  servers and syncs is the point of the feature.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.firewalls import (
    DuplicateFirewallError,
    Firewall,
    FirewallLimitError,
    FirewallNotFoundError,
    FirewallRule,
    FirewallService,
    InvalidFirewallRuleError,
    ensure_remote_firewall,
    firewall_port_of,
)

USER_A = uuid4()
USER_B = uuid4()

HTTP_RULE = FirewallRule(direction="in", protocol="tcp", port="443", cidrs=("0.0.0.0/0",))


class _Resp:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        import json

        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._body = body
        self.content = json.dumps(body).encode()
        self.is_error = status_code >= 400
        self.text = json.dumps(body)

    def json(self) -> dict[str, Any]:
        return self._body


class LocalFirewallRepo:
    def __init__(self) -> None:
        self.by_id: dict[UUID, Firewall] = {}

    async def add(self, firewall: Firewall) -> Firewall:
        saved = Firewall(
            id=firewall.id or uuid4(),
            user_id=firewall.user_id,
            name=firewall.name,
            rules=firewall.rules,
            provider_firewall_id=firewall.provider_firewall_id,
            created_at=firewall.created_at,
        )
        self.by_id[saved.id] = saved  # type: ignore[index]
        return saved

    async def get(self, firewall_id: UUID) -> Firewall | None:
        return self.by_id.get(firewall_id)

    async def list_for_user(self, user_id: UUID) -> list[Firewall]:
        return sorted(
            (f for f in self.by_id.values() if f.user_id == user_id), key=lambda f: str(f.id)
        )

    async def save(self, firewall: Firewall) -> Firewall:
        self.by_id[firewall.id] = firewall  # type: ignore[index]
        return firewall

    async def delete(self, firewall_id: UUID) -> None:
        self.by_id.pop(firewall_id, None)


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


def make_service(
    max_firewalls: int = 10,
) -> tuple[FirewallService, LocalFirewallRepo, RecordingAudit]:
    from cloud_platform.modules.audit.service import AuditTrail

    repo = LocalFirewallRepo()
    audit = RecordingAudit()
    service = FirewallService(repo, AuditTrail(audit), max_firewalls_per_user=max_firewalls)
    return service, repo, audit


# ---------------------------------------------------------------------------
# Rule validation
# ---------------------------------------------------------------------------


class TestRuleValidation:
    def test_valid_tcp_rule_with_range(self) -> None:
        rule = FirewallRule("in", "tcp", "8000-8100", ("10.0.0.0/8",))
        assert rule.port == "8000-8100"

    def test_icmp_rule_without_port_is_valid(self) -> None:
        assert FirewallRule("in", "icmp", None, ("0.0.0.0/0",)).port is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"direction": "sideways", "protocol": "tcp"},  # bad direction
            {"direction": "in", "protocol": "smtp"},  # bad protocol
            {"direction": "in", "protocol": "tcp", "port": "0"},  # port out of range
            {"direction": "in", "protocol": "tcp", "port": "70000"},
            {"direction": "in", "protocol": "tcp", "port": "abc"},
            {"direction": "in", "protocol": "tcp", "port": "200-100"},  # inverted range
            {"direction": "in", "protocol": "tcp", "cidrs": ()},  # no cidrs
            {"direction": "in", "protocol": "tcp", "cidrs": ("999.1.2.3/32",)},  # bad cidr
        ],
    )
    def test_invalid_rules_rejected(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(InvalidFirewallRuleError):
            FirewallRule(cidrs=kwargs.pop("cidrs", ("0.0.0.0/0",)), **kwargs)

    def test_bare_host_is_accepted_and_normalized(self) -> None:
        # ip_network(strict=False) treats a bare host as a /32-style network
        rule = FirewallRule("in", "tcp", "22", ("10.0.0.1",))
        assert rule.cidrs == ("10.0.0.1",)

    def test_dict_round_trip(self) -> None:
        rule = FirewallRule("out", "udp", "53", ("::/0",))
        restored = FirewallRule.from_dict(rule.to_dict())
        assert restored == rule

    def test_host_cidr_normalized_by_ipaddress_strict_false(self) -> None:
        # a bare host with /32-style intent passes via strict=False handling
        rule = FirewallRule("in", "tcp", "22", ("203.0.113.7/32",))
        assert rule.cidrs == ("203.0.113.7/32",)


# ---------------------------------------------------------------------------
# Service: reusable rulebooks + ownership scoping
# ---------------------------------------------------------------------------


class TestOwnershipScoping:
    async def test_create_happy_path_audited(self) -> None:
        service, _repo, audit = make_service()
        fw = await service.create(
            actor_user_id=USER_A,
            owner_user_id=USER_A,
            name="web",
            rules=[HTTP_RULE],
        )
        assert len(fw.rules) == 1
        event = audit.events[-1]
        assert event.action == "firewall.created"
        assert event.actor_type is ActorType.USER
        assert event.actor_id == USER_A

    async def test_cannot_create_for_another_user(self) -> None:
        service, repo, _audit = make_service()
        with pytest.raises(FirewallNotFoundError):
            await service.create(
                actor_user_id=USER_A, owner_user_id=USER_B, name="web", rules=[HTTP_RULE]
            )
        assert repo.by_id == {}

    async def test_duplicate_name_per_user_rejected(self) -> None:
        service, _repo, _audit = make_service()
        await service.create(
            actor_user_id=USER_A, owner_user_id=USER_A, name="web", rules=[HTTP_RULE]
        )
        with pytest.raises(DuplicateFirewallError):
            await service.create(
                actor_user_id=USER_A, owner_user_id=USER_A, name="web", rules=[HTTP_RULE]
            )

    async def test_same_name_for_different_users_is_fine(self) -> None:
        service, repo, _audit = make_service()
        await service.create(actor_user_id=USER_A, owner_user_id=USER_A, name="web", rules=[])
        b = await service.create(actor_user_id=USER_B, owner_user_id=USER_B, name="web", rules=[])
        assert len(repo.by_id) == 2 and b.user_id == USER_B

    async def test_quota_enforced(self) -> None:
        service, _repo, _audit = make_service(max_firewalls=1)
        await service.create(actor_user_id=USER_A, owner_user_id=USER_A, name="one", rules=[])
        with pytest.raises(FirewallLimitError):
            await service.create(actor_user_id=USER_A, owner_user_id=USER_A, name="two", rules=[])

    async def test_foreign_reads_and_deletes_are_not_found(self) -> None:
        service, repo, _audit = make_service()
        mine = await service.create(
            actor_user_id=USER_A, owner_user_id=USER_A, name="mine", rules=[HTTP_RULE]
        )
        with pytest.raises(FirewallNotFoundError):
            await service.list_keys(actor_user_id=USER_B, owner_user_id=USER_A)
        with pytest.raises(FirewallNotFoundError):
            await service.get_owned(actor_user_id=USER_B, firewall_id=mine.id)  # type: ignore[arg-type]
        with pytest.raises(FirewallNotFoundError):
            await service.delete(actor_user_id=USER_B, firewall_id=mine.id)  # type: ignore[arg-type]
        assert len(repo.by_id) == 1  # untouched
        await service.delete(actor_user_id=USER_A, firewall_id=mine.id)  # type: ignore[arg-type]
        assert repo.by_id == {}

    async def test_replace_rules_updates_content_and_audits(self) -> None:
        service, _repo, audit = make_service()
        fw = await service.create(actor_user_id=USER_A, owner_user_id=USER_A, name="web", rules=[])
        ssh_rule = FirewallRule("in", "tcp", "22", ("203.0.113.0/24",))
        updated = await service.replace_rules(
            actor_user_id=USER_A,
            firewall_id=fw.id,
            rules=[HTTP_RULE, ssh_rule],  # type: ignore[arg-type]
        )
        assert [r.port for r in updated.rules] == ["443", "22"]
        actions = [e.action for e in audit.events]
        assert actions == ["firewall.created", "firewall.rules_replaced"]


# ---------------------------------------------------------------------------
# Reusable provider sync (idempotent by name)
# ---------------------------------------------------------------------------


class FakeFirewallPort:
    def __init__(self) -> None:
        self.remote: dict[str, tuple[str, list[dict[str, object]]]] = {}  # name -> (id, rules)
        self.creates = 0
        self.updates = 0

    async def list_firewalls(self) -> list[tuple[str, str]]:
        return [(fid, name) for name, (fid, _rules) in self.remote.items()]

    async def create_firewall(self, name: str, rules: list[dict[str, object]]) -> str:
        self.creates += 1
        fid = f"fw-{self.creates}"
        self.remote[name] = (fid, rules)
        return fid

    async def update_firewall_rules(
        self, provider_firewall_id: str, rules: list[dict[str, object]]
    ) -> None:
        self.updates += 1
        for name, (fid, _old) in self.remote.items():
            if fid == provider_firewall_id:
                self.remote[name] = (fid, rules)


class TestReusableSync:
    async def test_first_sync_creates_then_resync_updates_in_place(self) -> None:
        port = FakeFirewallPort()
        fw = Firewall(user_id=USER_A, name="web", rules=(HTTP_RULE,))
        fid = await ensure_remote_firewall(port, fw)
        assert fid == "fw-1" and port.creates == 1
        # resync the SAME name -> UPDATE not duplicate
        fid2 = await ensure_remote_firewall(port, fw)
        assert fid2 == fid
        assert port.creates == 1 and port.updates == 1
        # changed rules flow into the SAME remote object
        stricter = Firewall(user_id=USER_A, name="web", rules=())
        stricter = Firewall(
            user_id=USER_A,
            name="web",
            rules=(FirewallRule("in", "tcp", "22", ("10.0.0.0/8",)),),
        )
        fid3 = await ensure_remote_firewall(port, stricter)
        assert fid3 == fid
        assert port.creates == 1 and port.updates == 2
        assert port.remote["web"][1][0]["port"] == "22"

    def test_probe_present_only_when_port_exists(self) -> None:
        class WithFirewalls:
            firewalls = FakeFirewallPort()

        class Without:
            pass

        assert firewall_port_of(WithFirewalls()) is not None
        assert firewall_port_of(Without()) is None


class TestHetznerAdapterMapping:
    async def test_hetzner_firewall_api_maps_endpoints_and_rules(self) -> None:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        calls: list[tuple[str, str, dict[str, Any] | None]] = []

        class Transport:
            async def request(self, method: str, path: str, **kwargs: Any):
                body = kwargs.get("json")
                calls.append((method, path, body))
                if path == "/firewalls" and method == "GET":
                    return _Resp(200, {"firewalls": [{"id": 5, "name": "web"}]})
                if path == "/firewalls" and method == "POST":
                    return _Resp(201, {"firewall": {"id": 6}})
                if path == "/firewalls/6" and method == "PUT":
                    return _Resp(200, {"firewall": {"id": 6}})
                if path.endswith("/apply_to_resources"):
                    assert body["apply_to"][0]["server"]["id"] == 42
                    return _Resp(201, {"action": {"status": "running"}})
                raise AssertionError(f"{method} {path}")

        provider = HetznerCloudProvider(token="t")
        provider._client = Transport()  # type: ignore[assignment]
        api = provider.firewalls

        assert await api.list_firewalls() == [("5", "web")]
        rules = [{"direction": "in", "protocol": "tcp", "port": "443", "cidrs": ["0.0.0.0/0"]}]
        assert await api.create_firewall("locked", rules) == "6"
        _method, _path, body = calls[1]
        hetzner_rules = body["rules"]
        assert hetzner_rules[0] == {
            "direction": "in",
            "protocol": "tcp",
            "port": "443",
            "source_ips": ["0.0.0.0/0"],
        }
        outbound = [{"direction": "out", "protocol": "udp", "port": "53", "cidrs": ["::/0"]}]
        await api.update_firewall_rules("6", outbound)
        assert calls[-1][0] == "PUT" and calls[-1][2]["rules"][0]["destination_ips"] == ["::/0"]
        await api.apply_to_servers("6", ["42"])
