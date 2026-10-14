"""Atomic create receipts and fulfillment pins on the existing operation/server/order rows."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import Operation as OperationRow
from cloud_platform.db.base import Provider as ProviderRow
from cloud_platform.db.base import ProviderOrder as OrderRow
from cloud_platform.db.base import Server as ServerRow
from cloud_platform.db.timestamps import to_db_utc, utc_now
from cloud_platform.modules.operations.create_attempts import (
    CreateAttemptConflict,
    CreateRoutingAttempt,
    CreateRoutingReceipt,
    create_routing,
    last_attempt,
    safe_fact,
    safe_pre_post,
)
from cloud_platform.modules.operations.domain import Operation, OperationStatus, OperationType
from cloud_platform.modules.operations.repository import _to_domain
from cloud_platform.providers.errors import DEFINITIVE_CAPACITY_REFUSAL_CODES
from cloud_platform.providers.routing import normalize_account_id

_CREATE_STATES = frozenset({"requested", "provisioning"})
_COMPLETION_KEYS = frozenset(
    {
        "provider_server_id",
        "provider_order_id",
        "provider_state",
        "provider_status",
        "idempotency_key",
        "operation_key",
        "settled",
        "recovered",
        "resolved_by",
    }
)


class SqlAlchemyCreateAccountAttemptRepository:
    """Lock order is operation -> server -> optional order; every mutation is fenced."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def _bundle(
        self,
        session: AsyncSession,
        operation_id: UUID,
        *,
        server_id: UUID | None = None,
        order_id: UUID | None = None,
    ) -> tuple[Any, Any, Any]:
        operation_row = await session.scalar(
            select(OperationRow).where(OperationRow.id == operation_id).with_for_update()
        )
        if operation_row is None:
            raise CreateAttemptConflict("create operation no longer exists")
        operation = cast(Any, operation_row)
        if operation.operation_type == OperationType.ORDER_CREATE.value:
            if operation.resource_type != "server_order":
                raise CreateAttemptConflict("order create resource type mismatch")
            if order_id is not None and order_id != operation.resource_id:
                raise CreateAttemptConflict("operation/order identity mismatch")
            order_id = operation.resource_id
            bound_server = await session.scalar(
                select(OrderRow.server_id).where(OrderRow.id == order_id)
            )
        elif operation.operation_type == OperationType.SERVER_CREATE.value:
            if operation.resource_type not in {"cloud_server", "server"}:
                raise CreateAttemptConflict("server create resource type mismatch")
            if order_id is not None:
                raise CreateAttemptConflict("server create cannot bind an unrelated order")
            bound_server = operation.resource_id
        else:
            raise CreateAttemptConflict("operation is not a create intent")
        if server_id is not None and server_id != bound_server:
            raise CreateAttemptConflict("operation/server identity mismatch")
        server_row = await session.scalar(
            select(ServerRow).where(ServerRow.id == bound_server).with_for_update()
        )
        if server_row is None:
            raise CreateAttemptConflict("create server no longer exists")
        server = cast(Any, server_row)
        provider_key = await session.scalar(
            select(ProviderRow.name).where(ProviderRow.id == server.provider_id)
        )
        if provider_key != operation.provider_key:
            raise CreateAttemptConflict("operation/server provider mismatch")
        order = None
        if order_id is not None:
            order = await session.scalar(
                select(OrderRow).where(OrderRow.id == order_id).with_for_update()
            )
            if (
                order is None
                or order.server_id != server.id
                or order.operation_key != operation.operation_key
                or order.provider_key != operation.provider_key
            ):
                raise CreateAttemptConflict("create order binding mismatch")
        receipt = create_routing(_to_domain(operation))
        if receipt is not None:
            fingerprint = server.offer_fingerprint or {}
            if "provider_account_id" in fingerprint and (
                fingerprint["provider_account_id"] != receipt.get("catalog_account_id")
            ):
                raise CreateAttemptConflict("receipt changed immutable catalog provenance")
            self._owned(server, order, normalize_account_id(server.credential_account_id))
            attempts = receipt["attempts"]
            if attempts:
                self._owned(server, order, attempts[-1]["account_id"])
        return operation, server, order

    @staticmethod
    def _claim_matches(operation: Any, generation: int, *, recovery: bool = False) -> Operation:
        statuses = {OperationStatus.IN_FLIGHT.value}
        if recovery:
            statuses.add(OperationStatus.OUTCOME_UNKNOWN.value)
        if operation.attempts != generation or operation.status not in statuses:
            raise CreateAttemptConflict("create claim was fenced or is no longer live")
        domain = _to_domain(operation)
        if create_routing(domain) is None:
            raise CreateAttemptConflict("attempted operation has no routing proof")
        return domain

    @staticmethod
    def _unaccepted(server: Any, order: Any) -> None:
        if server.provider_server_id or server.state not in _CREATE_STATES:
            raise CreateAttemptConflict("server no longer permits a new create")
        if order is not None and (order.provider_order_id or order.status != "pending_submit"):
            raise CreateAttemptConflict("order no longer permits a new create")

    @staticmethod
    def _owned(server: Any, order: Any, account_id: str) -> None:
        if normalize_account_id(server.credential_account_id) != account_id:
            raise CreateAttemptConflict("receipt/server ownership mismatch")
        if order is not None and normalize_account_id(order.credential_account_id) != account_id:
            raise CreateAttemptConflict("receipt/order ownership mismatch")

    @staticmethod
    def _receipt(operation: Any, receipt: CreateRoutingReceipt) -> None:
        response = dict(operation.provider_response or {})
        response["create_routing"] = receipt
        operation.provider_response = response

    @staticmethod
    async def _commit(session: AsyncSession, operation: Any) -> Operation:
        operation.updated_at = to_db_utc(utc_now())
        await session.flush()
        result = _to_domain(operation)
        await session.commit()
        return result

    async def claim(
        self,
        operation_id: UUID,
        server_id: UUID,
        order_id: UUID | None = None,
        catalog_account_id: str | None = None,
    ) -> Operation | None:
        async with self._session_factory() as session:
            operation, server, order = await self._bundle(
                session,
                operation_id,
                server_id=server_id,
                order_id=order_id,
            )
            if operation.status != OperationStatus.PENDING.value:
                return None
            self._unaccepted(server, order)
            self._owned(server, order, normalize_account_id(server.credential_account_id))
            fingerprint = server.offer_fingerprint or {}
            if "fingerprint_version" in fingerprint and (
                fingerprint["fingerprint_version"] != 3
                or fingerprint.get("fulfillment_policy") != "capacity_failover"
            ):
                raise CreateAttemptConflict(
                    "historical hourly contracts cannot enter account failover"
                )
            if "provider_account_id" in fingerprint and (
                fingerprint["provider_account_id"] != catalog_account_id
            ):
                raise CreateAttemptConflict("catalog provenance differs from the immutable intent")
            if catalog_account_id is not None and (
                safe_fact(catalog_account_id) is None
                or normalize_account_id(catalog_account_id) != catalog_account_id
            ):
                raise CreateAttemptConflict("invalid catalog account provenance")
            domain = _to_domain(operation)
            receipt = create_routing(domain)
            if receipt is None:
                if operation.attempts != 0 or (order is not None and order.post_attempted_at):
                    raise CreateAttemptConflict("historical attempt has no pre-POST routing proof")
                if any(
                    (operation.provider_response or {}).get(key)
                    for key in ("provider_server_id", "provider_order_id")
                ):
                    raise CreateAttemptConflict("operation already contains provider identity")
                receipt = {
                    "version": 1,
                    "policy": "capacity_failover",
                    "catalog_account_id": catalog_account_id,
                    "attempts": [],
                }
            elif not safe_pre_post(domain):
                raise CreateAttemptConflict(
                    "only proven pre-POST/refused attempts may be reclaimed"
                )
            if receipt.get("catalog_account_id") != catalog_account_id:
                raise CreateAttemptConflict("reclaim changed original catalog provenance")
            self._receipt(operation, receipt)
            operation.status = OperationStatus.IN_FLIGHT.value
            operation.attempts += 1
            operation.error = None
            return await self._commit(session, operation)

    async def start_attempt(
        self,
        operation_id: UUID,
        claim_generation: int,
        account_id: str,
        *,
        request_facts: Mapping[str, str] | None = None,
    ) -> Operation:
        account_id = normalize_account_id(account_id)
        async with self._session_factory() as session:
            operation, server, order = await self._bundle(session, operation_id)
            domain = self._claim_matches(operation, claim_generation)
            self._unaccepted(server, order)
            if not safe_pre_post(domain):
                raise CreateAttemptConflict("previous mutation is accepted, refused, or uncertain")
            receipt = create_routing(domain)
            assert receipt is not None
            attempts: list[CreateRoutingAttempt] = receipt["attempts"]
            if attempts:
                self._owned(server, order, attempts[-1]["account_id"])
            if any(attempt["account_id"] == account_id for attempt in attempts):
                raise CreateAttemptConflict("a definitively refused account cannot be sent again")
            if request_facts is not None:
                if set(request_facts) != {"name", "plan_id", "location_id", "image_id"} or any(
                    not isinstance(value, str) or not value.strip()
                    for value in request_facts.values()
                ):
                    raise CreateAttemptConflict("legacy create request is incomplete")
                fingerprint = dict(server.offer_fingerprint or {})
                if "fingerprint_version" in fingerprint:
                    raise CreateAttemptConflict("legacy request cannot alter an hourly contract")
                frozen = fingerprint.get("legacy_create_request")
                if frozen is not None and frozen != dict(request_facts):
                    raise CreateAttemptConflict("legacy create request changed after SENT")
                fingerprint["legacy_create_request"] = dict(request_facts)
                server.offer_fingerprint = fingerprint
                server.image_id = request_facts["image_id"]
            server.credential_account_id = account_id
            server.updated_at = to_db_utc(utc_now())
            if order is not None:
                order.credential_account_id = account_id
                order.post_attempted_at = utc_now()
                order.updated_at = to_db_utc(utc_now())
            attempts.append(
                {"sequence": len(attempts) + 1, "account_id": account_id, "phase": "sent"}
            )
            self._receipt(operation, receipt)
            return await self._commit(session, operation)

    async def record_refusal(
        self,
        operation_id: UUID,
        claim_generation: int,
        account_id: str,
        *,
        capacity: bool,
        error_code: str | None = None,
        quota_names: tuple[str, ...] = (),
    ) -> Operation:
        account_id = normalize_account_id(account_id)
        if not isinstance(capacity, bool) or (
            capacity and error_code not in DEFINITIVE_CAPACITY_REFUSAL_CODES
        ):
            raise CreateAttemptConflict(
                "capacity refusal requires a documented pre-acceptance code"
            )
        async with self._session_factory() as session:
            operation, server, order = await self._bundle(session, operation_id)
            domain = self._claim_matches(operation, claim_generation)
            self._unaccepted(server, order)
            self._owned(server, order, account_id)
            receipt = create_routing(domain)
            attempt = receipt["attempts"][-1] if receipt and receipt["attempts"] else None
            if (
                receipt is None
                or attempt is None
                or attempt["phase"] != "sent"
                or attempt["account_id"] != account_id
            ):
                raise CreateAttemptConflict("refusal does not belong to the live SENT attempt")
            attempt["phase"] = "capacity_refused" if capacity else "refused"
            code = safe_fact(error_code)
            if code is not None:
                attempt["error_code"] = code
            attempt["quota_names"] = [name for name in quota_names if safe_fact(name) is not None]
            self._receipt(operation, receipt)
            return await self._commit(session, operation)

    async def record_unknown(
        self,
        operation_id: UUID,
        claim_generation: int,
        account_id: str,
    ) -> Operation:
        account_id = normalize_account_id(account_id)
        async with self._session_factory() as session:
            operation, server, order = await self._bundle(session, operation_id)
            domain = self._claim_matches(operation, claim_generation)
            self._owned(server, order, account_id)
            receipt = create_routing(domain)
            attempt = receipt["attempts"][-1] if receipt and receipt["attempts"] else None
            if (
                receipt is None
                or attempt is None
                or attempt["phase"] != "sent"
                or attempt["account_id"] != account_id
            ):
                raise CreateAttemptConflict("unknown outcome does not belong to the SENT attempt")
            attempt["phase"] = "outcome_unknown"
            self._receipt(operation, receipt)
            operation.status = OperationStatus.OUTCOME_UNKNOWN.value
            operation.error = "provider create outcome unknown; read-only recovery required"
            if order is not None:
                order.status = "outcome_unknown"
                order.error = operation.error
            return await self._commit(session, operation)

    async def record_acceptance(
        self,
        operation_id: UUID,
        claim_generation: int,
        account_id: str,
        provider_server_id: str,
        *,
        ipv4: str | None = None,
        ipv6: str | None = None,
    ) -> Operation:
        account_id = normalize_account_id(account_id)
        if safe_fact(provider_server_id) is None:
            raise CreateAttemptConflict("accepted provider identity is invalid")
        async with self._session_factory() as session:
            operation, server, order = await self._bundle(session, operation_id)
            domain = self._claim_matches(operation, claim_generation, recovery=True)
            self._owned(server, order, account_id)
            receipt = create_routing(domain)
            attempt = receipt["attempts"][-1] if receipt and receipt["attempts"] else None
            if (
                receipt is None
                or attempt is None
                or attempt["account_id"] != account_id
                or attempt["phase"]
                not in {
                    "sent",
                    "outcome_unknown",
                    "accepted",
                }
            ):
                raise CreateAttemptConflict("acceptance does not belong to the attempted account")
            if server.state not in _CREATE_STATES and not server.provider_server_id:
                raise CreateAttemptConflict("server left the create window before acceptance")
            identities = [
                server.provider_server_id,
                (operation.provider_response or {}).get("provider_server_id"),
            ]
            if order is not None:
                identities.append(order.provider_order_id)
            if attempt.get("provider_server_id"):
                identities.append(attempt["provider_server_id"])
            if any(identity and identity != provider_server_id for identity in identities):
                raise CreateAttemptConflict("accepted provider identity cannot be replaced")
            first_acceptance = attempt["phase"] != "accepted"
            attempt["phase"] = "accepted"
            attempt["provider_server_id"] = provider_server_id
            receipt["accepted_account_id"] = account_id
            self._receipt(operation, receipt)
            operation.provider_response = {
                **(operation.provider_response or {}),
                "provider_server_id": provider_server_id,
            }
            server.provider_server_id = provider_server_id
            if ipv4 is not None:
                server.ipv4 = ipv4
            if ipv6 is not None:
                server.ipv6 = ipv6
            server.updated_at = to_db_utc(utc_now())
            if order is not None:
                if order.status not in {
                    "pending_submit",
                    "outcome_unknown",
                    "submitted",
                    "provisioning",
                    "active",
                }:
                    raise CreateAttemptConflict("order cannot attach an accepted resource")
                order.provider_order_id = provider_server_id
                if order.status in {"pending_submit", "outcome_unknown"}:
                    order.status = "submitted"
                order.error = None
                if first_acceptance:
                    order.attempts += 1
                order.updated_at = to_db_utc(utc_now())
            return await self._commit(session, operation)

    async def save_outcome(
        self,
        operation_id: UUID,
        claim_generation: int,
        status: OperationStatus,
        *,
        error: str | None = None,
        correlation: Mapping[str, object] | None = None,
    ) -> Operation:
        async with self._session_factory() as session:
            operation, server, order = await self._bundle(session, operation_id)
            domain = self._claim_matches(operation, claim_generation, recovery=True)
            attempt = last_attempt(domain)
            if attempt is not None:
                self._owned(server, order, attempt["account_id"])
            if status == OperationStatus.COMPLETED:
                if (
                    attempt is None
                    or attempt["phase"] != "accepted"
                    or not server.provider_server_id
                ):
                    raise CreateAttemptConflict("completion requires durable accepted identity")
                if order is None and server.state == "requested":
                    server.state = "provisioning"
                server.updated_at = to_db_utc(utc_now())
            elif status == OperationStatus.PENDING:
                if not safe_pre_post(domain):
                    raise CreateAttemptConflict("uncertain mutations cannot be requeued")
                self._unaccepted(server, order)
            elif status == OperationStatus.FAILED:
                if attempt is not None and attempt["phase"] not in {"capacity_refused", "refused"}:
                    raise CreateAttemptConflict(
                        "accepted/uncertain mutations cannot fail or release funds"
                    )
                self._unaccepted(server, order)
                server.state = "error"
                server.updated_at = to_db_utc(utc_now())
                if order is not None:
                    order.status = "failed"
                    order.error = error
                    order.updated_at = to_db_utc(utc_now())
            elif status == OperationStatus.OUTCOME_UNKNOWN:
                if attempt is None or attempt["phase"] not in {
                    "sent",
                    "outcome_unknown",
                    "accepted",
                }:
                    raise CreateAttemptConflict("there is no potentially accepted mutation")
            else:
                raise CreateAttemptConflict("unsupported guarded finalization")
            if correlation:
                if set(correlation) - _COMPLETION_KEYS:
                    raise CreateAttemptConflict("unsafe completion correlation fields")
                for key in ("provider_server_id", "provider_order_id"):
                    if key in correlation and correlation[key] != server.provider_server_id:
                        raise CreateAttemptConflict(
                            "completion correlation changes accepted identity"
                        )
                operation.provider_response = {**(operation.provider_response or {}), **correlation}
            operation.status = status.value
            operation.error = error
            return await self._commit(session, operation)

    async def resume_safe_claim(
        self,
        operation_id: UUID,
        claim_generation: int,
    ) -> Operation:
        """Fence a stale worker before safe requeue OR same-account read-only recovery."""
        async with self._session_factory() as session:
            operation, server, order = await self._bundle(session, operation_id)
            domain = self._claim_matches(operation, claim_generation, recovery=True)
            attempt = last_attempt(domain)
            if attempt is not None:
                self._owned(server, order, attempt["account_id"])
            operation.attempts += 1
            if safe_pre_post(domain):
                self._unaccepted(server, order)
                operation.status = OperationStatus.PENDING.value
            elif attempt is not None and attempt["phase"] == "refused":
                self._unaccepted(server, order)
                operation.status = OperationStatus.IN_FLIGHT.value
            elif attempt is not None and attempt["phase"] in {"sent", "outcome_unknown"}:
                receipt = create_routing(domain)
                assert receipt is not None
                receipt["attempts"][-1]["phase"] = "outcome_unknown"
                self._receipt(operation, receipt)
                operation.status = OperationStatus.OUTCOME_UNKNOWN.value
                if order is not None:
                    order.status = "outcome_unknown"
            elif attempt is None or attempt["phase"] != "accepted":
                raise CreateAttemptConflict("historical routing proof cannot authorize recovery")
            return await self._commit(session, operation)
