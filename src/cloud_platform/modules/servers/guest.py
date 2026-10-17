"""Opt-in guest management for the exact operator test VPS, via pinned OpenSSH."""

from __future__ import annotations

import asyncio
import re
from hashlib import sha256
from pathlib import Path
from typing import Any

from cloud_platform.core.config import get_settings
from cloud_platform.modules.servers.confirmations import ConfirmationBinding
from cloud_platform.modules.servers.models import ServerActionOutcome, ServerOperation
from cloud_platform.modules.servers.test_identity import TEST_IPV4, is_test_server
from cloud_platform.modules.users.superadmins import require_superadmin

SSH_CONFIG_PATH = Path("/etc/cloud-platform/management-ssh/config")


def guest_management_configured() -> bool:
    alias = get_settings().test_server_ssh_alias
    try:
        return bool(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", alias) and SSH_CONFIG_PATH.is_file()
        )
    except OSError:
        return False


def port_binding(actor: Any, server: Any, port: int) -> ConfirmationBinding:
    require_superadmin(actor, get_settings().telegram_superadmin_user_ids)
    if not is_test_server(server):
        raise ValueError("guest management is restricted to the verified test VPS")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("SSH port must be an integer between 1024 and 65535")
    if not guest_management_configured():
        raise ValueError("test-server pinned OpenSSH configuration is not installed")
    return ConfirmationBinding(
        customer_id=actor.id,
        server_id=server.id,
        operation=ServerOperation.SSH_PORT_CHANGE,
        arguments={"port": port},
    )


async def _ssh(*args: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ssh",
        "-F",
        str(SSH_CONFIG_PATH),
        "-oBatchMode=yes",
        "-oStrictHostKeyChecking=yes",
        "-oConnectTimeout=10",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=40)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("guest operation outcome unknown; inspect the server") from None
    if process.returncode != 0:
        raise RuntimeError("guest operation was not verified; inspect the server")
    return output


async def change_test_ssh_port(
    container: Any,
    actor: Any,
    server: Any,
    port: int,
    token: str,
    key: str,
) -> ServerActionOutcome:
    binding = port_binding(actor, server, port)
    alias = get_settings().test_server_ssh_alias
    config = (await _ssh("-G", alias)).decode("utf-8")
    hostname = next(
        (line.split(" ", 1)[1] for line in config.splitlines() if line.startswith("hostname ")), ""
    )
    if hostname != TEST_IPV4:
        raise ValueError("configured SSH alias must resolve to the exact test-server IPv4")
    confirmation = await container.confirmation_verifier().consume(token, binding)
    if not confirmation.ok:
        raise ValueError("port-change confirmation expired, used or invalid")
    receipt = sha256(key.encode()).hexdigest()
    # Both values are bounded integers/hex. The privileged helper is installed by the operator.
    await _ssh(
        "-oHostKeyAlias=" + TEST_IPV4,
        alias,
        f"sudo -n /usr/local/sbin/serverhub-ssh-port {port} {receipt}",
    )
    await _ssh("-oHostKeyAlias=" + TEST_IPV4, "-p", str(port), alias, "true")
    return ServerActionOutcome(
        operation=ServerOperation.SSH_PORT_CHANGE,
        accepted=True,
        detail=f"SSH port {port} verified; previous port remains available",
    )
