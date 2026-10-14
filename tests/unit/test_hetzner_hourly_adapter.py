"""Hetzner hourly adapter against documented /v1 envelope shapes (no live writes)."""

from __future__ import annotations

import json
import secrets

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.credentials import CredentialHolder, credential_from_value
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderCapacityError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderUnavailable,
)
from cloud_platform.providers.hetzner.client import HetznerCloudProvider, operation_label
from cloud_platform.providers.hetzner.hourly import HetznerHourlyCloudProvider


def envelope(key: str, values: list[dict], next_page: int | None = None, *, page: int = 1) -> dict:
    return {key: values, "meta": {"pagination": {"page": page, "next_page": next_page}}}


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
        if request.url.path == "/v1/pricing":
            return reply({"pricing": {"currency": "EUR"}})
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
        recovered = await provider.find_by_reference("fsn1", "srv-123")
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
                await provider.find_by_reference("fsn1", "srv-123")
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
        if request.url.path == "/v1/pricing":
            return reply({"pricing": {"currency": "EUR"}})
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
        assert len(posts) == 1
        assert posts[0] == {
            "name": "srv-123",
            "server_type": "cx22",
            "image": "100",
            "location": "fsn1",
            "labels": {"platform-operation": operation_label("test-key-123")},
        }
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
                await provider.find_by_reference("fsn1", "srv-123")
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
            if create_status == 403:
                return reply({"error": {"code": "resource_limit_exceeded"}}, 403)
            return reply(
                {
                    "server": {**server(), "labels": json.loads(request.content)["labels"]},
                    "action": {"status": "running"},
                    "root_password": None,
                },
                201,
            )
        if request.url.path == "/v1/servers":
            return reply(envelope("servers", []))
        if request.url.path == "/v1/pricing":
            return reply({"pricing": {"currency": "EUR"}})
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
        create_status = 403
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
                    "server": {**server(), "labels": json.loads(request.content)["labels"]},
                    "action": {"status": "running"},
                    "next_actions": [],
                    "root_password": issued if len(posted) == 1 else None,
                },
                201,
            )
        if request.url.path == "/v1/servers":
            return reply(envelope("servers", []))
        if request.url.path == "/v1/pricing":
            return reply({"pricing": {"currency": "EUR"}})
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
            envelope(
                "servers",
                [] if page == 1 else [server()],
                next_page=2 if page == 1 else None,
                page=page,
            )
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


@pytest.mark.asyncio
async def test_account_bound_hourly_mutations_share_rotating_credentials() -> None:
    holder = CredentialHolder("synthetic-initial")
    monthly = HetznerCloudProvider("synthetic-initial", credential_source=holder, account_id="hz-b")
    hourly = HetznerHourlyCloudProvider(provider=monthly, account_id="hz-b")
    mutations: list[str] = []
    rotated_headers: list[bool] = []

    def handle(request: httpx.Request) -> httpx.Response:
        rotated_headers.append(request.headers["Authorization"] == "Bearer synthetic-rotated")
        if request.method == "POST":
            mutations.append("POST")
            return reply(
                {"server": {**server(), "labels": json.loads(request.content)["labels"]}}, 201
            )
        if request.method == "DELETE":
            mutations.append("DELETE")
            return httpx.Response(204)
        if request.url.path == "/v1/servers/54321":
            return reply({"server": server()})
        if request.url.path == "/v1/servers":
            return reply(envelope("servers", []))
        if request.url.path == "/v1/pricing":
            return reply({"pricing": {"currency": "EUR"}})
        if request.url.path == "/v1/server_types":
            return reply(envelope("server_types", [server_type()]))
        if request.url.path == "/v1/locations":
            return reply(envelope("locations", [{"id": 1, "name": "fsn1"}]))
        if request.url.path == "/v1/images":
            return reply(envelope("images", [image()]))
        raise AssertionError(request.url)

    await monthly._client.aclose()
    monthly._client = httpx.AsyncClient(
        base_url="https://api.hetzner.cloud/v1", transport=httpx.MockTransport(handle)
    )
    try:
        await holder.swap(credential_from_value("synthetic-rotated"))
        created = await hourly.create_instance(
            instance_type="cx22",
            image_id="100",
            region="fsn1",
            reference="srv-123",
            root_disk_size_gb=40,
            root_disk_storage_type="local",
            idempotency_key=IdempotencyKey("shared-provider"),
        )
        assert created.account_id == "hz-b"
        assert (await hourly.get_instance("54321")).account_id == "hz-b"
        await hourly.delete_instance("54321")
        await hourly.aclose()
        # Closing a borrowing hourly adapter must not close the managed provider.
        assert (await monthly.get_server("54321")).id == "54321"
        assert mutations == ["POST", "DELETE"]
        assert rotated_headers and all(rotated_headers)
    finally:
        await monthly.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_shared_mutation_delete_unknown_status_is_never_resent(status: int) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(status, json={"error": {"code": "unavailable"}})

    provider = adapter(handle)
    try:
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.delete_instance("54321")
        assert calls == ["DELETE"]
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["id", "name", "image", "server_type", "datacenter", "labels", "json", "shape"],
)
async def test_unproven_successful_hourly_create_remains_unknown(failure: str) -> None:
    posts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append("POST")
            if failure == "json":
                return httpx.Response(201, content=b"not-json")
            if failure == "shape":
                return httpx.Response(201, json=[])
            item = {**server(), "labels": json.loads(request.content)["labels"]}
            item[failure] = None
            return reply({"server": item}, 201)
        if request.url.path == "/v1/servers":
            return reply(envelope("servers", []))
        if request.url.path == "/v1/pricing":
            return reply({"pricing": {"currency": "EUR"}})
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
                idempotency_key=IdempotencyKey("unknown-response"),
            )
        assert posts == ["POST"]
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_create_ambiguous_http_status_sends_once(status: int) -> None:
    posts = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request)
            return reply({"error": {"code": "service_error"}}, status)
        if request.url.path == "/v1/pricing":
            return reply({"pricing": {"currency": "EUR"}})
        if request.url.path == "/v1/server_types":
            return reply(envelope("server_types", [server_type()]))
        if request.url.path == "/v1/images":
            return reply(envelope("images", [image()]))
        if request.url.path == "/v1/locations":
            return reply(envelope("locations", [{"id": 1, "name": "fsn1"}]))
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
                root_disk_storage_type="LOCAL",
                idempotency_key=IdempotencyKey("hourly:ambiguous"),
            )
        assert len(posts) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["matched", "duplicate", "foreign", "partial"])
