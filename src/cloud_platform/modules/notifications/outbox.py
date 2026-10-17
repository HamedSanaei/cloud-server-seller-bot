"""Append a customer-safe notification inside the caller's existing DB transaction."""

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from cloud_platform.db.base import BusinessLogEvent


def notice_row(*, key: str, user_id: UUID, text: str, server_id: str | None = None) -> Any:
    payload = {"user_id": str(user_id), "text": text[:1800]}
    if server_id:
        payload["server_id"] = server_id
    return BusinessLogEvent(
        event_key="notice:" + sha256(key.encode()).hexdigest(),
        event_type="customer.notice",
        payload=payload,
        status="PENDING",
        attempts=0,
        created_at=datetime.now(UTC),
    )
