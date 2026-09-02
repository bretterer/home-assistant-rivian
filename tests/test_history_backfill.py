"""Unit and integration tests for Rivian Historical Recorder Backfill Engine and CLI."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian.const import DOMAIN
from custom_components.rivian.drive_models import (
    MICRO_DRIVE_THRESHOLD_MILES,
    MPGE_FACTOR,
)
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.history_backfill import (
    async_backfill_from_recorder,
    open_sqlite_readonly,
    reconstruct_drives_from_sqlite,
    resolve_recorder_entities,
)

FIXTURE_DB_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "reggie_10day_history.db"
)
TEST_VIN = "7PDSGABA1NN000001"


class TestSQLiteReadOnlyEnforcement:
    """Test suite for strict SQLite read-only mode (mode=ro)."""

    def test_open_sqlite_readonly_success(self) -> None:
        """Test opening valid SQLite database in read-only mode."""
        conn = open_sqlite_readonly(FIXTURE_DB_PATH)
        assert conn is not None
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM states_meta")
        count = cur.fetchone()[0]
        assert count >= 7
        conn.close()

    def test_write_operations_strictly_prevented(self) -> None:
        """Test that write/insert/update/delete operations raise OperationalError in mode=ro."""
        conn = open_sqlite_readonly(FIXTURE_DB_PATH)
        cur = conn.cursor()

        # Attempt table creation
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            cur.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY)")

        # Attempt insert
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            cur.execute(
                "INSERT INTO states_meta (metadata_id, entity_id) VALUES (999, 'sensor.test')"
            )

        # Attempt update
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            cur.execute(
                "UPDATE states_meta SET entity_id = 'sensor.corrupt' WHERE metadata_id = 1"
            )

        # Attempt delete
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            cur.execute("DELETE FROM states_meta WHERE metadata_id = 1")

        conn.close()

    def test_nonexistent_file_raises_filenotfound(self) -> None:
        """Test that opening a non-existent database file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            open_sqlite_readonly("non_existent_database_path_12345.db")


