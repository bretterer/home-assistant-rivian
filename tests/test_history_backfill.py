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
from custom_components.rivian.drive_track import DriveTrack
from custom_components.rivian.history_backfill import (
    async_backfill_from_recorder,
    async_resolve_vehicle_entity_ids,
    open_sqlite_readonly,
    reconstruct_dcfc_sessions_from_sqlite,
    reconstruct_drives_from_sqlite,
    reconstruct_tracks_for_windows,
    reconstruct_vampire_events_from_drives,
    resolve_recorder_entities,
)

FIXTURE_DB_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "r1s_10day_history.db"
)
TEST_VIN = "7PDSGABA1NN000001"
TEST_VEHICLE_ID = "r1s_test"


def _build_track_recorder_db(db_path: str, t0: float) -> None:
    """Build a synthetic recorder DB (real HA schema subset) with GPS tracks.

    Layout: a phone device_tracker with a LOWER metadata_id than the
    vehicle's, plus the vehicle's device_tracker (entity_id ending in
    "_location", 5s-interval fixes for 10 minutes starting at t0) and
    speed/altitude/battery/odometer sensors recorded in mph/ft/%/mi.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id TEXT)"
    )
    cur.execute(
        "CREATE TABLE state_attributes (attributes_id INTEGER PRIMARY KEY, "
        "hash INTEGER, shared_attrs TEXT)"
    )
    cur.execute(
        "CREATE TABLE states (state_id INTEGER PRIMARY KEY, metadata_id INTEGER, "
        "state TEXT, attributes_id INTEGER, last_updated_ts REAL, "
        "last_changed_ts REAL, attributes TEXT)"
    )

    # Phone tracker gets the lowest metadata_id, so any naive "first match"
    # or "lowest id" selection would (incorrectly) pick it.
    cur.execute("INSERT INTO states_meta VALUES (1, 'device_tracker.phone_location_x')")
    cur.execute(
        f"INSERT INTO states_meta VALUES (2, 'device_tracker.{TEST_VEHICLE_ID}_location')"
    )
    cur.execute(f"INSERT INTO states_meta VALUES (3, 'sensor.{TEST_VEHICLE_ID}_speed')")
    cur.execute(
        f"INSERT INTO states_meta VALUES (4, 'sensor.{TEST_VEHICLE_ID}_altitude')"
    )
    cur.execute(
        "INSERT INTO states_meta VALUES "
        f"(5, 'sensor.{TEST_VEHICLE_ID}_battery_state_of_charge')"
    )
    cur.execute(
        f"INSERT INTO states_meta VALUES (6, 'sensor.{TEST_VEHICLE_ID}_odometer')"
    )

    attrs_id = 1

    def _insert_attrs(shared_attrs: dict) -> int:
        nonlocal attrs_id
        cur.execute(
            "INSERT INTO state_attributes (attributes_id, hash, shared_attrs) "
            "VALUES (?, 0, ?)",
            (attrs_id, json.dumps(shared_attrs)),
        )
        this_id = attrs_id
        attrs_id += 1
        return this_id

    # Phone: a couple of points, far away, never in the vehicle's time window
    # of interest except by coincidence of overlapping timestamps.
    phone_attrs = _insert_attrs({"latitude": 10.0, "longitude": 10.0})
    cur.execute(
        "INSERT INTO states (metadata_id, state, attributes_id, last_updated_ts) "
        "VALUES (1, 'home', ?, ?)",
        (phone_attrs, t0),
    )

    # Vehicle GPS: a fix every 5 seconds for 10 minutes, moving northeast.
    n_points = 121
    for i in range(n_points):
        ts = t0 + i * 5.0
        lat = 39.7242 + i * 0.0001
        lon = -104.9880 + i * 0.0001
        aid = _insert_attrs({"latitude": lat, "longitude": lon})
        cur.execute(
            "INSERT INTO states (metadata_id, state, attributes_id, last_updated_ts) "
            "VALUES (2, 'not_home', ?, ?)",
            (aid, ts),
        )

    # Telemetry sampled every 30 seconds, with a unit_of_measurement in each
    # row's shared_attrs (as HA's recorder actually stores it, in the user's
    # display units).
    for i in range(0, n_points, 6):
        ts = t0 + i * 5.0
        speed_attrs = _insert_attrs({"unit_of_measurement": "mph"})
        cur.execute(
            "INSERT INTO states (metadata_id, state, attributes_id, last_updated_ts) "
            "VALUES (3, ?, ?, ?)",
            ("35.0", speed_attrs, ts),
        )
        alt_attrs = _insert_attrs({"unit_of_measurement": "ft"})
        cur.execute(
            "INSERT INTO states (metadata_id, state, attributes_id, last_updated_ts) "
            "VALUES (4, ?, ?, ?)",
            ("2500.0", alt_attrs, ts),
        )
        soc_attrs = _insert_attrs({"unit_of_measurement": "%"})
        cur.execute(
            "INSERT INTO states (metadata_id, state, attributes_id, last_updated_ts) "
            "VALUES (5, ?, ?, ?)",
            (str(80.0 - i * 0.01), soc_attrs, ts),
        )
        odo_attrs = _insert_attrs({"unit_of_measurement": "mi"})
        cur.execute(
            "INSERT INTO states (metadata_id, state, attributes_id, last_updated_ts) "
            "VALUES (6, ?, ?, ?)",
            (str(1000.0 + i * 0.01), odo_attrs, ts),
        )

    conn.commit()
    conn.close()


def _total_drive_rows(store: DriveStore) -> int:
    """Return the all-time count of valid + micro drive rows for a DriveStore's VIN.

    DriveStore's sync cache API no longer exposes a raw ``.drives`` list (it
    is cache-only and excludes micro-drives from ``drive_count``), so the
    all-time valid count and the all-time micro count are summed instead.
    """
    stats = store.get_stats_all_time()
    return stats.drive_count + stats.total_micro_drives


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
        """Test resolving entities for r1s_test from fixture database."""
        conn = open_sqlite_readonly(FIXTURE_DB_PATH)
        entities = resolve_recorder_entities(conn, vin=TEST_VIN, vehicle_id="r1s_test")
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

    def test_resolve_entities_empty_database(self) -> None:
        """Test entity resolution on an empty database with no tables returns empty dict."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db = tf.name

        try:
            conn = sqlite3.connect(temp_db)
            conn.close()

            ro_conn = open_sqlite_readonly(temp_db)
            resolved = resolve_recorder_entities(ro_conn, vehicle_id="r1s_test")
            ro_conn.close()

            assert resolved == {}
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)


