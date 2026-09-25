"""Provider credential-account CAPACITY: classification, state, and consequences.

Production incident (2026-09-25): a fresh hourly request reached Leaseweb and was
definitively refused with

    errorCode=PC-2031  Customer limit reached  correlationId=07376219-...

on the ``sales-org-north`` credential account. Leaseweb publishes NO quota
endpoint, so capacity is a fact that can only be LEARNED from such a refusal and
then remembered. These tests pin the boundaries:

* the code/message pair becomes a DEDICATED condition (never a generic 400), with
  the provider's error code and correlation id preserved for admin diagnostics;
* the durable state gates NEW orders only, expires on its own, and can be cleared
  by an operator;
* an ACCEPTED contract is never retried on another credential account, and its
  already-failed operation is never resurrected;
* the customer sees a dedicated message that leaks no account id or code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from cloud_platform.modules.provider_capacity.domain import (
    DEFAULT_LIMIT_TTL_SECONDS,
    AccountCapacity,
    AccountCapacityState,
    CapacityObservation,
)
from cloud_platform.providers.errors import ProviderCapacityError
from cloud_platform.providers.leaseweb.errors import (
    CAPACITY_ERROR_CODES,
    LeasewebCapacityError,
    LeasewebValidationError,
    error_for_response,
    is_capacity_exhausted,
    parse_error_payload,
)

PC2031_BODY: dict[str, Any] = {
    "errorCode": "PC-2031",
    "errorMessage": "Customer limit reached",
    "correlationId": "07376219-7bcd-43d9-a5ea-4128fa57345a",
}


def _response(
    *, body: dict[str, Any], status: int = 400, correlation_header: str | None = None
) -> httpx.Response:
    headers = {"APIGW-CORRELATION-ID": correlation_header} if correlation_header else {}
    return httpx.Response(status_code=status, json=body, headers=headers)


class TestPc2031Classification:
    def test_the_documented_code_is_a_capacity_condition(self) -> None:
        assert "PC-2031" in CAPACITY_ERROR_CODES
        assert is_capacity_exhausted("PC-2031", "Customer limit reached") is True
        assert is_capacity_exhausted("pc-2031", None) is True

    def test_the_message_alone_is_enough_when_the_code_is_absent(self) -> None:
        assert is_capacity_exhausted(None, "Customer limit reached") is True
        assert is_capacity_exhausted(None, "customer LIMIT reached") is True

    def test_unrelated_failures_are_never_capacity(self) -> None:
        # A loose "limit" match would misclassify real validation errors.
        assert is_capacity_exhausted("PC-1000", "Invalid region") is False
        assert is_capacity_exhausted(None, "rootDiskSize must be at least 5") is False
        assert is_capacity_exhausted(None, None) is False
        assert is_capacity_exhausted(None, "") is False

    def test_http_400_with_pc2031_becomes_the_dedicated_error(self) -> None:
        payload = parse_error_payload(_response(body=PC2031_BODY))
        error = error_for_response(payload, endpoint="/publicCloud/v1/instances")
        assert isinstance(error, LeasewebCapacityError)
        # Both classifications hold: it is still a permanent 400-class failure
        # (never retried automatically) AND a provider-account capacity fact.
        assert isinstance(error, LeasewebValidationError)
        assert isinstance(error, ProviderCapacityError)
        assert error.retryable is False
        assert error.capacity_exhausted is True

    def test_the_error_carries_code_message_and_correlation_for_admins(self) -> None:
        # The body's correlationId is the operator-facing correlation (the
        # header is only a routing id and is used when the body omits one).
        payload = parse_error_payload(_response(body=PC2031_BODY))
        error = error_for_response(payload, endpoint="/publicCloud/v1/instances")
        assert error.error_code == "PC-2031"
        assert error.correlation_id == "07376219-7bcd-43d9-a5ea-4128fa57345a"
        header_only = parse_error_payload(
            _response(
                body={"errorCode": "PC-2031", "errorMessage": "Customer limit reached"},
                correlation_header="07376219-header",
            )
        )
        assert header_only.correlation_id == "07376219-header"
        # The actionable provider text survives, bounded and redacted.
        assert "Customer limit reached" in str(error)
        assert "errorCode=PC-2031" in str(error)

    def test_an_ordinary_validation_failure_stays_generic(self) -> None:
        payload = parse_error_payload(
            _response(
                body={
                    "errorCode": "PC-1001",
                    "errorMessage": "Validation Failed",
                    "errorDetails": {"rootDiskSize": ["must be at least 5"]},
                }
            )
        )
        error = error_for_response(payload, endpoint="/publicCloud/v1/instances")
        assert isinstance(error, LeasewebValidationError)
        assert not isinstance(error, LeasewebCapacityError)
        # errorDetails still survive for the operator (the earlier incident).
        assert "rootDiskSize: must be at least 5" in str(error)

    def test_a_capacity_message_never_leaks_a_credential(self) -> None:
        """A provider may echo request material; capacity adds no leak."""
        payload = parse_error_payload(
            _response(
                body={
                    "errorCode": "PC-2031",
                    "errorMessage": "Customer limit reached for X-LSW-Auth: super-secret-key",
                    "correlationId": "cid",
                }
            ),
            secrets=("super-secret-key",),
        )
        error = error_for_response(payload, endpoint="/publicCloud/v1/instances")
        assert "super-secret-key" not in str(error)
        assert "<redacted>" in str(error)


class TestAccountCapacityState:
    def test_default_state_accepts_new_orders(self) -> None:
        record = AccountCapacity(provider_key="leaseweb", credential_account_id="north")
        assert record.state is AccountCapacityState.HEALTHY
        assert record.accepts_new_orders() is True
        assert record.is_limit_reached() is False
        assert record.expired() is False

    def test_a_limit_signal_is_time_bounded_and_accumulates_evidence(self) -> None:
        now = datetime(2026, 9, 25, 12, tzinfo=UTC)
        record = AccountCapacity(
            provider_key="leaseweb",
            credential_account_id="north",
            observations=0,
        )
        first = record.with_limit_reached(
            observation=CapacityObservation(
                error_code="PC-2031",
                correlation_id="cid-1",
                location_id="eu-central-1",
                product_id="lsw.m4.large",
            ),
            ttl_seconds=DEFAULT_LIMIT_TTL_SECONDS,
            now=now,
        )
        assert first.is_limit_reached(now=now) is True
        assert first.accepts_new_orders(now=now) is False
        assert first.expires_at == now + timedelta(seconds=DEFAULT_LIMIT_TTL_SECONDS)
        assert first.observations == 1

        # A second definitive refusal refreshes the window from NOW (a sliding
        # TTL, not a silently extended one) and counts the observation.
        later = now + timedelta(minutes=30)
        second = first.with_limit_reached(
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=DEFAULT_LIMIT_TTL_SECONDS,
            now=later,
        )
        assert second.observations == 2
        assert second.expires_at == later + timedelta(seconds=DEFAULT_LIMIT_TTL_SECONDS)
        # Evidence from the earlier refusal is not discarded.
        assert second.correlation_id == "cid-1"
        assert second.location_id == "eu-central-1"

    def test_the_state_expires_on_its_own(self) -> None:
        now = datetime(2026, 9, 25, 12, tzinfo=UTC)
        record = AccountCapacity(
            provider_key="leaseweb", credential_account_id="north", observations=0
        ).with_limit_reached(
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=60,
            now=now,
        )
        assert record.is_limit_reached(now=now + timedelta(seconds=59)) is True
        assert record.expired(now=now + timedelta(seconds=60)) is True
        # Expired means eligible again: one refusal can never disable an
        # account permanently.
        assert record.accepts_new_orders(now=now + timedelta(seconds=60)) is True
        assert record.is_limit_reached(now=now + timedelta(seconds=60)) is False

    def test_a_naive_datetime_is_read_as_utc(self) -> None:
        # The DB may hand back a naive timestamp; a wrong tz interpretation
        # would silently expire (or immortalize) a live signal.
        aware = datetime(2026, 9, 25, 12, tzinfo=UTC)
        record = AccountCapacity(
            provider_key="leaseweb", credential_account_id="north", observations=0
        ).with_limit_reached(
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=3600,
            now=aware,
        )
        naive_now = aware.replace(tzinfo=None)
        assert record.is_limit_reached(now=naive_now) is True

    def test_recovery_keeps_the_evidence_but_restores_eligibility(self) -> None:
        now = datetime(2026, 9, 25, 12, tzinfo=UTC)
        record = AccountCapacity(
            provider_key="leaseweb", credential_account_id="north", observations=0
        ).with_limit_reached(
            observation=CapacityObservation(error_code="PC-2031", correlation_id="cid"),
            ttl_seconds=3600,
            now=now,
        )
        recovered = record.recovered(now=now)
        assert recovered.state is AccountCapacityState.HEALTHY
        assert recovered.accepts_new_orders(now=now) is True
        assert recovered.expires_at is None
        assert recovered.error_code == "PC-2031"
        assert recovered.correlation_id == "cid"
        assert recovered.observations == 1

    def test_invalid_ttl_and_identity_are_refused(self) -> None:
        record = AccountCapacity(provider_key="leaseweb", credential_account_id="north")
        with pytest.raises(ValueError):
            record.with_limit_reached(
                observation=CapacityObservation(error_code="PC-2031"), ttl_seconds=1
            )
        with pytest.raises(ValueError):
            AccountCapacity(provider_key="", credential_account_id="north")
        with pytest.raises(ValueError):
            AccountCapacity(provider_key="leaseweb", credential_account_id="  ")
        with pytest.raises(ValueError):
            CapacityObservation(error_code="   ")
