"""Classify HTTP liveness status and fail closed on network errors."""

import io
from unittest.mock import MagicMock

import pytest

from cloud_platform.core import http_health


@pytest.mark.parametrize(
    "line,healthy",
    [
        (b"HTTP/1.1 200 OK\r\n", True),
        (b"HTTP/1.0 200 OK\r\n", True),
        (b"HTTP/1.1 503 Unavailable\r\n", False),
        (b"HTTP/1.1 401 Unauthorized\r\n", False),
        (b"HTTP/1.1 200", False),
        (b"not-http 200 OK\n", False),
        (b"", False),
    ],
)
def test_real_http_status_is_required(monkeypatch, line, healthy):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.makefile.return_value = io.BytesIO(line)
    monkeypatch.setattr(http_health.socket, "socket", lambda *_args: connection)
    assert http_health.api_healthy() is healthy


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionRefusedError()])
def test_network_failures_fail_closed(monkeypatch, error):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.connect.side_effect = error
    monkeypatch.setattr(http_health.socket, "socket", lambda *_args: connection)
    assert http_health.api_healthy() is False
