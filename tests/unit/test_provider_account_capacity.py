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

    def test_an_elapsed_window_settles_to_unknown_not_healthy(self) -> None:
        """Time passing removes FRESHNESS, never the fact that recovery is
        unproven. An expired refusal must NOT make the account eligible again:
        Leaseweb does not free a Sales Organization's customer limit because an
        hour went by."""
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
        settled = record.settled(now=now + timedelta(seconds=60))
        assert settled.state is AccountCapacityState.UNKNOWN_AFTER_LIMIT
        assert settled.blocked_reason(now=now + timedelta(seconds=60)) == "unknown-after-limit"
        # Still not eligible, on the record or after settling.
        assert record.accepts_new_orders(now=now + timedelta(seconds=60)) is False
        assert record.is_limit_reached(now=now + timedelta(seconds=60)) is True
        assert settled.accepts_new_orders(now=now + timedelta(seconds=600)) is False
        # The evidence (and its own timeline) survives the settlement.
        assert settled.error_code == "PC-2031"
        assert settled.expires_at == record.expires_at

    def test_only_positive_proof_restores_eligibility(self) -> None:
        """An operator clear (or a verified positive signal) is the ONE path
        back to HEALTHY, and it keeps the refusal history for diagnostics."""
        now = datetime(2026, 9, 25, 12, tzinfo=UTC)
        record = (
            AccountCapacity(provider_key="leaseweb", credential_account_id="north", observations=0)
            .with_limit_reached(
                observation=CapacityObservation(error_code="PC-2031"),
                ttl_seconds=60,
                now=now,
            )
            .settled(now=now + timedelta(seconds=600))
        )
        assert record.state is AccountCapacityState.UNKNOWN_AFTER_LIMIT
        recovered = record.recovered(now=now + timedelta(seconds=600))
        assert recovered.state is AccountCapacityState.HEALTHY
        assert recovered.accepts_new_orders(now=now + timedelta(seconds=600)) is True
        assert recovered.blocked_reason() is None
        assert recovered.error_code == "PC-2031"  # evidence kept, eligibility changed

    def test_a_fresh_refusal_after_settlement_refreshes_the_window(self) -> None:
        now = datetime(2026, 9, 25, 12, tzinfo=UTC)
        record = (
            AccountCapacity(provider_key="leaseweb", credential_account_id="north", observations=1)
            .with_limit_reached(
                observation=CapacityObservation(error_code="PC-2031"), ttl_seconds=60, now=now
            )
            .settled(now=now + timedelta(seconds=600))
            .with_limit_reached(
                observation=CapacityObservation(correlation_id="cid-2"),
                ttl_seconds=60,
                now=now + timedelta(seconds=900),
            )
        )
        assert record.state is AccountCapacityState.LIMIT_REACHED
        assert record.observations == 3
        assert record.expires_at == now + timedelta(seconds=960)

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


#: The sanitized ``operations.error`` text production stored for the incident
#: (server 5e4bf88c, operation ``server-create:5e4bf88c-...``).
PC2031_OPERATION_TEXT = (
    "provider account has no capacity for new instances: errorCode=PC-2031; Customer limit "
    "reached; correlationId=07376219-7bcd-43d9-a5ea-4128fa57345a; HTTP 400"
)
#: An unrelated provider 400: a region the credential does not serve.
VALIDATION_OPERATION_TEXT = (
    "hourly offer revalidation failed: errorCode=400; Validation Failed; region: "
    'The value "eu-west-9" is not valid region; HTTP 400'
)


def _evidence(
    source_ref: str = "server-create:5e4bf88c-cabe-4ab1-9eb4-cf448e046382",
    *,
    account: str = "sales-org-north",
    error_code: str | None = "PC-2031",
    observed_at: datetime | None = None,
) -> Any:
    from cloud_platform.modules.provider_capacity.domain import HistoricalCapacityEvidence

    return HistoricalCapacityEvidence(
        provider_key="leaseweb",
        credential_account_id=account,
        source_ref=source_ref,
        error_code=error_code,
        correlation_id="07376219-7bcd-43d9-a5ea-4128fa57345a",
        observed_at=observed_at,
    )


