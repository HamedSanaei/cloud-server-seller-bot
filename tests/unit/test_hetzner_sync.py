"""Tests for Hetzner catalog sync."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cloud_platform.providers.hetzner.sync import (
    HetznerCatalogSyncer,
    SyncResult,
)


class TestHetznerCatalogSyncer:
    """Tests for HetznerCatalogSyncer."""

    @pytest.fixture
    def mock_session_factory(self):
        """Create a mock session factory."""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.commit = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock()
        # Mock get to return None (no existing provider)
        mock_session.get = AsyncMock(return_value=None)
        return lambda: mock_session

    @pytest.fixture
    def syncer(self, mock_session_factory):
        """Create a syncer with mocked dependencies."""
        with patch("cloud_platform.providers.hetzner.sync.get_settings") as mock_settings:
            mock_settings.return_value.hetzner_api_token = "test-token"
            syncer = HetznerCatalogSyncer(
                session_factory=mock_session_factory,
                token="test-token",
            )
            syncer._client = AsyncMock()
            yield syncer

    @pytest.mark.asyncio
    async def test_sync_locations_success(self, syncer):
        """Test successful locations sync with pagination."""
        # Mock paginated responses
        page1_response = {
            "locations": [
                {"id": 1, "name": "fsn1", "country": "DE", "city": "Falkenstein"},
                {"id": 2, "name": "nbg1", "country": "DE", "city": "Nuremberg"},
            ],
            "meta": {"pagination": {"next_page": 2}},
        }
        page2_response = {
            "locations": [
                {"id": 3, "name": "hel1", "country": "FI", "city": "Helsinki"},
            ],
            "meta": {"pagination": {}},
        }

        syncer._request = AsyncMock(side_effect=[page1_response, page2_response])

        # Mock session factory
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.commit = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )
        mock_session.get = AsyncMock(return_value=None)  # No existing provider

        syncer._session_factory = lambda: mock_session

        result = await syncer.sync_locations()

        assert isinstance(result, SyncResult)
        assert result.total_fetched == 3
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_sync_plans_success(self, syncer):
        """Plans sync ingests location-aware prices from the real API shape."""
        from unittest.mock import MagicMock
        from uuid import uuid4

        page1_response = {
            "server_types": [
                {
                    "id": 20,
                    "name": "CX22",
                    "cores": 2,
                    "memory": 4,
                    "disk": 40,
                    "architecture": "x86",
                    "cpu_type": "shared",
                    "storage_type": "local",
                    "prices": [
                        {
                            "location": "fsn1",
                            "hourly": {"gross": "0.0219"},
                            "monthly": {"gross": "15.87"},
                        },
                        {
                            "location": "nbg1",
                            "hourly": {"gross": "0.0225"},
                            "monthly": {"gross": "16.14"},
                        },
                    ],
                }
            ],
            "meta": {"pagination": {}},
        }

        syncer._request = AsyncMock(return_value=page1_response)

        provider_row = MagicMock()
        provider_row.id = uuid4()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.commit = AsyncMock()
        mock_session.add = MagicMock()
        # Each location upsert opens a session: provider lookup (found), then
        # catalog lookup (row missing). Two locations -> four executions.
        provider_result = MagicMock(scalars=lambda: MagicMock(first=lambda: provider_row))
        missing_result = MagicMock(scalars=lambda: MagicMock(first=lambda: None))
        mock_session.execute = AsyncMock(
            side_effect=[
                provider_result,
                missing_result,
                provider_result,
                missing_result,
            ]
        )

        syncer._session_factory = lambda: mock_session

        result = await syncer.sync_plans()

        assert isinstance(result, SyncResult)
        assert result.total_fetched == 1
        # One catalog row per (plan, location) — location-aware, not "global".
        assert result.total_upserted == 2
        assert result.total_skipped == 0

        added = [c.args[0] for c in mock_session.add.call_args_list]
        assert {a.provider_location_id for a in added} == {"fsn1", "nbg1"}
        # Decimal math: 0.0219 EUR/h -> 2.19 -> 2 minor; 0.0225 -> 2.25 -> 2.
        assert {a.price_per_quantum for a in added} == {2}
        assert all(a.currency == "EUR" for a in added)

    @pytest.mark.asyncio
    async def test_sync_images_success(self, syncer):
        """Test successful images sync."""
        page1_response = {
            "images": [
                {"id": "img-1", "name": "ubuntu-22.04", "type": "system"},
                {"id": "img-2", "name": "debian-11", "type": "system"},
            ],
            "meta": {"pagination": {}},
        }

        syncer._request = AsyncMock(return_value=page1_response)

        result = await syncer.sync_images()

        assert isinstance(result, SyncResult)
        assert result.total_fetched == 2
        assert result.total_upserted == 2

    @pytest.mark.asyncio
    async def test_sync_all_aggregates_results(self, syncer):
        """Test sync_all aggregates results from all sync operations."""
        from cloud_platform.providers.hetzner.sync import SyncResult

        syncer.sync_locations = AsyncMock(return_value=SyncResult(10, 5, 2, []))
        syncer.sync_plans = AsyncMock(return_value=SyncResult(20, 15, 3, []))
        syncer.sync_images = AsyncMock(return_value=SyncResult(30, 25, 5, []))

        results = await syncer.sync_all()

        assert "locations" in results
        assert "plans" in results
        assert "images" in results
        assert results["locations"].total_fetched == 10
        assert results["plans"].total_fetched == 20
        assert results["images"].total_fetched == 30
