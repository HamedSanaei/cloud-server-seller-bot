"""Customer-facing server management (Telegram «سرورهای من»).

The module owns the application layer for every customer-visible VPS action:
ownership, capability policy, one-time confirmations, idempotency, audit and
business events. It is provider-neutral by construction — provider work happens
behind :mod:`cloud_platform.providers.vps_ports`.

Import the service from :mod:`cloud_platform.modules.servers.service` to keep
module import cycles out of the bot/container path.
"""

from __future__ import annotations

__all__: list[str] = []
