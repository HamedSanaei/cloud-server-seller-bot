"""Persistence-boundary UTC normalization for legacy naive timestamps.

The production schema deliberately mixes two timestamp contracts:

* ``TIMESTAMP WITH TIME ZONE`` columns, declared ``DateTime(timezone=True)``,
  store and return timezone-aware UTC values (``servers.last_accrued_at``,
  ``payment_sessions.fx_observed_at``, ...). They must stay aware.
* legacy ``TIMESTAMP WITHOUT TIME ZONE`` columns, declared as plain
  ``DateTime``, store **naive UTC**. The repositories already assumed that
  convention on reads (see ``_aware_or_none`` in the compute/billing/renewals
  adapters) but not on writes.

asyncpg enforces the distinction at the wire level: binding a
timezone-*aware* ``datetime`` into a naive ``timestamp`` column raises::

    asyncpg.exceptions.DataError: invalid input for query argument $n:
    datetime.datetime(... tzinfo=datetime.timezone.utc)
    (can't subtract offset-naive and offset-aware datetimes)

That aborted operation claiming *before* the provider POST, so a server could
stay REQUESTED forever, and it also broke payment-session reconciliation.

The contract implemented here is deliberately explicit and one-directional:

* domain/aware value -> convert to UTC -> strip ``tzinfo`` only at the DB
  boundary (:func:`to_db_utc`),
* naive DB value -> attach UTC when entering the domain/service layer
  (:func:`from_db_utc`).

A naive value is never reinterpreted as local time: naive values already are
the storage contract (naive UTC), so they pass through unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = [
    "from_db_utc",
    "from_db_utc_or_none",
    "to_db_utc",
    "to_db_utc_or_none",
    "utc_now",
]


def utc_now() -> datetime:
    """The current time, timezone-aware in UTC (never a naive local clock)."""
    return datetime.now(UTC)


def to_db_utc(value: datetime) -> datetime:
    """Normalize a domain datetime for a legacy naive ``timestamp`` column.

    An aware value is converted to UTC and stripped of its ``tzinfo``; a naive
    value is returned unchanged because naive means "already UTC" in this
    schema (it is never reinterpreted as local time).
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def to_db_utc_or_none(value: datetime | None) -> datetime | None:
    """Nullable :func:`to_db_utc`."""
    if value is None:
        return None
    return to_db_utc(value)


def from_db_utc(value: datetime) -> datetime:
    """Normalize a legacy naive ``timestamp`` value into aware UTC.

    Aware values (from genuinely ``timestamptz`` columns) are converted to UTC
    so the service layer only ever sees one timezone.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def from_db_utc_or_none(value: datetime | None) -> datetime | None:
    """Nullable :func:`from_db_utc`."""
    if value is None:
        return None
    return from_db_utc(value)
