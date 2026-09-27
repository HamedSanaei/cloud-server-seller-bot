"""Hetzner hourly adapter against documented /v1 envelope shapes (no live writes)."""

from __future__ import annotations

import json
import secrets

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderCapacityError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderUnavailable,
)
from cloud_platform.providers.hetzner.hourly import HetznerHourlyCloudProvider


def envelope(key: str, values: list[dict], next_page: int | None = None) -> dict:
    return {key: values, "meta": {"pagination": {"page": 1, "next_page": next_page}}}


def server(name: str = "srv-123", location: str = "fsn1", image: int = 100) -> dict:
    return {
        "id": 54321,
        "name": name,
        "status": "initializing",
        "server_type": {"id": 22, "name": "cx22"},
        "image": {"id": image, "name": "ubuntu-24.04"},
        "datacenter": {"location": {"name": location}},
        "public_net": {"ipv4": {"ip": "192.0.2.10"}},
    }


def server_type(architecture: str = "x86") -> dict:
    return {
        "id": 22,
        "name": "cx22",
        "architecture": architecture,
        "cores": 2,
        "memory": 4,
        "disk": 40,
        "storage_type": "local",
        "locations": [{"name": "fsn1", "available": True}],
        "prices": [
            {
                "location": "fsn1",
                "price_hourly": {"gross": "0.0075"},
                "price_monthly": {"gross": "3.92"},
                "included_traffic": 21990232555520,
            }
        ],
    }


def image(architecture: str = "x86") -> dict:
    return {
        "id": 100,
        "name": "ubuntu-24.04",
        "type": "system",
        "status": "available",
        "os_flavor": "ubuntu",
        "architecture": architecture,
        "deprecated": False,
    }


def adapter(handler) -> HetznerHourlyCloudProvider:
    provider = HetznerHourlyCloudProvider("not-a-real-token")
    provider._provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.hetzner.cloud/v1"
    )
    return provider


