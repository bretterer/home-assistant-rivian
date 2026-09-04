"""Unit tests for Open-Meteo weather client and temperature calculations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Self
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.rivian.weather import (
    OpenMeteoWeatherClient,
    async_get_current_temperature,
    async_get_historical_temperature_for_timestamp,
    async_get_historical_temperatures,
    calculate_distance_weighted_temperature,
    calculate_window_average_temperature,
    get_interpolated_temperature,
)


class MockClientResponse:
    """Mock aiohttp ClientResponse for testing."""

    def __init__(
        self,
        status: int = 200,
        json_data: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.status = status
        self._json_data = json_data or {}
        self._raise_exc = raise_exc

    async def json(self) -> dict[str, Any]:
        if self._raise_exc:
            raise self._raise_exc
        return self._json_data

    async def __aenter__(self) -> Self:
        if self._raise_exc and not isinstance(
            self._raise_exc, (KeyError, TypeError, ValueError)
        ):
            raise self._raise_exc
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class TestDistanceWeightedTemperature:
    """Tests for calculate_distance_weighted_temperature algorithm."""

    def test_empty_samples_returns_none(self) -> None:
        """Test empty samples list returns None."""
        assert calculate_distance_weighted_temperature([]) is None
        assert calculate_distance_weighted_temperature([{"temp_f": None}]) is None

    def test_single_sample_returns_sample_temp(self) -> None:
        """Test single sample returns that sample's temperature."""
        samples = [
            {
                "timestamp": "2026-08-20T14:30:00Z",
                "lat": 39.7392,
                "lon": -104.9903,
                "temp_f": 72.4,
                "distance_at_sample": 0.1,
            }
        ]
        result = calculate_distance_weighted_temperature(
            samples, total_distance_miles=10.0
        )
        assert result == 72.4

    def test_multi_segment_distance_weighted_integration(self) -> None:
        """Test multiple waypoints integrate temperature weighted by segment distance."""
        # Drive 30 miles total:
        # Waypoint 1 at 0 mi: 70°F
        # Waypoint 2 at 10 mi: 80°F (segment 1: 0-10 mi, avg temp = 75°F, weight = 10 mi)
        # Waypoint 3 at 30 mi: 90°F (segment 2: 10-30 mi, avg temp = 85°F, weight = 20 mi)
        # Total weighted sum: 75*10 + 85*20 = 750 + 1700 = 2450
        # Total distance: 30 mi -> 2450 / 30 = 81.67 -> 81.7°F
        samples = [
            {"distance_at_sample": 0.0, "temp_f": 70.0},
            {"distance_at_sample": 10.0, "temp_f": 80.0},
            {"distance_at_sample": 30.0, "temp_f": 90.0},
        ]
        result = calculate_distance_weighted_temperature(
            samples, total_distance_miles=30.0
        )
        assert result == 81.7

    def test_head_and_tail_segment_extension(self) -> None:
        """Test first sample taken at 2 mi and last at 8 mi for a 10 mi drive."""
        # Drive 10 miles total:
        # Waypoint 1 at 2.0 mi: 60°F -> Head segment (0 to 2 mi) weighted by 60°F (weight = 2) -> 120
        # Waypoint 2 at 8.0 mi: 70°F -> Middle segment (2 to 8 mi) avg temp 65°F (weight = 6) -> 390
        # Tail segment (8 to 10 mi) weighted by 70°F (weight = 2) -> 140
        # Total sum: 120 + 390 + 140 = 650
        # Total distance = 10 mi -> 650 / 10 = 65.0°F
        samples = [
            {"distance_at_sample": 2.0, "temp_f": 60.0},
            {"distance_at_sample": 8.0, "temp_f": 70.0},
        ]
        result = calculate_distance_weighted_temperature(
            samples, total_distance_miles=10.0
        )
        assert result == 65.0

    def test_zero_delta_distance_fallback_to_simple_average(self) -> None:
        """Test samples at same distance fall back to arithmetic average."""
        samples = [
            {"distance_at_sample": 0.0, "temp_f": 60.0},
            {"distance_at_sample": 0.0, "temp_f": 70.0},
        ]
        result = calculate_distance_weighted_temperature(
            samples, total_distance_miles=0.0
        )
        assert result == 65.0


