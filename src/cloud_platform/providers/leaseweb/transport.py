"""The single shared Leaseweb HTTP transport (LEASEWEB-VPS-API §5).

Every Leaseweb API family in this repository — the ordering catalog, the
account orders API and the modern VPS API — speaks through ONE
:class:`LeasewebTransport`. It owns, exactly once:

- base URL and the ``X-LSW-Auth: <API-KEY>`` header (plain key, no
  ``Bearer`` prefix; re-read per request when a rotatable
  :class:`~cloud_platform.providers.credentials.CredentialSource` is used);
- JSON encoding/decoding, query parameters, connect/read timeouts;
- status parsing, structured error mapping into the Leaseweb hierarchy and
  correlation/reference extraction;
- a conservative client-side throttle plus bounded ``Retry-After``-aware
  retries for READ-ONLY calls;
- the mutation-outcome classification that keeps a billable ``POST`` from
  ever being blindly retried;
- secret redaction (the key never reaches a log, metric label or error).

Reads vs mutations
------------------
A read (``mutating=False``) may retry transient failures (429 honoring
``Retry-After``, and transport failures at the caller's discretion). A
mutation (``mutating=True``) is NEVER retried inside the transport and is
classified as:

- **provably not transmitted** (connect refused/timeout, pool timeout) ->
  :class:`LeasewebUnavailableError` — safe to retry with the same identity;
- **possibly transmitted** (read/write timeout, dropped connection, 5xx,
  429, unparseable body) -> :class:`LeasewebAmbiguousMutationError`, which
  is a ``ProviderOutcomeUnknown``: the operation becomes OUTCOME_UNKNOWN
  and is resolved by read-only reconciliation or human review, never by an
  automatic second POST.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

import httpx

from cloud_platform.observability.metrics import metrics
from cloud_platform.providers.credentials import CredentialSource
from cloud_platform.providers.leaseweb.errors import (
    LeasewebAmbiguousMutationError,
    LeasewebError,
    LeasewebTimeoutError,
    LeasewebUnavailableError,
    error_for_response,
    parse_error_payload,
    redact_known,
    redact_sensitive,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "LeasewebTransport",
    "MutationOutcome",
    "Throttle",
    "classify_transport_error",
    "operation_label",
]

#: Production API host. Overridable per environment through configuration.
DEFAULT_BASE_URL = "https://api.leaseweb.com"

#: Default connect/read/write/pool timeout for every Leaseweb request.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Maximum pause honored from a ``Retry-After`` header (seconds).
MAX_RETRY_AFTER_SECONDS = 30.0


class MutationOutcome(StrEnum):
    """How far a (possibly failing) mutation got.

    ``NOT_TRANSMITTED``: the request provably never reached Leaseweb, so
    re-sending the same identity is safe. ``REJECTED``: Leaseweb answered
    with a definitive rejection and no resource was created.
    ``ACCEPTED``: Leaseweb accepted the mutation. ``AMBIGUOUS``: the
    request may or may not have been applied — never re-send automatically.
    """

    NOT_TRANSMITTED = "not_transmitted"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    AMBIGUOUS = "ambiguous"


class Throttle:
    """A conservative client-side request throttle (max requests/second).

    Leaseweb documents no numeric limit at capture time, so the client
    enforces its own ceiling by construction. ``wait`` is injectable for
    tests (no real sleeping).
    """

    def __init__(self, max_rps: float = 10.0, wait: Any = asyncio.sleep) -> None:
        if max_rps <= 0:
            raise ValueError("max_rps must be > 0")
        self.max_rps = max_rps
        self.wait = wait
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, now: Any = time.monotonic) -> None:
        async with self._lock:
            interval = 1.0 / self.max_rps
            wait_time = self._last + interval - now()
            if wait_time > 0:
                await self.wait(wait_time)
            self._last = now()


def parse_retry_after(value: str | None) -> int | None:
    """Parse a ``Retry-After`` header (delta-seconds) into whole seconds."""
    if not value:
        return None
    try:
        return max(0, int(float(value)))
    except ValueError:
        return None


def operation_label(method: str, path: str) -> str:
    """Bounded endpoint-shape label for metrics: long/UUID segments become ``{id}``."""
    shaped = []
    for segment in path.split("?")[0].strip("/").split("/"):
        if len(segment) > 20 or _looks_like_uuid(segment):
            shaped.append("{id}")
        else:
            shaped.append(segment)
    return f"{method} /{'/'.join(shaped)}"


def _looks_like_uuid(segment: str) -> bool:
    parts = segment.split("-")
    hexdigits = set("0123456789abcdefABCDEF")
    return len(parts) == 5 and all(p and all(c in hexdigits for c in p) for p in parts)


def classify_transport_error(exc: Exception) -> MutationOutcome:
    """Classify a transport failure for a MUTATING request.

    ``ConnectError``/``ConnectTimeout``/``PoolTimeout`` fail before any byte
    reaches the server, so the mutation was definitively NOT applied and a
    re-send with the same identity is safe. Every other transport failure
    (read/write timeout, dropped connection, generic protocol errors) may
    have happened after transmission: the outcome is AMBIGUOUS.
    """
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        return MutationOutcome.NOT_TRANSMITTED
    return MutationOutcome.AMBIGUOUS


class LeasewebTransport:
    """One authenticated, instrumented, redacting HTTP transport."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        throttle: Throttle | None = None,
        max_retries: int = 3,
        credential_source: CredentialSource | None = None,
        provider_key: str = "leaseweb",
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if credential_source is not None:
            # Runtime-rotatable (M10-008): the auth header is set per request
            # from the source; api_key is only the initial value.
            default_headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        else:
            # Contract: plain key in the X-LSW-Auth header - NO Bearer prefix.
            default_headers = {
                "X-LSW-Auth": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        self._credential_source = credential_source
        #: Values that must never survive into a log, an exception message or
        #: a metric label — even when Leaseweb echoes them back inside an error
        #: body: the static key plus every credential resolved at runtime.
        #: Kept in memory only; never logged or persisted.
        self._known_secrets: set[str] = {api_key}
        self._throttle = throttle or Throttle()
        self._max_retries = max_retries
        self._provider_key = provider_key
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=default_headers,
            timeout=httpx.Timeout(timeout_seconds),
        )

    # ------------------------------------------------------------------
    # Introspection / lifecycle
    # ------------------------------------------------------------------

    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying HTTP client.

        Adapters re-expose this through their own ``_client`` property, so
        tests (and the raw credential-verification call) can inject or patch
        the transport without touching a second HTTP client.
        """
        return self._client

    def set_client(self, client: httpx.AsyncClient) -> None:
        """Replace the underlying client (test injection / transport reuse)."""
        self._client = client

    @property
    def base_url(self) -> str:
        return str(self._client.base_url)

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def throttle(self) -> Throttle:
        return self._throttle

    async def aclose(self) -> None:
        await self._client.aclose()

    # Back-compat alias used by the pre-existing adapters.
    async def close(self) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        mutating: bool = False,
    ) -> Any:
        """Perform one Leaseweb request and return the decoded JSON body.

        Returns ``{}`` for an empty ``204``/no-content response. Raises a
        :class:`LeasewebError` subclass for every non-2xx status and for
        transport failures (never a raw ``httpx`` exception).
        """
        operation = operation_label(method, path)
        async with metrics.provider_call(self._provider_key, operation):
            return await self._perform(
                method,
                path,
                params=params,
                json=json,
                headers=headers,
                mutating=mutating,
                operation=operation,
            )

    async def request_raw(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Perform a request and return the raw response (auth checks).

        Used only by read-only credential verification, which needs to
        inspect the status without raising.
        """
        await self._throttle.acquire()
        try:
            return await self._client.request(method, path, params=params, headers=headers)
        except httpx.TransportError as exc:
            raise self._transport_error(exc, endpoint=operation_label(method, path)) from exc

    async def _perform(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        json: Any | None,
        headers: Mapping[str, str] | None,
        mutating: bool,
        operation: str,
    ) -> Any:
        request_headers = await self._request_headers(headers)
        kwargs: dict[str, Any] = {"params": params, "json": json, "headers": request_headers}
        attempt = 0
        while True:
            await self._throttle.acquire()
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                raise self._transport_error(exc, endpoint=operation, mutating=mutating) from exc
            if response.status_code != 429 or mutating or attempt >= self._max_retries:
                break
            # Read-only requests may honor the pause in-adapter; a mutation is
            # never re-sent here (a mutating 429 is an unknown outcome).
            await self._throttle.wait(self._retry_delay(response, attempt))
            attempt += 1
        return self._decode(response, endpoint=operation, mutating=mutating)

    async def _request_headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        """Request headers, resolving a rotatable credential per request."""
        merged = dict(headers or {})
        if self._credential_source is not None:
            # Resolved per request so a rotated credential takes effect at once.
            credential = await self._credential_source.get()
            merged["X-LSW-Auth"] = credential.value
            if credential.value:
                self._known_secrets.add(credential.value)
        return merged

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        delay = parse_retry_after(response.headers.get("Retry-After"))
        if delay is None:
            delay = 0.5 * (2**attempt)
        return min(float(delay), MAX_RETRY_AFTER_SECONDS)

    # ------------------------------------------------------------------
    # Response decoding / error mapping
    # ------------------------------------------------------------------

    def _decode(self, response: httpx.Response, *, endpoint: str, mutating: bool) -> Any:
        if response.is_success:
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except Exception:
                if mutating:
                    # The mutation WAS accepted (2xx) but the body is unusable:
                    # treat the outcome as unknown rather than guessing an id.
                    raise LeasewebAmbiguousMutationError(
                        f"{endpoint}: {response.status_code} response body is not valid JSON; "
                        "outcome unknown",
                    ) from None
                raise LeasewebError(
                    f"{endpoint}: response body is not valid JSON",
                ) from None

        payload = parse_error_payload(response, secrets=self._known_secrets)
        if mutating and (payload.http_status == 429 or payload.http_status >= 500):
            # The provider may have processed the request before failing.
            raise LeasewebAmbiguousMutationError(
                f"{endpoint}: mutating request returned HTTP {payload.http_status}; "
                f"outcome unknown; {payload.summary}",
                payload=payload,
                endpoint=endpoint,
            )
        raise error_for_response(payload, endpoint=endpoint)

    def _transport_error(
        self,
        exc: httpx.TransportError,
        *,
        endpoint: str,
        mutating: bool = False,
    ) -> LeasewebError:
        safe = redact_known(redact_sensitive(str(exc)), self._known_secrets)[:200]
        if mutating:
            outcome = classify_transport_error(exc)
            if outcome is MutationOutcome.NOT_TRANSMITTED:
                return LeasewebUnavailableError(
                    f"{endpoint}: request was not transmitted ({type(exc).__name__}): {safe}",
                    endpoint=endpoint,
                )
            return LeasewebAmbiguousMutationError(
                f"{endpoint}: transport failure may have reached Leaseweb "
                f"({type(exc).__name__}); outcome unknown: {safe}",
                endpoint=endpoint,
            )
        if isinstance(exc, httpx.TimeoutException):
            return LeasewebTimeoutError(
                f"{endpoint}: request timed out ({type(exc).__name__}): {safe}",
                endpoint=endpoint,
            )
        return LeasewebUnavailableError(
            f"{endpoint}: transport failure ({type(exc).__name__}): {safe}",
            endpoint=endpoint,
        )

    def raise_for_status(self, response: httpx.Response, *, endpoint: str | None = None) -> None:
        """Raise the mapped Leaseweb error for a non-success response."""
        if response.is_success:
            return
        payload = parse_error_payload(response, secrets=self._known_secrets)
        raise error_for_response(payload, endpoint=endpoint)