class TestEmpiricalBaselineBackfill:
    """Test suite verifying exact empirical baseline reproduction for R1S Test 10-day dataset."""

    @pytest.mark.asyncio
    async def test_reconstruct_drives_baseline_metrics(self) -> None:
        """Verify synchronous reconstruction reproduces 58 valid drives, 370.93 mi, 131.05 kWh, 2.83 mi/kWh."""
        drives, _meta = reconstruct_drives_from_sqlite(
            db_path=FIXTURE_DB_PATH,
            vin=TEST_VIN,
            vehicle_id="r1s_test",
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

    @pytest.mark.asyncio
    async def test_dry_run_never_constructs_or_loads_a_store(self) -> None:
        """A store's first load can import legacy JSON; a dry run must not trigger it."""
        mock_weather_client = MagicMock()
        mock_weather_client.async_get_historical_temperatures = AsyncMock(
            return_value={}
        )
        hass = MagicMock()
        hass.data = {DOMAIN: {"_analytics_db": MagicMock()}}
        hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
        unloaded_store = MagicMock(is_loaded=False)
        unloaded_store.async_list_drives = AsyncMock(side_effect=AssertionError)

        with patch(
            "custom_components.rivian.history_backfill.DriveStore",
            side_effect=AssertionError("dry run constructed a DriveStore"),
        ):
            await async_backfill_from_recorder(
                hass=hass,
                vin=TEST_VIN,
                db_path=FIXTURE_DB_PATH,
                dry_run=True,
                weather_client=mock_weather_client,
            )
            await async_backfill_from_recorder(
                hass=hass,
                vin=TEST_VIN,
                db_path=FIXTURE_DB_PATH,
                dry_run=True,
                weather_client=mock_weather_client,
                store=unloaded_store,
            )

        unloaded_store.async_list_drives.assert_not_called()

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

    def test_reconstruct_from_empty_database(self) -> None:
        """Test reconstruct_drives_from_sqlite handles an empty database gracefully."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db = tf.name

        try:
            conn = sqlite3.connect(temp_db)
            conn.close()

            drives, meta = reconstruct_drives_from_sqlite(temp_db)
            assert drives == []
            assert meta == {}
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)

    def test_reconstruct_from_corrupt_database(self) -> None:
        """Test reconstruct_drives_from_sqlite handles a corrupted database file gracefully."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db = tf.name

        try:
            with open(temp_db, "wb") as f:
                f.write(b"NOT_A_VALID_SQLITE_DATABASE_HEADER_DATA")

            drives, meta = reconstruct_drives_from_sqlite(temp_db)
            assert drives == []
            assert meta == {}
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)


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
        """Verify gear spans separated by <= 60s are merged, while spans > 60s are separate."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db = tf.name

        try:
            conn = sqlite3.connect(temp_db)
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id VARCHAR(255))"
            )
            cur.execute(
                "INSERT INTO states_meta VALUES (1, 'sensor.r1s_test_gear_selector')"
            )
            cur.execute(
                "INSERT INTO states_meta VALUES (2, 'sensor.r1s_test_odometer')"
            )
            cur.execute(
                "INSERT INTO states_meta VALUES (3, 'sensor.r1s_test_battery_level')"
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


class TestVehicleContextReconstruction:
    """Test suite for range/drive-mode/trailer/driver fields during backfill."""

    def _build_context_db(self, temp_db: str) -> None:
        conn = sqlite3.connect(temp_db)
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id VARCHAR(255))"
        )
        cur.execute(
            "INSERT INTO states_meta VALUES (1, 'sensor.r1s_test_gear_selector')"
        )
        cur.execute("INSERT INTO states_meta VALUES (2, 'sensor.r1s_test_odometer')")
        cur.execute(
            "INSERT INTO states_meta VALUES (3, 'sensor.r1s_test_battery_level')"
        )
        cur.execute(
            "INSERT INTO states_meta VALUES (4, 'sensor.r1s_test_distance_to_empty')"
        )
        cur.execute("INSERT INTO states_meta VALUES (5, 'sensor.r1s_test_drive_mode')")
        cur.execute(
            "INSERT INTO states_meta VALUES (6, 'sensor.r1s_test_trailer_status')"
        )
        cur.execute(
            "INSERT INTO states_meta VALUES (7, 'sensor.r1s_test_active_driver')"
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
        events = [
            (1, "drive", t0),
            (2, "1000.0", t0),
            (3, "80.0", t0),
            (4, "300.0", t0),
            (5, "everyday", t0),
            (6, "not_connected", t0),
            (7, "Kelly", t0),
            (4, "290.0", t0 + 100),
            (5, "sport", t0 + 100),
            (6, "connected", t0 + 100),
            (7, "Kelly", t0 + 100),
            (1, "park", t0 + 300),
            (2, "1007.0", t0 + 300),
            (3, "77.0", t0 + 300),
        ]
        cur.executemany(
            "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (?, ?, ?)",
            events,
        )
        conn.commit()
        conn.close()

    def test_context_fields_reconstructed_via_fuzzy_match(self) -> None:
        """distanceToEmpty/driveMode/trailerStatus/activeDriverName reconstruct."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db = tf.name
        try:
            self._build_context_db(temp_db)
            drives, _ = reconstruct_drives_from_sqlite(db_path=temp_db, vin=TEST_VIN)
            assert len(drives) == 1
            drive = drives[0]
            assert drive.start_range_mi == pytest.approx(300.0 * 0.621371, rel=1e-3)
            assert drive.end_range_mi == pytest.approx(290.0 * 0.621371, rel=1e-3)
            assert drive.drive_modes == ["All-Purpose", "Sport"]
            assert drive.trailer is True
            assert drive.driver == "Kelly"
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)

    def test_missing_context_entities_leave_fields_none(self) -> None:
        """Without those sensors in the recorder, context fields stay None/empty."""
        drives, _ = reconstruct_drives_from_sqlite(
            db_path=FIXTURE_DB_PATH, vin=TEST_VIN
        )
        assert drives
        for drive in drives:
            assert drive.start_range_mi is None
            assert drive.end_range_mi is None
            assert drive.drive_modes == []
            assert drive.trailer is None
            assert drive.driver is None