class TestEntityResolution:
    """Test suite for dynamic entity resolution from recorder metadata."""

    def test_resolve_entities_from_fixture(self) -> None:
        """Test resolving entities for reggie from fixture database."""
        conn = open_sqlite_readonly(FIXTURE_DB_PATH)
        entities = resolve_recorder_entities(conn, vin=TEST_VIN, vehicle_id="reggie")
        conn.close()

        assert "gear_selector" in entities
        assert "odometer" in entities
        assert "battery_level" in entities
        assert "speed" in entities
        assert "altitude" in entities
        assert "latitude" in entities
        assert "longitude" in entities
        assert "battery_capacity" in entities

    def test_resolve_entities_with_custom_names(self) -> None:
        """Test resolving entities with alternate naming patterns."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db = tf.name

        try:
            conn = sqlite3.connect(temp_db)
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id VARCHAR(255))"
            )
            cur.executemany(
                "INSERT INTO states_meta VALUES (?, ?)",
                [
                    (1, "sensor.custom_r1t_gear_status"),
                    (2, "sensor.custom_r1t_vehicle_mileage"),
                    (3, "sensor.custom_r1t_battery_soc"),
                    (4, "sensor.custom_r1t_speed"),
                    (5, "sensor.custom_r1t_elevation"),
                ],
            )
            conn.commit()
            conn.close()

            ro_conn = open_sqlite_readonly(temp_db)
            resolved = resolve_recorder_entities(ro_conn, vehicle_id="custom_r1t")
            ro_conn.close()

            assert resolved.get("gear_selector") == 1
            assert resolved.get("odometer") == 2
            assert resolved.get("battery_level") == 3
            assert resolved.get("speed") == 4
            assert resolved.get("altitude") == 5
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)


class TestEmpiricalBaselineBackfill:
    """Test suite verifying exact empirical baseline reproduction for R1S Reggie 10-day dataset."""

    @pytest.mark.asyncio
    async def test_reconstruct_drives_baseline_metrics(self) -> None:
        """Verify synchronous reconstruction reproduces 58 valid drives, 370.93 mi, 131.05 kWh, 2.83 mi/kWh."""
        drives, _meta = reconstruct_drives_from_sqlite(
            db_path=FIXTURE_DB_PATH,
            vin=TEST_VIN,
            vehicle_id="reggie",
        )

        assert len(drives) == 62
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
    async def test_async_backfill_dry_run_reproduces_baseline(self) -> None:
        """Verify async_backfill_from_recorder returns full baseline in dry_run mode without storage writes."""
        mock_weather_client = MagicMock()
        mock_weather_client.async_get_historical_temperatures = AsyncMock(
            return_value={"2026-08-11T12:00": 72.5}
        )

        result = await async_backfill_from_recorder(
            hass=None,
            vin=TEST_VIN,
            db_path=FIXTURE_DB_PATH,
            dry_run=True,
            weather_client=mock_weather_client,
        )

        assert result["drives_found"] == 62
        assert result["valid_drives"] == 58
        assert result["micro_drives"] == 4
        assert result["total_miles"] == 370.93
        assert result["total_kwh"] == 131.05
        assert result["efficiency_mi_kwh"] == 2.83
        assert round(result["mpge"], 1) == 95.4
        assert result["duplicates_skipped"] == 0
        assert len(result["drives"]) == 62

    def test_speed_bins_and_elevation_populated(self) -> None:
        """Verify speed bins and elevation changes are correctly populated across reconstructed drives."""
        drives, _ = reconstruct_drives_from_sqlite(
            db_path=FIXTURE_DB_PATH,
            vin=TEST_VIN,
        )

        for drive in drives:
            assert isinstance(drive.speed_bins, dict)
            assert "0-9" in drive.speed_bins
            assert "20-29" in drive.speed_bins
            assert "80+" in drive.speed_bins
            assert drive.start_soc >= drive.end_soc
            assert drive.battery_capacity_kwh == 135.0
            assert drive.duration_seconds > 0


class TestParkDebounceLogic:
    """Test suite verifying 60-second Park debounce behavior during backfill."""

    def test_drive_12_debounce_merge_in_fixture(self) -> None:
        """Verify Drive 12 in the fixture (paused 35s in Park) was merged into 1 drive rather than 2."""
        conn = open_sqlite_readonly(FIXTURE_DB_PATH)
        cur = conn.cursor()
        # Count raw shifts from park to drive
        cur.execute(
            "SELECT COUNT(*) FROM states WHERE metadata_id = 1 AND state = 'drive'"
        )
        raw_drive_shifts = cur.fetchone()[0]
        conn.close()

        # Raw drive shifts in fixture is 63 due to Drive 12 debounce test
        assert raw_drive_shifts == 63

        # After 60-second debounce, total drives reconstructed is 62 (58 valid + 4 micro)
        drives, meta = reconstruct_drives_from_sqlite(
            db_path=FIXTURE_DB_PATH, vin=TEST_VIN
        )
        assert len(drives) == 62
        assert meta.get("merged_drives") == 62

    def test_synthetic_debounce_thresholds(self) -> None:
        """Verify segments separated by <= 60s are merged, while segments > 60s are separate."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db = tf.name

        try:
            conn = sqlite3.connect(temp_db)
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id VARCHAR(255))"
            )
            cur.execute(
                "INSERT INTO states_meta VALUES (1, 'sensor.reggie_gear_selector')"
            )
            cur.execute("INSERT INTO states_meta VALUES (2, 'sensor.reggie_odometer')")
            cur.execute(
                "INSERT INTO states_meta VALUES (3, 'sensor.reggie_battery_level')"
            )

            cur.execute("""
                CREATE TABLE states (
                    state_id INTEGER PRIMARY KEY,
                    metadata_id INTEGER,
                    state VARCHAR(255),
                    last_updated_ts REAL
                )
            """)

            t0 = 1786435200.0
            # Drive 1 Part A: t0 to t0+300
            # Park for 45 seconds: t0+300 to t0+345 (<= 60s -> merged)
            # Drive 1 Part B: t0+345 to t0+600
            # Park for 120 seconds: t0+600 to t0+720 (> 60s -> not merged)
            # Drive 2: t0+720 to t0+1000
            # Park
            events = [
                (1, "drive", t0),
                (2, "1000.0", t0),
                (3, "80.0", t0),
                (1, "park", t0 + 300),
                (2, "1003.0", t0 + 300),
                (3, "79.0", t0 + 300),
                (1, "drive", t0 + 345),
                (2, "1003.0", t0 + 345),
                (3, "79.0", t0 + 345),
                (1, "park", t0 + 600),
                (2, "1007.0", t0 + 600),
                (3, "77.5", t0 + 600),
                (1, "drive", t0 + 720),
                (2, "1007.0", t0 + 720),
                (3, "77.5", t0 + 720),
                (1, "park", t0 + 1000),
                (2, "1012.0", t0 + 1000),
                (3, "76.0", t0 + 1000),
            ]
            cur.executemany(
                "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (?, ?, ?)",
                events,
            )
            conn.commit()
            conn.close()

            drives, _ = reconstruct_drives_from_sqlite(db_path=temp_db, vin=TEST_VIN)
            assert len(drives) == 2
            # Drive 1 merged: 1000.0 to 1007.0 = 7.0 mi
            assert drives[0].distance_miles == 7.0
            # Drive 2: 1007.0 to 1012.0 = 5.0 mi
            assert drives[1].distance_miles == 5.0
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)