class TestHistoricalTemperatureInterpolation:
    """Tests for get_interpolated_temperature."""

    def test_exact_timestamp_match(self) -> None:
        """Test exact hourly timestamp match."""
        hourly = {
            "2026-08-20T14:00": 74.0,
            "2026-08-20T15:00": 76.5,
        }
        temp = get_interpolated_temperature(hourly, "2026-08-20T14:00:00Z")
        assert temp == 74.0

    def test_linear_interpolation_between_hours(self) -> None:
        """Test linear interpolation for mid-hour timestamps."""
        hourly = {
            "2026-08-20T14:00": 74.0,
            "2026-08-20T15:00": 76.5,
        }
        # 14:30 is exactly halfway -> 74.0 + 0.5 * 2.5 = 75.25°F
        temp_half = get_interpolated_temperature(hourly, "2026-08-20T14:30:00Z")
        assert temp_half == 75.25

        # 14:15 is 25% -> 74.0 + 0.25 * 2.5 = 74.625 -> 74.62°F
        temp_quarter = get_interpolated_temperature(hourly, "2026-08-20T14:15:00Z")
        assert temp_quarter == 74.62

    def test_boundary_clamping(self) -> None:
        """Test timestamps before or after hourly range are clamped."""
        hourly = {
            "2026-08-20T10:00": 60.0,
            "2026-08-20T12:00": 70.0,
        }
        before = get_interpolated_temperature(hourly, "2026-08-20T08:00:00Z")
        assert before == 60.0

        after = get_interpolated_temperature(hourly, "2026-08-20T15:00:00Z")
        assert after == 70.0

    def test_invalid_or_empty_inputs(self) -> None:
        """Test invalid inputs return None safely."""
        assert get_interpolated_temperature({}, "2026-08-20T14:00:00Z") is None
        assert (
            get_interpolated_temperature(
                {"2026-08-20T14:00": 70.0}, "invalid-timestamp"
            )
            is None
        )


