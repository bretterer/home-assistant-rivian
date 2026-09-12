"""Unit tests for Rivian isolated drive storage and data models."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.rivian.drive_models import (
    MICRO_DRIVE_THRESHOLD_MILES,
    MPGE_FACTOR,
    AggregatedDriveStats,
    ChargingSample,
    ChargingSessionRecord,
    DriveRecord,
    DriveState,
    DriveStatus,
    SpeedBinData,
    VampireDrainRecord,
)
from custom_components.rivian.drive_storage import DriveStore

TEST_VIN = "7PDSGABA8NN000000"


def _create_sample_drive(
    drive_id: str = "7PDSGABA8NN000000_1724164200",
    distance_miles: float = 12.45,
    energy_kwh: float = 4.15,
    start_time: str = "2026-08-20T14:30:00Z",
    end_time: str = "2026-08-20T14:55:00Z",
    is_micro_drive: bool = False,
    start_altitude_ft: float = 5280.0,
    end_altitude_ft: float = 5120.0,
    integrated_temperature_f: float | None = 74.5,
) -> DriveRecord:
    """Helper to generate a test DriveRecord."""
    return DriveRecord(
        vin=TEST_VIN,
        drive_id=drive_id,
        start_time=start_time,
        end_time=end_time,
        distance_miles=distance_miles,
        duration_seconds=1500.0,
        start_soc=82.5,
        end_soc=79.4,
        battery_capacity_kwh=135.0,
        energy_kwh=energy_kwh,
        start_altitude_ft=start_altitude_ft,
        end_altitude_ft=end_altitude_ft,
        avg_speed_mph=29.88,
        max_speed_mph=62.1,
        integrated_temperature_f=integrated_temperature_f,
        speed_bins={
            "0-9": SpeedBinData(miles=0.45, seconds=120.0),
            "10-19": SpeedBinData(miles=1.10, seconds=240.0),
            "20-29": SpeedBinData(miles=2.50, seconds=360.0),
            "30-39": SpeedBinData(miles=3.20, seconds=300.0),
            "40-49": SpeedBinData(miles=4.00, seconds=320.0),
            "50-59": SpeedBinData(miles=1.20, seconds=100.0),
        },
        is_micro_drive=is_micro_drive,
    )


class TestDriveModels:
    """Tests for data models and serialization."""

    def test_speed_bin_data_serialization(self) -> None:
        """Test SpeedBinData to_dict and from_dict."""
        bin_data = SpeedBinData(miles=3.4567, seconds=125.4)
        serialized = bin_data.to_dict()
        assert serialized == {"miles": 3.457, "seconds": 125.4}

        restored = SpeedBinData.from_dict(serialized)
        assert restored.miles == 3.457
        assert restored.seconds == 125.4

        # Test instantiation from numeric miles
        from_num = SpeedBinData.from_dict(5.2)
        assert from_num.miles == 5.2
        assert from_num.seconds == 0.0

    def test_aggregated_drive_stats_serialization_and_derivation(self) -> None:
        """Test AggregatedDriveStats calculations and serialization."""
        stats = AggregatedDriveStats(
            total_miles=100.0,
            total_kwh=40.0,
            drive_count=4,
            total_duration_seconds=7200.0,
            total_micro_drives=1,
        )
        serialized = stats.to_dict()
        assert serialized["total_miles"] == 100.0
        assert serialized["total_kwh"] == 40.0
        assert serialized["drive_count"] == 4
        assert serialized["total_micro_drives"] == 1

        restored = AggregatedDriveStats.from_dict(serialized)
        assert restored.total_miles == 100.0
        assert restored.total_kwh == 40.0
        assert restored.efficiency_mi_kwh == 2.5
        assert restored.mpge == pytest.approx(2.5 * MPGE_FACTOR, rel=1e-2)
        assert restored.avg_distance_miles == 25.0

    def test_drive_state_serialization(self) -> None:
        """Test DriveState model."""
        state = DriveState(
            is_driving=True,
            status=DriveStatus.DRIVING.value,
            current_trip_distance_mi=4.5,
            current_trip_duration=320.0,
            current_trip_kwh=1.5,
            current_trip_efficiency=3.0,
            current_speed_mph=45.0,
            current_altitude_ft=5300.0,
            gps_locked=True,
        )
        d = state.to_dict()
        assert d["is_driving"] is True
        assert d["status"] == "Driving"
        assert d["current_trip_distance_mi"] == 4.5
        assert d["gps_locked"] is True

        restored = DriveState.from_dict(d)
        assert restored.is_driving is True
        assert restored.status == "Driving"
        assert restored.current_trip_distance_mi == 4.5
        assert restored.gps_locked is True

    def test_drive_record_auto_computations(self) -> None:
        """Test DriveRecord automatic field computation in __post_init__."""
        drive = DriveRecord(
            vin=TEST_VIN,
            drive_id="test_001",
            start_time="2026-08-20T10:00:00Z",
            end_time="2026-08-20T10:30:00Z",
            distance_miles=10.0,
            duration_seconds=1800.0,
            start_soc=80.0,
            end_soc=77.0,
            battery_capacity_kwh=135.0,
            energy_kwh=4.0,
            start_altitude_ft=5000.0,
            end_altitude_ft=5200.0,
        )

        assert drive.elevation_change_ft == 200.0
        assert drive.efficiency_mi_kwh == 2.5
        assert drive.mpge == pytest.approx(2.5 * MPGE_FACTOR, rel=1e-3)
        assert drive.is_micro_drive is False

        # Test micro drive auto-tagging
        micro_drive = DriveRecord(
            vin=TEST_VIN,
            drive_id="test_micro",
            start_time="2026-08-20T10:00:00Z",
            end_time="2026-08-20T10:02:00Z",
            distance_miles=0.3,
            duration_seconds=120.0,
            start_soc=80.0,
            end_soc=80.0,
            battery_capacity_kwh=135.0,
            energy_kwh=0.1,
        )
        assert micro_drive.is_micro_drive is True

    def test_drive_record_full_roundtrip_serialization(self) -> None:
        """Test DriveRecord serialization to dict and from_dict."""
        drive = _create_sample_drive()
        drive_dict = drive.to_dict()

        assert drive_dict["vin"] == TEST_VIN
        assert drive_dict["drive_id"] == "7PDSGABA8NN000000_1724164200"
        assert drive_dict["distance_miles"] == 12.45
        assert drive_dict["energy_kwh"] == 4.15
        assert drive_dict["efficiency_mi_kwh"] == 3.0
        assert drive_dict["mpge"] == pytest.approx(3.0 * MPGE_FACTOR, rel=1e-2)
        assert drive_dict["elevation_change_ft"] == -160.0
        assert drive_dict["speed_bins"]["0-9"] == {"miles": 0.45, "seconds": 120.0}

        restored = DriveRecord.from_dict(drive_dict)
        assert restored.vin == drive.vin
        assert restored.drive_id == drive.drive_id
        assert restored.distance_miles == drive.distance_miles
        assert restored.energy_kwh == drive.energy_kwh
        assert restored.efficiency_mi_kwh == pytest.approx(
            drive.efficiency_mi_kwh, rel=1e-3
        )
        assert restored.mpge == pytest.approx(drive.mpge, rel=1e-3)
        assert restored.elevation_change_ft == drive.elevation_change_ft
        assert restored.integrated_temperature_f == drive.integrated_temperature_f
        assert restored.is_micro_drive is False


class TestDriveStore:
    """Tests for DriveStore CRUD, deduplication, rolling stats, and clean reset."""

    @pytest.mark.asyncio
    async def test_async_load_empty_store(self, mock_hass: Any) -> None:
        """Test loading from non-existent storage returns empty list cleanly."""
        store = DriveStore(mock_hass, TEST_VIN)
        assert store.is_loaded is False

        drives = await store.async_load()
        assert drives == []
        assert store.is_loaded is True
        assert store.drives == []

    @pytest.mark.asyncio
    async def test_async_save_and_load_drive(self, mock_hass: Any) -> None:
        """Test persisting a drive and loading it back."""
        store = DriveStore(mock_hass, TEST_VIN)
        drive1 = _create_sample_drive(
            drive_id="drive_1", distance_miles=15.0, energy_kwh=5.0
        )

        saved = await store.async_save_drive(drive1)
        assert saved is True
        assert len(store.drives) == 1
        assert store.drives[0].drive_id == "drive_1"

        # Create fresh store instance for same VIN to verify load from storage
        store2 = DriveStore(mock_hass, TEST_VIN)
        # Point store2's underlying mock Store to the same data
        store2._store._data = store._store._data  # type: ignore[attr-defined]

        loaded_drives = await store2.async_load()
        assert len(loaded_drives) == 1
        assert loaded_drives[0].drive_id == "drive_1"
        assert loaded_drives[0].distance_miles == 15.0
        assert loaded_drives[0].energy_kwh == 5.0
        assert loaded_drives[0].efficiency_mi_kwh == 3.0

    @pytest.mark.asyncio
    async def test_deduplication_on_save_drive(self, mock_hass: Any) -> None:
        """Test that saving a drive with an existing drive_id updates without duplicating."""
        store = DriveStore(mock_hass, TEST_VIN)
        drive1 = _create_sample_drive(
            drive_id="drive_dup", distance_miles=10.0, energy_kwh=4.0
        )
        await store.async_save_drive(drive1)
        assert len(store.drives) == 1
        assert store.drives[0].distance_miles == 10.0

        # Save updated drive with same drive_id
        drive1_updated = _create_sample_drive(
            drive_id="drive_dup", distance_miles=12.0, energy_kwh=4.0
        )
        await store.async_save_drive(drive1_updated)

        assert len(store.drives) == 1
        assert store.drives[0].distance_miles == 12.0
        assert store.drives[0].efficiency_mi_kwh == 3.0

    @pytest.mark.asyncio
    async def test_batch_save_and_deduplication(self, mock_hass: Any) -> None:
        """Test batch saving drives and verifying 100% deduplication on re-save."""
        store = DriveStore(mock_hass, TEST_VIN)

        drives_batch = [
            _create_sample_drive(
                drive_id=f"batch_{i}", distance_miles=10.0 + i, energy_kwh=3.5
            )
            for i in range(5)
        ]

        # Initial batch save
        new_count = await store.async_save_drives_batch(drives_batch)
        assert new_count == 5
        assert len(store.drives) == 5

        # Re-save the exact same batch: 0 new drives should be added
        new_count_2 = await store.async_save_drives_batch(drives_batch)
        assert new_count_2 == 0
        assert len(store.drives) == 5

        # Partially overlapping batch
        mixed_batch = [
            _create_sample_drive(
                drive_id="batch_0", distance_miles=10.0, energy_kwh=3.5
            ),
            _create_sample_drive(
                drive_id="batch_new_1", distance_miles=20.0, energy_kwh=6.0
            ),
        ]
        new_count_3 = await store.async_save_drives_batch(mixed_batch)
        assert new_count_3 == 1
        assert len(store.drives) == 6

    @pytest.mark.asyncio
    async def test_async_get_drives_with_min_distance(self, mock_hass: Any) -> None:
        """Test filtering drives by minimum distance."""
        store = DriveStore(mock_hass, TEST_VIN)
        drives = [
            _create_sample_drive(
                drive_id="d1", distance_miles=0.2, is_micro_drive=True
            ),
            _create_sample_drive(
                drive_id="d2", distance_miles=0.45, is_micro_drive=True
            ),
            _create_sample_drive(drive_id="d3", distance_miles=5.0),
            _create_sample_drive(drive_id="d4", distance_miles=15.0),
        ]
        await store.async_save_drives_batch(drives)

        all_drives = await store.async_get_drives()
        assert len(all_drives) == 4

        valid_drives = await store.async_get_drives(
            min_distance=MICRO_DRIVE_THRESHOLD_MILES
        )
        assert len(valid_drives) == 2
        assert {d.drive_id for d in valid_drives} == {"d3", "d4"}

    @pytest.mark.asyncio
    async def test_statistics_and_micro_drive_exclusion(self, mock_hass: Any) -> None:
        """Test rolling 30-day and all-time weighted efficiency excluding micro drives."""
        store = DriveStore(mock_hass, TEST_VIN)

        now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Drive 1: 10 days ago (within 30d), 20 miles, 5 kWh -> 4.0 mi/kWh
        t1 = (now - timedelta(days=10)).isoformat()
        d1 = _create_sample_drive(
            drive_id="d1",
            distance_miles=20.0,
            energy_kwh=5.0,
            start_time=t1,
            end_time=t1,
        )

        # Drive 2: 5 days ago (within 30d), 30 miles, 15 kWh -> 2.0 mi/kWh
        t2 = (now - timedelta(days=5)).isoformat()
        d2 = _create_sample_drive(
            drive_id="d2",
            distance_miles=30.0,
            energy_kwh=15.0,
            start_time=t2,
            end_time=t2,
        )

        # Drive 3: 40 days ago (outside 30d, but in all-time), 50 miles, 20 kWh -> 2.5 mi/kWh
        t3 = (now - timedelta(days=40)).isoformat()
        d3 = _create_sample_drive(
            drive_id="d3",
            distance_miles=50.0,
            energy_kwh=20.0,
            start_time=t3,
            end_time=t3,
        )

        # Micro Drive 4: 2 days ago, 0.3 miles, 0.2 kWh (must be excluded from stats)
        t4 = (now - timedelta(days=2)).isoformat()
        d4 = _create_sample_drive(
            drive_id="d4_micro",
            distance_miles=0.3,
            energy_kwh=0.2,
            start_time=t4,
            end_time=t4,
            is_micro_drive=True,
        )

        await store.async_save_drives_batch([d1, d2, d3, d4])

        # 30-Day Stats: d1 + d2 (50 miles, 20 kWh)
        # Weighted efficiency = 50 / 20 = 2.50 mi/kWh
        # MPGe = 2.5 * 33.705 = 84.26
        stats_30d = store.get_stats_30d(reference_time=now)
        assert stats_30d.drive_count == 2
        assert stats_30d.total_miles == 50.0
        assert stats_30d.total_kwh == 20.0
        assert stats_30d.efficiency_mi_kwh == 2.50
        assert stats_30d.mpge == 84.26
        assert stats_30d.total_micro_drives == 1

        # All-Time Stats: d1 + d2 + d3 (100 miles, 40 kWh)
        # Weighted efficiency = 100 / 40 = 2.50 mi/kWh
        # MPGe = 2.5 * 33.705 = 84.26
        stats_all = store.get_stats_all_time()
        assert stats_all.drive_count == 3
        assert stats_all.total_miles == 100.0
        assert stats_all.total_kwh == 40.0
        assert stats_all.efficiency_mi_kwh == 2.50
        assert stats_all.mpge == 84.26
        assert stats_all.total_micro_drives == 1

    @pytest.mark.asyncio
    async def test_empirical_baseline_aggregation(self, mock_hass: Any) -> None:
        """Test R1S Reggie empirical baseline aggregation: 370.93 mi, 131.05 kWh -> 2.83 mi/kWh (95.4 MPGe)."""
        store = DriveStore(mock_hass, TEST_VIN)

        # Synthesize 58 valid drives totaling 370.93 miles and 131.05 kWh
        # and 4 micro drives totaling 1.2 miles
        drives: list[DriveRecord] = []
        miles_per_drive = 370.93 / 58
        kwh_per_drive = 131.05 / 58

        for i in range(58):
            drives.append(
                _create_sample_drive(
                    drive_id=f"reggie_drive_{i}",
                    distance_miles=miles_per_drive,
                    energy_kwh=kwh_per_drive,
                    start_time="2026-08-20T12:00:00Z",
                    end_time="2026-08-20T12:30:00Z",
                )
            )

        for j in range(4):
            drives.append(
                _create_sample_drive(
                    drive_id=f"reggie_micro_{j}",
                    distance_miles=0.3,
                    energy_kwh=0.1,
                    is_micro_drive=True,
                )
            )

        await store.async_save_drives_batch(drives)

        stats = store.get_stats_all_time()
        assert stats.drive_count == 58
        assert stats.total_miles == 370.93
        assert stats.total_kwh == 131.05
        assert stats.efficiency_mi_kwh == 2.83
        assert stats.mpge == pytest.approx(
            95.4, rel=1e-2
        )  # 2.83 mi/kWh * 33.705 ~= 95.4 MPGe
        assert stats.total_micro_drives == 4

    @pytest.mark.asyncio
    async def test_async_reset_cleans_storage(self, mock_hass: Any) -> None:
        """Test reset removes storage and clears in-memory state cleanly."""
        store = DriveStore(mock_hass, TEST_VIN)
        drive = _create_sample_drive()
        await store.async_save_drive(drive)
        assert len(store.drives) == 1

        await store.async_reset()
        assert store.drives == []
        assert len(store._drives_by_id) == 0

        # Reloading after reset returns empty list
        drives = await store.async_load()
        assert drives == []

    @pytest.mark.asyncio
    async def test_store_metadata_and_key(self, mock_hass: Any) -> None:
        """Test store version and key formatting."""
        store = DriveStore(mock_hass, TEST_VIN)
        assert store.key == f"rivian_drives_{TEST_VIN}.json"
        assert store._store.version == 1
        assert store._store.minor_version == 1

    @pytest.mark.asyncio
    async def test_empty_batch_save(self, mock_hass: Any) -> None:
        """Test saving an empty batch of drives."""
        store = DriveStore(mock_hass, TEST_VIN)
        count = await store.async_save_drives_batch([])
        assert count == 0
        assert store.drives == []

    @pytest.mark.asyncio
    async def test_corrupt_timestamps_in_30d_stats(self, mock_hass: Any) -> None:
        """Test handling of corrupt or unparseable timestamps."""
        store = DriveStore(mock_hass, TEST_VIN)
        drive_invalid_time = _create_sample_drive(
            drive_id="invalid_ts",
            start_time="not-a-timestamp",
            end_time="also-invalid",
            distance_miles=10.0,
            energy_kwh=4.0,
        )
        await store.async_save_drive(drive_invalid_time)

        # 30d stats should safely ignore drives with unparseable timestamps
        stats = store.get_stats_30d()
        assert stats.drive_count == 0
        assert stats.total_miles == 0.0

        # All-time stats should still count the valid distance/energy
        stats_all = store.get_stats_all_time()
        assert stats_all.drive_count == 1
        assert stats_all.total_miles == 10.0
        assert stats_all.efficiency_mi_kwh == 2.5

    @pytest.mark.asyncio
    async def test_zero_division_safety(self, mock_hass: Any) -> None:
        """Test stats when total energy or distance is zero."""
        store = DriveStore(mock_hass, TEST_VIN)
        drive_zero_energy = _create_sample_drive(
            drive_id="zero_energy",
            distance_miles=5.0,
            energy_kwh=0.0,
            start_time="2026-08-20T12:00:00Z",
            end_time="2026-08-20T12:30:00Z",
        )
        await store.async_save_drive(drive_zero_energy)

        stats = store.get_stats_all_time()
        assert stats.drive_count == 1
        assert stats.total_miles == 5.0
        assert stats.total_kwh == 0.0
        assert stats.efficiency_mi_kwh == 0.0
        assert stats.mpge == 0.0

    def test_vampire_drain_record_serialization(self) -> None:
        """Test serialization and deserialization of VampireDrainRecord."""
        rec = VampireDrainRecord(
            start_time="2026-08-25T14:00:00Z",
            end_time="2026-08-25T21:00:00Z",
            idle_hours=7.0,
            start_soc=51.6,
            end_soc=51.5,
            drain_soc=0.1,
            drain_kwh=0.14,
            rate_pct_per_day=0.34,
            avg_watts=20.0,
            avg_temp_f=75.2,
            latitude=43.6150,
            longitude=-116.2023,
        )
        d = rec.to_dict()
        assert d["idle_hours"] == 7.0
        assert d["drain_kwh"] == 0.14
        assert d["avg_temp_f"] == 75.2
        assert d["latitude"] == 43.615

        rec2 = VampireDrainRecord.from_dict(d)
        assert rec2.idle_hours == 7.0
        assert rec2.drain_kwh == 0.14
        assert rec2.avg_temp_f == 75.2
        assert rec2.latitude == 43.615

    @pytest.mark.asyncio
    async def test_vampire_events_storage_persistence(self, mock_hass: Any) -> None:
        """Test saving, appending, loading, and resetting vampire events in DriveStore."""
        store = DriveStore(mock_hass, TEST_VIN)
        rec1 = VampireDrainRecord(
            start_time="2026-08-25T14:00:00Z",
            end_time="2026-08-25T21:00:00Z",
            idle_hours=7.0,
            start_soc=51.6,
            end_soc=51.5,
            drain_soc=0.1,
            drain_kwh=0.14,
            rate_pct_per_day=0.34,
            avg_watts=20.0,
            avg_temp_f=75.2,
        )
        await store.async_save_vampire_events([rec1])
        assert len(store.vampire_events) == 1

        rec2 = VampireDrainRecord(
            start_time="2026-08-26T14:00:00Z",
            end_time="2026-08-26T21:00:00Z",
            idle_hours=7.0,
            start_soc=67.0,
            end_soc=66.6,
            drain_soc=0.4,
            drain_kwh=0.58,
            rate_pct_per_day=1.43,
            avg_watts=86.0,
            avg_temp_f=78.0,
        )
        await store.async_append_vampire_event(rec2)
        assert len(store.vampire_events) == 2

        # Re-load in a new store instance to test disk persistence
        new_store = DriveStore(mock_hass, TEST_VIN)
        new_store._store._data = store._store._data  # type: ignore[attr-defined]
        await new_store.async_load()
        assert len(new_store.vampire_events) == 2
        assert new_store.vampire_events[0].drain_kwh == 0.14
        assert new_store.vampire_events[1].drain_kwh == 0.58

        # Test reset
        await new_store.async_reset()
        assert new_store.vampire_events == []

    @pytest.mark.asyncio
    async def test_multi_period_stats_and_retention_pruning(
        self, mock_hass: Any
    ) -> None:
        """Test 30d, 90d, 365d stats calculations, period filtering, and 365-day retention pruning."""
        store = DriveStore(mock_hass, TEST_VIN)
        ref_time = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Drive 1: 10 days ago (within 30d, 90d, 365d)
        d1 = _create_sample_drive(
            drive_id="d1",
            distance_miles=10.0,
            energy_kwh=4.0,  # 2.5 mi/kWh
            start_time="2026-08-22T12:00:00Z",
            end_time="2026-08-22T12:30:00Z",
        )
        # Drive 2: 60 days ago (outside 30d, within 90d, 365d)
        d2 = _create_sample_drive(
            drive_id="d2",
            distance_miles=20.0,
            energy_kwh=5.0,  # 4.0 mi/kWh
            start_time="2026-07-03T12:00:00Z",
            end_time="2026-07-03T12:30:00Z",
        )
        # Drive 3: 200 days ago (outside 90d, within 365d)
        d3 = _create_sample_drive(
            drive_id="d3",
            distance_miles=30.0,
            energy_kwh=10.0,  # 3.0 mi/kWh
            start_time="2026-02-13T12:00:00Z",
            end_time="2026-02-13T12:30:00Z",
        )
        # Drive 4: 400 days ago (outside 365d)
        d4 = _create_sample_drive(
            drive_id="d4",
            distance_miles=40.0,
            energy_kwh=10.0,
            start_time="2025-07-28T12:00:00Z",
            end_time="2025-07-28T12:30:00Z",
        )

        await store.async_save_drives_batch([d1, d2, d3, d4])

        # Test stats for periods
        s30 = store.get_stats_30d(reference_time=ref_time)
        assert s30.drive_count == 1
        assert s30.total_miles == 10.0
        assert s30.efficiency_mi_kwh == 2.5

        s90 = store.get_stats_90d(reference_time=ref_time)
        assert s90.drive_count == 2
        assert s90.total_miles == 30.0
        assert s90.efficiency_mi_kwh == round(30.0 / 9.0, 2)  # 3.33

        s365 = store.get_stats_365d(reference_time=ref_time)
        assert s365.drive_count == 3
        assert s365.total_miles == 60.0
        assert s365.efficiency_mi_kwh == round(60.0 / 19.0, 2)  # 3.16

        s_all = store.get_stats_all_time()
        assert s_all.drive_count == 4
        assert s_all.total_miles == 100.0

        # Test period filtering
        drives_90 = store.get_drives_for_period(days=90, reference_time=ref_time)
        assert len(drives_90) == 2
        assert {d.drive_id for d in drives_90} == {"d1", "d2"}

        drives_365 = store.get_drives_for_period(days=365, reference_time=ref_time)
        assert len(drives_365) == 3
        assert {d.drive_id for d in drives_365} == {"d1", "d2", "d3"}

        # Test vampire events period filtering
        v1 = VampireDrainRecord(
            start_time="2026-08-20T12:00:00Z",
            end_time="2026-08-21T12:00:00Z",
            idle_hours=24.0,
            start_soc=80.0,
            end_soc=78.0,
            drain_soc=2.0,
            drain_kwh=2.7,
            rate_pct_per_day=2.0,
            avg_watts=112.5,
        )
        v2 = VampireDrainRecord(
            start_time="2025-06-01T12:00:00Z",
            end_time="2025-06-02T12:00:00Z",
            idle_hours=24.0,
            start_soc=80.0,
            end_soc=78.0,
            drain_soc=2.0,
            drain_kwh=2.7,
            rate_pct_per_day=2.0,
            avg_watts=112.5,
        )
        await store.async_save_vampire_events([v1, v2])

        v_90 = store.get_vampire_events_for_period(days=90, reference_time=ref_time)
        assert len(v_90) == 1
        assert v_90[0].start_time == "2026-08-20T12:00:00Z"

        # Test pruning older than 365 days
        pruned_count = store.prune_older_than(days=365, reference_time=ref_time)
        assert pruned_count == 2  # 1 drive (d4) + 1 vampire (v2)
        assert len(store.drives) == 3
        assert len(store.vampire_events) == 1
        assert "d4" not in [d.drive_id for d in store.drives]


class TestDCFCStorageAndModels:
    """Validate DC fast charging models, serialization, and DriveStore persistence."""

    def test_charging_sample_serialization(self) -> None:
        """Test ChargingSample serialization and instantiation."""
        sample = ChargingSample(
            timestamp="2026-09-01T10:15:00Z",
            soc=45.2,
            power_kw=185.6,
            battery_temp_f=85.0,
        )
        data = sample.to_dict()
        assert data["soc"] == 45.2
        assert data["power_kw"] == 185.6
        assert data["battery_temp_f"] == 85.0

        restored = ChargingSample.from_dict(data)
        assert restored.soc == 45.2
        assert restored.power_kw == 185.6
        assert restored.battery_temp_f == 85.0

    def test_charging_session_record_serialization(self) -> None:
        """Test ChargingSessionRecord serialization."""
        samples = [
            ChargingSample(timestamp="2026-09-01T10:00:00Z", soc=15.0, power_kw=215.0),
            ChargingSample(timestamp="2026-09-01T10:15:00Z", soc=50.0, power_kw=145.0),
            ChargingSample(timestamp="2026-09-01T10:30:00Z", soc=80.0, power_kw=62.0),
        ]
        session = ChargingSessionRecord(
            session_id="session_1",
            start_time="2026-09-01T10:00:00Z",
            end_time="2026-09-01T10:30:00Z",
            start_soc=15.0,
            end_soc=80.0,
            energy_added_kwh=87.75,
            max_power_kw=215.0,
            avg_power_kw=140.7,
            samples=samples,
            is_dcfc=True,
        )
        data = session.to_dict()
        assert data["session_id"] == "session_1"
        assert data["max_power_kw"] == 215.0
        assert data["is_dcfc"] is True
        assert len(data["samples"]) == 3

        restored = ChargingSessionRecord.from_dict(data)
        assert restored.session_id == "session_1"
        assert len(restored.samples) == 3
        assert restored.samples[0].power_kw == 215.0

    @pytest.mark.asyncio
    async def test_dcfc_storage_persistence_and_cap(self, mock_hass: Any) -> None:
        """Test that DriveStore persists DCFC sessions and enforces 50-session FIFO cap."""
        store = DriveStore(mock_hass, TEST_VIN)
        await store.async_load()
        assert len(store.dcfc_sessions) == 0

        # Add 60 sessions to verify FIFO cap at 50
        for i in range(60):
            session = ChargingSessionRecord(
                session_id=f"session_{i}",
                start_time=f"2026-09-01T{i % 24:02d}:00:00Z",
                end_time=f"2026-09-01T{i % 24:02d}:30:00Z",
                start_soc=20.0,
                end_soc=70.0,
                energy_added_kwh=67.5,
                max_power_kw=150.0 + (i % 50),
                avg_power_kw=100.0,
                samples=[
                    ChargingSample(
                        timestamp=f"2026-09-01T{i % 24:02d}:00:00Z",
                        soc=20.0,
                        power_kw=150.0,
                    )
                ],
            )
            await store.async_append_dcfc_session(session)

        assert len(store.dcfc_sessions) == 50
        # Oldest sessions (0-9) should have been pruned; session_10 should be the first
        assert store.dcfc_sessions[0].session_id == "session_10"
        assert store.dcfc_sessions[-1].session_id == "session_59"

        # Verify get_dcfc_sessions limit
        recent_10 = store.get_dcfc_sessions(limit=10)
        assert len(recent_10) == 10
        assert recent_10[-1].session_id == "session_59"

        # Verify async_load restores the 50 sessions
        store_reload = DriveStore(mock_hass, TEST_VIN)
        store_reload._store._data = store._store._data
        await store_reload.async_load()
        assert len(store_reload.dcfc_sessions) == 50
        assert store_reload.dcfc_sessions[0].session_id == "session_10"



