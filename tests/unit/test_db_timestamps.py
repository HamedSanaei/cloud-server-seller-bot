"""Unit tests for the legacy naive-timestamp persistence boundary helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from cloud_platform.db.timestamps import (
    from_db_utc,
    from_db_utc_or_none,
    to_db_utc,
    to_db_utc_or_none,
    utc_now,
)


class TestToDbUtc:
    def test_aware_utc_is_stripped(self) -> None:
        value = datetime(2026, 9, 25, 12, 30, 15, 123456, tzinfo=UTC)
        assert to_db_utc(value) == datetime(2026, 9, 25, 12, 30, 15, 123456)
        assert to_db_utc(value).tzinfo is None

    def test_non_utc_offset_is_converted_then_stripped(self) -> None:
        """An aware value is converted to UTC first — never just stripped."""
        berlin = timezone(timedelta(hours=2))
        value = datetime(2026, 9, 25, 14, 30, tzinfo=berlin)
        assert to_db_utc(value) == datetime(2026, 9, 25, 12, 30)

    def test_naive_value_passes_through_as_utc(self) -> None:
        """Naive means already-UTC in this schema; local time is never assumed."""
        naive = datetime(2026, 9, 25, 12, 30)
        assert to_db_utc(naive) is naive

    def test_optional_variants(self) -> None:
        assert to_db_utc_or_none(None) is None
        assert to_db_utc_or_none(datetime(2026, 9, 25, 12, 30, tzinfo=UTC)) == datetime(
            2026, 9, 25, 12, 30
        )


class TestFromDbUtc:
    def test_naive_becomes_aware_utc(self) -> None:
        naive = datetime(2026, 9, 25, 12, 30, 15)
        restored = from_db_utc(naive)
        assert restored.tzinfo is UTC
        assert restored == datetime(2026, 9, 25, 12, 30, 15, tzinfo=UTC)

    def test_aware_values_are_normalized_to_utc(self) -> None:
        tehran = timezone(timedelta(hours=3, minutes=30))
        value = datetime(2026, 9, 25, 16, 0, tzinfo=tehran)
        assert from_db_utc(value) == datetime(2026, 9, 25, 12, 30, tzinfo=UTC)

    def test_optional_variants(self) -> None:
        assert from_db_utc_or_none(None) is None
        assert from_db_utc_or_none(datetime(2026, 9, 25, 12, 30)).tzinfo is UTC


class TestRoundTrip:
    def test_round_trip_is_lossless(self) -> None:
        moment = utc_now()
        assert from_db_utc(to_db_utc(moment)) == moment

    def test_utc_now_is_aware(self) -> None:
        assert utc_now().tzinfo is UTC