class TestOpenMeteoWeatherClient:
    """Tests for OpenMeteoWeatherClient async methods and caching."""

    @pytest.mark.asyncio
    async def test_live_forecast_success_and_caching(self, mock_hass: Any) -> None:
        """Test fetching live temperature and verifying grid caching."""
        mock_session = MagicMock()
        payload = {
            "latitude": 37.77,
            "longitude": -122.42,
            "current": {
                "time": "2026-09-02T05:00",
                "temperature_2m": 64.8,
            },
        }

        mock_session.get.return_value = MockClientResponse(
            status=200, json_data=payload
        )

        client = OpenMeteoWeatherClient(hass=mock_hass, session=mock_session)

        # First call hits the mock session
        temp1 = await client.async_get_current_temperature(37.774929, -122.419418)
        assert temp1 == 64.8
        assert mock_session.get.call_count == 1

        # Second call with nearby coordinates (same 2-decimal grid) uses cache
        temp2 = await client.async_get_current_temperature(37.771111, -122.421111)
        assert temp2 == 64.8
        assert mock_session.get.call_count == 1  # No additional network request

        # Clear cache and verify network request occurs
        client.clear_cache()
        temp3 = await client.async_get_current_temperature(37.774929, -122.419418)
        assert temp3 == 64.8
        assert mock_session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_live_forecast_error_resilience(self, mock_hass: Any) -> None:
        """Test live forecast returns None without raising on HTTP error or timeout."""
        mock_session = MagicMock()
        mock_session.get.return_value = MockClientResponse(status=500, json_data={})

        client = OpenMeteoWeatherClient(hass=mock_hass, session=mock_session)
        temp = await client.async_get_current_temperature(37.77, -122.42)
        assert temp is None

        # Test network timeout
        mock_session.get.side_effect = asyncio.TimeoutError("Request timed out")
        temp_timeout = await client.async_get_current_temperature(40.0, -105.0)
        assert temp_timeout is None

        # Test client connection error
        mock_session.get.side_effect = aiohttp.ClientConnectionError("Offline")
        temp_err = await client.async_get_current_temperature(40.0, -105.0)
        assert temp_err is None

    @pytest.mark.asyncio
    async def test_archive_api_success_and_caching(self, mock_hass: Any) -> None:
        """Test historical archive query and caching."""
        mock_session = MagicMock()
        payload = {
            "latitude": 39.74,
            "longitude": -104.99,
            "hourly": {
                "time": [
                    "2026-08-20T12:00",
                    "2026-08-20T13:00",
                    "2026-08-20T14:00",
                ],
                "temperature_2m": [68.2, 71.5, 75.0],
            },
        }

        mock_session.get.return_value = MockClientResponse(
            status=200, json_data=payload
        )

        client = OpenMeteoWeatherClient(hass=mock_hass, session=mock_session)

        # Query archive
        hourly = await client.async_get_historical_temperatures(
            latitude=39.7392,
            longitude=-104.9903,
            start_date="2026-08-20",
            end_date="2026-08-20",
        )
        assert hourly is not None
        assert len(hourly) == 3
        assert hourly["2026-08-20T14:00"] == 75.0
        assert mock_session.get.call_count == 1

        # Second query for same grid and dates uses cache
        cached_hourly = await client.async_get_historical_temperatures(
            latitude=39.7401,
            longitude=-104.9911,
            start_date="2026-08-20",
            end_date="2026-08-20",
        )
        assert cached_hourly == hourly
        assert mock_session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_archive_interpolated_temperature_for_timestamp(
        self, mock_hass: Any
    ) -> None:
        """Test end-to-end historical temperature lookup for a timestamp."""
        mock_session = MagicMock()
        payload = {
            "latitude": 39.74,
            "longitude": -104.99,
            "hourly": {
                "time": [
                    "2026-08-20T14:00",
                    "2026-08-20T15:00",
                ],
                "temperature_2m": [70.0, 80.0],
            },
        }
        mock_session.get.return_value = MockClientResponse(
            status=200, json_data=payload
        )

        client = OpenMeteoWeatherClient(hass=mock_hass, session=mock_session)
        target_dt = datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc)
        temp = await client.async_get_historical_temperature_for_timestamp(
            latitude=39.7392,
            longitude=-104.9903,
            target_time=target_dt,
        )
        assert temp == 75.0

    @pytest.mark.asyncio
    async def test_module_level_helpers(self, mock_hass: Any) -> None:
        """Test module-level convenience functions."""
        mock_session = MagicMock()
        payload = {
            "latitude": 37.77,
            "longitude": -122.42,
            "current": {"temperature_2m": 62.0},
            "hourly": {
                "time": ["2026-08-20T14:00", "2026-08-20T15:00"],
                "temperature_2m": [60.0, 70.0],
            },
        }
        mock_session.get.return_value = MockClientResponse(
            status=200, json_data=payload
        )

        cur_temp = await async_get_current_temperature(
            mock_hass, 37.77, -122.42, session=mock_session
        )
        assert cur_temp == 62.0

        hist_temps = await async_get_historical_temperatures(
            mock_hass,
            37.77,
            -122.42,
            "2026-08-20",
            "2026-08-20",
            session=mock_session,
        )
        assert hist_temps is not None
        assert hist_temps["2026-08-20T14:00"] == 60.0

        target_temp = await async_get_historical_temperature_for_timestamp(
            mock_hass,
            37.77,
            -122.42,
            "2026-08-20T14:30:00Z",
            session=mock_session,
        )
        assert target_temp == 65.0


class TestWindowAverageTemperature:
    """Tests for calculate_window_average_temperature algorithm."""

    def test_window_average_empty_temps(self) -> None:
        """Test with empty hourly temperatures dictionary."""
        assert calculate_window_average_temperature({}, "2026-08-20T10:00:00Z", "2026-08-20T12:00:00Z") is None

    def test_window_average_invalid_dates(self) -> None:
        """Test with invalid date strings."""
        temps = {"2026-08-20T10:00": 70.0, "2026-08-20T11:00": 72.0}
        assert calculate_window_average_temperature(temps, "invalid", "2026-08-20T12:00:00Z") is None

    def test_window_average_calculation(self) -> None:
        """Test accurate calculation of window average temperature across hours."""
        hourly = {
            "2026-08-20T10:00:00+00:00": 60.0,
            "2026-08-20T11:00:00+00:00": 70.0,
            "2026-08-20T12:00:00+00:00": 80.0,
        }
        # Interval from 10:00 to 12:00 should average endpoints (60, 80) and intermediate (70) -> 70.0
        avg = calculate_window_average_temperature(
            hourly, "2026-08-20T10:00:00+00:00", "2026-08-20T12:00:00+00:00"
        )
        assert avg == 70.0