class _FakeCapacityRepo:
    """Durable-store double: applied source refs are remembered (idempotent)."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.seen: list[str] = []
        self.applied: set[str] = set()
        self._fail_on = set(fail_on or set())

    async def record_historical_evidence(
        self, evidence: Any, *, ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS, now: Any = None
    ) -> Any:
        self.seen.append(evidence.source_ref)
        if evidence.source_ref in self._fail_on:
            raise RuntimeError("write failed")
        if evidence.source_ref in self.applied:
            return None
        self.applied.add(evidence.source_ref)
        return AccountCapacity(
            provider_key=evidence.provider_key,
            credential_account_id=evidence.credential_account_id,
            state=AccountCapacityState.UNKNOWN_AFTER_LIMIT,
            error_code=evidence.error_code,
            observations=1,
        )


class _FakeEvidenceSource:
    def __init__(self, items: tuple[Any, ...] = (), *, error: Exception | None = None) -> None:
        self.items = items
        self.error = error
        self.asked: list[tuple[str, int]] = []

    async def failed_capacity_evidence(self, provider_key: str, *, limit: int = 200) -> Any:
        self.asked.append((provider_key, limit))
        if self.error is not None:
            raise self.error
        return self.items


class TestHistoricalReconciliation:
    """Recovering a refusal the platform never recorded per ACCOUNT.

    The incident's create failed BEFORE the capacity feature existed, so the
    store was empty and the next customer order went to an account already at
    its provider limit. The reconciliation closes that gap once, without
    touching the operation and without a provider call.
    """

    def _service(self, repo: Any, source: Any, *, ttl_seconds: int = 3600) -> Any:
        from cloud_platform.modules.provider_capacity.reconciliation import (
            CapacityReconciliationService,
        )

        return CapacityReconciliationService(
            capacity_repo=repo, evidence_source=source, ttl_seconds=ttl_seconds
        )

    async def test_an_incident_is_applied_once_and_a_replay_appends_nothing(self) -> None:
        repo = _FakeCapacityRepo()
        source = _FakeEvidenceSource((_evidence(),))
        service = self._service(repo, source)

        first = await service.run("leaseweb")
        assert first.evidence_found == 1
        assert first.applied == (("sales-org-north", "unknown_after_limit"),)
        assert first.summary() == "provider=leaseweb evidence=1 applied=1 already=0 failed=0"
        # The operation key is the idempotency anchor: a restart, a second
        # worker or an operator retry applies nothing new.
        second = await service.run("leaseweb")
        assert second.applied == ()
        assert second.already_recorded == (_evidence().source_ref,)
        assert repo.seen == [_evidence().source_ref, _evidence().source_ref]
        assert source.asked == [("leaseweb", 200), ("leaseweb", 200)]

    async def test_a_failing_evidence_source_is_a_report_not_an_exception(self) -> None:
        """Startup must never crash because history could not be read."""
        repo = _FakeCapacityRepo()
        service = self._service(repo, _FakeEvidenceSource(error=RuntimeError("db down")))

        report = await service.run("leaseweb")
        assert report.errors == ("evidence source: RuntimeError",)
        assert report.applied == ()
        assert repo.seen == []

    async def test_one_failing_item_never_aborts_the_others(self) -> None:
        first, second = _evidence("op-1"), _evidence("op-2", account="sales-org-uk")
        repo = _FakeCapacityRepo(fail_on={"op-1"})
        service = self._service(repo, _FakeEvidenceSource((first, second)))

        report = await service.run("leaseweb")
        assert report.failed == ("op-1",)
        assert report.applied == (("sales-org-uk", "unknown_after_limit"),)
        assert report.applied_count == 1

    async def test_the_read_limit_must_be_positive(self) -> None:
        service = self._service(_FakeCapacityRepo(), _FakeEvidenceSource())
        with pytest.raises(ValueError):
            await service.run("leaseweb", limit=0)

    def test_only_a_proven_capacity_refusal_becomes_evidence(self) -> None:
        """The DECISION is the audited classifier, not a text heuristic: a
        generic provider 400 (region/image/validation) is never recovered as
        capacity, and the capacity text is recovered with its code."""
        from cloud_platform.providers.leaseweb.capacity_evidence import parse_capacity_evidence

        proven = parse_capacity_evidence(
            PC2031_OPERATION_TEXT,
            provider_key="leaseweb",
            credential_account_id="sales-org-north",
            source_ref="server-create:5e4bf88c",
        )
        assert proven is not None
        assert proven.error_code == "PC-2031"
        assert proven.correlation_id == "07376219-7bcd-43d9-a5ea-4128fa57345a"
        assert proven.source_ref == "server-create:5e4bf88c"
        # What reaches the durable store is the safe subset only: an error
        # code and a routing id — never the operation text, a credential or a
        # request body.
        observation = proven.as_observation()
        assert (observation.error_code, observation.correlation_id) == (
            "PC-2031",
            "07376219-7bcd-43d9-a5ea-4128fa57345a",
        )
        assert (observation.location_id, observation.product_id) == (None, None)

        assert (
            parse_capacity_evidence(
                VALIDATION_OPERATION_TEXT,
                provider_key="leaseweb",
                credential_account_id="sales-org-north",
                source_ref="server-create:5e4bf88c",
            )
            is None
        )
        assert (
            parse_capacity_evidence(
                None,
                provider_key="leaseweb",
                credential_account_id="sales-org-north",
                source_ref="server-create:5e4bf88c",
            )
            is None
        )