def reply(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


@pytest.mark.asyncio
async def test_authoritative_locations_prices_images_and_validation() -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/locations":
            return reply(
                envelope(
                    "locations",
                    [
                        {
                            "id": 1,
                            "name": "fsn1",
                            "description": "Falkenstein 1",
                            "country": "DE",
                            "city": "Falkenstein",
                        }
                    ],
                )
            )
        if request.url.path == "/v1/server_types":
            return reply(envelope("server_types", [server_type()]))
        if request.url.path == "/v1/images":
            return reply(
                envelope(
                    "images",
                    [
                        image(),
                        {**image("arm"), "id": 103},
                        {**image(), "id": 101, "deprecated": True},
                        {**image(), "id": 102, "status": "unavailable"},
                    ],
                )
            )
        raise AssertionError(request.url)

    provider = adapter(handle)
    try:
        assert (await provider.read_locations())[0].id == "fsn1"
        read = await provider.read_instance_types("fsn1")
        assert (
            read.plans[0].plan_id,
            read.plans[0].hourly_rate_exact,
            read.plans[0].monthly_rate_exact,
        ) == ("cx22", "0.0075", "3.92")
        assert [item.id for item in await provider.list_images("fsn1")] == ["100", "103"]
        facts = await provider.validate_hourly_offer_for_checkout(
            location_id="fsn1",
            product_id="cx22",
            image_id="100",
            currency="EUR",
            expected_cost_minor=1,
            expected_cost_exact="0.0075",
        )
        assert (facts.root_disk.size_gb, facts.root_disk.storage_type) == (40, "LOCAL")
        pinned = await provider.validate_hourly_offer_for_checkout(
            location_id="fsn1",
            product_id="cx22",
            image_id="100",
            currency="EUR",
            expected_cost_minor=1,
            expected_cost_exact="0.0075",
            root_disk_size_gb=40,
            root_disk_storage_type="LOCAL",
        )
        assert pinned.root_disk == facts.root_disk
        with pytest.raises(ProviderConflict):
            await provider.validate_hourly_offer_for_checkout(
                location_id="fsn1",
                product_id="cx22",
                image_id="100",
                currency="EUR",
                expected_cost_minor=1,
                expected_cost_exact="0.0076",
            )
        with pytest.raises(ProviderNotFound):
            await provider.validate_hourly_offer_for_checkout(
                location_id="fsn1",
                product_id="cx22",
                image_id="103",
                currency="EUR",
                expected_cost_minor=1,
                expected_cost_exact="0.0075",
            )
        assert all(path.startswith("/v1/") for path in paths)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_existing_reference_recovered_without_second_post() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + request.url.path)
        assert request.method == "GET"
        assert request.url.params.get("name") == "srv-123"
        return reply(envelope("servers", [server()]))

    provider = adapter(handle)
    try:
        recovered = await provider.create_instance(
            instance_type="cx22",
            image_id="100",
            region="fsn1",
            reference="srv-123",
            root_disk_size_gb=40,
            root_disk_storage_type="local",
            idempotency_key=IdempotencyKey("test-key-123"),
        )
        assert (recovered.id, recovered.region, recovered.plan_id, recovered.image_id) == (
            "54321",
            "fsn1",
            "cx22",
            "100",
        )
        assert calls == ["GET /v1/servers"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_incomplete_or_ambiguous_recovery_never_posts() -> None:
    for rows in ([{**server(), "datacenter": None}], [server(), {**server(), "id": 54322}]):

        def handle(request: httpx.Request, rows=rows) -> httpx.Response:
            assert request.method == "GET"
            return reply(envelope("servers", rows))

        provider = adapter(handle)
        try:
            with pytest.raises(ProviderError):
                await provider.create_instance(
                    instance_type="cx22",
                    image_id="100",
                    region="fsn1",
                    reference="srv-123",
                    root_disk_size_gb=40,
                    root_disk_storage_type="local",
                    idempotency_key=IdempotencyKey("test-key-123"),
                )
        finally:
            await provider.close()


@pytest.mark.asyncio
async def test_create_posts_only_documented_fields_and_unknown_is_never_retried() -> None:
    posts: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return reply({"error": {"code": "service_error"}}, 503)
        if request.url.path == "/v1/servers":
            return reply(envelope("servers", []))
        if request.url.path == "/v1/server_types":
            return reply(envelope("server_types", [server_type()]))
        if request.url.path == "/v1/locations":
            return reply(envelope("locations", [{"id": 1, "name": "fsn1"}]))
        if request.url.path == "/v1/images":
            return reply(envelope("images", [image()]))
        raise AssertionError(request.url)

    provider = adapter(handle)
    try:
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.create_instance(
                instance_type="cx22",
                image_id="100",
                region="fsn1",
                reference="srv-123",
                root_disk_size_gb=40,
                root_disk_storage_type="local",
                idempotency_key=IdempotencyKey("test-key-123"),
            )
        assert posts == [
            {"name": "srv-123", "server_type": "cx22", "image": "100", "location": "fsn1"}
        ]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_failed_recovery_read_and_auth_block_creation() -> None:
    for code, expected in ((503, ProviderUnavailable), (401, ProviderAuthError)):

        def handle(request: httpx.Request, code=code) -> httpx.Response:
            assert request.method == "GET"
            return reply({"error": {"message": "unavailable"}}, code)

        provider = adapter(handle)
        try:
            with pytest.raises(expected):
                await provider.create_instance(
                    instance_type="cx22",
                    image_id="100",
                    region="fsn1",
                    reference="srv-123",
                    root_disk_size_gb=40,
                    root_disk_storage_type="local",
                    idempotency_key=IdempotencyKey("test-key-123"),
                )
        finally:
            await provider.close()


@pytest.mark.asyncio
async def test_delete_is_idempotent_for_missing_id_and_unknown_on_ambiguous_failure() -> None:
    status = 404

    def handle(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("DELETE", "/v1/servers/54321")
        return reply({"error": {"message": "unavailable"}}, status)

    provider = adapter(handle)
    try:
        await provider.delete_instance("54321")
        status = 503
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.delete_instance("54321")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_create_attaches_proven_provider_identity_and_classifies_quota() -> None:
    create_status = 201

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            if create_status == 422:
                return reply({"error": {"code": "resource_limit_exceeded"}}, 422)
            return reply(
                {"server": server(), "action": {"status": "running"}, "root_password": None},
                201,
            )
        if request.url.path == "/v1/servers":
            return reply(envelope("servers", []))
        if request.url.path == "/v1/server_types":
            return reply(envelope("server_types", [server_type()]))
        if request.url.path == "/v1/locations":
            return reply(envelope("locations", [{"id": 1, "name": "fsn1"}]))
        if request.url.path == "/v1/images":
            return reply(envelope("images", [image()]))
        raise AssertionError(request.url)

    provider = adapter(handle)
    kwargs = dict(
        instance_type="cx22",
        image_id="100",
        region="fsn1",
        reference="srv-123",
        root_disk_size_gb=40,
        root_disk_storage_type="local",
        idempotency_key=IdempotencyKey("test-key-123"),
    )
    try:
        created = await provider.create_instance(**kwargs)
        assert (created.id, created.status, created.image_id, created.plan_id) == (
            "54321",
            "initializing",
            "100",
            "cx22",
        )
        create_status = 422
        with pytest.raises(ProviderCapacityError):
            await provider.create_instance(**kwargs)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_create_password_is_one_time_and_only_from_top_level_without_ssh_key() -> None:
    issued = secrets.token_urlsafe(32)
    posted: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return reply(
                {
                    "server": server(),
                    "action": {"status": "running"},
                    "next_actions": [],
                    "root_password": issued if len(posted) == 1 else None,
                },
                201,
            )
        if request.url.path == "/v1/servers":
            return reply(envelope("servers", []))
        if request.url.path == "/v1/server_types":
            return reply(envelope("server_types", [server_type()]))
        if request.url.path == "/v1/locations":
            return reply(envelope("locations", [{"id": 1, "name": "fsn1"}]))
        if request.url.path == "/v1/images":
            return reply(envelope("images", [image()]))
        raise AssertionError(request.url)

    provider = adapter(handle)
    kwargs = dict(
        instance_type="cx22",
        image_id="100",
        region="fsn1",
        reference="srv-123",
        root_disk_size_gb=40,
        root_disk_storage_type="local",
        idempotency_key=IdempotencyKey("test-key-123"),
    )
    try:
        created = await provider.create_instance(**kwargs)
        assert created.create_password is not None
        assert created.ssh_username == "root"
        assert issued not in repr(created)
        assert created.create_password.reveal() == issued
        assert created.create_password.reveal() is None
        with_key = await provider.create_instance(**kwargs, ssh_key_id="selected-key")
        assert with_key.create_password is None
        assert with_key.ssh_username is None
        assert "ssh_keys" not in posted[0]
        assert posted[1]["ssh_keys"] == ["selected-key"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_reference_search_reads_every_page_and_get_by_provider_id() -> None:
    pages: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/servers/54321":
            return reply({"server": server()})
        page = int(request.url.params["page"])
        pages.append(page)
        assert request.url.params["name"] == "srv-123"
        return reply(
            envelope("servers", [] if page == 1 else [server()], next_page=2 if page == 1 else None)
        )

    provider = adapter(handle)
    try:
        assert (await provider.find_by_reference("fsn1", "srv-123")).id == "54321"
        assert pages == [1, 2]
        assert (await provider.get_instance("54321")).id == "54321"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_incomplete_pagination_cannot_prove_reference_absence() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return reply({"servers": [], "meta": {}})

    provider = adapter(handle)
    try:
        with pytest.raises(ProviderError, match="incomplete list envelope"):
            await provider.find_by_reference("fsn1", "srv-123")
    finally:
        await provider.close()
