"""Tiny, configuration-free HTTP liveness probe; executable with python -I -S."""

from __future__ import annotations

import socket

SOCKET_TIMEOUT_SECONDS = 10


def api_healthy() -> bool:
    """Check the real local HTTP endpoint, not merely an open TCP port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(SOCKET_TIMEOUT_SECONDS)
            connection.connect(("127.0.0.1", 8000))
            connection.sendall(
                b"GET /health/live HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            with connection.makefile("rb") as response:
                line = response.readline(4096)
            parts = line.split()
            return (
                line.endswith(b"\n")
                and len(parts) >= 2
                and parts[0] in (b"HTTP/1.0", b"HTTP/1.1")
                and parts[1] == b"200"
            )
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(0 if api_healthy() else 1)
