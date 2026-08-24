"""Contract tests for REST API v1 (M14-001).

Acceptance: stable resource/error/idempotency contract.

These tests PIN the contract:

- every error response uses the one envelope
  ``{"error": {"code", "message", "details"}}`` with stable codes;
- mutating endpoints REQUIRE an ``Idempotency-Key`` (428 otherwise);
- identity is required (401 envelope);
- the /v1 resource paths exist and are versioned;
- domain errors map to stable codes without leaking foreign resources.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_platform.api.v1 import ErrorCode
from cloud_platform.api.v1 import router as v1_router
from cloud_platform.api.v1.dependencies import USER_HEADER, get_current_user_id
from cloud_platform.api.v1.router import _catalog_repo, _ssh_key_service
from cloud_platform.modules.sshkeys.domain import SshKeyNotFoundError

USER_ID = uuid4()


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


def build_app() -> TestClient:
    app = FastAPI()
    app.include_router(v1_router)
    from cloud_platform.api.v1.errors import install_error_handlers

    install_error_handlers(app)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID
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
            resp = client.get("/v1/does-not-exist", headers={USER_HEADER: str(USER_ID)})
        assert resp.status_code == 404
        assert_envelope(resp.json())

    def test_missing_identity_is_401_envelope(self) -> None:
        app = FastAPI()
        app.include_router(v1_router)
        from cloud_platform.api.v1.errors import install_error_handlers

        install_error_handlers(app)
        app.dependency_overrides.pop(get_current_user_id, None)
        with TestClient(app) as client:
            resp = client.get("/v1/wallet")
        assert resp.status_code == 401
        error = assert_envelope(resp.json())
        assert error["code"] == ErrorCode.UNAUTHORIZED.value

    def test_invalid_body_is_validation_error_envelope(self) -> None:
        with build_app() as client:
            resp = client.post(
                "/v1/ssh-keys",
                headers={USER_HEADER: str(USER_ID), "Idempotency-Key": "k1"},
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
                headers={USER_HEADER: str(USER_ID), "Idempotency-Key": "k2"},
            )
        assert resp.status_code == 404
        error = assert_envelope(resp.json())
        assert error["code"] == ErrorCode.NOT_FOUND.value


class TestIdempotencyContract:
    @pytest.mark.parametrize(
        ("method", "path"), [("post", "/v1/ssh-keys"), ("delete", f"/v1/servers/{uuid4()}")]
    )
    def test_mutations_require_the_header(self, method: str, path: str) -> None:
        with build_app() as client:
            resp = getattr(client, method)(path, headers={USER_HEADER: str(USER_ID)})
        assert resp.status_code == 428
        error = assert_envelope(resp.json())
        assert error["code"] == ErrorCode.IDEMPOTENCY_REQUIRED.value

    def test_reads_do_not_need_a_key(self) -> None:
        with build_app() as client:
            resp = client.get("/v1/catalog/offers", headers={USER_HEADER: str(USER_ID)})
        assert resp.status_code == 200

    def test_oversized_key_is_validation_error(self) -> None:
        with build_app() as client:
            resp = client.post(
                "/v1/ssh-keys",
                headers={USER_HEADER: str(USER_ID), "Idempotency-Key": "x" * 129},
                json={"name": "n", "public_key": "k"},
            )
        assert resp.status_code == 400
        assert_envelope(resp.json())


class TestResources:
    def test_catalog_offers_only_enabled(self) -> None:
        with build_app() as client:
            resp = client.get("/v1/catalog/offers", headers={USER_HEADER: str(USER_ID)})
        body = resp.json()
        assert len(body["offers"]) == 1
        offer = body["offers"][0]
        assert offer["provider_key"] == "hetzner"
        assert offer["price_per_quantum"] == "11"

    def test_ssh_key_registration_returns_fingerprint(self) -> None:
        with build_app() as client:
            resp = client.post(
                "/v1/ssh-keys",
                headers={USER_HEADER: str(USER_ID), "Idempotency-Key": "k3"},
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
