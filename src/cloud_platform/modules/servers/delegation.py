"""Fail-closed access policy for the exact reserved, operator-funded test VPS."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from cloud_platform.modules.servers.test_identity import is_test_server

# No power, credentials, console, reinstall, delete, port, IP mutation or billing
# privilege is implied by visibility. Writes need a separately reviewed design.
DELEGATABLE_OPERATIONS = frozenset({"view", "refresh", "traffic", "ip_list", "monitoring"})


def binding(server: Any) -> str:
    return sha256(
        f"{server.id}:{server.user_id}:{server.provider_key}:{server.provider_server_id}:"
        f"{server.credential_account_id}:{server.ipv4}:{server.funding_source}:"
        f"{server.funding_operator_id}".encode()
    ).hexdigest()


def validate_operations(operations: list[str]) -> list[str]:
    if not isinstance(operations, list) or not all(isinstance(op, str) for op in operations):
        raise ValueError("operations must be a list of explicit permissions")
    selected = set(operations)
    if "view" not in selected or not selected <= DELEGATABLE_OPERATIONS:
        raise ValueError("an explicit read-only delegation including view is required")
    return sorted(selected)


def permissions(grant: Any, server: Any, user_id: Any) -> frozenset[str]:
    if (
        grant is None
        or server is None
        or grant.revoked_at is not None
        or grant.delegate_user_id != user_id
        or grant.server_id != server.id
        or grant.owner_user_id != server.user_id
        or user_id == server.user_id
        or not is_test_server(server)
        or grant.binding != binding(server)
    ):
        return frozenset()
    try:
        return frozenset(validate_operations(grant.operations))
    except (ValueError, TypeError):
        return frozenset()
