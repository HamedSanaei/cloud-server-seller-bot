"""Durable pre-acceptance routing proof for one create intent, never a retry policy."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import NotRequired, Protocol, TypedDict, cast
from uuid import UUID

from cloud_platform.modules.operations.domain import Operation, OperationStatus
from cloud_platform.providers.errors import DEFINITIVE_CAPACITY_REFUSAL_CODES
from cloud_platform.providers.routing import normalize_account_id

PHASES = frozenset({"sent", "capacity_refused", "refused", "outcome_unknown", "accepted"})
_SAFE_CODE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class CreateRoutingAttempt(TypedDict):
    sequence: int
    account_id: str
    phase: str
    error_code: NotRequired[str | None]
    quota_names: NotRequired[list[str]]
    provider_server_id: NotRequired[str | None]


class CreateRoutingReceipt(TypedDict):
    version: int
    policy: str
    catalog_account_id: NotRequired[str | None]
    attempts: list[CreateRoutingAttempt]
    accepted_account_id: NotRequired[str | None]


class CreateAttemptConflict(RuntimeError):
    """The intent/claim/receipt no longer permits this write; stop before POST."""


def safe_fact(value: str | None) -> str | None:
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


def create_routing(operation: Operation) -> CreateRoutingReceipt | None:
    response = operation.provider_response or {}
    if "create_routing" not in response:
        return None
    raw = response["create_routing"]
    if (
        not isinstance(raw, dict)
        or type(raw.get("version")) is not int
        or raw.get("version") != 1
        or raw.get("policy") != "capacity_failover"
    ):
        raise CreateAttemptConflict("missing or unsupported create routing proof")
    if set(raw) - {"version", "policy", "catalog_account_id", "attempts", "accepted_account_id"}:
        raise CreateAttemptConflict("unexpected create routing fields")
    catalog_account = raw.get("catalog_account_id")
    if catalog_account is not None and (
        not isinstance(catalog_account, str)
        or safe_fact(catalog_account) is None
        or normalize_account_id(catalog_account) != catalog_account
    ):
        raise CreateAttemptConflict("invalid catalog observation provenance")
    attempts = raw.get("attempts")
    if not isinstance(attempts, list):
        raise CreateAttemptConflict("invalid account attempt history")
    accepted = None
    accounts: set[str] = set()
    for sequence, attempt in enumerate(attempts, 1):
        if (
            not isinstance(attempt, dict)
            or type(attempt.get("sequence")) is not int
            or attempt.get("sequence") != sequence
        ):
            raise CreateAttemptConflict("invalid account attempt sequence")
        if set(attempt) - {
            "sequence",
            "account_id",
            "phase",
            "error_code",
            "quota_names",
            "provider_server_id",
        }:
            raise CreateAttemptConflict("unexpected account attempt fields")
        account = attempt.get("account_id")
        if (
            not isinstance(account, str)
            or safe_fact(account) is None
            or normalize_account_id(account) != account
            or account in accounts
        ):
            raise CreateAttemptConflict("invalid attempted credential identity")
        accounts.add(account)
        phase = attempt.get("phase")
        if not isinstance(phase, str) or phase not in PHASES:
            raise CreateAttemptConflict("invalid account attempt phase")
        if sequence < len(attempts) and phase != "capacity_refused":
            raise CreateAttemptConflict("account changed without definitive capacity proof")
        if attempt.get("error_code") is not None and safe_fact(attempt["error_code"]) is None:
            raise CreateAttemptConflict("unsafe provider error code")
        if (
            phase == "capacity_refused"
            and attempt.get("error_code") not in DEFINITIVE_CAPACITY_REFUSAL_CODES
        ):
            raise CreateAttemptConflict("capacity refusal lacks documented pre-acceptance evidence")
        quotas = attempt.get("quota_names", [])
        if not isinstance(quotas, list) or any(safe_fact(name) is None for name in quotas):
            raise CreateAttemptConflict("unsafe quota names")
        identity = attempt.get("provider_server_id")
        if phase == "accepted":
            if not isinstance(identity, str) or not identity.strip():
                raise CreateAttemptConflict("accepted attempt lacks provider identity")
            accepted = account
        elif identity is not None:
            raise CreateAttemptConflict("unaccepted attempt contains provider identity")
    if raw.get("accepted_account_id") != accepted:
        raise CreateAttemptConflict("accepted ownership disagrees with attempt history")
    return cast(CreateRoutingReceipt, deepcopy(raw))


def last_attempt(operation: Operation) -> CreateRoutingAttempt | None:
    receipt = create_routing(operation)
    attempts = receipt["attempts"] if receipt is not None else []
    return attempts[-1] if attempts else None


def refused_accounts(operation: Operation) -> frozenset[str]:
    receipt = create_routing(operation)
    if receipt is None:
        return frozenset()
    return frozenset(
        attempt["account_id"]
        for attempt in receipt["attempts"]
        if attempt["phase"] == "capacity_refused"
    )


def current_account(operation: Operation, server_account_id: str | None) -> str:
    attempt = last_attempt(operation)
    account = normalize_account_id(server_account_id)
    if attempt is not None and attempt["account_id"] != account:
        raise CreateAttemptConflict("receipt and fulfillment ownership disagree")
    return account


def safe_pre_post(operation: Operation) -> bool:
    receipt = create_routing(operation)
    if receipt is None or any(
        (operation.provider_response or {}).get(key)
        for key in ("provider_server_id", "provider_order_id")
    ):
        return False
    attempt = last_attempt(operation)
    return attempt is None or attempt["phase"] == "capacity_refused"


class CreateAccountAttemptRepository(Protocol):
    async def claim(
        self,
        operation_id: UUID,
        server_id: UUID,
        order_id: UUID | None = None,
        catalog_account_id: str | None = None,
    ) -> Operation | None: ...

    async def start_attempt(
        self,
        operation_id: UUID,
        claim_generation: int,
        account_id: str,
        *,
        request_facts: Mapping[str, str] | None = None,
    ) -> Operation: ...

    async def record_refusal(
        self,
        operation_id: UUID,
        claim_generation: int,
        account_id: str,
        *,
        capacity: bool,
        error_code: str | None = None,
        quota_names: tuple[str, ...] = (),
    ) -> Operation: ...

    async def record_unknown(
        self,
        operation_id: UUID,
        claim_generation: int,
        account_id: str,
    ) -> Operation: ...

    async def record_acceptance(
        self,
        operation_id: UUID,
        claim_generation: int,
        account_id: str,
        provider_server_id: str,
        *,
        ipv4: str | None = None,
        ipv6: str | None = None,
    ) -> Operation: ...

    async def save_outcome(
        self,
        operation_id: UUID,
        claim_generation: int,
        status: OperationStatus,
        *,
        error: str | None = None,
        correlation: Mapping[str, object] | None = None,
    ) -> Operation: ...

    async def resume_safe_claim(
        self,
        operation_id: UUID,
        claim_generation: int,
    ) -> Operation: ...