async def test_exact_operation_recovery_is_read_only_and_fail_closed(mode: str) -> None:
    key = "server-create:12345678-1234-1234-1234-123456789abc"
    label = operation_label(key)
    assert len(label) == 63
    assert label.startswith("op-")
    assert all(char in "0123456789abcdef" for char in label[3:])
    assert label != operation_label(key + "-different")
    calls = []
    item = {
        **server(),
        "labels": {"platform-operation": label, "platform_server_id": "platform-123"},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.method == "GET"
        if request.url.path == "/v1/servers/54321":
            return reply({"server": item})
        assert request.url.path == "/v1/servers"
        if mode == "partial":
            return reply({"servers": [item], "meta": {}})
        rows = [item]
        if mode == "duplicate":
            rows.append({**item, "id": 54322})
        if mode == "foreign":
            rows = [{**item, "labels": {**item["labels"], "platform_server_id": "other"}}]
        return reply(envelope("servers", rows))

    provider = adapter(handle)
    try:
        if mode == "matched":
            found = await provider.find_by_operation(key, platform_server_id="platform-123")
            assert found.id == "54321"
            assert calls == [("GET", "/v1/servers"), ("GET", "/v1/servers/54321")]
        else:
            error = ProviderUnavailable if mode == "partial" else ProviderConflict
            with pytest.raises(error):
                await provider.find_by_operation(key, platform_server_id="platform-123")
            assert calls == [("GET", "/v1/servers")]
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("label_key", ["platform-operation", "platform_server_id"])
async def test_mismatched_acceptance_label_is_unknown(label_key: str) -> None:
    posts = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            posts.append(body)
            labels = {**body["labels"], label_key: "foreign"}
            return reply({"server": {**server(), "labels": labels}}, 201)
        if request.url.path == "/v1/pricing":
            return reply({"pricing": {"currency": "EUR"}})
        if request.url.path == "/v1/server_types":
            return reply(envelope("server_types", [server_type()]))
        if request.url.path == "/v1/images":
            return reply(envelope("images", [image()]))
        if request.url.path == "/v1/locations":
            return reply(envelope("locations", [{"id": 1, "name": "fsn1"}]))
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
                root_disk_storage_type="LOCAL",
                idempotency_key=IdempotencyKey("hourly:labels"),
                platform_server_id="platform-123",
            )
        assert len(posts) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_candidate_currency_mismatch_refuses_before_any_billable_post() -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.method == "GET"
        if request.url.path == "/v1/pricing":
            return reply({"pricing": {"currency": "USD"}})
        if request.url.path == "/v1/server_types":
            return reply(envelope("server_types", [server_type()]))
        if request.url.path == "/v1/locations":
            return reply(envelope("locations", [{"name": "fsn1"}]))
        if request.url.path == "/v1/images":
            return reply(envelope("images", [image()]))
        raise AssertionError(request.url)

    provider = adapter(handle)
    try:
        with pytest.raises(ProviderConflict):
            await provider.validate_hourly_offer_for_checkout(
                location_id="fsn1",
                product_id="cx22",
                image_id="100",
                currency="EUR",
                expected_cost_minor=1,
                expected_cost_exact="0.0075",
            )
        assert paths == ["/v1/pricing"]
        facts = await provider.validate_hourly_offer_for_checkout(
            location_id="fsn1",
            product_id="cx22",
            image_id="100",
            currency="USD",
            expected_cost_minor=1,
            expected_cost_exact="0.0075",
        )
        assert facts.instance_type.currency == "USD"
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"pricing": {}}, {"pricing": {"currency": "ZZZ"}}])
async def test_checkout_missing_currency_fails_before_mutation(payload: dict) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/pricing"
        return reply(payload)

    provider = adapter(handle)
    try:
        with pytest.raises(ProviderUnavailable):
            await provider.validate_hourly_offer_for_checkout(
                location_id="fsn1",
                product_id="cx22",
                image_id="100",
                currency="EUR",
                expected_cost_minor=1,
                expected_cost_exact="0.0075",
            )
        assert len(requests) == 1
    finally:
        await provider.close()
