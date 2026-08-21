from uuid import uuid4

import pytest

from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState


def test_delete_must_follow_lifecycle() -> None:
    server = CloudServer(uuid4(), uuid4(), "hetzner", uuid4(), ServerLifecycleState.RUNNING, "123")
    server.transition_to(ServerLifecycleState.DELETE_REQUESTED)
    server.transition_to(ServerLifecycleState.DELETING)
    server.transition_to(ServerLifecycleState.DELETED)
    with pytest.raises(ValueError):
        server.transition_to(ServerLifecycleState.RUNNING)
