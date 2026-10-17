"""Read-only completion proof for accepted management commands; never redispatch."""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update

from cloud_platform.core.config import get_settings
from cloud_platform.db.base import Operation
from cloud_platform.db.timestamps import to_db_utc
from cloud_platform.modules.notifications.outbox import notice_row
from cloud_platform.modules.servers.service import ServerUnavailableError
from cloud_platform.modules.servers.test_identity import TEST_IPV4, is_test_resource, is_test_server
from cloud_platform.providers.routing import provider_for

_ACTIONS = {
    "start": "power-on",
    "stop": "power-off",
    "password_reset": "reset-password",  # pragma: allowlist secret -- operation name
    "ssh_port": "ssh-port",
}
_RUNNING = {"RUNNING", "ACTIVE", "ON"}
_STOPPED = {"STOPPED", "OFF"}


def _provider(registry: Any, server: Any) -> Any:
    provider = provider_for(registry, server.provider_key, server.credential_account_id)
    return getattr(provider, "vps_management", provider)


def _fingerprint(password: str) -> str:
    key = get_settings().callback_signing_key
    if not key:
        raise ServerUnavailableError("credential observation signing key is not configured")
    return hmac.digest(key.encode(), password.encode(), "sha256").hex()


async def _password(provider: Any, provider_id: str) -> tuple[str, str] | None:
    reader = getattr(provider, "get_vps_root_password", None)
    if not callable(reader):
        return None
    credential = await reader(provider_id)
    if credential is None or credential.root_password is None:
        return None
    password = credential.root_password.reveal()
    return (password, credential.username) if password else None


