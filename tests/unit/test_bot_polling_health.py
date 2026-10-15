"""Only successful long polls count as bot health; probes never poll Telegram."""

from __future__ import annotations

import builtins
import json
import os
import runpy
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiogram import Bot
from aiogram.methods import GetMe, GetUpdates

from cloud_platform.bot import health


async def test_disconnected_and_non_polling_clients_do_not_appear_healthy(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    clock = SimpleNamespace(monotonic=lambda: 100.0)
    monkeypatch.setattr(health, "time", clock)
    heartbeat = health.PollingHeartbeat(path)

    async def success(bot, method):
        return []

    async def disconnected(bot, method):
        raise RuntimeError("network unavailable")

    bot = MagicMock(spec=Bot)
    await heartbeat(success, bot, GetMe())
    assert not health.polling_healthy(path, now=100)
    with pytest.raises(RuntimeError, match="network unavailable"):
        await heartbeat(disconnected, bot, GetUpdates())
    assert not health.polling_healthy(path, now=100)
    await heartbeat(success, bot, GetUpdates())
    assert health.polling_healthy(path, now=100)
    assert health.polling_healthy(path, now=190)
    assert not health.polling_healthy(path, now=191)
    assert not health.polling_healthy(path, now=99)
    with pytest.raises(RuntimeError):
        await heartbeat(disconnected, bot, GetUpdates())
    assert not health.polling_healthy(path, now=191)
    heartbeat.clear()
    assert not health.polling_healthy(path, now=100)


async def test_new_poller_cannot_inherit_a_predecessors_fresh_heartbeat(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"pid": os.getpid(), "last_poll": 100}))
    assert health.polling_healthy(path, now=100)
    heartbeat = health.PollingHeartbeat(path)
    assert not health.polling_healthy(path, now=100)
    clock = SimpleNamespace(monotonic=lambda: 200.0)
    monkeypatch.setattr(health, "time", clock)

    async def success(bot, method):
        return []

    await heartbeat(success, MagicMock(spec=Bot), GetUpdates())
    assert health.polling_healthy(path, now=200)
    assert not health.polling_healthy(path, now=291)


@pytest.mark.parametrize(
    "record",
    [
        "not JSON",
        "[]",
        "{}",
        '{"pid":true,"last_poll":100}',
        '{"pid":0,"last_poll":100}',
        '{"pid":1,"last_poll":true}',
        '{"pid":1,"last_poll":NaN}',
    ],
)
def test_corrupt_or_invalid_heartbeat_fails_closed(tmp_path, record):
    path = tmp_path / "heartbeat.json"
    path.write_text(record)
    assert not health.polling_healthy(path, now=100)


@pytest.mark.parametrize("last_poll,expected_exit", [(100, 0), (10, 0), (9, 1), (101, 1)])
def test_cli_checks_heartbeat_without_importing_telegram_sdk_or_network(
    tmp_path, monkeypatch, capsys, last_poll, expected_exit
):
    path = tmp_path / "cloud-platform-bot-polling.json"
    record = json.dumps({"pid": os.getpid(), "last_poll": last_poll})
    path.write_text(record)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    real_import = builtins.__import__

    def import_without_sdk_or_network(name, *args, **kwargs):
        if name.split(".", 1)[0] in {"aiogram", "socket"}:
            raise AssertionError(f"CLI probe must not import {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_sdk_or_network)
    with pytest.raises(SystemExit) as result:
        runpy.run_path(health.__file__, run_name="__main__")
    assert result.value.code == expected_exit
    output = capsys.readouterr().out
    assert ("Telegram polling healthy" in output) is (expected_exit == 0)
    # The CLI can neither refresh nor replace the poller's heartbeat.
    assert path.read_text() == record
