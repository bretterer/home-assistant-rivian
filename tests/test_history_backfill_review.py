"""Backfill review fixes: overlapping drain events, recent temperatures, paging."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import history_backfill
from custom_components.rivian.drive_models import DriveRecord, VampireDrainRecord
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.history_backfill import (
    _async_existing_drive_intervals,
    async_backfill_from_recorder,
)

VIN = "7PDSGABA1NN000002"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _vampire(start: datetime, end: datetime, **kw: Any) -> VampireDrainRecord:
    return VampireDrainRecord(
        start_time=_iso(start),
        end_time=_iso(end),
        idle_hours=round((end - start).total_seconds() / 3600.0, 2),
        start_soc=80.0,
        end_soc=79.0,
        drain_soc=1.0,
        drain_kwh=1.0,
        rate_pct_per_day=2.0,
        avg_watts=50.0,
        **kw,
    )


def _drive(drive_id: str, start: datetime, minutes: float = 10.0, **kw: Any):
    end = start + timedelta(minutes=minutes)
    return DriveRecord(
        vin=VIN,
        drive_id=drive_id,
        start_time=_iso(start),
        end_time=_iso(end),
        distance_miles=5.0,
        duration_seconds=minutes * 60.0,
        start_soc=80.0,
        end_soc=78.0,
        battery_capacity_kwh=135.0,
        energy_kwh=3.0,
        **kw,
    )


def _run_with(meta: dict[str, Any], drives: list[DriveRecord] | None = None):
    return patch.object(
        history_backfill, "reconstruct_drives", return_value=(drives or [], meta)
    )


class TestOverlappingDrainEvents:
    @pytest.mark.asyncio
    async def test_rebuilt_event_overlapping_stored_one_is_skipped(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        store = DriveStore(hass=mock_hass, vin=VIN, db=analytics_db)
        await store.async_load()
        t0 = (datetime.now(UTC) - timedelta(days=10)).replace(
            minute=0, second=0, microsecond=0
        )
        # The live event starts when the drive finalizes ...
        await store.async_append_vampire_event(_vampire(t0, t0 + timedelta(hours=8)))
        # ... the rebuilt one at the recorder's gear change, a minute or two off.
        rebuilt = _vampire(
            t0 - timedelta(minutes=2), t0 + timedelta(hours=8, minutes=1)
        )
        elsewhere = _vampire(t0 + timedelta(days=2), t0 + timedelta(days=2, hours=6))
        weather = MagicMock()
        weather.async_get_historical_temperatures = AsyncMock(return_value={})
        with _run_with({"vampire_events": [rebuilt, elsewhere]}):
            await async_backfill_from_recorder(
                hass=mock_hass,
                vin=VIN,
                db_path="unused",
                dry_run=False,
                store=store,
                weather_client=weather,
                tracks=False,
            )
        events = store.recent_vampire_events
        assert [e.start_time for e in events] == [
            _iso(t0),
            _iso(t0 + timedelta(days=2)),
        ]


class TestRecentBackfilledTemperature:
    @pytest.mark.asyncio
    async def test_recent_drive_uses_forecast_recent_past(self, mock_hass: Any) -> None:
        start = datetime.now(UTC).replace(
            minute=0, second=0, microsecond=0
        ) - timedelta(days=1)
        drive = _drive("recent1", start, start_lat=44.0, start_lon=-116.0)
        weather = MagicMock()
        # The archive lags: it has nothing for yesterday.
        weather.async_get_historical_temperatures = AsyncMock(return_value={})
        weather.async_get_recent_hourly_temperatures = AsyncMock(
            return_value={
                _iso(start)[:-1]: 41.0,
                _iso(start + timedelta(hours=1))[:-1]: 43.0,
            }
        )
        with _run_with({}, [drive]):
            result = await async_backfill_from_recorder(
                hass=mock_hass,
                vin=VIN,
                db_path="unused",
                dry_run=True,
                weather_client=weather,
                tracks=False,
            )
        assert result["drives"][0].integrated_temperature_f == 41.0
        weather.async_get_recent_hourly_temperatures.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_old_drive_does_not_use_recent_path(self, mock_hass: Any) -> None:
        start = datetime.now(UTC) - timedelta(days=200)
        drive = _drive("old1", start, start_lat=44.0, start_lon=-116.0)
        weather = MagicMock()
        weather.async_get_historical_temperatures = AsyncMock(return_value={})
        weather.async_get_recent_hourly_temperatures = AsyncMock(return_value={})
        with _run_with({}, [drive]):
            await async_backfill_from_recorder(
                hass=mock_hass,
                vin=VIN,
                db_path="unused",
                dry_run=True,
                weather_client=weather,
                tracks=False,
            )
        weather.async_get_recent_hourly_temperatures.assert_not_called()


class TestExactDrivePaging:
    @pytest.mark.asyncio
    async def test_more_drives_than_a_page_with_equal_start_all_returned(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        store = DriveStore(hass=mock_hass, vin=VIN, db=analytics_db)
        await store.async_load()
        t0 = datetime(2026, 2, 1, 8, 0, tzinfo=UTC)
        count = 5037  # far past any page or old cap
        # Every drive shares one start time, so no page boundary can split them.
        await store.async_save_drives_batch(
            [_drive(f"same{i:04d}", t0) for i in range(count)]
        )
        intervals = await _async_existing_drive_intervals(store)
        assert {i[2] for i in intervals} == {f"same{i:04d}" for i in range(count)}