class TestStorageDeduplication:
    """Test suite verifying 100% deduplication idempotency when persisting backfilled drives."""

    @pytest.mark.asyncio
    async def test_repeated_backfill_deduplication(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Verify backfilling twice results in 62 drives saved on run 1, and 0 added on run 2."""
        store = DriveStore(hass=mock_hass, vin=TEST_VIN, db=analytics_db)
        await store.async_load()
        assert _total_drive_rows(store) == 0

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
        assert _total_drive_rows(store) == 62

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
        assert _total_drive_rows(store) == 62

    @pytest.mark.asyncio
    async def test_backfill_never_overwrites_a_stored_drive_with_the_same_id(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A live drive sharing the reconstruction's id keeps its live-only data."""
        store = DriveStore(hass=mock_hass, vin=TEST_VIN, db=analytics_db)
        await store.async_load()
        weather = MagicMock()
        weather.async_get_historical_temperatures = AsyncMock(return_value={})
        await async_backfill_from_recorder(
            hass=mock_hass,
            vin=TEST_VIN,
            db_path=FIXTURE_DB_PATH,
            dry_run=False,
            store=store,
            weather_client=weather,
        )
        # Stand in for the live record of one of those drives: same id, plus
        # data only live capture has.
        first = (await store.async_list_drives(limit=1, include_micro=True))[0]
        stored = await store.async_get_drive_detail(first["drive_id"])
        assert stored is not None
        record = analytics_db._row_to_drive
        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT * FROM drives WHERE drive_id = ?", (first["drive_id"],)
            ).fetchone()
        live = record(row, hydrate=True)
        live.driver = "Live Driver"
        await store.async_save_drives_batch([live])

        await async_backfill_from_recorder(
            hass=mock_hass,
            vin=TEST_VIN,
            db_path=FIXTURE_DB_PATH,
            dry_run=False,
            store=store,
            weather_client=weather,
        )

        detail = await store.async_get_drive_detail(first["drive_id"])
        assert detail["drive"]["driver"] == "Live Driver"


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
                    "name": "r1s_test",
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
        assert mock_hass.services.has_service(DOMAIN, "create_efficiency_dashboard")
        assert mock_hass.services.has_service(DOMAIN, "recompute_drive_stats")

        # Test calling service with default dry_run (should default to True)
        with patch(
            "custom_components.rivian.async_backfill_from_recorder",
            new_callable=AsyncMock,
        ) as mock_backfill:
            await mock_hass.services.async_call(
                DOMAIN,
                "backfill_drive_history",
                service_data={
                    "vin": TEST_VIN,
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
                tracks=True,
            )

        # Test calling service with explicit dry_run=False
        with patch(
            "custom_components.rivian.async_backfill_from_recorder",
            new_callable=AsyncMock,
        ) as mock_backfill_live:
            await mock_hass.services.async_call(
                DOMAIN,
                "backfill_drive_history",
                service_data={
                    "vin": TEST_VIN,
                    "dry_run": False,
                    "db_path": FIXTURE_DB_PATH,
                },
            )
            mock_backfill_live.assert_called_once_with(
                hass=mock_hass,
                vin=TEST_VIN,
                days=None,
                dry_run=False,
                db_path=FIXTURE_DB_PATH,
                store=ANY,
                tracks=True,
            )

        # Test unload cleans up both services
        with patch(
            "custom_components.rivian.get_rivian_api_from_entry", return_value=mock_api
        ):
            await async_unload_entry(mock_hass, mock_config_entry)

        assert not mock_hass.services.has_service(DOMAIN, "backfill_drive_history")
        assert not mock_hass.services.has_service(DOMAIN, "create_efficiency_dashboard")
        assert not mock_hass.services.has_service(DOMAIN, "recompute_drive_stats")


class TestVampireEventReconstruction:
    """Tests for reconstruct_vampire_events_from_drives function."""

    def test_reconstruct_vampire_events_filters_charging_and_short_stops(self) -> None:
        """Test vampire drain reconstruction correctly excludes charging and short stops."""
        from custom_components.rivian.drive_models import DriveRecord

        drives = [
            DriveRecord(
                vin=TEST_VIN,
                drive_id="d1",
                start_time="2026-08-25T13:00:00Z",
                end_time="2026-08-25T13:30:00Z",
                distance_miles=10.0,
                duration_seconds=1800.0,
                start_soc=80.0,
                end_soc=75.0,
                battery_capacity_kwh=135.0,
                energy_kwh=6.75,
                end_lat=39.7242,
                end_lon=-104.9880,
            ),
            # Drive 2 starts 10 minutes later (short stop < 30 min -> should be skipped)
            DriveRecord(
                vin=TEST_VIN,
                drive_id="d2",
                start_time="2026-08-25T13:40:00Z",
                end_time="2026-08-25T14:00:00Z",
                distance_miles=5.0,
                duration_seconds=1200.0,
                start_soc=75.0,
                end_soc=73.0,
                battery_capacity_kwh=135.0,
                energy_kwh=2.7,
                end_lat=39.7342,
                end_lon=-104.9980,
            ),
            # Drive 3 starts 8 hours later, SOC dropped from 73.0 to 72.5 (valid vampire drain)
            DriveRecord(
                vin=TEST_VIN,
                drive_id="d3",
                start_time="2026-08-25T22:00:00Z",
                end_time="2026-08-25T22:30:00Z",
                distance_miles=8.0,
                duration_seconds=1800.0,
                start_soc=72.5,
                end_soc=70.0,
                battery_capacity_kwh=135.0,
                energy_kwh=3.38,
                end_lat=39.7442,
                end_lon=-105.0080,
            ),
            # Drive 4 starts 10 hours later, SOC jumped from 70.0 to 90.0 (charging event -> excluded)
            DriveRecord(
                vin=TEST_VIN,
                drive_id="d4",
                start_time="2026-08-26T08:30:00Z",
                end_time="2026-08-26T09:00:00Z",
                distance_miles=12.0,
                duration_seconds=1800.0,
                start_soc=90.0,
                end_soc=85.0,
                battery_capacity_kwh=135.0,
                energy_kwh=6.75,
            ),
        ]

        vampire_events = reconstruct_vampire_events_from_drives(drives, 135.0)
        assert len(vampire_events) == 1
        event = vampire_events[0]
        assert event.idle_hours == 8.0
        assert event.drain_soc == 0.5
        assert event.drain_kwh == round((0.5 * 135.0) / 100.0, 2)
        assert event.rate_pct_per_day == round((0.5 / 8.0) * 24.0, 2)
        assert event.latitude == 39.7342
        assert event.longitude == -104.9980


class TestTrackReconstruction:
    """Tests for reconstruct_tracks_for_windows and its unit conversions."""

    def test_tracks_rebuilt_only_for_windows_with_data(self, tmp_path: Any) -> None:
        """A window overlapping the tracker data yields a track; an empty one does not."""
        t0 = 1790000000.0
        db_path = str(tmp_path / "tracks.db")
        _build_track_recorder_db(db_path, t0)

        entity_ids = {
            "device_tracker": f"device_tracker.{TEST_VEHICLE_ID}_location",
            "speed": f"sensor.{TEST_VEHICLE_ID}_speed",
            "altitude": f"sensor.{TEST_VEHICLE_ID}_altitude",
            "battery_level": f"sensor.{TEST_VEHICLE_ID}_battery_state_of_charge",
            "odometer": f"sensor.{TEST_VEHICLE_ID}_odometer",
        }
        windows = [
            ("drive_with_data", t0, t0 + 600.0),
            ("drive_without_data", t0 + 100000.0, t0 + 100600.0),
        ]

        result = reconstruct_tracks_for_windows(db_path, entity_ids, windows)

        assert "drive_with_data" in result
        assert "drive_without_data" not in result
        track = result["drive_with_data"]
        assert isinstance(track, DriveTrack)
        assert len(track) > 100

    def test_explicit_entity_map_uses_vehicle_not_phone(self, tmp_path: Any) -> None:
        """The explicit entity_id map resolves the vehicle tracker, not the phone."""
        t0 = 1790000000.0
        db_path = str(tmp_path / "tracks.db")
        _build_track_recorder_db(db_path, t0)

        entity_ids = {"device_tracker": f"device_tracker.{TEST_VEHICLE_ID}_location"}
        windows = [("drive1", t0, t0 + 600.0)]

        result = reconstruct_tracks_for_windows(db_path, entity_ids, windows)
        track = result["drive1"]

        # The vehicle track moves northeast starting at (39.7242, -104.9880); the
        # phone sits at (10.0, 10.0). If the phone had been picked instead,
        # none of these points would be anywhere near the vehicle's path.
        assert all(39.0 < p.lat < 41.0 for p in track.points)
        assert all(-106.0 < p.lon < -104.0 for p in track.points)

    def test_fuzzy_fallback_prefers_location_suffix(self, tmp_path: Any) -> None:
        """With no explicit map, the fuzzy fallback still avoids the phone tracker."""
        t0 = 1790000000.0
        db_path = str(tmp_path / "tracks.db")
        _build_track_recorder_db(db_path, t0)

        windows = [("drive1", t0, t0 + 600.0)]
        result = reconstruct_tracks_for_windows(
            db_path, {}, windows, vin=TEST_VIN, vehicle_id=TEST_VEHICLE_ID
        )

        assert "drive1" in result
        track = result["drive1"]
        assert all(39.0 < p.lat < 41.0 for p in track.points)

    def test_units_converted_to_si(self, tmp_path: Any) -> None:
        """Speed/altitude/odometer are converted from mph/ft/mi to SI units."""
        t0 = 1790000000.0
        db_path = str(tmp_path / "tracks.db")
        _build_track_recorder_db(db_path, t0)

        entity_ids = {
            "device_tracker": f"device_tracker.{TEST_VEHICLE_ID}_location",
            "speed": f"sensor.{TEST_VEHICLE_ID}_speed",
            "altitude": f"sensor.{TEST_VEHICLE_ID}_altitude",
            "odometer": f"sensor.{TEST_VEHICLE_ID}_odometer",
        }
        windows = [("drive1", t0, t0 + 600.0)]
        track = reconstruct_tracks_for_windows(db_path, entity_ids, windows)["drive1"]

        speed_points = [p for p in track.points if p.speed_mps is not None]
        alt_points = [p for p in track.points if p.alt_m is not None]
        odo_points = [p for p in track.points if p.odo_m is not None]
        assert speed_points and alt_points and odo_points

        # 35 mph -> ~15.65 m/s
        assert speed_points[0].speed_mps == pytest.approx(15.6464, abs=0.01)
        # 2500 ft -> ~762.0 m
        assert alt_points[0].alt_m == pytest.approx(762.0, abs=0.1)
        # 1000 mi -> ~1,609,344 m
        assert odo_points[0].odo_m == pytest.approx(1609344.0, rel=1e-4)

    def test_windows_with_insufficient_points_omitted(self, tmp_path: Any) -> None:
        """A window with fewer than 2 valid tracker points is left out entirely."""
        t0 = 1790000000.0
        db_path = str(tmp_path / "tracks.db")
        _build_track_recorder_db(db_path, t0)

        entity_ids = {"device_tracker": f"device_tracker.{TEST_VEHICLE_ID}_location"}
        # A 1-second window straddling exactly one tracker fix (t0), padded by
        # +/-30s from reconstruct_tracks_for_windows still only reaches a
        # handful of points; use a window far past the last recorded fix so
        # zero points fall inside it after padding.
        windows = [("no_data", t0 + 100000.0, t0 + 100000.5)]

        result = reconstruct_tracks_for_windows(db_path, entity_ids, windows)
        assert "no_data" not in result

    def test_no_windows_returns_empty(self, tmp_path: Any) -> None:
        """An empty windows list returns an empty dict without opening the DB."""
        result = reconstruct_tracks_for_windows("does_not_exist.db", {}, [])
        assert result == {}


class TestAsyncResolveVehicleEntityIds:
    """Tests for the entity-registry-based track entity resolver."""

    @pytest.mark.asyncio
    async def test_resolves_expected_unique_ids(self, mock_hass: Any) -> None:
        """Verify it looks up the exact (domain, DOMAIN, unique_id) triples."""
        mock_registry = MagicMock()

        def _get_entity_id(domain: str, platform: str, unique_id: str) -> str | None:
            mapping = {
                ("device_tracker", DOMAIN, f"{TEST_VIN}-location"): (
                    "device_tracker.r1s_test_location"
                ),
                ("sensor", DOMAIN, f"{TEST_VIN}-speed"): "sensor.r1s_test_speed",
            }
            return mapping.get((domain, platform, unique_id))

        mock_registry.async_get_entity_id.side_effect = _get_entity_id
        mock_er_module = MagicMock()
        mock_er_module.async_get.return_value = mock_registry

        with patch("custom_components.rivian.history_backfill.er", mock_er_module):
            resolved = await async_resolve_vehicle_entity_ids(mock_hass, TEST_VIN)

        assert resolved == {
            "device_tracker": "device_tracker.r1s_test_location",
            "speed": "sensor.r1s_test_speed",
        }

    @pytest.mark.asyncio
    async def test_no_registry_returns_empty(self, mock_hass: Any) -> None:
        """Without a usable entity registry, it returns {} rather than raising."""
        resolved = await async_resolve_vehicle_entity_ids(mock_hass, TEST_VIN)
        assert resolved == {}


class TestBackfillTrackIntegration:
    """Tests for async_backfill_from_recorder's GPS-track-rebuild step."""

    def _make_fake_store(self, missing: list[tuple[str, float, float]]) -> MagicMock:
        store = MagicMock()
        store.async_drives_missing_tracks = AsyncMock(return_value=missing)
        store.async_upsert_tracks = AsyncMock(return_value=len(missing))
        store.async_list_drives = AsyncMock(return_value=[])
        store.async_save_drives_batch = AsyncMock(return_value=0)
        store.async_save_vampire_events = AsyncMock()
        store.async_save_dcfc_sessions = AsyncMock()
        return store

    @pytest.mark.asyncio
    async def test_dry_run_previews_tracks_without_writing(self, tmp_path: Any) -> None:
        """dry_run=True reports track counts but never calls async_upsert_tracks."""
        t0 = 1790000000.0
        db_path = str(tmp_path / "tracks.db")
        _build_track_recorder_db(db_path, t0)

        fake_store = self._make_fake_store([("drive1", t0, t0 + 600.0)])

        result = await async_backfill_from_recorder(
            hass=None,
            vin=TEST_VIN,
            vehicle_id=TEST_VEHICLE_ID,
            db_path=db_path,
            dry_run=True,
            store=fake_store,
            tracks=True,
        )

        assert result["drives_missing_tracks"] == 1
        assert result["tracks_rebuilt"] == 1
        assert result["tracks_written"] == 0
        fake_store.async_upsert_tracks.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_run_writes_tracks_with_backfill_source(
        self, tmp_path: Any
    ) -> None:
        """dry_run=False calls async_upsert_tracks(..., source='backfill')."""
        t0 = 1790000000.0
        db_path = str(tmp_path / "tracks.db")
        _build_track_recorder_db(db_path, t0)

        fake_store = self._make_fake_store([("drive1", t0, t0 + 600.0)])

        result = await async_backfill_from_recorder(
            hass=None,
            vin=TEST_VIN,
            vehicle_id=TEST_VEHICLE_ID,
            db_path=db_path,
            dry_run=False,
            store=fake_store,
            tracks=True,
        )

        assert result["tracks_written"] == 1
        fake_store.async_upsert_tracks.assert_called_once()
        call_args = fake_store.async_upsert_tracks.call_args
        assert call_args.kwargs["source"] == "backfill"
        items = call_args.args[0]
        assert len(items) == 1
        assert items[0][0] == "drive1"
        assert isinstance(items[0][1], DriveTrack)

    @pytest.mark.asyncio
    async def test_tracks_false_skips_rebuild_entirely(self, tmp_path: Any) -> None:
        """tracks=False never touches async_drives_missing_tracks."""
        t0 = 1790000000.0
        db_path = str(tmp_path / "tracks.db")
        _build_track_recorder_db(db_path, t0)

        fake_store = self._make_fake_store([("drive1", t0, t0 + 600.0)])

        result = await async_backfill_from_recorder(
            hass=None,
            vin=TEST_VIN,
            db_path=db_path,
            dry_run=True,
            store=fake_store,
            tracks=False,
        )

        assert result["drives_missing_tracks"] == 0
        fake_store.async_drives_missing_tracks.assert_not_called()


class TestHomeCoordinateFillRemoved:
    """Regression test: drives with missing GPS no longer inherit HA's home location."""

    @pytest.mark.asyncio
    async def test_missing_gps_drive_keeps_none_coordinates(self) -> None:
        """A reconstructed drive with no GPS data is not filled with home lat/lon."""
        mock_hass = MagicMock()
        mock_hass.config.latitude = 41.1400
        mock_hass.config.longitude = -104.8200
        mock_hass.config.components = []

        async def _add_executor_job(target: Any, *args: Any) -> Any:
            return target(*args)

        mock_hass.async_add_executor_job = AsyncMock(side_effect=_add_executor_job)
        mock_hass.bus = MagicMock()
        mock_hass.data = {}

        mock_weather_client = MagicMock()
        mock_weather_client.async_get_historical_temperatures = AsyncMock(
            return_value={}
        )

        # A fake store (no real analytics DB behind it) so the overlap-skip
        # lookup added alongside track rebuilding has something to call
        # without needing a fully wired hass.data["_analytics_db"].
        fake_store = MagicMock()
        fake_store.async_list_drives = AsyncMock(return_value=[])

        result = await async_backfill_from_recorder(
            hass=mock_hass,
            vin=TEST_VIN,
            db_path=FIXTURE_DB_PATH,
            dry_run=True,
            store=fake_store,
            weather_client=mock_weather_client,
            tracks=False,
        )

        drives = result["drives"]
        assert drives  # sanity: the fixture does produce drives
        # None of the reconstructed drives should have been coerced onto HA's
        # home coordinates; any that lack real GPS data stay None.
        home_lat = round(mock_hass.config.latitude, 1)
        home_lon = round(mock_hass.config.longitude, 1)
        for d in drives:
            if d.start_lat is not None and d.start_lon is not None:
                assert (
                    round(d.start_lat, 1) != home_lat
                    or round(d.start_lon, 1) != home_lon
                )


class TestOverlapSkipExisting:
    """Tests for skipping reconstructed drives that duplicate an existing one."""

    @pytest.mark.asyncio
    async def test_overlapping_drive_with_different_id_is_skipped(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A reconstructed drive overlapping >50% of an existing (different-id)
        stored drive is dropped before persisting, and counted as skipped.
        """
        store = DriveStore(hass=mock_hass, vin=TEST_VIN, db=analytics_db)
        await store.async_load()

        # Reconstruct the real baseline drives once to learn their exact
        # start/end times, then pre-seed the store with the SAME drives but
        # under different, "live-recorded"-style drive_ids, simulating the
        # scenario this check targets.
        drives, _meta = reconstruct_drives_from_sqlite(
            db_path=FIXTURE_DB_PATH, vin=TEST_VIN, vehicle_id="r1s_test"
        )
        assert drives

        from dataclasses import replace

        live_style_drives = [replace(d, drive_id=f"live_{d.drive_id}") for d in drives]
        await store.async_save_drives_batch(live_style_drives)

        mock_weather_client = MagicMock()
        mock_weather_client.async_get_historical_temperatures = AsyncMock(
            return_value={}
        )

        result = await async_backfill_from_recorder(
            hass=mock_hass,
            vin=TEST_VIN,
            db_path=FIXTURE_DB_PATH,
            dry_run=True,
            store=store,
            weather_client=mock_weather_client,
            tracks=False,
        )

        assert result["overlap_skipped_existing"] == len(drives)
        assert result["drives_found"] == 0


def _make_charging_states_conn(
    status_rows: list[tuple[str, float]],
    soc_rows: list[tuple[str, float]],
) -> sqlite3.Connection:
    """Build an in-memory recorder-shaped `states` table for DCFC reconstruction.

    ``status_rows``/``soc_rows`` are ``(state, timestamp)`` pairs for
    metadata_id 1 (charging_status) and metadata_id 2 (battery_level)
    respectively.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE states (state_id INTEGER PRIMARY KEY, metadata_id INTEGER, "
        "state TEXT, last_updated_ts REAL)"
    )
    for state, ts in status_rows:
        cur.execute(
            "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (1, ?, ?)",
            (state, ts),
        )
    for state, ts in soc_rows:
        cur.execute(
            "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (2, ?, ?)",
            (str(state), ts),
        )
    conn.commit()
    return conn


class TestReconstructDcfcSessionsFromSqlite:
    """Regression tests locking in `reconstruct_dcfc_sessions_from_sqlite` behavior.

    These exercise the SoC-derivative estimation now shared with
    `charging.estimate_charge_curve` (`charging.py`), guarding against
    regressions from that refactor.
    """

    def test_real_dcfc_session_recorded(self) -> None:
        """A 45.0% -> 80.9% rise over 33 minutes is reconstructed as DCFC."""
        t0 = 1_700_000_000.0
        duration_s = 33 * 60
        step = 30.0
        n_steps = int(duration_s // step)
        soc_rows = [
            (
                round(45.0 + (80.9 - 45.0) * (i / n_steps), 2),
                t0 + i * step,
            )
            for i in range(n_steps + 1)
        ]
        status_rows = [
            ("on", t0 + i * 60.0) for i in range(int(duration_s // 60.0) + 1)
        ] + [("off", t0 + duration_s)]
        conn = _make_charging_states_conn(status_rows, soc_rows)
        try:
            sessions = reconstruct_dcfc_sessions_from_sqlite(
                conn=conn,
                entity_map={"charging_status": 1, "battery_level": 2},
                pack_capacity=135.0,
                vin=TEST_VIN,
            )
        finally:
            conn.close()

        assert len(sessions) == 1
        session = sessions[0]
        assert session.is_dcfc is True
        assert session.start_soc == pytest.approx(45.0, abs=0.5)
        assert session.end_soc == pytest.approx(80.9, abs=0.5)
        assert 60.0 < session.max_power_kw < 225.0
        assert session.samples

    def test_l2_like_rise_kept_as_ac(self) -> None:
        """A ~7 kW-equivalent rise over an hour is kept as an AC session."""
        t0 = 1_700_000_000.0
        duration_s = 3600.0
        step = 30.0
        n_steps = int(duration_s // step)
        start_soc = 40.0
        end_soc = start_soc + (7.0 / 135.0) * 100.0
        soc_rows = [
            (
                round(start_soc + (end_soc - start_soc) * (i / n_steps), 3),
                t0 + i * step,
            )
            for i in range(n_steps + 1)
        ]
        status_rows = [
            ("on", t0 + i * 60.0) for i in range(int(duration_s // 60.0) + 1)
        ] + [("off", t0 + duration_s)]
        conn = _make_charging_states_conn(status_rows, soc_rows)
        try:
            sessions = reconstruct_dcfc_sessions_from_sqlite(
                conn=conn,
                entity_map={"charging_status": 1, "battery_level": 2},
                pack_capacity=135.0,
                vin=TEST_VIN,
            )
        finally:
            conn.close()

        assert len(sessions) == 1
        assert sessions[0].kind == "ac"
        assert sessions[0].is_dcfc is False
        assert sessions[0].source == "backfill"
        assert sessions[0].max_power_kw < 22.0
        assert len(sessions[0].samples) <= 20
