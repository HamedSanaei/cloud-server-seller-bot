from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class AuditEvent:
    occurred_at: datetime
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    metadata: dict[str, Any]