async def prepare_observation(
    registry: Any, server: Any, actor_id: UUID, action: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    action = _ACTIONS.get(action, action)
    proof: dict[str, Any] = {
        "action": action,
        "actor_id": str(actor_id),
        "owner_id": str(server.user_id),
        "provider_id": server.provider_server_id,
        "account_id": server.credential_account_id,
        "lifecycle": "running",
    }
    if action == "ssh-port":
        return proof
    try:
        provider = _provider(registry, server)
        reader = getattr(provider, "get_vps_info", None)
        if not callable(reader):
            if is_test_resource(server):
                raise ServerUnavailableError("test-resource inventory cannot be verified")
            proof["lifecycle"] = "outcome_unknown"
            return proof
        info = await reader(server.provider_server_id)
        if info is None or str(info.id) != str(server.provider_server_id):
            raise ServerUnavailableError("provider resource identity cannot be verified")
        if is_test_resource(server):
            ips = await provider.list_vps_ips(server.provider_server_id)
            if not is_test_server(server) or not any(str(row.ip) == TEST_IPV4 for row in ips):
                raise ServerUnavailableError("reserved test-resource identity has drifted")
        proof.update(before_state=info.state, before_started_at=info.started_at)
        if action == "reinstall":
            proof["image"] = arguments.get("image")
        if action in {"reset-password", "reinstall"}:
            credential = await _password(provider, server.provider_server_id)
            proof["before_credential"] = _fingerprint(credential[0]) if credential else None
        return proof
    except ServerUnavailableError:
        raise
    except Exception:
        raise ServerUnavailableError("provider inventory could not be verified") from None


def observed_status(proof: dict[str, Any], info: Any) -> tuple[str, str]:
    """An HTTP 202 or unchanged boot/image is not completion evidence."""
    if info is None:
        return (
            ("success", "provider_resource_absent")
            if proof["action"] == "delete"
            else ("failed", "provider_resource_missing")
        )
    if str(info.id) != str(proof["provider_id"]):
        return "outcome_unknown", "provider_identity_changed"
    state = str(info.state).upper()
    action = proof["action"]
    if action == "power-on" and state in _RUNNING:
        return "success", "provider_running"
    if action == "power-off" and state in _STOPPED:
        return "success", "provider_stopped"
    boot_changed = bool(
        proof.get("before_started_at")
        and info.started_at
        and info.started_at != proof["before_started_at"]
    )
    if action == "reboot" and state in _RUNNING and boot_changed:
        return "success", "provider_boot_changed"
    if (
        action == "reinstall"
        and state in _RUNNING
        and boot_changed
        and info.image_id == proof.get("image")
    ):
        return "success", "provider_image_and_boot_verified"
    if action == "delete":
        return "running", "awaiting_deletion_reconciliation"
    return "running", "awaiting_provider_evidence"


async def reconcile_management(container: Any, *, server_id: UUID | None = None) -> int:
    # Provider reads happen outside transactions. Final proof, encrypted password
    # and customer outbox records commit together under an operation row lock.
    async with container.session_factory() as session:
        statement = select(Operation).where(
            Operation.operation_type == "server_management",
            Operation.provider_response["management"]["lifecycle"].astext.in_(
                ["running", "outcome_unknown"]
            ),
            Operation.status.in_(["in_flight", "completed", "outcome_unknown"]),
            Operation.provider_response["management"]["before_state"].astext.is_not(None),
        )
        if server_id is not None:
            statement = statement.where(Operation.resource_id == server_id)
        candidates = (
            (
                await session.execute(
                    statement.order_by(Operation.updated_at, Operation.created_at).limit(100)
                )
            )
            .scalars()
            .all()
        )
    completed = 0
    errors: list[Exception] = []
    for candidate in candidates:
        original = dict(candidate.provider_response or {})
        proof = dict(original["management"])
        try:
            server = await container.server_repository().get(candidate.resource_id)
            credential = None
            info = None
            if (
                server is None
                or server.provider_server_id != proof["provider_id"]
                or server.credential_account_id != proof["account_id"]
            ):
                status, detail = "outcome_unknown", "provider_binding_changed"
            else:
                provider = _provider(container.provider_registry, server)
                info = await provider.get_vps_info(proof["provider_id"])
                status, detail = observed_status(proof, info)
                if (
                    info is not None
                    and str(info.id) == str(proof["provider_id"])
                    and proof["action"] in {"reset-password", "reinstall"}
                ):
                    credential = await _password(provider, proof["provider_id"])
                    if credential and _fingerprint(credential[0]) == proof.get("before_credential"):
                        credential = None
                    if proof["action"] == "reset-password":
                        status, detail = (
                            ("success", "provider_replacement_credential_issued")
                            if credential
                            else ("running", "awaiting_replacement_credential")
                        )
            async with container.session_factory() as session, session.begin():
                row = (
                    await session.execute(
                        select(Operation).where(Operation.id == candidate.id).with_for_update()
                    )
                ).scalar_one()
                if row.provider_response != original:
                    continue
                if credential:
                    await container.server_credential_store().replace_issued(
                        server_id=server.id,
                        provider_server_id=proof["provider_id"],
                        password=credential[0],
                        username=credential[1],
                        session=session,
                    )
                proof.update(
                    lifecycle=status, detail=detail, observed_state=info.state if info else None
                )
                result = dict(
                    original.get("result") or {"accepted": True, "receiptId": str(row.id)}
                )
                body = result.get("data", result)
                body.update(
                    status=status, detail=detail, outcome_unknown=status == "outcome_unknown"
                )
                row.provider_response = {**original, "management": proof, "result": result}
                if status == "success" and row.status in {"in_flight", "outcome_unknown"}:
                    row.status = "completed"
                    row.error = None
                row.updated_at = to_db_utc(datetime.now(UTC))
                if status in {"success", "failed"}:
                    for user_id in {proof["actor_id"], proof["owner_id"]}:
                        session.add(
                            notice_row(
                                key=f"management-observed:{row.id}:{user_id}:{status}",
                                user_id=UUID(user_id),
                                text=(
                                    f"نتیجه عملیات {proof['action']} "
                                    f"روی سرور {str(candidate.resource_id)[:8]}: {status}\n"
                                    f"نتیجه تأییدشده: {detail}"
                                ),
                                server_id=str(candidate.resource_id),
                            )
                        )
            completed += int(status in {"success", "failed"})
        except Exception as exc:
            # A read/commit failure cannot justify any provider mutation or a
            # success notice. Leave the receipt for the next read-only pass.
            logging.getLogger(__name__).warning(
                "Management reconciliation unavailable (%s)", type(exc).__name__
            )
            errors.append(RuntimeError(f"management reconciliation failed ({type(exc).__name__})"))
            # Rotate a temporarily unavailable provider out of the front of a
            # bounded batch without overwriting any concurrently recorded proof.
            async with container.session_factory() as session, session.begin():
                await session.execute(
                    update(Operation)
                    .where(Operation.id == candidate.id, Operation.provider_response == original)
                    .values(updated_at=to_db_utc(datetime.now(UTC)))
                )
    if errors:
        raise ExceptionGroup("management reconciliation failed", errors) from None
    return completed


def public_receipt(row: Any) -> dict[str, Any]:
    from cloud_platform.db.timestamps import from_db_utc

    payload = row.provider_response or {}
    proof = payload.get("management") or {}
    result = payload.get("result") or {}
    body = result.get("data", result)
    status = str(row.status)
    if status == "completed":
        status = proof.get("lifecycle") or "dispatch_completed"
    elif status == "in_flight":
        status = "running"
    return {
        "id": str(row.id),
        "type": row.operation_type,
        "action": proof.get("action") or body.get("operation"),
        "status": status,
        "dispatchStatus": row.status,
        "detail": proof.get("detail"),
        "observedState": proof.get("observed_state"),
        "createdAt": from_db_utc(row.created_at).isoformat(),
        "updatedAt": from_db_utc(row.updated_at).isoformat(),
    }


async def management_history(container: Any, server_id: UUID) -> list[dict[str, Any]]:
    async with container.session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Operation)
                    .where(Operation.resource_type == "server", Operation.resource_id == server_id)
                    .order_by(Operation.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        return [public_receipt(row) for row in rows]
