"""Fake-HTTP tests for the Leaseweb ordering-VPS adapter (LEASEWEB-MVP).

Covers the Ordering API contract: X-LSW-Auth header, product list/detail
parsing, price parsing (integer minor units, no float), order request
serialization, LEDGER-OWNED dedup (a fresh operation always POSTs exactly
once — account-wide order similarity never suppresses or reuses an order),
conservative ambiguous-outcome classification, read-only recovery (generic
candidates are NEVER auto-attached), error mapping and rate-limit retry.
POSTs are always mocked — no real order can ever be placed here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.base import (
    CreateServerRequest,
    OrderRecoveryVerdict,
)
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.leaseweb.client import Throttle
from cloud_platform.providers.leaseweb.ordering import (
    LeaseWebOrderingProvider,
    to_minor_units,
)

KEY = "LSW-ORDERING-TEST-KEY"
IK = IdempotencyKey("order-create:test-server")


def _no_sleep() -> Throttle:
    async def wait(_s: float) -> None:
        return None

    return Throttle(max_rps=1000.0, wait=wait)


def _response(status_code: int, payload: Any = None) -> httpx.Response:
    body = b"" if payload is None else json.dumps(payload).encode()
    return httpx.Response(
        status_code,
        headers={"Content-Type": "application/json"},
        content=body,
        request=httpx.Request("GET", "https://api.test/x"),
    )


def _provider(handler: Any) -> LeaseWebOrderingProvider:
    provider = LeaseWebOrderingProvider(
        api_key=KEY,
        base_url="https://api.test",
        locations=("AMS-01", "FRA-01"),
        throttle=_no_sleep(),
        max_retries=2,
    )
    provider._client = AsyncMock()  # type: ignore[method-assign]
    provider._client.request = AsyncMock(side_effect=handler)
    return provider


def _product_item(
    item_id: str = "VPS02_1",
    *,
    name: str = "VPS S",
    vcpu: str = "2",
    ram: str = "4",
    disk: str = "100 GB",
    total: str = "12.99",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "vCpu": vcpu,
        "vRam": ram,
        "nvmeStorage": disk,
        "traffic": "10 TB",
        "price": {"basePrice": "10.00", "total": total, "currency": "EUR"},
    }


def _product_detail_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "vps": {
            **item,
            "location": ["AMS-01", "FRA-01"],
            "configurationOptions": {
                "operatingSystem": {
                    "options": [
                        {"name": "Ubuntu 24.04", "price": "0.00", "currency": "EUR"},
                        {"name": "Windows Server 2022", "price": "15.00", "currency": "EUR"},
                    ]
                },
                "controlPanel": {"options": []},
            },
            "price": {
                "basePrice": "10.00",
                "total": "12.99",
                "currency": "EUR",
                "contractTerms": [
                    {"key": "1_MONTH", "total": "12.99"},
                    {"key": "12_MONTHS", "total": "11.49"},
                ],
            },
        }
    }


class TestAuthHeader:
    def test_constructor_sets_lsw_auth_header(self) -> None:
        provider = LeaseWebOrderingProvider(
            api_key=KEY, base_url="https://x", locations=("AMS-01",), throttle=_no_sleep()
        )
        headers = {k.lower(): v for k, v in provider._client.headers.items()}
        assert headers["x-lsw-auth"] == KEY
        assert "authorization" not in headers

    async def test_key_never_leaks_into_errors(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(401, {"errorMessage": "Invalid API key"}))
        with pytest.raises(ProviderAuthError) as exc:
            await provider.list_products("AMS-01")
        assert KEY not in str(exc.value)


class TestCatalogParsing:
    async def test_list_products_parses_specs_and_price(self) -> None:
        provider = _provider(
            lambda m, p, **kw: _response(
                200, {"_metadata": {"totalCount": 1}, "vpss": [_product_item()]}
            )
        )
        products = await provider.list_products("AMS-01")
        assert len(products) == 1
        product = products[0]
        assert product.id == "VPS02_1"
        assert product.vcpu == 2
        assert product.ram_gb == 4  # vRam is GB per the ordering API
        assert product.disk_gb == 100
        assert product.traffic == "10 TB"
        assert product.monthly_price_minor == 1299
        assert product.currency == "EUR"

    async def test_list_products_paginates(self) -> None:
        calls = 0

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            nonlocal calls
            calls += 1
            offset = int((kw.get("params") or {}).get("offset", 0))
            items = [_product_item(item_id=f"VPS{i}") for i in range(offset, offset + 2)]
            return _response(200, {"_metadata": {"totalCount": 4}, "vpss": items})

        provider = _provider(handler)
        products = await provider.list_products("AMS-01")
        assert len(products) == 4
        assert calls == 2

    async def test_get_product_parses_options_and_monthly_price(self) -> None:
        provider = _provider(
            lambda m, p, **kw: _response(200, _product_detail_payload(_product_item()))
        )
        detail = await provider.get_product("AMS-01", "VPS02_1")
        assert detail.product.monthly_price_minor == 1299
        names = [o.name for o in detail.os_options]
        assert names == ["Ubuntu 24.04", "Windows Server 2022"]
        assert [o.name for o in detail.free_os_options()] == ["Ubuntu 24.04"]
        assert detail.contract_terms == {"1_MONTH": 1299, "12_MONTHS": 1149}

    async def test_get_product_respects_os_allowlist(self) -> None:
        provider = LeaseWebOrderingProvider(
            api_key=KEY,
            base_url="https://api.test",
            locations=("AMS-01",),
            os_allowlist=("ubuntu 24.04",),
            throttle=_no_sleep(),
        )
        provider._client = AsyncMock()  # type: ignore[method-assign]
        provider._client.request = AsyncMock(
            side_effect=lambda m, p, **kw: _response(200, _product_detail_payload(_product_item()))
        )
        detail = await provider.get_product("AMS-01", "VPS02_1")
        assert [o.name for o in detail.os_options] == ["Ubuntu 24.04"]

    async def test_os_name_allowed_rejects_paid_options_by_default(self) -> None:
        provider = _provider(
            lambda m, p, **kw: _response(200, _product_detail_payload(_product_item()))
        )
        detail = await provider.get_product("AMS-01", "VPS02_1")
        assert provider.os_name_allowed(detail, "Ubuntu 24.04") is True
        assert provider.os_name_allowed(detail, "Windows Server 2022") is False
        assert provider.os_name_allowed(detail, "Debian 12") is False


class TestPricing:
    def test_to_minor_units_never_uses_float(self) -> None:
        from decimal import Decimal

        assert to_minor_units(Decimal("12.99")) == 1299
        assert to_minor_units(Decimal("0.005")) == 1  # half-up rounding
        assert to_minor_units(Decimal("10")) == 1000


def _order_row(
    order_id: str = "LS-ORD-OLD",
    *,
    price: str = "12.99",
    currency: str = "EUR",
    term: str = "1_MONTH",
    cycle: str = "1_MONTH",
    created_at: str | None = None,
    product_id: str = "VIRTUAL_SERVER",
) -> dict[str, Any]:
    from datetime import UTC, datetime

    return {
        "id": order_id,
        "type": "NEW_ORDER",
        "createdAt": created_at or datetime.now(UTC).isoformat(),
        "services": [
            {
                "productId": product_id,
                "pricePerFrequency": price,
                "currency": currency,
                "contractTerm": term,
                "billingCycle": cycle,
            }
        ],
    }


def _order_request(**labels: str) -> CreateServerRequest:
    return CreateServerRequest(
        name="srv-test",
        plan_id="VPS02_1",
        image_id="Ubuntu 24.04",
        location_id="AMS-01",
        labels=labels
        or {
            "provider_price_minor": "1299",
            "provider_currency": "EUR",
            "contract_term": "1_MONTH",
            "billing_cycle": "1_MONTH",
            "platform_server_id": "srv-1",
        },
    )


class TestOrdering:
    async def test_order_request_serialization(self) -> None:
        seen: dict[str, Any] = {}

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            seen.update(method=m, path=p, body=kw.get("json"))
            return _response(201, {"orderId": "LS-ORD-123"})

        provider = _provider(handler)
        request = _order_request()
        ticket = await provider.place_order(request, IK)
        assert seen["method"] == "POST"
        assert seen["path"] == "/ordering/v1/products/vps/VPS02_1/order"
        assert seen["body"] == {
            "location": "AMS-01",
            "operatingSystem": "Ubuntu 24.04",
            "contractTerm": "1_MONTH",
            "billingCycle": "1_MONTH",
        }
        assert ticket.provider_order_id == "LS-ORD-123"
        assert ticket.state == "accepted"

    async def test_two_independent_same_plan_orders_are_two_posts(self) -> None:
        """RELEASE-BLOCKER regression: customer A and customer B buy the
        exact same VPS02_1 / AMS-01 / Ubuntu / 12.99 EUR five minutes
        apart. Each MUST produce its own provider POST and its own order
        id — the previous get-before-create heuristic returned A's order
        as B's and never POSTed for B."""
        posts: list[str] = []

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            if m == "POST":
                posts.append(f"{m} {p}")
                return _response(201, {"orderId": f"LS-ORD-{len(posts)}"})
            return _response(200, {"orders": [_order_row("LS-ORD-A")]})

        provider = _provider(handler)
        ticket_a = await provider.place_order(_order_request(), IK)
        ticket_b = await provider.place_order(
            _order_request(), IdempotencyKey("order-create:other")
        )
        # Exactly TWO independent POSTs, no reuse of A's provider order.
        assert len(posts) == 2
        assert ticket_a.provider_order_id == "LS-ORD-1"
        assert ticket_b.provider_order_id == "LS-ORD-2"
        assert ticket_a.provider_order_id != ticket_b.provider_order_id
        assert ticket_a.state == "accepted"
        assert ticket_b.state == "accepted"

    async def test_one_unrelated_prior_order_never_suppresses_fresh_post(self) -> None:
        """The account contains exactly ONE recent VIRTUAL_SERVER order
        matching price/currency/term/cycle. The current local operation is
        brand-new and unrelated: the prior order MUST NOT suppress the
        POST — the account is never even scanned during a fresh POST."""
        calls: list[str] = []

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            calls.append(f"{m} {p}")
            if m == "POST":
                return _response(201, {"orderId": "LS-ORD-NEW"})
            return _response(200, {"orders": [_order_row("LS-ORD-OLD")]})

        provider = _provider(handler)
        ticket = await provider.place_order(_order_request(), IK)
        assert ticket.provider_order_id == "LS-ORD-NEW"
        assert calls == ["POST /ordering/v1/products/vps/VPS02_1/order"]

    async def test_different_plan_same_price_still_posts_fresh_order(self) -> None:
        """One prior VPS02_1 / 12.99 order exists; the new operation orders
        a DIFFERENT VPS product at the same 12.99. The previous provider
        order is never reused: a fresh POST must occur."""
        calls: list[str] = []

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            calls.append(f"{m} {p}")
            if m == "POST":
                return _response(201, {"orderId": "LS-ORD-OTHER-PLAN"})
            return _response(200, {"orders": [_order_row("LS-ORD-A")]})

        provider = _provider(handler)
        ticket = await provider.place_order(_order_request(), IK)
        assert ticket.provider_order_id == "LS-ORD-OTHER-PLAN"
        assert sum(1 for c in calls if c.startswith("POST")) == 1

    async def test_different_location_same_price_still_posts_fresh_order(self) -> None:
        """One prior AMS-01 / 12.99 order exists; the new operation orders
        FRA-01 at the same 12.99. The Orders API cannot show location, so
        a fresh POST MUST occur — the AMS order is never attached to FRA."""
        calls: list[str] = []

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            calls.append(f"{m} {p}")
            if m == "POST":
                return _response(201, {"orderId": "LS-ORD-FRA-NEW"})
            return _response(200, {"orders": [_order_row("LS-ORD-AMS")]})

        provider = _provider(handler)
        request = CreateServerRequest(
            name="srv-test",
            plan_id="VPS02_1",
            image_id="Ubuntu 24.04",
            location_id="FRA-01",
            labels={
                "provider_price_minor": "1299",
                "provider_currency": "EUR",
                "contract_term": "1_MONTH",
                "billing_cycle": "1_MONTH",
            },
        )
        ticket = await provider.place_order(request, IK)
        assert ticket.provider_order_id == "LS-ORD-FRA-NEW"
        assert sum(1 for c in calls if c.startswith("POST")) == 1

    async def test_unreachable_orders_endpoint_never_blocks_a_fresh_post(self) -> None:
        """The local operation ledger — not the account order history — is
        the dedup mechanism. If GET /account/v1/orders is down, a fresh
        POST still proceeds (the previous get-before-create backstop would
        have blocked it)."""
        posts = 0

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            nonlocal posts
            if m == "POST":
                posts += 1
                return _response(201, {"orderId": "LS-ORD-1"})
            raise httpx.ConnectError("orders endpoint unreachable")

        provider = _provider(handler)
        ticket = await provider.place_order(_order_request(), IK)
        assert ticket.provider_order_id == "LS-ORD-1"
        assert posts == 1

    async def test_recovery_normalizes_underscore_and_space_term_forms(self) -> None:
        """The ordering API uses 1_MONTH; the orders API reports 1 MONTH.
        The read-only recovery scan must still count the candidate (and
        escalate it as unproven, never attach it)."""
        provider = _provider(
            lambda m, p, **kw: _response(
                200, {"orders": [_order_row(term="1 MONTH", cycle="1 MONTH")]}
            )
        )
        result = await provider.recover_order(
            provider_cost_minor=1299,
            currency="EUR",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            since=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert result.verdict is OrderRecoveryVerdict.AMBIGUOUS
        assert result.candidate_count == 1

    async def test_order_missing_order_id_is_unknown_outcome(self) -> None:
        """A 201 without orderId cannot be trusted: the outcome is unknown,
        never a plain error that a retry would blindly re-POST."""
        provider = _provider(lambda m, p, **kw: _response(201, {"status": "ok"}))
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.place_order(_order_request(), IK)


class TestRecoveryScan:
    async def _recover(
        self, orders_payload: dict[str, Any]
    ) -> tuple[LeaseWebOrderingProvider, Any]:
        provider = _provider(lambda m, p, **kw: _response(200, orders_payload))
        return provider, provider

    async def test_single_generic_candidate_is_ambiguous_never_matched(self) -> None:
        """RELEASE-BLOCKER regression: exactly ONE generic candidate
        matching price/currency/term/cycle/time is NOT proof of ownership
        (another customer's identical order satisfies the same facts). The
        adapter must NOT auto-attach it — AMBIGUOUS, no provider_order_id."""
        provider, _ = await self._recover({"orders": [_order_row("LS-ORD-9")]})
        result = await provider.recover_order(
            provider_cost_minor=1299,
            currency="EUR",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            since=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert result.verdict is OrderRecoveryVerdict.AMBIGUOUS
        assert result.provider_order_id is None
        assert result.candidate_count == 1
        assert "cannot prove" in result.reason

    async def test_single_candidate_with_equipment_id_is_still_not_attached(self) -> None:
        """Even a candidate that already carries an ``equipmentId`` is not
        auto-attached: the equipment chain proves the order's SPEC (another
        same-spec order has the same attributes), not its ownership by this
        local operation — a human decides."""
        row = _order_row("LS-ORD-9")
        row["services"][0]["equipmentId"] = "vps-77"
        provider, _ = await self._recover({"orders": [row]})
        result = await provider.recover_order(
            provider_cost_minor=1299,
            currency="EUR",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            since=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert result.verdict is OrderRecoveryVerdict.AMBIGUOUS
        assert result.provider_order_id is None
        assert result.candidate_count == 1

    async def test_ambiguous_when_several_candidates(self) -> None:
        provider, _ = await self._recover(
            {"orders": [_order_row("LS-ORD-A"), _order_row("LS-ORD-B")]}
        )
        result = await provider.recover_order(
            provider_cost_minor=1299,
            currency="EUR",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            since=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert result.verdict is OrderRecoveryVerdict.AMBIGUOUS
        assert result.provider_order_id is None
        assert result.candidate_count == 2

    async def test_insufficient_data_is_no_match_not_a_guess(self) -> None:
        """Same price but the service row carries no term/cycle/currency:
        the facts cannot be confirmed — NO_MATCH (absence not provable),
        never a candidate pick."""
        provider, _ = await self._recover(
            {
                "orders": [
                    {
                        "id": "LS-ORD-X",
                        "type": "NEW_ORDER",
                        "createdAt": datetime.now(UTC).isoformat(),
                        "services": [{"productId": "VIRTUAL_SERVER", "pricePerFrequency": "12.99"}],
                    }
                ]
            }
        )
        result = await provider.recover_order(
            provider_cost_minor=1299,
            currency="EUR",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            since=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert result.verdict is OrderRecoveryVerdict.NO_MATCH
        assert result.provider_order_id is None

    async def test_scan_failure_is_reported_not_matched(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(503, {"errorMessage": "down"}))
        result = await provider.recover_order(
            provider_cost_minor=1299,
            currency="EUR",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            since=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert result.verdict is OrderRecoveryVerdict.SCAN_FAILED

    async def test_old_orders_outside_window_never_match(self) -> None:
        provider, _ = await self._recover(
            {"orders": [_order_row("LS-ORD-OLD", created_at="2026-08-01T10:00:00+00:00")]}
        )
        result = await provider.recover_order(
            provider_cost_minor=1299,
            currency="EUR",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            since=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert result.verdict is OrderRecoveryVerdict.NO_MATCH


def _failing_post_provider(post_error: Exception) -> tuple[LeaseWebOrderingProvider, list[str]]:
    calls: list[str] = []

    def handler(m: str, p: str, **kw: Any) -> httpx.Response:
        calls.append(f"{m} {p}")
        if m == "POST":
            raise post_error
        return _response(200, {"orders": []})

    return _provider(handler), calls


class TestMutatingTransport:
    async def test_connect_error_before_transmission_is_retryable(self) -> None:
        """A connection-level failure proves NOTHING was transmitted: the
        POST may be retried (the worker re-queues with the same key)."""
        provider, _calls = _failing_post_provider(httpx.ConnectError("connection refused"))
        with pytest.raises(ProviderUnavailable):
            await provider.place_order(_order_request(), IK)

    async def test_connect_timeout_before_transmission_is_retryable(self) -> None:
        provider, _calls = _failing_post_provider(httpx.ConnectTimeout("no route"))
        with pytest.raises(ProviderUnavailable):
            await provider.place_order(_order_request(), IK)

    async def test_pool_timeout_before_transmission_is_retryable(self) -> None:
        provider, _calls = _failing_post_provider(httpx.PoolTimeout("no free connection"))
        with pytest.raises(ProviderUnavailable):
            await provider.place_order(_order_request(), IK)

    async def test_read_timeout_after_transmission_is_unknown(self) -> None:
        """A read timeout happens AFTER the request was transmitted: the
        order may exist — the outcome is UNKNOWN, never auto-retried."""
        provider, calls = _failing_post_provider(httpx.ReadTimeout("no response"))
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.place_order(_order_request(), IK)
        # Exactly ONE POST attempt: no in-adapter re-send of a billable POST.
        assert sum(1 for c in calls if c.startswith("POST")) == 1

    async def test_write_timeout_is_unknown(self) -> None:
        provider, calls = _failing_post_provider(httpx.WriteTimeout("stalled"))
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.place_order(_order_request(), IK)
        assert sum(1 for c in calls if c.startswith("POST")) == 1

    async def test_remote_protocol_error_is_unknown(self) -> None:
        provider, calls = _failing_post_provider(httpx.RemoteProtocolError("connection dropped"))
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.place_order(_order_request(), IK)
        assert sum(1 for c in calls if c.startswith("POST")) == 1

    async def test_5xx_after_post_is_unknown_not_unavailable(self) -> None:
        calls: list[str] = []

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            calls.append(f"{m} {p}")
            if m == "POST":
                return _response(503, {"errorMessage": "down"})
            return _response(200, {"orders": []})

        provider = _provider(handler)
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.place_order(_order_request(), IK)
        assert sum(1 for c in calls if c.startswith("POST")) == 1

    async def test_mutating_429_is_conservatively_unknown_outcome(self) -> None:
        """The Leaseweb contract does NOT state that a 429 guarantees the
        request was NOT processed. A billable POST that returns 429 is
        therefore classified conservatively as an unknown outcome (never
        automatically re-sent) — the operation escalates to read-only
        recovery / manual review instead of a blind second POST."""
        calls: list[str] = []

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            calls.append(f"{m} {p}")
            if m == "POST":
                return _response(429, {"errorMessage": "slow down"})
            return _response(200, {"orders": []})

        provider = _provider(handler)
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.place_order(_order_request(), IK)
        # The adapter never re-sends a billable POST internally.
        assert sum(1 for c in calls if c.startswith("POST")) == 1


class TestErrorMapping:
    async def test_401_maps_to_auth_error(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(401, {"errorMessage": "bad key"}))
        with pytest.raises(ProviderAuthError):
            await provider.list_products("AMS-01")

    async def test_404_maps_to_not_found(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(404, {"errorMessage": "gone"}))
        with pytest.raises(ProviderNotFound):
            await provider.get_product("AMS-01", "VPS02_1")

    async def test_429_maps_to_rate_limited(self) -> None:
        provider = _provider(
            lambda m, p, **kw: _response(429, {"errorMessage": "slow down"}),
        )
        with pytest.raises(ProviderRateLimited):
            await provider.list_products("AMS-01")

    async def test_500_maps_to_unavailable(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(503, {"errorMessage": "down"}))
        with pytest.raises(ProviderUnavailable):
            await provider.list_products("AMS-01")

    async def test_rate_limit_retry_succeeds_after_wait(self) -> None:
        attempts = 0

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _response(429, {"errorMessage": "throttled"})
            return _response(200, {"_metadata": {"totalCount": 0}, "vpss": []})

        waits: list[float] = []

        async def record_wait(seconds: float) -> None:
            waits.append(seconds)

        provider = LeaseWebOrderingProvider(
            api_key=KEY,
            base_url="https://api.test",
            locations=("AMS-01",),
            throttle=Throttle(max_rps=1000.0, wait=record_wait),
            max_retries=2,
        )
        provider._client = AsyncMock()  # type: ignore[method-assign]
        provider._client.request = AsyncMock(side_effect=handler)
        await provider.list_products("AMS-01")
        assert attempts == 2
        # The throttle also records its acquire delays; the retry backoff
        # (0.5s for the first retry) is among them.
        assert 0.5 in waits
        assert len(waits) == 2  # retry backoff + second acquire

    async def test_transport_error_maps_to_unavailable(self) -> None:
        provider = _provider(
            lambda m, p, **kw: (_ for _ in ()).throw(httpx.TimeoutException("timed out"))
        )
        with pytest.raises(ProviderUnavailable):
            await provider.list_products("AMS-01")


class TestVpsManagement:
    async def test_get_server_parses_ips(self) -> None:
        provider = _provider(
            lambda m, p, **kw: _response(
                200,
                {
                    "id": "vps-1",
                    "reference": "srv-1",
                    "state": "running",
                    "datacenter": "AMS-01",
                    "ips": [
                        {"ip": "1.2.3.4", "version": "4"},
                        {"ip": "2001:db8::1", "version": "6"},
                    ],
                },
            )
        )
        server = await provider.get_server("vps-1")
        assert server is not None
        assert server.id == "vps-1"
        assert server.ipv4 == "1.2.3.4"
        assert server.ipv6 == "2001:db8::1"

    async def test_get_server_404_returns_none(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(404, {"errorMessage": "gone"}))
        assert await provider.get_server("vps-missing") is None


class TestOrderingCatalogViews:
    async def test_list_plans_unions_locations(self) -> None:
        calls = []

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            calls.append((m, p, kw.get("params")))
            if m == "GET" and p == "/ordering/v1/products/vps":
                return _response(200, {"vpss": [_product_item()]})
            return _response(200, {"vpss": []})

        provider = _provider(handler)
        plans = await provider.list_plans()
        assert len(plans) == 2  # AMS-01 + FRA-01 each return one product
        plan = plans[0]
        assert plan.id == "VPS02_1"
        assert plan.vcpu == 2
        assert plan.memory_mb == 4 * 1024
        assert plan.metadata["monthly_price_minor"] == 1299

    async def test_list_images_unions_free_os_options(self) -> None:
        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            if m == "GET" and p == "/ordering/v1/products/vps":
                return _response(200, {"vpss": [_product_item()]})
            return _response(200, _product_detail_payload(_product_item()))

        provider = _provider(handler)
        images = await provider.list_images()
        assert {img.id for img in images} == {"Ubuntu 24.04"}

    async def test_list_images_skips_failed_details(self) -> None:
        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            if m == "GET" and p == "/ordering/v1/products/vps":
                return _response(200, {"vpss": [_product_item()]})
            return _response(503, {"errorMessage": "down"})

        provider = _provider(handler)
        assert await provider.list_images() == []


class TestGetOrderAndMatching:
    async def test_get_order_maps_status_and_metadata(self) -> None:
        payload = {
            "id": "LS-ORD-1",
            "contractId": "C-42",
            "type": "NEW_ORDER",
            "services": [
                {
                    "id": "S-1",
                    "productId": "VIRTUAL_SERVER",
                    "status": "ACTIVE",
                    "equipmentId": "vps-77",
                    "pricePerFrequency": 12.99,
                    "currency": "EUR",
                    "contractTerm": "1 MONTH",
                    "billingCycle": "1 MONTH",
                    "deliveryEstimate": "5 - 7 business days",
                }
            ],
        }
        provider = _provider(lambda m, p, **kw: _response(200, payload))
        ticket = await provider.get_order("LS-ORD-1")
        assert ticket.state == "provisioned"
        assert ticket.provider_resource_id == "vps-77"
        assert ticket.metadata["contract_id"] == "C-42"
        assert ticket.metadata["product_id"] == "VIRTUAL_SERVER"
        assert ticket.metadata["order_status"] == "ACTIVE"
        assert ticket.metadata["price_per_frequency_minor"] == 1299
        assert ticket.metadata["contract_term"] == "1 MONTH"

    async def test_get_order_maps_cancelled(self) -> None:
        payload = {
            "id": "LS-ORD-1",
            "services": [{"productId": "VIRTUAL_SERVER", "status": "CANCELLED"}],
        }
        provider = _provider(lambda m, p, **kw: _response(200, payload))
        ticket = await provider.get_order("LS-ORD-1")
        assert ticket.state == "failed"

    async def test_match_vps_for_order_uses_equipment_id(self) -> None:
        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            if p == "/account/v1/orders/LS-ORD-1":
                return _response(
                    200,
                    {
                        "id": "LS-ORD-1",
                        "services": [{"productId": "VIRTUAL_SERVER", "equipmentId": "vps-77"}],
                    },
                )
            if p == "/publicCloud/v1/vps/vps-77":
                return _response(200, {"id": "vps-77", "state": "RUNNING"})
            return _response(200, {"vpss": []})

        provider = _provider(handler)
        vps_id = await provider.match_vps_for_order(
            "LS-ORD-1", location="AMS-01", product_name="VPS S", since=datetime.now(UTC)
        )
        assert vps_id == "vps-77"

    async def test_match_vps_falls_back_to_list_and_is_ambiguous(self) -> None:
        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            if p == "/account/v1/orders/LS-ORD-1":
                return _response(
                    200, {"id": "LS-ORD-1", "services": [{"productId": "VIRTUAL_SERVER"}]}
                )
            if p == "/publicCloud/v1/vps":
                return _response(
                    200,
                    {
                        "vps": [
                            {"id": "vps-1", "datacenter": "AMS-01", "pack": "VPS S"},
                            {"id": "vps-2", "datacenter": "AMS-01", "pack": "VPS S"},
                        ]
                    },
                )
            return _response(404, {"errorMessage": "gone"})

        provider = _provider(handler)
        from cloud_platform.providers.leaseweb.ordering import VpsMatchAmbiguous

        with pytest.raises(VpsMatchAmbiguous):
            await provider.match_vps_for_order(
                "LS-ORD-1", location="AMS-01", product_name="VPS S", since=datetime.now(UTC)
            )

    async def test_match_vps_returns_none_when_unprovisioned(self) -> None:
        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            if p == "/account/v1/orders/LS-ORD-1":
                return _response(
                    200, {"id": "LS-ORD-1", "services": [{"productId": "VIRTUAL_SERVER"}]}
                )
            return _response(200, {"vps": []})

        provider = _provider(handler)
        from cloud_platform.providers.errors import ProviderNotFound

        with pytest.raises(ProviderNotFound):
            await provider.match_vps_for_order(
                "LS-ORD-1", location="AMS-01", product_name="VPS S", since=datetime.now(UTC)
            )

    async def test_get_vps_credentials_returns_usernames_only(self) -> None:
        provider = _provider(
            lambda m, p, **kw: _response(
                200, {"credentials": [{"type": "os", "username": "root"}, {"type": "database"}]}
            )
        )
        creds = await provider.get_vps_credentials("vps-77")
        assert creds == [{"type": "os", "username": "root"}]

    async def test_verify_credential_sends_candidate_key(self) -> None:
        seen: dict[str, Any] = {}

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            seen["auth"] = (kw.get("headers") or {}).get("X-LSW-Auth")
            return _response(200, {"vpss": []})

        provider = _provider(handler)
        await provider.verify_credential("candidate-key-xyz")
        assert seen["auth"] == "candidate-key-xyz"

    async def test_power_actions_and_delete(self) -> None:
        calls: list[str] = []

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            calls.append(f"{m} {p}")
            return _response(204)

        provider = _provider(handler)
        from cloud_platform.core.idempotency import IdempotencyKey

        await provider.power_on("vps-1", IdempotencyKey("power-on-key-1"))
        await provider.power_off("vps-1", IdempotencyKey("power-off-key-2"))
        await provider.reboot("vps-1", IdempotencyKey("reboot-key-3"))
        assert calls == [
            "POST /publicCloud/v1/vps/vps-1/start",
            "POST /publicCloud/v1/vps/vps-1/stop",
            "POST /publicCloud/v1/vps/vps-1/reboot",
        ]
        with pytest.raises(ProviderError):
            await provider.delete_server("vps-1", IdempotencyKey("delete-key-4"))
        with pytest.raises(ProviderError):
            await provider.create_server(
                CreateServerRequest(name="x", plan_id="p", image_id="i", location_id="l"),
                IdempotencyKey("create-key-5"),
            )

    async def test_order_scan_paginates(self) -> None:
        from datetime import UTC, datetime

        calls = []

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            offset = int((kw.get("params") or {}).get("offset", 0))
            calls.append(offset)
            page_size = min(100, 250 - offset)
            rows = [_order_row(f"LS-ORD-{offset + i}") for i in range(page_size)]
            return _response(200, {"_metadata": {"totalCount": 250}, "orders": rows})

        provider = _provider(handler)
        matches = await provider._scan_matching_orders(
            provider_cost_minor=1299,
            currency="EUR",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            since=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert len(matches) == 250
        assert calls == [0, 100, 200]

    async def test_order_scan_rejects_wrong_currency_and_term(self) -> None:
        from datetime import UTC, datetime

        rows = [
            _order_row("LS-ORD-USD", currency="USD"),
            _order_row("LS-ORD-12M", term="12_MONTHS"),
        ]
        provider = _provider(lambda m, p, **kw: _response(200, {"orders": rows}))
        matches = await provider._scan_matching_orders(
            provider_cost_minor=1299,
            currency="EUR",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            since=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert matches == []

    async def test_order_scan_rejects_old_and_future_orders(self) -> None:
        from datetime import UTC, datetime

        rows = [
            _order_row("LS-ORD-OLD", created_at="2026-07-01T00:00:00+00:00"),
            _order_row(
                "LS-ORD-FUTURE", created_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat()
            ),
        ]
        provider = _provider(lambda m, p, **kw: _response(200, {"orders": rows}))
        matches = await provider._scan_matching_orders(
            provider_cost_minor=1299,
            currency="EUR",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            since=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert matches == []
