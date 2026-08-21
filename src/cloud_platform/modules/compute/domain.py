from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class ServerLifecycleState(StrEnum):
    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    DELETE_REQUESTED = "delete_requested"
    DELETING = "deleting"
    DELETED = "deleted"
    MANUAL_REVIEW = "manual_review"


_ALLOWED: dict[ServerLifecycleState, frozenset[ServerLifecycleState]] = {
    ServerLifecycleState.REQUESTED: frozenset({ServerLifecycleState.PROVISIONING, ServerLifecycleState.ERROR}),
    ServerLifecycleState.PROVISIONING: frozenset({ServerLifecycleState.RUNNING, ServerLifecycleState.ERROR, ServerLifecycleState.MANUAL_REVIEW}),
    ServerLifecycleState.RUNNING: frozenset({ServerLifecycleState.STOPPED, ServerLifecycleState.DELETE_REQUESTED, ServerLifecycleState.ERROR}),
    ServerLifecycleState.STOPPED: frozenset({ServerLifecycleState.RUNNING, ServerLifecycleState.DELETE_REQUESTED, ServerLifecycleState.ERROR}),
    ServerLifecycleState.ERROR: frozenset({ServerLifecycleState.PROVISIONING, ServerLifecycleState.DELETE_REQUESTED, ServerLifecycleState.MANUAL_REVIEW}),
    ServerLifecycleState.DELETE_REQUESTED: frozenset({ServerLifecycleState.DELETING, ServerLifecycleState.DELETED, ServerLifecycleState.MANUAL_REVIEW}),
    ServerLifecycleState.DELETING: frozenset({ServerLifecycleState.DELETED, ServerLifecycleState.MANUAL_REVIEW}),
    ServerLifecycleState.DELETED: frozenset(),
    ServerLifecycleState.MANUAL_REVIEW: frozenset({ServerLifecycleState.PROVISIONING, ServerLifecycleState.DELETE_REQUESTED, ServerLifecycleState.DELETED}),
}


@dataclass(slots=True)
class CloudServer:
    id: UUID
    user_id: UUID
    provider_key: str
    provider_account_id: UUID
    state: ServerLifecycleState
    provider_server_id: str | None = None

    def transition_to(self, target: ServerLifecycleState) -> None:
        if target not in _ALLOWED[self.state]:
            raise ValueError(f"invalid lifecycle transition: {self.state} -> {target}")
        self.state = target
