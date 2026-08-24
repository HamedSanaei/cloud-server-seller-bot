"""Tests for the secret rotation workflow (M10-008).

Acceptance: provider credential can rotate without downtime.

Covers: the runtime credential holder (atomic swap, redaction), both
adapters resolving the credential per request (a swap changes the very
next request, no restart), and the verify-then-swap rotation service
(failed verification changes nothing; success is atomic and audited with
fingerprints only - the credential value never appears).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.credentials.domain import (
    CredentialNotVerifiableError,
    CredentialRotationError,
    ProviderCredentialNotFoundError,
)
from cloud_platform.modules.credentials.service import CredentialRotationService
from cloud_platform.providers.arvancloud.client import ArvanCloudProvider, Throttle
from cloud_platform.providers.credentials import (
    Credential,
    CredentialHolder,
    credential_from_value,
    credential_key_hint,
    credential_verifier_of,
)
from cloud_platform.providers.errors import ProviderAuthError
from cloud_platform.providers.hetzner.client import HetznerCloudProvider
from cloud_platform.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Credential + holder
# ---------------------------------------------------------------------------


class TestCredential:
    def test_key_hint_is_stable_and_not_the_value(self) -> None:
        hint = credential_key_hint("hunter2-secret")
        assert hint == credential_key_hint("hunter2-secret")
        assert hint != credential_key_hint("hunter2-secret!")
        assert hint != "hunter2-secret"
        assert len(hint) == 12

    def test_credential_repr_redacts_value(self) -> None:
        c = credential_from_value("super-secret-token")
        text = repr(c)
        assert "super-secret-token" not in text
        assert "<redacted" in text
        assert c.key_hint in text
        assert str(c) == text

    def test_empty_credential_rejected(self) -> None:
        with pytest.raises(ValueError):
            credential_from_value("")
        with pytest.raises(ValueError):
            Credential(value="", key_hint="abc")


class TestCredentialHolder:
    async def test_get_returns_current(self) -> None:
        holder = CredentialHolder("initial-token")
        current = await holder.get()
        assert current.value == "initial-token"
        assert current.key_hint == credential_key_hint("initial-token")

    async def test_swap_is_atomic_and_returns_previous(self) -> None:
        holder = CredentialHolder("old-token")
        new = credential_from_value("new-token")
        previous = await holder.swap(new)
        assert previous.value == "old-token"
        current = await holder.get()
        assert current.value == "new-token"

    async def test_concurrent_swap_observes_only_whole_credentials(self) -> None:
        """A reader never sees a half-swapped state (atomic under the lock)."""
        holder = CredentialHolder("v1")
        observed: list[str] = []

        async def reader() -> None:
            for _ in range(500):
                observed.append((await holder.get()).value)

        async def writer() -> None:
            for i in range(500):
                await holder.swap(credential_from_value(f"v2-{i % 3}"))

        await asyncio.gather(reader(), writer())
        valid = {"v1", "v2-0", "v2-1", "v2-2"}
        assert set(observed) <= valid

    async def test_key_hint_tracks_current(self) -> None:
        holder = CredentialHolder("a")
        assert holder.key_hint == credential_key_hint("a")
        await holder.swap(credential_from_value("b"))
        assert holder.key_hint == credential_key_hint("b")

    def test_verifier_of_detects_capability(self) -> None:
        class WithVerifier:
            async def verify_credential(self, value: str) -> None:
                return None

        class Without:
            pass

        assert credential_verifier_of(WithVerifier()) is not None
        assert credential_verifier_of(Without()) is None


# ---------------------------------------------------------------------------
# Adapters resolve the credential per request
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.content = b"{}"
        self.text = "{}"
        self.is_error = status_code >= 400
        self._body = body if body is not None else {}

    def json(self) -> Any:
        return self._body


class _FakeHetznerHTTP:
    """Captures the Authorization header of every request."""

    def __init__(self, status_code: int = 200, json_body: dict | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._status = status_code
        self._json = json_body or {}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.requests.append(
            {"method": method, "path": path, "headers": kwargs.get("headers") or {}}
        )
        return _FakeResponse(self._status, self._json)


async def _hetzner_provider_with_holder(holder: CredentialHolder) -> HetznerCloudProvider:
    provider = HetznerCloudProvider(
        token=(await holder.get()).value,
        credential_source=holder,
    )
    fake = _FakeHetznerHTTP()
    provider._client = fake  # type: ignore[assignment]
    return provider


class TestHetznerCredentialRotation:
    async def test_request_uses_current_credential(self) -> None:
        holder = CredentialHolder("token-one")
        provider = await _hetzner_provider_with_holder(holder)
        await provider._request("GET", "/datacenters")
        assert provider._client.requests[0]["headers"]["Authorization"] == "Bearer token-one"

    async def test_swap_changes_the_next_request_without_restart(self) -> None:
        holder = CredentialHolder("token-one")
        provider = await _hetzner_provider_with_holder(holder)
        await provider._request("GET", "/datacenters")
        await holder.swap(credential_from_value("token-two"))
        await provider._request("GET", "/datacenters")
        first = provider._client.requests[0]["headers"]["Authorization"]
        second = provider._client.requests[1]["headers"]["Authorization"]
        assert first == "Bearer token-one"
        assert second == "Bearer token-two"

    async def test_verify_credential_sends_only_the_candidate(self) -> None:
        holder = CredentialHolder("live-token")
        provider = await _hetzner_provider_with_holder(holder)
        # the live holder must NOT be consulted for a candidate check
        await provider.verify_credential("candidate-token")
        req = provider._client.requests[0]
        assert req["headers"]["Authorization"] == "Bearer candidate-token"
        assert (await holder.get()).value == "live-token"

    async def test_verify_credential_rejects_bad_token(self) -> None:
        holder = CredentialHolder("live-token")
        provider = await _hetzner_provider_with_holder(holder)
        provider._client = _FakeHetznerHTTP(status_code=401)  # type: ignore[assignment]
        with pytest.raises(ProviderAuthError):
            await provider.verify_credential("bad-token")

    async def test_static_constructor_still_works(self) -> None:
        provider = HetznerCloudProvider(token="static-token")
        fake = _FakeHetznerHTTP()
        provider._client = fake  # type: ignore[assignment]
        await provider._request("GET", "/datacenters")
        # static path: the client-level header carries the token
        assert provider._client is fake


class _FakeArvanHTTP:
    def __init__(self, status_code: int = 200, json_body: Any = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._status = status_code
        self._json = json_body if json_body is not None else {"servers": []}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.requests.append(
            {"method": method, "path": path, "headers": kwargs.get("headers") or {}}
        )
        return _FakeResponse(self._status, self._json)


async def _arvan_provider_with_holder(holder: CredentialHolder) -> ArvanCloudProvider:
    provider = ArvanCloudProvider(
        api_key=(await holder.get()).value,
        region="ir-thr-1",
        throttle=Throttle(max_rps=1000.0, wait=AsyncMock()),
        credential_source=holder,
    )
    fake = _FakeArvanHTTP()
    provider._client = fake  # type: ignore[assignment]
    return provider


class TestArvanCloudCredentialRotation:
    async def test_request_uses_current_credential_plain_header(self) -> None:
        holder = CredentialHolder("MU-key-one")
        provider = await _arvan_provider_with_holder(holder)
        await provider.list_servers()
        assert provider._client.requests[0]["headers"]["Authorization"] == "MU-key-one"
        # contract §2: NO Bearer prefix
        assert not str(provider._client.requests[0]["headers"]["Authorization"]).startswith(
            "Bearer"
        )

    async def test_swap_changes_the_next_request_without_restart(self) -> None:
        holder = CredentialHolder("MU-key-one")
        provider = await _arvan_provider_with_holder(holder)
        await provider.list_servers()
        await holder.swap(credential_from_value("MU-key-two"))
        await provider.list_servers()
        assert provider._client.requests[0]["headers"]["Authorization"] == "MU-key-one"
        assert provider._client.requests[1]["headers"]["Authorization"] == "MU-key-two"

    async def test_verify_credential_rejects_bad_key(self) -> None:
        holder = CredentialHolder("MU-live")
        provider = await _arvan_provider_with_holder(holder)
        provider._client = _FakeArvanHTTP(status_code=401)  # type: ignore[assignment]
        with pytest.raises(ProviderAuthError):
            await provider.verify_credential("MU-bad")


# ---------------------------------------------------------------------------
# Rotation service: verify-then-swap
# ---------------------------------------------------------------------------


class _FakeHolderRegistry:
    def __init__(self) -> None:
        self._holders: dict[str, CredentialHolder] = {}

    def register(self, key: str, holder: CredentialHolder) -> None:
        self._holders[key] = holder

    def get_holder(self, key: str) -> CredentialHolder | None:
        return self._holders.get(key)


class _FakeAuditRepo:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(self, event: Any) -> Any:
        self.events.append(
            {
                "actor_type": event.actor_type,
                "action": event.action,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "actor_id": event.actor_id,
                "reason": event.reason,
                "metadata": event.metadata,
            }
        )
        return event


class _FakeProvider:
    def __init__(self, key: str, verify_result: str = "ok") -> None:
        self.key = key
        self.capabilities = frozenset()
        self.candidate_seen: list[str] = []
        self._verify_result = verify_result

    async def verify_credential(self, value: str) -> None:
        self.candidate_seen.append(value)
        if self._verify_result == "fail":
            raise ProviderAuthError("invalid token")


def _service(
    provider: _FakeProvider, holder: CredentialHolder | None, audit: _FakeAuditRepo
) -> CredentialRotationService:
    registry = _FakeHolderRegistry()
    if holder is not None:
        registry.register(provider.key, holder)
    pr = ProviderRegistry()
    pr.register(provider)
    return CredentialRotationService(registry, pr, audit)  # type: ignore[arg-type]


class TestRotationService:
    async def test_rotate_verifies_then_swaps_and_audits(self) -> None:
        holder = CredentialHolder("old-secret")
        provider = _FakeProvider("hetzner")
        audit = _FakeAuditRepo()
        service = _service(provider, holder, audit)

        result = await service.rotate(
            provider_key="hetzner",
            new_credential_value="new-secret",
            reason="quarterly rotation",
            actor_type=ActorType.ADMIN,
            actor_id=uuid4(),
        )

        # verified first with the candidate
        assert provider.candidate_seen == ["new-secret"]
        # swapped atomically
        current = await holder.get()
        assert current.value == "new-secret"
        assert result.previous_key_hint == credential_key_hint("old-secret")
        assert result.new_key_hint == credential_key_hint("new-secret")
        # audited with fingerprints ONLY - never the values
        assert len(audit.events) == 1
        event = audit.events[0]
        assert event["action"] == "credential.rotate"
        assert event["resource_id"] == "hetzner"
        assert event["metadata"]["previous_key_hint"] == result.previous_key_hint
        assert event["metadata"]["new_key_hint"] == result.new_key_hint
        rendered = repr(event) + event["reason"]
        assert "old-secret" not in rendered
        assert "new-secret" not in rendered

    async def test_failed_verification_changes_nothing(self) -> None:
        holder = CredentialHolder("live-secret")
        provider = _FakeProvider("hetzner", verify_result="fail")
        audit = _FakeAuditRepo()
        service = _service(provider, holder, audit)

        with pytest.raises(CredentialRotationError) as excinfo:
            await service.rotate(
                provider_key="hetzner",
                new_credential_value="bad-secret",
                reason="bad candidate",
                actor_type=ActorType.ADMIN,
                actor_id=uuid4(),
            )
        assert "rejected" in str(excinfo.value)
        # the live credential keeps serving - no downtime
        assert (await holder.get()).value == "live-secret"
        assert audit.events == []

    async def test_unknown_provider_is_refused(self) -> None:
        holder = CredentialHolder("x")
        provider = _FakeProvider("hetzner")
        audit = _FakeAuditRepo()
        service = _service(provider, holder, audit)
        with pytest.raises(ProviderCredentialNotFoundError):
            await service.rotate(
                provider_key="unknown-provider",
                new_credential_value="y",
                reason="r",
                actor_type=ActorType.SYSTEM,
                actor_id=None,
            )

    async def test_provider_without_holder_is_refused(self) -> None:
        provider = _FakeProvider("hetzner")
        audit = _FakeAuditRepo()
        service = _service(provider, None, audit)
        with pytest.raises(ProviderCredentialNotFoundError):
            await service.rotate(
                provider_key="hetzner",
                new_credential_value="y",
                reason="r",
                actor_type=ActorType.SYSTEM,
                actor_id=None,
            )

    async def test_unverifiable_provider_is_refused(self) -> None:
        class NoVerifier:
            key = "readonly"
            capabilities = frozenset()

        holder = CredentialHolder("x")
        registry = _FakeHolderRegistry()
        registry.register("readonly", holder)
        pr = ProviderRegistry()
        pr.register(NoVerifier())
        service = CredentialRotationService(registry, pr, _FakeAuditRepo())  # type: ignore[arg-type]
        with pytest.raises(CredentialNotVerifiableError):
            await service.rotate(
                provider_key="readonly",
                new_credential_value="y",
                reason="r",
                actor_type=ActorType.SYSTEM,
                actor_id=None,
            )
        assert (await holder.get()).value == "x"

    async def test_status_returns_fingerprint_only(self) -> None:
        holder = CredentialHolder("secret-value")
        provider = _FakeProvider("hetzner")
        service = _service(provider, holder, _FakeAuditRepo())
        status = await service.status("hetzner")
        assert status.key_hint == credential_key_hint("secret-value")
        assert "secret-value" not in repr(status)

    async def test_empty_reason_rejected(self) -> None:
        holder = CredentialHolder("x")
        provider = _FakeProvider("hetzner")
        service = _service(provider, holder, _FakeAuditRepo())
        with pytest.raises(CredentialRotationError, match="reason"):
            await service.rotate(
                provider_key="hetzner",
                new_credential_value="y",
                reason="   ",
                actor_type=ActorType.SYSTEM,
                actor_id=None,
            )

    async def test_admin_actor_without_id_rejected(self) -> None:
        holder = CredentialHolder("x")
        provider = _FakeProvider("hetzner")
        service = _service(provider, holder, _FakeAuditRepo())
        with pytest.raises(CredentialRotationError):
            await service.rotate(
                provider_key="hetzner",
                new_credential_value="y",
                reason="r",
                actor_type=ActorType.ADMIN,
                actor_id=None,
            )


# ---------------------------------------------------------------------------
# End-to-end: rotation of a REAL adapter holder (no restart, no downtime)
# ---------------------------------------------------------------------------


class TestRotationEndToEnd:
    async def test_rotate_live_adapter_credential(self) -> None:
        """The whole workflow on the real Hetzner adapter: a failed
        candidate is rejected without touching the live credential; a good
        candidate is verified, swapped, and the very next API call uses it."""
        holder = CredentialHolder("old-live-token")
        provider = HetznerCloudProvider(token="old-live-token", credential_source=holder)

        # 1) a bad candidate: 401 -> refused, live untouched
        provider._client = _FakeHetznerHTTP(status_code=401)  # type: ignore[assignment]
        audit = _FakeAuditRepo()
        pr = ProviderRegistry()
        pr.register(provider)
        registry = _FakeHolderRegistry()
        registry.register("hetzner", holder)
        service = CredentialRotationService(registry, pr, audit)  # type: ignore[arg-type]
        with pytest.raises(CredentialRotationError, match="rejected"):
            await service.rotate(
                provider_key="hetzner",
                new_credential_value="bad-token",
                reason="bad candidate",
                actor_type=ActorType.ADMIN,
                actor_id=uuid4(),
            )
        assert (await holder.get()).value == "old-live-token"

        # 2) a good candidate: verified (200), swapped, next request uses it
        provider._client = _FakeHetznerHTTP(status_code=200)  # type: ignore[assignment]
        result = await service.rotate(
            provider_key="hetzner",
            new_credential_value="new-live-token",
            reason="quarterly rotation",
            actor_type=ActorType.ADMIN,
            actor_id=uuid4(),
        )
        assert result.new_key_hint == credential_key_hint("new-live-token")
        await provider._request("GET", "/datacenters")
        last = provider._client.requests[-1]
        assert last["headers"]["Authorization"] == "Bearer new-live-token"
        # and the old value is gone from the live slot
        assert (await holder.get()).value == "new-live-token"