class TestStorageDeduplication:
    """Test suite verifying 100% deduplication idempotency when persisting backfilled drives."""

    @pytest.mark.asyncio
    async def test_repeated_backfill_deduplication(self, mock_hass: Any) -> None:
        """Verify backfilling twice results in 62 drives saved on run 1, and 0 added on run 2."""
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


class TestCLIBackfillScript:
    """Test suite for scripts/backfill_drives_from_sqlite.py standalone execution."""

    def test_cli_dry_run_execution(self) -> None:
        """Test invoking CLI script in dry-run mode via subprocess."""
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "backfill_drives_from_sqlite.py"
        )
        cmd = [
            sys.executable,
            script_path,
            "--db-path",
            FIXTURE_DB_PATH,
            "--vin",
            TEST_VIN,
            "--dry-run",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert result.returncode == 0
        output = result.stdout

        assert "Backfill Results Summary:" in output
        assert "Total Drives Reconstructed : 62" in output
        assert "Valid Drives (>= 0.5 mi)   : 58" in output
        assert "Micro-Drives (< 0.5 mi)    : 4" in output
        assert "370.93 miles" in output
        assert "131.05 kWh" in output
        assert "2.83 mi/kWh" in output
        assert "95.4 MPGe" in output

    def test_cli_output_json_file(self) -> None:
        """Test CLI script --output option generates valid JSON summary and drive list."""
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "backfill_drives_from_sqlite.py"
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            json_out = tf.name

        try:
            cmd = [
                sys.executable,
                script_path,
                "--db-path",
                FIXTURE_DB_PATH,
                "--vin",
                TEST_VIN,
                "--dry-run",
                "--output",
                json_out,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert result.returncode == 0

            assert os.path.isfile(json_out)
            with open(json_out, encoding="utf-8") as f:
                data = json.load(f)

            summary = data["summary"]
            assert summary["total_drives"] == 62
            assert summary["valid_drives"] == 58
            assert summary["micro_drives"] == 4
            assert summary["total_miles"] == 370.93
            assert summary["total_kwh"] == 131.05
            assert summary["efficiency_mi_kwh"] == 2.83
            assert round(summary["mpge"], 1) == 95.4
            assert len(data["drives"]) == 62
        finally:
            if os.path.exists(json_out):
                os.remove(json_out)

    def test_cli_nonexistent_database(self) -> None:
        """Test CLI script returns non-zero exit code when given invalid database path."""
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "backfill_drives_from_sqlite.py"
        )
        cmd = [
            sys.executable,
            script_path,
            "--db-path",
            "non_existent_db_file_xyz.db",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert result.returncode != 0
        assert "Error: Database file not found" in result.stderr


class TestServiceRegistration:
    """Test suite for rivian.backfill_drive_history Home Assistant service."""

    @pytest.mark.asyncio
    async def test_service_registration_and_execution(
        self, mock_hass: Any, mock_config_entry: Any
    ) -> None:
        """Test registering the service and calling it via hass.services.async_call."""
        from custom_components.rivian import async_setup_entry, async_unload_entry

        mock_hass.data[DOMAIN] = {}

        mock_api = MagicMock()
        mock_api.create_csrf_token = AsyncMock()
        mock_api.close = AsyncMock()

        mock_user_coordinator = MagicMock()
        mock_user_coordinator.data = {"registrationChannels": True}
        mock_user_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_user_coordinator.get_vehicles = MagicMock(
            return_value={
                "test_vehicle_id_1": {
                    "id": "test_vehicle_id_1",
                    "vin": TEST_VIN,
                    "name": "reggie",
                    "model": "R1S",
                }
            }
        )

        mock_vehicle_coordinator = MagicMock()
        mock_vehicle_coordinator.data = {"gearStatus": {"value": "park"}}
        mock_vehicle_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_vehicle_coordinator.charging_coordinator = MagicMock()
        mock_vehicle_coordinator.charging_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_vehicle_coordinator.drivers_coordinator = MagicMock()
        mock_vehicle_coordinator.drivers_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_vehicle_coordinator.async_add_listener = MagicMock(
            return_value=MagicMock()
        )

        mock_wallbox_coordinator = MagicMock()
        mock_wallbox_coordinator.async_config_entry_first_refresh = AsyncMock()

        mock_hass.config_entries = MagicMock()
        mock_hass.config_entries.async_forward_entry_setups = AsyncMock()
        mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

        # Set up entry
        with (
            patch(
                "custom_components.rivian.get_rivian_api_from_entry",
                return_value=mock_api,
            ),
            patch(
                "custom_components.rivian.UserCoordinator",
                return_value=mock_user_coordinator,
            ),
            patch(
                "custom_components.rivian.VehicleCoordinator",
                return_value=mock_vehicle_coordinator,
            ),
            patch(
                "custom_components.rivian.WallboxCoordinator",
                return_value=mock_wallbox_coordinator,
            ),
        ):
            setup_ok = await async_setup_entry(mock_hass, mock_config_entry)
            assert setup_ok is True

        assert mock_hass.services.has_service(DOMAIN, "backfill_drive_history")

        # Test calling service
        with patch(
            "custom_components.rivian.async_backfill_from_recorder",
            new_callable=AsyncMock,
        ) as mock_backfill:
            await mock_hass.services.async_call(
                DOMAIN,
                "backfill_drive_history",
                service_data={
                    "vin": TEST_VIN,
                    "dry_run": True,
                    "db_path": FIXTURE_DB_PATH,
                },
            )
            mock_backfill.assert_called_once_with(
                hass=mock_hass,
                vin=TEST_VIN,
                days=None,
                dry_run=True,
                db_path=FIXTURE_DB_PATH,
                store=ANY,
            )

        # Test unload cleans up service
        with patch(
            "custom_components.rivian.get_rivian_api_from_entry", return_value=mock_api
        ):
            await async_unload_entry(mock_hass, mock_config_entry)

        assert not mock_hass.services.has_service(DOMAIN, "backfill_drive_history")
