"""Durable receipt for management requests, including asynchronous/ambiguous results."""

from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from hashlib import sha256
from json import dumps
from typing import Any
from uuid import UUID

from cloud_platform.modules.operations.domain import OperationStatus
from cloud_platform.modules.servers.models import ServerActionOutcome
from cloud_platform.modules.servers.service import (
    ServerAmbiguousOutcomeError,
    ServerManagementError,
)


class ManagementReceiptConflict(RuntimeError):
    """A prior request is unresolved, failed, or bound to different facts."""


async def execute_management(
    repository: Any,
    *,
    key: str,
    user_id: UUID,
    server: Any,
    action: str,
    arguments: dict[str, Any],
    prepare: Callable[[], Awaitable[dict[str, Any]]],
    dispatch: Callable[[], Awaitable[Any]],
) -> dict[str, Any]:
    digest = sha256(
        dumps(
            {
                "actor": str(user_id),
                "server": str(server.id),
                "action": action,
                "arguments": arguments,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    try:
        operation = await repository.management_intent(
            key="management:" + key,
            server_id=server.id,
            provider_key=server.provider_key,
            digest=digest,
        )
    except ValueError as exc:
        raise ManagementReceiptConflict("request key carries different facts") from exc
    if operation.status is OperationStatus.COMPLETED:
        result = dict((operation.provider_response or {})["result"])
        (result.get("data") or result)["replayed"] = True
        return result
    claimed = await repository.claim(operation.id)
    if claimed is None:
        raise ManagementReceiptConflict("request already dispatched; inspect its recorded outcome")
    try:
        proof = await prepare()
        claimed.provider_response = {**(claimed.provider_response or {}), "management": proof}
        await repository.save(claimed)
    except Exception as exc:
        # Preparation has not dispatched anything; this is a definitive failure.
        claimed.fail("management preflight could not verify the resource")
        claimed.provider_response = {
            **(claimed.provider_response or {}),
            "management": {
                "action": action,
                "lifecycle": "failed",
                "detail": f"preflight_failed:{type(exc).__name__}",
            },
        }
        await repository.save(claimed)
        raise
    try:
        outcome = await dispatch()
        if isinstance(outcome, ServerActionOutcome) and is_dataclass(outcome):
            result = asdict(outcome)
            result["operation"] = outcome.operation.value
        else:
            result = dict(outcome)
        result["receiptId"] = str(claimed.id)
        body = result.get("data", result)
        status = (
            "outcome_unknown"
            if body.get("outcome_unknown")
            else "success"
            if proof.get("action") == "ssh-port"
            or body.get("detail") == "provider_credential_stored"
            else proof.get("lifecycle", "running")
        )
        body["status"] = status
        proof["lifecycle"] = status
        claimed.complete({"command_digest": digest, "management": proof, "result": result})
        await repository.save(claimed)
        return result
    except Exception as exc:
        # Never retry a provider call because the response/receipt write was lost.
        if claimed.status is OperationStatus.IN_FLIGHT:
            cause = exc.__cause__ or exc
            definite = isinstance(cause, ServerManagementError) and not isinstance(
                cause, ServerAmbiguousOutcomeError
            )
            definite = definite or getattr(exc, "code", None) in {
                "validation_error",
                "action_not_allowed",
                "not_found",
                "forbidden",
            }
            if definite:
                claimed.fail("management request was rejected")
                proof.update(lifecycle="failed", detail=f"request_rejected:{type(cause).__name__}")
                claimed.provider_response = {
                    **(claimed.provider_response or {}),
                    "management": proof,
                }
            else:
                claimed.mark_outcome_unknown("management outcome requires read-only reconciliation")
            await repository.save(claimed)
        raise
