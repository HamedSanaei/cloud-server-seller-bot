"""Tests for SSH key CRUD/sync (M13-001).

Acceptance: user keys are ownership-scoped - every service operation is
performed as an ACTING USER and another user's keys are indistinguishable
from nonexistent ones (no existence leak). Also covered: OpenSSH public-key
validation + SHA256 fingerprints, per-user dedup/quota, audit trail, the
Hetzner ssh_keys port, and idempotent provider sync.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.sshkeys import (
    DuplicateSshKeyError,
    InvalidPublicKeyError,
    SshKey,
    SshKeyLimitError,
    SshKeyNotFoundError,
    SshKeyService,
    compute_fingerprint,
    parse_public_key,
    provider_key_name,
    ssh_key_port_of,
    sync_keys_to_provider,
)

USER_A = uuid4()
USER_B = uuid4()

# a real ed25519 key blob (openssh wire format), used as the known vector
_BLOB = base64.b64decode("AAAAC3NzaC1lZDI1NTE5AAAAIGbZ7SUiX0nC1pGcXkEOrBThtV0zr+8mZ+O4/0j6rCcU")
ED25519_KEY = "ssh-ed25519 " + base64.b64encode(_BLOB).decode() + " user@host"
ED25519_FP = "SHA256:" + base64.b64encode(hashlib.sha256(_BLOB).digest()).decode().rstrip("=")


class FakeKeyRepo:
    def __init__(self) -> None:
        self.by_id: dict[Any, SshKey] = {}

    async def add(self, key: SshKey) -> SshKey:
        assert key.id is None or key.id not in self.by_id
        saved = SshKey(
            id=key.id or uuid4(),
            user_id=key.user_id,
            name=key.name,
            public_key=key.public_key,
            fingerprint=key.fingerprint,
            created_at=key.created_at,
        )
        self.by_id[saved.id] = saved  # type: ignore[index]
        return saved

    async def get(self, key_id: Any) -> SshKey | None:
        return self.by_id.get(key_id)

    async def list_for_user(self, user_id: Any) -> list[SshKey]:
        return sorted(
            (k for k in self.by_id.values() if k.user_id == user_id),
            key=lambda k: str(k.id),
        )

    async def delete(self, key_id: Any) -> None:
        self.by_id.pop(key_id, None)


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record_mutation(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


def make_service(max_keys: int = 20) -> tuple[SshKeyService, FakeKeyRepo, RecordingAudit]:
    repo = FakeKeyRepo()
    audit = RecordingAudit()
    return SshKeyService(repo, audit, max_keys_per_user=max_keys), repo, audit


# ---------------------------------------------------------------------------
# Domain: validation + fingerprinting
# ---------------------------------------------------------------------------


class TestDomainValidation:
    def test_known_vector_fingerprint(self) -> None:
        assert ED25519_KEY.split()[0] == "ssh-ed25519"
        assert compute_fingerprint(ED25519_KEY) == ED25519_FP

    def test_parse_returns_type_and_blob(self) -> None:
        key_type, blob = parse_public_key(ED25519_KEY)
        assert key_type == "ssh-ed25519"
        assert blob == _BLOB

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty
            "not-a-key",  # one part
            "rsa-sha2-512 AAAA",  # unsupported key type
            "weird-type " + base64.b64encode(b"x").decode(),
            "ssh-ed25519 !!!not-base64!!!",  # invalid base64 body
            "ssh-ed25519",  # missing body
        ],
    )
    def test_invalid_keys_rejected(self, bad: str) -> None:
        with pytest.raises(InvalidPublicKeyError):
            compute_fingerprint(bad)

    def test_comment_with_spaces_is_allowed(self) -> None:
        key = ED25519_KEY + " comment with spaces"
        assert compute_fingerprint(key) == ED25519_FP  # fingerprint ignores comment

    def test_entity_normalizes_and_fingerprints(self) -> None:
        key = SshKey(user_id=USER_A, name=" laptop ", public_key=ED25519_KEY)
        assert key.name == "laptop"
        assert key.public_key == "ssh-ed25519 " + base64.b64encode(_BLOB).decode()
        assert key.fingerprint == ED25519_FP

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name"):
            SshKey(user_id=USER_A, name="   ", public_key=ED25519_KEY)


# ---------------------------------------------------------------------------
# Service: CRUD with ownership scoping
# ---------------------------------------------------------------------------


class TestRegisterScoping:
    async def test_register_happy_path_audited(self) -> None:
        service, _repo, audit = make_service()
        key = await service.register(
            actor_user_id=USER_A, owner_user_id=USER_A, name="laptop", public_key=ED25519_KEY
        )
        assert key.fingerprint == ED25519_FP
        assert len(audit.events) == 1
        event = audit.events[0]
        assert event["action"] == "sshkey.registered"
        assert event["actor_type"] is ActorType.USER
        assert event["actor_id"] == USER_A

    async def test_cannot_register_for_another_user(self) -> None:
        service, repo, _audit = make_service()
        with pytest.raises(SshKeyNotFoundError):
            await service.register(
                actor_user_id=USER_A, owner_user_id=USER_B, name="x", public_key=ED25519_KEY
            )
        assert repo.by_id == {}

    async def test_duplicate_name_rejected(self) -> None:
        service, _repo, _audit = make_service()
        await service.register(
            actor_user_id=USER_A, owner_user_id=USER_A, name="laptop", public_key=ED25519_KEY
        )
        other_blob = base64.b64encode(b"another-blob").decode()
        with pytest.raises(DuplicateSshKeyError, match="name"):
            await service.register(
                actor_user_id=USER_A,
                owner_user_id=USER_A,
                name="laptop",
                public_key=f"ssh-rsa {other_blob}",
            )

    async def test_same_material_twice_rejected(self) -> None:
        service, _repo, _audit = make_service()
        await service.register(
            actor_user_id=USER_A, owner_user_id=USER_A, name="first", public_key=ED25519_KEY
        )
        with pytest.raises(DuplicateSshKeyError, match="already registered"):
            await service.register(
                actor_user_id=USER_A, owner_user_id=USER_A, name="second", public_key=ED25519_KEY
            )

    async def test_quota_enforced(self) -> None:
        service, _repo, _audit = make_service(max_keys=1)
        await service.register(
            actor_user_id=USER_A, owner_user_id=USER_A, name="one", public_key=ED25519_KEY
        )
        other = "ssh-rsa " + base64.b64encode(b"zzz").decode()
        with pytest.raises(SshKeyLimitError):
            await service.register(
                actor_user_id=USER_A, owner_user_id=USER_A, name="two", public_key=other
            )


class TestReadDeleteScoping:
    async def test_list_scoped_to_owner(self) -> None:
        service, _repo, _audit = make_service()
        mine = await service.register(
            actor_user_id=USER_A, owner_user_id=USER_A, name="mine", public_key=ED25519_KEY
        )
        listed = await service.list_keys(actor_user_id=USER_A, owner_user_id=USER_A)
        assert [k.id for k in listed] == [mine.id]
        # B listing A's keys is denied outright...
        with pytest.raises(SshKeyNotFoundError):
            await service.list_keys(actor_user_id=USER_B, owner_user_id=USER_A)
        # ...and B reading A's key by id sees NOT FOUND (no existence leak)
        with pytest.raises(SshKeyNotFoundError):
            await service.get_owned(actor_user_id=USER_B, key_id=mine.id)  # type: ignore[arg-type]

    async def test_delete_scoped_to_owner_and_audited(self) -> None:
        service, repo, audit = make_service()
        key = await service.register(
            actor_user_id=USER_A, owner_user_id=USER_A, name="gone", public_key=ED25519_KEY
        )
        # B cannot delete A's key; to B it simply does not exist
        with pytest.raises(SshKeyNotFoundError):
            await service.delete(actor_user_id=USER_B, key_id=key.id)  # type: ignore[arg-type]
        assert len(repo.by_id) == 1  # untouched
        await service.delete(actor_user_id=USER_A, key_id=key.id)  # type: ignore[arg-type]
        assert repo.by_id == {}
        actions = [e["action"] for e in audit.events]
        assert actions == ["sshkey.registered", "sshkey.deleted"]

    async def test_missing_key_reads_as_not_found(self) -> None:
        service, _repo, _audit = make_service()
        with pytest.raises(SshKeyNotFoundError):
            await service.delete(actor_user_id=USER_A, key_id=uuid4())


# ---------------------------------------------------------------------------
# Provider sync
# ---------------------------------------------------------------------------


class _Resp:
    """Minimal stand-in for an httpx.Response at the transport boundary."""

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


class FakeProviderPort:
    def __init__(self, remote: list[tuple[str, str, str]] | None = None) -> None:
        self.remote = list(remote or [])  # (id, name, fingerprint)
        self.uploads: list[tuple[str, str]] = []

    async def list_ssh_keys(self) -> list[tuple[str, str, str]]:
        return list(self.remote)

    async def upload_ssh_key(self, name: str, public_key: str) -> str:
        self.uploads.append((name, public_key))
        remote_id = f"key-{len(self.remote) + 1}"
        self.remote.append((remote_id, name, compute_fingerprint(public_key)))
        return remote_id


class TestSync:
    async def test_upload_when_absent_then_reuse_on_resync(self) -> None:
        port = FakeProviderPort()
        keys = [SshKey(id=uuid4(), user_id=USER_A, name="laptop", public_key=ED25519_KEY)]
        ids = await sync_keys_to_provider(port, keys)
        assert ids == ["key-1"]
        assert len(port.uploads) == 1
        # resync is IDEMPOTENT: same name+material => reuse, no re-upload
        ids2 = await sync_keys_to_provider(port, keys)
        assert ids2 == ids
        assert len(port.uploads) == 1

    def test_deterministic_names_are_user_scoped(self) -> None:
        ka = SshKey(id=uuid4(), user_id=USER_A, name="Laptop 1", public_key=ED25519_KEY)
        kb = SshKey(id=uuid4(), user_id=USER_B, name="Laptop 1", public_key=ED25519_KEY)
        na, nb = provider_key_name(ka), provider_key_name(kb)
        assert na != nb  # users never collide at the provider
        assert "-" in na and " " not in na

    async def test_foreign_material_under_our_name_raises(self) -> None:
        foreign_fp = "SHA256:c3RhbmRhcmQtdGVzdC12ZWN0b3I"
        port = FakeProviderPort(
            remote=[
                (
                    "remote-9",
                    provider_key_name(
                        SshKey(id=uuid4(), user_id=USER_A, name="laptop", public_key=ED25519_KEY)
                    ),
                    foreign_fp,
                )
            ]
        )
        keys = [SshKey(id=uuid4(), user_id=USER_A, name="laptop", public_key=ED25519_KEY)]
        with pytest.raises(DuplicateSshKeyError, match="different material"):
            await sync_keys_to_provider(port, keys)
        assert port.uploads == []

    def test_capability_probe(self) -> None:
        class WithKeys:
            ssh_keys = FakeProviderPort()

        class WithoutKeys:
            pass

        assert ssh_key_port_of(WithKeys()) is not None
        assert ssh_key_port_of(WithoutKeys()) is None

    async def test_ids_for_provider_lists_own_keys_only(self) -> None:
        service, repo, _audit = make_service()
        # A registers through the service; B's key is planted directly.
        await service.register(
            actor_user_id=USER_A, owner_user_id=USER_A, name="a", public_key=ED25519_KEY
        )
        b_key = await repo.add(SshKey(user_id=USER_B, name="b", public_key=ED25519_KEY))
        ids_a = await service.ids_for_provider(USER_A)
        ids_b = await service.ids_for_provider(USER_B)
        assert len(ids_a) == 1 and str(b_key.id) not in ids_a
        assert [str(b_key.id)] == ids_b

    async def test_hetzner_port_maps_api(self) -> None:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        calls: list[tuple[str, str]] = []

        class FakeTransport:
            async def request(self, method: str, path: str, **kwargs: Any):
                calls.append((method, path))
                if path == "/ssh_keys" and method == "GET":
                    return _Resp(
                        200,
                        {
                            "ssh_keys": [
                                {"id": 42, "name": "platform-x-laptop", "fingerprint": "FP"}
                            ]
                        },
                    )
                if path == "/ssh_keys" and method == "POST":
                    return _Resp(201, {"ssh_key": {"id": 43}})
                if path.startswith("/ssh_keys/") and method == "DELETE":
                    return _Resp(204, {})
                raise AssertionError(f"unexpected {method} {path}")

        provider = HetznerCloudProvider(token="test-token")
        provider._client = FakeTransport()  # type: ignore[assignment]
        port = ssh_key_port_of(provider)
        assert port is not None
        assert await port.list_ssh_keys() == [("42", "platform-x-laptop", "FP")]
        assert await port.upload_ssh_key("n", ED25519_KEY) == "43"
        await port.delete_ssh_key("43")  # no raise on 204
        await port.delete_ssh_key("999")  # 404 maps to idempotent success
        assert ("DELETE", "/ssh_keys/999") in calls
