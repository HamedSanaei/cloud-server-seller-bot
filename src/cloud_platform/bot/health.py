"""Read-only health probe backed by successful Telegram long-poll responses.

The probe never calls getUpdates: only the existing poller's request middleware
records progress. A live but disconnected or duplicate-token poller goes stale.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "cloud-platform-bot-polling.json"
MAX_POLL_AGE_SECONDS = 90


def polling_healthy(path: Path = HEARTBEAT_PATH, *, now: float | None = None) -> bool:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        pid, last_poll = record["pid"], record["last_poll"]
        if type(pid) is not int or pid <= 0 or type(last_poll) not in (int, float):
            return False
        age = (time.monotonic() if now is None else now) - last_poll
        if not math.isfinite(age) or not 0 <= age <= MAX_POLL_AGE_SECONDS:
            return False
        # Containers run Linux. Reject a fresh file left by an exited poller;
        # the healthcheck process is distinct from the process in the record.
        if sys.platform == "linux" and not Path(f"/proc/{pid}").is_dir():
            return False
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return False
    return True


if __name__ == "__main__":
    healthy = polling_healthy()
    print("Telegram polling healthy" if healthy else "Telegram polling heartbeat unavailable/stale")
    raise SystemExit(0 if healthy else 1)
else:
    # Healthcheck subprocesses need only the read-only stdlib probe above.
    # Import the Telegram SDK only for the existing poller's middleware.
    from aiogram import Bot
    from aiogram.client.session.middlewares.base import (
        BaseRequestMiddleware,
        NextRequestMiddlewareType,
    )
    from aiogram.methods import GetUpdates, Response, TelegramMethod
    from aiogram.methods.base import TelegramType

    class PollingHeartbeat(BaseRequestMiddleware):
        def __init__(self, path: Path = HEARTBEAT_PATH) -> None:
            self._path = path
            self._last_write = float("-inf")
            self.clear()

        def clear(self) -> None:
            self._path.unlink(missing_ok=True)

        async def __call__(
            self,
            make_request: NextRequestMiddlewareType[TelegramType],
            bot: Bot,
            method: TelegramMethod[TelegramType],
        ) -> Response[TelegramType]:
            response = await make_request(bot, method)
            if isinstance(method, GetUpdates):
                now = time.monotonic()
                # Bound local I/O during bursts; long-poll failures never refresh it.
                if now - self._last_write >= 5:
                    temporary = self._path.with_suffix(".tmp")
                    temporary.write_text(
                        json.dumps({"pid": os.getpid(), "last_poll": now}), encoding="utf-8"
                    )
                    temporary.replace(self._path)
                    self._last_write = now
            return response
