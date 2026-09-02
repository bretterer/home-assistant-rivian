"""Empirical challenger stress-test suite for Rivian Trip Efficiency & Analytics."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import sqlite3
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rivian.drive_models import (
    MICRO_DRIVE_THRESHOLD_MILES,
    MPGE_FACTOR,
    AggregatedDriveStats,
    DriveRecord,
)
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.history_backfill import (
    _get_speed_bin_key,
    async_backfill_from_recorder,
    open_sqlite_readonly,
    reconstruct_drives_from_sqlite,
)

FIXTURE_DB_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "reggie_10day_history.db"
)
TEST_VIN = "7PDSGABA1NN000001"


class TestEmpiricalChallengerReproduction:
    """Empirical Challenger Test 1: Exact baseline reproduction & deduplication."""

    def test_reggie_empirical_baseline(self) -> None:
        """Verify exact reproduction of empirical baseline: 58 valid drives, 370.93 mi, 131.05 kWh, 2.83 mi/kWh, 95.4 MPGe."""
        drives, _meta = reconstruct_drives_from_sqlite(
            FIXTURE_DB_PATH, vin=TEST_VIN, vehicle_id="reggie"
        )
        valid_drives = [
            d
            for d in drives
            if not d.is_micro_drive and d.distance_miles >= MICRO_DRIVE_THRESHOLD_MILES
        ]
        micro_drives = [
            d
            for d in drives
            if d.is_micro_drive or d.distance_miles < MICRO_DRIVE_THRESHOLD_MILES
        ]

        assert len(drives) == 62
        assert len(valid_drives) == 58
        assert len(micro_drives) == 4

        total_miles = round(sum(d.distance_miles for d in valid_drives), 2)
        total_kwh = round(sum(d.energy_kwh for d in valid_drives), 2)
        efficiency = round(total_miles / total_kwh, 2)
        mpge = round(efficiency * MPGE_FACTOR, 1)

        assert total_miles == 370.93
        assert total_kwh == 131.05
        assert efficiency == 2.83
        assert mpge == 95.4

    @pytest.mark.asyncio
    async def test_repeated_backfill_idempotent_deduplication(
        self, mock_hass: Any
    ) -> None:
        """Verify backfilling twice results in 62 saved on run 1, and 0 added (62 skipped) on run 2."""
        store = DriveStore(hass=mock_hass, vin=TEST_VIN)
        await store.async_load()
        assert len(store.drives) == 0

        mock_weather_client = MagicMock()
        mock_weather_client.async_get_historical_temperatures = AsyncMock(
            return_value={"2026-08-11T12:00": 70.0}
        )

        # Run 1: Persist backfill
        res1 = await async_backfill_from_recorder(
            hass=mock_hass,
            vin=TEST_VIN,
            db_path=FIXTURE_DB_PATH,
            dry_run=False,
            store=store,
            weather_client=mock_weather_client,
        )
        assert res1["drives_found"] == 62
        assert res1["valid_drives"] == 58
        assert res1["micro_drives"] == 4
        assert res1["duplicates_skipped"] == 0
        assert len(store.drives) == 62

        # Run 2: Re-run backfill against same store
        res2 = await async_backfill_from_recorder(
            hass=mock_hass,
            vin=TEST_VIN,
            db_path=FIXTURE_DB_PATH,
            dry_run=False,
            store=store,
            weather_client=mock_weather_client,
        )
        assert res2["drives_found"] == 62
        assert res2["duplicates_skipped"] == 62
        assert len(store.drives) == 62

        # Run 3: Third run to ensure continued idempotency
        res3 = await async_backfill_from_recorder(
            hass=mock_hass,
            vin=TEST_VIN,
            db_path=FIXTURE_DB_PATH,
            dry_run=False,
            store=store,
            weather_client=mock_weather_client,
        )
        assert res3["drives_found"] == 62
        assert res3["duplicates_skipped"] == 62
        assert len(store.drives) == 62


class TestEmpiricalChallengerDatabaseSafety:
    """Empirical Challenger Test 2: SQLite read-only mode stress testing."""

    def test_sqlite_readonly_blocks_all_mutations(self) -> None:
        """Attempt various write, schema change, and administrative operations in mode=ro."""
        conn = open_sqlite_readonly(FIXTURE_DB_PATH)
        cur = conn.cursor()

        mutations = [
            ("CREATE TABLE", "CREATE TABLE adversarial_test (id INTEGER)"),
            (
                "INSERT",
                "INSERT INTO states_meta (metadata_id, entity_id) VALUES (9999, 'sensor.hack')",
            ),
            (
                "UPDATE",
                "UPDATE states_meta SET entity_id = 'sensor.hacked' WHERE metadata_id = 1",
            ),
            ("DELETE", "DELETE FROM states WHERE state_id = 1"),
            ("DROP TABLE", "DROP TABLE states"),
            ("ALTER TABLE", "ALTER TABLE states ADD COLUMN hacked INTEGER"),
        ]

        for op_name, sql in mutations:
            with pytest.raises(sqlite3.OperationalError) as exc_info:
                cur.execute(sql)
            assert "readonly" in str(exc_info.value).lower(), (
                f"{op_name} did not fail with readonly error: {exc_info.value}"
            )

        conn.close()


class TestEmpiricalChallengerEdgeCases:
    """Empirical Challenger Test 3: Edge cases, boundaries, clean reset, and math rigor."""

    def test_zero_distance_drive(self) -> None:
        """Test drive with 0 distance handled gracefully without error."""
        drive = DriveRecord(
            vin=TEST_VIN,
            drive_id="zero_dist_01",
            start_time="2026-08-20T10:00:00Z",
            end_time="2026-08-20T10:05:00Z",
            distance_miles=0.0,
            duration_seconds=300.0,
            start_soc=80.0,
            end_soc=79.5,
            battery_capacity_kwh=135.0,
            energy_kwh=0.675,
        )
        assert drive.efficiency_mi_kwh == 0.0
        assert drive.mpge == 0.0
        assert drive.is_micro_drive is True

    def test_zero_energy_regen_drive(self) -> None:
        """Test drive with 0 kWh energy consumed (net regen or same SoC) has zero division safety."""
        drive = DriveRecord(
            vin=TEST_VIN,
            drive_id="zero_energy_01",
            start_time="2026-08-20T11:00:00Z",
            end_time="2026-08-20T11:20:00Z",
            distance_miles=5.0,
            duration_seconds=1200.0,
            start_soc=80.0,
            end_soc=80.0,
            battery_capacity_kwh=135.0,
            energy_kwh=0.0,
        )
        assert drive.efficiency_mi_kwh == 0.0
        assert drive.mpge == 0.0
        assert drive.is_micro_drive is False

    def test_negative_elevation_downhill_drive(self) -> None:
        """Test downhill drive with negative elevation change."""
        drive = DriveRecord(
            vin=TEST_VIN,
            drive_id="downhill_01",
            start_time="2026-08-20T11:00:00Z",
            end_time="2026-08-20T11:30:00Z",
            distance_miles=15.0,
            duration_seconds=1800.0,
            start_soc=80.0,
            end_soc=78.0,
            battery_capacity_kwh=135.0,
            energy_kwh=2.7,
            start_altitude_ft=6000.0,
            end_altitude_ft=4500.0,
        )
        assert drive.elevation_change_ft == -1500.0
        assert drive.efficiency_mi_kwh == pytest.approx(15.0 / 2.7, rel=1e-2)

    def test_negative_temperature_subzero_drive(self) -> None:
        """Test drive in sub-zero ambient temperature."""
        drive = DriveRecord(
            vin=TEST_VIN,
            drive_id="winter_01",
            start_time="2026-01-15T08:00:00Z",
            end_time="2026-01-15T08:45:00Z",
            distance_miles=20.0,
            duration_seconds=2700.0,
            start_soc=90.0,
            end_soc=78.0,
            battery_capacity_kwh=135.0,
            energy_kwh=16.2,
            integrated_temperature_f=-4.5,
        )
        assert drive.integrated_temperature_f == -4.5
        d_dict = drive.to_dict()
        assert d_dict["integrated_temperature_f"] == -4.5
        restored = DriveRecord.from_dict(d_dict)
        assert restored.integrated_temperature_f == -4.5

    def test_empty_aggregation_stats_division_safety(self) -> None:
        """Test AggregatedDriveStats with 0 miles and 0 kWh."""
        stats = AggregatedDriveStats(total_miles=0.0, total_kwh=0.0)
        assert stats.efficiency_mi_kwh == 0.0
        assert stats.mpge == 0.0

    @pytest.mark.asyncio
    async def test_micro_drive_strict_threshold_boundary(self, mock_hass: Any) -> None:
        """Test exact boundary: 0.49 mi is excluded as micro drive, 0.50 mi is included in rolling stats."""
        now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        t_iso = now.isoformat()

        store = DriveStore(mock_hass, "BOUNDARY_TEST_VIN")
        await store.async_load()

        drive_49 = DriveRecord(
            vin="BOUNDARY_TEST_VIN",
            drive_id="drive_0_49",
            start_time=t_iso,
            end_time=t_iso,
            distance_miles=0.49,
            duration_seconds=120.0,
            start_soc=80.0,
            end_soc=79.9,
            battery_capacity_kwh=135.0,
            energy_kwh=0.135,
        )

        drive_50 = DriveRecord(
            vin="BOUNDARY_TEST_VIN",
            drive_id="drive_0_50",
            start_time=t_iso,
            end_time=t_iso,
            distance_miles=0.50,
            duration_seconds=120.0,
            start_soc=80.0,
            end_soc=79.8,
            battery_capacity_kwh=135.0,
            energy_kwh=0.270,
        )

        assert drive_49.is_micro_drive is True
        assert drive_50.is_micro_drive is False

        # Save 0.49 mi drive
        await store.async_save_drive(drive_49)
        stats_30d = store.get_stats_30d(reference_time=now)
        stats_all = store.get_stats_all_time()
        assert stats_30d.drive_count == 0
        assert stats_30d.total_miles == 0.0
        assert stats_all.drive_count == 0
        assert stats_all.total_miles == 0.0
        assert stats_all.total_micro_drives == 1

        # Save 0.50 mi drive
        await store.async_save_drive(drive_50)
        stats_30d_2 = store.get_stats_30d(reference_time=now)
        stats_all_2 = store.get_stats_all_time()
        assert stats_30d_2.drive_count == 1
        assert stats_30d_2.total_miles == 0.50
        assert stats_30d_2.total_kwh == 0.27
        assert stats_30d_2.efficiency_mi_kwh == pytest.approx(0.50 / 0.27, rel=1e-2)
        assert stats_all_2.drive_count == 1
        assert stats_all_2.total_miles == 0.50
        assert stats_all_2.total_micro_drives == 1

    @pytest.mark.asyncio
    async def test_clean_reset_and_storage_deletion_zero_residue(
        self, mock_hass: Any
    ) -> None:
        """Test deleting storage via async_reset() completely removes all state and disk file."""
        store = DriveStore(mock_hass, "RESET_TEST_VIN")
        await store.async_load()

        drive = DriveRecord(
            vin="RESET_TEST_VIN",
            drive_id="reset_drive_01",
            start_time="2026-08-20T10:00:00Z",
            end_time="2026-08-20T10:30:00Z",
            distance_miles=15.0,
            duration_seconds=1800.0,
            start_soc=80.0,
            end_soc=75.0,
            battery_capacity_kwh=135.0,
            energy_kwh=6.75,
        )
        await store.async_save_drive(drive)
        assert len(store.drives) == 1
        assert store._store._data is not None

        # Reset
        await store.async_reset()
        assert len(store.drives) == 0
        assert len(store._drives_by_id) == 0
        assert store._store._data is None

        # Re-load
        reloaded = await store.async_load()
        assert len(reloaded) == 0

    def test_speed_bins_comprehensive_ranges(self) -> None:
        """Test speed bin boundaries across negative, zero, bin borders, and extreme speeds."""
        cases = [
            (-10.0, "0-9"),
            (0.0, "0-9"),
            (9.99, "0-9"),
            (10.0, "10-19"),
            (19.99, "10-19"),
            (20.0, "20-29"),
            (29.99, "20-29"),
            (30.0, "30-39"),
            (39.99, "30-39"),
            (40.0, "40-49"),
            (49.99, "40-49"),
            (50.0, "50-59"),
            (59.99, "50-59"),
            (60.0, "60-69"),
            (69.99, "60-69"),
            (70.0, "70-79"),
            (79.99, "70-79"),
            (80.0, "80+"),
            (80.01, "80+"),
            (120.0, "80+"),
        ]
        for spd, expected in cases:
            assert (
                _get_speed_bin_key(spd) == expected
            ), f"Speed {spd} should be bin {expected}"

    @pytest.mark.asyncio
    async def test_weighted_efficiency_vs_average_of_averages(
        self, mock_hass: Any
    ) -> None:
        """Test that period efficiency computes sum(miles)/sum(kWh) instead of average-of-averages."""
        t_iso = "2026-08-20T12:00:00Z"
        drive_a = DriveRecord(
            vin="MATH_VIN",
            drive_id="A",
            start_time=t_iso,
            end_time=t_iso,
            distance_miles=100.0,
            duration_seconds=3600.0,
            start_soc=90.0,
            end_soc=70.0,
            battery_capacity_kwh=100.0,
            energy_kwh=20.0,
        )  # 5.0 mi/kWh
        drive_b = DriveRecord(
            vin="MATH_VIN",
            drive_id="B",
            start_time=t_iso,
            end_time=t_iso,
            distance_miles=10.0,
            duration_seconds=600.0,
            start_soc=70.0,
            end_soc=65.0,
            battery_capacity_kwh=100.0,
            energy_kwh=5.0,
        )  # 2.0 mi/kWh

        store = DriveStore(mock_hass, "MATH_VIN")
        await store.async_save_drives_batch([drive_a, drive_b])
        stats = store.get_stats_all_time()

        # True weighted efficiency = (100 + 10) / (20 + 5) = 110 / 25 = 4.40 mi/kWh
        # Simple unweighted average = (5.0 + 2.0) / 2 = 3.50 mi/kWh
        assert stats.efficiency_mi_kwh == 4.40
        assert stats.efficiency_mi_kwh != 3.50
        assert stats.mpge == pytest.approx(4.40 * 33.705, rel=1e-2)

    def test_cli_execution_and_summary_output(self) -> None:
        """Test standalone CLI execution against fixture database with strict stdout verification."""
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "backfill_drives_from_sqlite.py"
        )
        res = subprocess.run(
            [
                sys.executable,
                script_path,
                "--db-path",
                FIXTURE_DB_PATH,
                "--vin",
                TEST_VIN,
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0
        out = res.stdout
        assert "Total Drives Reconstructed : 62" in out
        assert "Valid Drives (>= 0.5 mi)   : 58" in out
        assert "Micro-Drives (< 0.5 mi)    : 4" in out
        assert "370.93 miles" in out
        assert "131.05 kWh" in out
        assert "2.83 mi/kWh" in out
        assert "95.4 MPGe" in out
