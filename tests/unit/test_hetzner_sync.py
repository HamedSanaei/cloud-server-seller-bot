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
        """Test successful plans sync."""
        page1_response = {
            "server_types": [
                {
                    "id": "cx22",
                    "name": "CX22",
                    "cores": 2,
                    "memory": 4,
                    "disk": 40,
                    "architecture": "x86",
                    "cpu_type": "shared",
                    "storage_type": "ssd",
                    "prices": [{"price_monthly": {"gross": "5.83"}}],
                }
            ],
            "meta": {"pagination": {}},
        }

        syncer._request = AsyncMock(return_value=page1_response)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.commit = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )
        mock_session.get = AsyncMock(return_value=None)

        syncer._session_factory = lambda: mock_session

        result = await syncer.sync_plans()

        assert isinstance(result, SyncResult)
        assert result.total_fetched == 1

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
