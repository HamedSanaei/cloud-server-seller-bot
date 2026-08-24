"""Contract tests for REST API v1 (M14-001/M14-002).

Acceptance: stable resource/error/idempotency contract.

These tests PIN the contract:

- every error response uses the one envelope
  ``{"error": {"code", "message", "details"}}`` with stable codes;
- mutating endpoints REQUIRE an ``Idempotency-Key`` (428 otherwise);
- identity is required (401 envelope);
- bearer tokens authenticate with SCOPES (403 when a scope is lacking);
- the /v1 resource paths exist and are versioned;
- domain errors map to stable codes without leaking foreign resources.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_platform.api.v1 import ErrorCode
from cloud_platform.api.v1 import router as v1_router
from cloud_platform.api.v1.dependencies import (
    get_token_authentication,
)
from cloud_platform.api.v1.router import _catalog_repo, _ssh_key_service, _token_service
from cloud_platform.modules.sshkeys.domain import SshKeyNotFoundError
from cloud_platform.modules.tokens.domain import TokenAuthentication, TokenScope

USER_ID = uuid4()
ALL_SCOPES_AUTH = TokenAuthentication(
    user_id=USER_ID, scopes=frozenset(TokenScope), token_id=uuid4()
)


class FakeCatalogRepo:
    async def list_offers(self) -> list[Any]:
        class Offer:
            id = uuid4()
            provider_key = "hetzner"
            plan_id = "cx22"
            location_id = "fsn1"
            name = "CX22"
            architecture = "x86"
            vcpu = 2
            memory_mb = 4096
            disk_gb = 40
            currency = "EUR"
            price_per_quantum = "11"  # minor units per hour
            quantum_seconds = 3600
            enabled = True
            description = ""

        class Disabled(Offer):
            enabled = False

        return [Offer(), Disabled()]


class FakeSshKeyService:
    def __init__(self) -> None:
        self.deleted: list[Any] = []

    async def register(
        self, *, actor_user_id: Any, owner_user_id: Any, name: str, public_key: str
    ) -> Any:
        key = type(
            "Key",
            (),
            {
                "id": uuid4(),
                "name": name,
                "fingerprint": "SHA256:abc",
                "created_at": None,
                "user_id": owner_user_id,
            },
        )()
        return key

    async def list_keys(self, *, actor_user_id: Any, owner_user_id: Any) -> list[Any]:
        return []

    async def delete(self, *, actor_user_id: Any, key_id: Any) -> None:
        raise SshKeyNotFoundError(f"ssh key {key_id} not found")


def build_app(auth: TokenAuthentication | None = ALL_SCOPES_AUTH) -> TestClient:
    app = FastAPI()
    app.include_router(v1_router)
    from cloud_platform.api.v1.errors import install_error_handlers

    install_error_handlers(app)
    if auth is not None:
        app.dependency_overrides[get_token_authentication] = lambda: auth
    app.dependency_overrides[_catalog_repo] = lambda: FakeCatalogRepo()
    app.dependency_overrides[_ssh_key_service] = lambda: FakeSshKeyService()
    return TestClient(app)


def assert_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """The one true error shape."""
    error = payload["error"]
    assert set(error) == {"code", "message", "details"}
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str)
    return error


class TestErrorEnvelope:
    def test_unknown_route_is_enveloped_404(self) -> None:
        with build_app() as client:
            resp = client.get("/v1/does-not-exist")
        assert resp.status_code == 404
        assert_envelope(resp.json())

    def test_missing_identity_is_401_envelope(self) -> None:
        app = FastAPI()
        app.include_router(v1_router)
        from cloud_platform.api.v1.errors import install_error_handlers

        install_error_handlers(app)
        app.dependency_overrides.pop(get_token_authentication, None)
        with TestClient(app) as client:
            resp = client.get("/v1/wallet")
        assert resp.status_code == 401
        error = assert_envelope(resp.json())
        assert error["code"] == ErrorCode.UNAUTHORIZED.value

    def test_invalid_body_is_validation_error_envelope(self) -> None:
        with build_app() as client:
            resp = client.post(
                "/v1/ssh-keys",
                headers={"Idempotency-Key": "k1"},
                json={"unexpected": 1},
            )
        # handler validates body itself -> validation_error envelope
        assert resp.status_code == 400
        error = assert_envelope(resp.json())
        assert error["code"] == ErrorCode.VALIDATION_ERROR.value

    def test_domain_error_maps_to_stable_code(self) -> None:
        with build_app() as client:
            resp = client.delete(
                f"/v1/ssh-keys/{uuid4()}",
                headers={"Idempotency-Key": "k2"},
            )
        assert resp.status_code == 404
        error = assert_envelope(resp.json())
        assert error["code"] == ErrorCode.NOT_FOUND.value


class TestScopeContract:
    def test_token_without_scope_is_forbidden(self) -> None:
        limited = TokenAuthentication(
            user_id=USER_ID,
            scopes=frozenset({TokenScope.CATALOG_READ}),
            token_id=uuid4(),
        )
        with build_app(auth=limited) as client:
            ok = client.get("/v1/catalog/offers")
            forbidden = client.get("/v1/wallet")
        assert ok.status_code == 200
        assert forbidden.status_code == 403
        error = assert_envelope(forbidden.json())
        assert error["code"] == ErrorCode.FORBIDDEN.value
        assert "wallet:read" in error["message"]

    def test_write_scope_needed_for_mutations(self) -> None:
        read_only = TokenAuthentication(
            user_id=USER_ID,
            scopes=frozenset({TokenScope.SERVERS_READ}),
            token_id=uuid4(),
        )
        with build_app(auth=read_only) as client:
            resp = client.post(
                f"/v1/servers/{uuid4()}/actions/power-on", headers={"Idempotency-Key": "k"}
            )
        assert resp.status_code == 403
        assert_envelope(resp.json())


class TestIdempotencyContract:
    @pytest.mark.parametrize(
        ("method", "path"), [("post", "/v1/ssh-keys"), ("delete", f"/v1/servers/{uuid4()}")]
    )
    def test_mutations_require_the_header(self, method: str, path: str) -> None:
        with build_app() as client:
            resp = getattr(client, method)(path)
        assert resp.status_code == 428
        error = assert_envelope(resp.json())
        assert error["code"] == ErrorCode.IDEMPOTENCY_REQUIRED.value

    def test_reads_do_not_need_a_key(self) -> None:
        with build_app() as client:
            resp = client.get("/v1/catalog/offers")
        assert resp.status_code == 200

    def test_oversized_key_is_validation_error(self) -> None:
        with build_app() as client:
            resp = client.post(
                "/v1/ssh-keys",
                headers={"Idempotency-Key": "x" * 129},
                json={"name": "n", "public_key": "k"},
            )
        assert resp.status_code == 400
        assert_envelope(resp.json())


class TestResources:
    def test_catalog_offers_only_enabled(self) -> None:
        with build_app() as client:
            resp = client.get("/v1/catalog/offers")
        body = resp.json()
        assert len(body["offers"]) == 1
        offer = body["offers"][0]
        assert offer["provider_key"] == "hetzner"
        assert offer["price_per_quantum"] == "11"

    def test_ssh_key_registration_returns_fingerprint(self) -> None:
        with build_app() as client:
            resp = client.post(
                "/v1/ssh-keys",
                headers={"Idempotency-Key": "k3"},
                json={"name": "laptop", "public_key": "ssh-ed25519 AAA"},
            )
        assert resp.status_code == 201
        assert resp.json()["key"]["fingerprint"] == "SHA256:abc"


class TestOpenApiStability:
    def test_v1_paths_are_pinned(self) -> None:
        app = FastAPI()
        app.include_router(v1_router)
        paths = set(app.openapi()["paths"])
        expected = {
            "/v1/catalog/offers",
            "/v1/wallet",
            "/v1/servers",
            "/v1/servers/{server_id}",
            "/v1/servers/{server_id}/actions/{action}",
            "/v1/ssh-keys",
            "/v1/ssh-keys/{key_id}",
            "/v1/auth/tokens",
            "/v1/auth/tokens/{token_id}",
        }
        missing = expected - paths
        assert not missing, f"contract paths missing from OpenAPI: {missing}"

    def test_all_v1_routes_are_version_prefixed(self) -> None:
        app = FastAPI()
        app.include_router(v1_router)
        for path in app.openapi()["paths"]:
            assert (
                path.startswith("/v1/")
                or path.startswith("/health")
                or path.startswith("/webhooks")
            ), path


class FakeTokenService:
    """Token management surface behind /v1/auth/tokens."""

    def __init__(self) -> None:
        self.revoked: list[UUID] = []

    async def create(
        self, *, actor_user_id: UUID, owner_user_id: UUID, name: str, scopes: Any
    ) -> tuple[Any, str]:
        token = type(
            "T",
            (),
            {"id": uuid4(), "name": name, "scopes": scopes},
        )()
        return token, "cpt_plaintext-shown-once"

    async def revoke(self, *, actor_user_id: UUID, token_id: UUID) -> None:
        self.revoked.append(token_id)


class TestTokenManagementSurface:
    def test_create_returns_plaintext_exactly_once(self) -> None:
        service = FakeTokenService()
        with build_app() as client:
            client.app.dependency_overrides[_token_service] = lambda: service  # type: ignore[attr-defined]
            resp = client.post(
                "/v1/auth/tokens",
                headers={"Idempotency-Key": "tk1"},
                json={"name": "ci", "scopes": ["catalog:read"]},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["plaintext"].startswith("cpt_")
        assert body["token"]["name"] == "ci"

    def test_revoke_is_wired(self) -> None:
        service = FakeTokenService()
        target = uuid4()
        with build_app() as client:
            client.app.dependency_overrides[_token_service] = lambda: service  # type: ignore[attr-defined]
            resp = client.delete(f"/v1/auth/tokens/{target}", headers={"Idempotency-Key": "tk2"})
        assert resp.status_code == 204
        assert service.revoked == [target]
