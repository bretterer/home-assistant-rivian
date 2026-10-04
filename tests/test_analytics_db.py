"""Unit tests for the SQLite-backed AnalyticsDatabase storage layer.

Covers schema/versioning, legacy JSON -> SQLite import (lossless and
idempotent), SQL-vs-legacy aggregate parity against the empirical fixture
baseline, NULL-``sort_ts`` handling, prune boundary conditions, and -- most
importantly -- that DriveStore's synchronous cache surface never touches
SQLite (the executor-thread guard's whole reason to exist).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
import sqlite3
import threading
import time
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.rivian import analytics_db as analytics_db_module, road_snap
from custom_components.rivian.analytics_db import (
    SCHEMA_VERSION,
    ActiveCheckpoint,
    AnalyticsDatabase,
)
from custom_components.rivian.const import DRIVE_SENSORS
from custom_components.rivian.drive_models import (
    STANDARD_SPEED_BINS,
    ChargingSample,
    ChargingSessionRecord,
    DriveChunk,
    DriveRecord,
    SpeedBinData,
    VampireDrainRecord,
)
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.drive_track import DriveTrack, TrackPoint
from custom_components.rivian.history_backfill import reconstruct_drives_from_sqlite
from custom_components.rivian.road_heat import (
    BASE_LEVEL,
    HEAT_FORMAT_VERSION,
    HeatGrid,
    RoadHeat,
    split_key,
    track_cells,
    track_passes,
)
from custom_components.rivian.sensor import RivianDriveSensorEntity

TEST_VIN = "7PDSGABA8NN000000"
FIXTURE_DB_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "r1s_10day_history.db"
)
EMPIRICAL_VIN = "7PDSGABA1NN000001"

# Frozen schema v1 DDL (the whole _SCHEMA_SQL contents as they existed before
# schema v2 added the drive_tracks/active_drive/active_track_chunks tables),
# used to build a v1 fixture database for the migration test. Do NOT update
# this to match a future schema change; it must stay a snapshot of v1.
_V1_SCHEMA_SQL = """
PRAGMA auto_vacuum = INCREMENTAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drives (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  distance_miles REAL NOT NULL, duration_seconds REAL NOT NULL,
  start_soc REAL, end_soc REAL, battery_capacity_kwh REAL,
  energy_kwh REAL NOT NULL, efficiency_mi_kwh REAL, mpge REAL,
  start_altitude_ft REAL, end_altitude_ft REAL, elevation_change_ft REAL,
  avg_speed_mph REAL, max_speed_mph REAL, integrated_temperature_f REAL,
  is_micro_drive INTEGER NOT NULL DEFAULT 0,
  start_odometer_mi REAL, end_odometer_mi REAL,
  start_lat REAL, start_lon REAL, end_lat REAL, end_lon REAL,
  speed_bins_json TEXT NOT NULL DEFAULT '{}',
  weather_json    TEXT NOT NULL DEFAULT '[]',
  segments_json   TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_drives ON drives(vin, drive_id);
CREATE INDEX IF NOT EXISTS ix_drives_window ON drives(vin, is_micro_drive, sort_ts);
CREATE INDEX IF NOT EXISTS ix_drives_recent ON drives(vin, sort_ts DESC, created_ts DESC);

CREATE TABLE IF NOT EXISTS vampire_events (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  idle_hours REAL NOT NULL, start_soc REAL NOT NULL, end_soc REAL NOT NULL,
  drain_soc REAL NOT NULL, drain_kwh REAL NOT NULL,
  rate_pct_per_day REAL NOT NULL, avg_watts REAL NOT NULL,
  avg_temp_f REAL, latitude REAL, longitude REAL,
  UNIQUE(vin, start_time, end_time)
);
CREATE INDEX IF NOT EXISTS ix_vampire_recent ON vampire_events(vin, sort_ts DESC);

CREATE TABLE IF NOT EXISTS dcfc_sessions (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, session_id TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  start_soc REAL NOT NULL, end_soc REAL NOT NULL,
  energy_added_kwh REAL NOT NULL, max_power_kw REAL NOT NULL, avg_power_kw REAL NOT NULL,
  is_dcfc INTEGER NOT NULL DEFAULT 1,
  sample_count INTEGER NOT NULL DEFAULT 0,
  samples_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE(vin, session_id)
);
CREATE INDEX IF NOT EXISTS ix_dcfc_recent ON dcfc_sessions(vin, start_ts DESC);
"""

# Frozen schema v3 DDL (before the v4 migration renamed drives.segments_json to
# chunks_json), used to build a v3 fixture database for the migration test. Do
# NOT update this to match a future schema change; it must stay a snapshot
# of v3.
_V3_SCHEMA_SQL = """
PRAGMA auto_vacuum = INCREMENTAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drives (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  distance_miles REAL NOT NULL, duration_seconds REAL NOT NULL,
  start_soc REAL, end_soc REAL, battery_capacity_kwh REAL,
  energy_kwh REAL NOT NULL, efficiency_mi_kwh REAL, mpge REAL,
  start_altitude_ft REAL, end_altitude_ft REAL, elevation_change_ft REAL,
  avg_speed_mph REAL, max_speed_mph REAL, integrated_temperature_f REAL,
  is_micro_drive INTEGER NOT NULL DEFAULT 0,
  start_odometer_mi REAL, end_odometer_mi REAL,
  start_lat REAL, start_lon REAL, end_lat REAL, end_lon REAL,
  speed_bins_json TEXT NOT NULL DEFAULT '{}',
  weather_json    TEXT NOT NULL DEFAULT '[]',
  segments_json   TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_drives ON drives(vin, drive_id);
CREATE INDEX IF NOT EXISTS ix_drives_window ON drives(vin, is_micro_drive, sort_ts);
CREATE INDEX IF NOT EXISTS ix_drives_recent ON drives(vin, sort_ts DESC, created_ts DESC);

CREATE TABLE IF NOT EXISTS vampire_events (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  idle_hours REAL NOT NULL, start_soc REAL NOT NULL, end_soc REAL NOT NULL,
  drain_soc REAL NOT NULL, drain_kwh REAL NOT NULL,
  rate_pct_per_day REAL NOT NULL, avg_watts REAL NOT NULL,
  avg_temp_f REAL, latitude REAL, longitude REAL,
  UNIQUE(vin, start_time, end_time)
);
CREATE INDEX IF NOT EXISTS ix_vampire_recent ON vampire_events(vin, sort_ts DESC);

CREATE TABLE IF NOT EXISTS dcfc_sessions (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, session_id TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  start_soc REAL NOT NULL, end_soc REAL NOT NULL,
  energy_added_kwh REAL NOT NULL, max_power_kw REAL NOT NULL, avg_power_kw REAL NOT NULL,
  is_dcfc INTEGER NOT NULL DEFAULT 1,
  sample_count INTEGER NOT NULL DEFAULT 0,
  samples_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE(vin, session_id)
);
CREATE INDEX IF NOT EXISTS ix_dcfc_recent ON dcfc_sessions(vin, start_ts DESC);

CREATE TABLE IF NOT EXISTS drive_tracks (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  sort_ts REAL, point_count INTEGER NOT NULL,
  min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
  source TEXT NOT NULL DEFAULT 'live',
  detail TEXT NOT NULL DEFAULT 'full',
  track_json TEXT NOT NULL, preview_json TEXT NOT NULL,
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL,
  UNIQUE(vin, drive_id)
);
CREATE INDEX IF NOT EXISTS ix_tracks_sort ON drive_tracks(vin, sort_ts);

CREATE TABLE IF NOT EXISTS active_drive (
  vin TEXT PRIMARY KEY, drive_id TEXT NOT NULL,
  state_json TEXT NOT NULL, updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS active_track_chunks (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  seq INTEGER NOT NULL, points_json TEXT NOT NULL,
  UNIQUE(vin, drive_id, seq)
);

CREATE TABLE IF NOT EXISTS vehicle_pictures (
  vin TEXT PRIMARY KEY, status TEXT NOT NULL,
  content_type TEXT, image BLOB, source_url TEXT,
  options_json TEXT NOT NULL DEFAULT '[]', fetched_ts REAL NOT NULL
);
"""

# Frozen schema v4 DDL (before the v5 migration added the road_heat/
# road_heat_drives tables), used to build a v4 fixture database for the
# migration test. Do NOT update this to match a future schema change; it must
# stay a snapshot of v4.
_V4_SCHEMA_SQL = """
PRAGMA auto_vacuum = INCREMENTAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drives (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  distance_miles REAL NOT NULL, duration_seconds REAL NOT NULL,
  start_soc REAL, end_soc REAL, battery_capacity_kwh REAL,
  energy_kwh REAL NOT NULL, efficiency_mi_kwh REAL, mpge REAL,
  start_altitude_ft REAL, end_altitude_ft REAL, elevation_change_ft REAL,
  avg_speed_mph REAL, max_speed_mph REAL, integrated_temperature_f REAL,
  is_micro_drive INTEGER NOT NULL DEFAULT 0,
  start_odometer_mi REAL, end_odometer_mi REAL,
  start_lat REAL, start_lon REAL, end_lat REAL, end_lon REAL,
  speed_bins_json TEXT NOT NULL DEFAULT '{}',
  weather_json    TEXT NOT NULL DEFAULT '[]',
  chunks_json     TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_drives ON drives(vin, drive_id);
CREATE INDEX IF NOT EXISTS ix_drives_window ON drives(vin, is_micro_drive, sort_ts);
CREATE INDEX IF NOT EXISTS ix_drives_recent ON drives(vin, sort_ts DESC, created_ts DESC);

CREATE TABLE IF NOT EXISTS vampire_events (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  idle_hours REAL NOT NULL, start_soc REAL NOT NULL, end_soc REAL NOT NULL,
  drain_soc REAL NOT NULL, drain_kwh REAL NOT NULL,
  rate_pct_per_day REAL NOT NULL, avg_watts REAL NOT NULL,
  avg_temp_f REAL, latitude REAL, longitude REAL,
  UNIQUE(vin, start_time, end_time)
);
CREATE INDEX IF NOT EXISTS ix_vampire_recent ON vampire_events(vin, sort_ts DESC);

CREATE TABLE IF NOT EXISTS dcfc_sessions (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, session_id TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  start_soc REAL NOT NULL, end_soc REAL NOT NULL,
  energy_added_kwh REAL NOT NULL, max_power_kw REAL NOT NULL, avg_power_kw REAL NOT NULL,
  is_dcfc INTEGER NOT NULL DEFAULT 1,
  sample_count INTEGER NOT NULL DEFAULT 0,
  samples_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE(vin, session_id)
);
CREATE INDEX IF NOT EXISTS ix_dcfc_recent ON dcfc_sessions(vin, start_ts DESC);

CREATE TABLE IF NOT EXISTS drive_tracks (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  sort_ts REAL, point_count INTEGER NOT NULL,
  min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
  source TEXT NOT NULL DEFAULT 'live',
  detail TEXT NOT NULL DEFAULT 'full',
  track_json TEXT NOT NULL, preview_json TEXT NOT NULL,
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL,
  UNIQUE(vin, drive_id)
);
CREATE INDEX IF NOT EXISTS ix_tracks_sort ON drive_tracks(vin, sort_ts);

CREATE TABLE IF NOT EXISTS active_drive (
  vin TEXT PRIMARY KEY, drive_id TEXT NOT NULL,
  state_json TEXT NOT NULL, updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS active_track_chunks (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  seq INTEGER NOT NULL, points_json TEXT NOT NULL,
  UNIQUE(vin, drive_id, seq)
);

CREATE TABLE IF NOT EXISTS vehicle_pictures (
  vin TEXT PRIMARY KEY, status TEXT NOT NULL,
  content_type TEXT, image BLOB, source_url TEXT,
  options_json TEXT NOT NULL DEFAULT '[]', fetched_ts REAL NOT NULL
);
"""

# Frozen schema v5 DDL (before the v6 migration added the per-drive
# summary-stats and vehicle-context columns), used to build a v5 fixture
# database for the migration test. Do NOT update this to match a future
# schema change; it must stay a snapshot of v5.
_V5_SCHEMA_SQL = """
PRAGMA auto_vacuum = INCREMENTAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drives (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  distance_miles REAL NOT NULL, duration_seconds REAL NOT NULL,
  start_soc REAL, end_soc REAL, battery_capacity_kwh REAL,
  energy_kwh REAL NOT NULL, efficiency_mi_kwh REAL, mpge REAL,
  start_altitude_ft REAL, end_altitude_ft REAL, elevation_change_ft REAL,
  avg_speed_mph REAL, max_speed_mph REAL, integrated_temperature_f REAL,
  is_micro_drive INTEGER NOT NULL DEFAULT 0,
  start_odometer_mi REAL, end_odometer_mi REAL,
  start_lat REAL, start_lon REAL, end_lat REAL, end_lon REAL,
  speed_bins_json TEXT NOT NULL DEFAULT '{}',
  weather_json    TEXT NOT NULL DEFAULT '[]',
  chunks_json     TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_drives ON drives(vin, drive_id);
CREATE INDEX IF NOT EXISTS ix_drives_window ON drives(vin, is_micro_drive, sort_ts);
CREATE INDEX IF NOT EXISTS ix_drives_recent ON drives(vin, sort_ts DESC, created_ts DESC);

CREATE TABLE IF NOT EXISTS vampire_events (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  idle_hours REAL NOT NULL, start_soc REAL NOT NULL, end_soc REAL NOT NULL,
  drain_soc REAL NOT NULL, drain_kwh REAL NOT NULL,
  rate_pct_per_day REAL NOT NULL, avg_watts REAL NOT NULL,
  avg_temp_f REAL, latitude REAL, longitude REAL,
  UNIQUE(vin, start_time, end_time)
);
CREATE INDEX IF NOT EXISTS ix_vampire_recent ON vampire_events(vin, sort_ts DESC);

CREATE TABLE IF NOT EXISTS dcfc_sessions (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, session_id TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  start_soc REAL NOT NULL, end_soc REAL NOT NULL,
  energy_added_kwh REAL NOT NULL, max_power_kw REAL NOT NULL, avg_power_kw REAL NOT NULL,
  is_dcfc INTEGER NOT NULL DEFAULT 1,
  sample_count INTEGER NOT NULL DEFAULT 0,
  samples_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE(vin, session_id)
);
CREATE INDEX IF NOT EXISTS ix_dcfc_recent ON dcfc_sessions(vin, start_ts DESC);

CREATE TABLE IF NOT EXISTS drive_tracks (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  sort_ts REAL, point_count INTEGER NOT NULL,
  min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
  source TEXT NOT NULL DEFAULT 'live',
  detail TEXT NOT NULL DEFAULT 'full',
  track_json TEXT NOT NULL, preview_json TEXT NOT NULL,
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL,
  UNIQUE(vin, drive_id)
);
CREATE INDEX IF NOT EXISTS ix_tracks_sort ON drive_tracks(vin, sort_ts);

CREATE TABLE IF NOT EXISTS active_drive (
  vin TEXT PRIMARY KEY, drive_id TEXT NOT NULL,
  state_json TEXT NOT NULL, updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS active_track_chunks (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  seq INTEGER NOT NULL, points_json TEXT NOT NULL,
  UNIQUE(vin, drive_id, seq)
);

CREATE TABLE IF NOT EXISTS vehicle_pictures (
  vin TEXT PRIMARY KEY, status TEXT NOT NULL,
  content_type TEXT, image BLOB, source_url TEXT,
  options_json TEXT NOT NULL DEFAULT '[]', fetched_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS road_heat (
  vin TEXT NOT NULL, month TEXT NOT NULL,
  level INTEGER NOT NULL, version INTEGER NOT NULL,
  cell_count INTEGER NOT NULL, drive_count INTEGER NOT NULL,
  data BLOB NOT NULL, updated_ts REAL NOT NULL,
  PRIMARY KEY (vin, month)
);

CREATE TABLE IF NOT EXISTS road_heat_drives (
  vin TEXT NOT NULL, drive_id TEXT NOT NULL, month TEXT NOT NULL,
  PRIMARY KEY (vin, drive_id)
);
"""

# Frozen schema v6 DDL (the whole _SCHEMA_SQL contents as they existed before
# schema v7 added the osm_roads/track_fills tables and drive_tracks.gaps_scanned),
# used to build a v6 fixture database for the migration test. Do NOT update
# this to match a future schema change; it must stay a snapshot of v6.
_V6_SCHEMA_SQL = """
PRAGMA auto_vacuum = INCREMENTAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drives (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  distance_miles REAL NOT NULL, duration_seconds REAL NOT NULL,
  start_soc REAL, end_soc REAL, battery_capacity_kwh REAL,
  energy_kwh REAL NOT NULL, efficiency_mi_kwh REAL, mpge REAL,
  start_altitude_ft REAL, end_altitude_ft REAL, elevation_change_ft REAL,
  avg_speed_mph REAL, max_speed_mph REAL, integrated_temperature_f REAL,
  is_micro_drive INTEGER NOT NULL DEFAULT 0,
  start_odometer_mi REAL, end_odometer_mi REAL,
  start_lat REAL, start_lon REAL, end_lat REAL, end_lon REAL,
  speed_bins_json TEXT NOT NULL DEFAULT '{}',
  weather_json    TEXT NOT NULL DEFAULT '[]',
  chunks_json     TEXT NOT NULL DEFAULT '[]',
  moving_seconds REAL, stopped_seconds REAL, stop_count INTEGER,
  climb_ft REAL, descent_ft REAL, track_max_speed_mph REAL,
  pct_distance_over_70mph REAL,
  start_range_mi REAL, end_range_mi REAL,
  drive_modes_json TEXT, trailer INTEGER, driver TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_drives ON drives(vin, drive_id);
CREATE INDEX IF NOT EXISTS ix_drives_window ON drives(vin, is_micro_drive, sort_ts);
CREATE INDEX IF NOT EXISTS ix_drives_recent ON drives(vin, sort_ts DESC, created_ts DESC);

CREATE TABLE IF NOT EXISTS vampire_events (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  idle_hours REAL NOT NULL, start_soc REAL NOT NULL, end_soc REAL NOT NULL,
  drain_soc REAL NOT NULL, drain_kwh REAL NOT NULL,
  rate_pct_per_day REAL NOT NULL, avg_watts REAL NOT NULL,
  avg_temp_f REAL, latitude REAL, longitude REAL,
  UNIQUE(vin, start_time, end_time)
);
CREATE INDEX IF NOT EXISTS ix_vampire_recent ON vampire_events(vin, sort_ts DESC);

CREATE TABLE IF NOT EXISTS dcfc_sessions (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, session_id TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  start_ts REAL, end_ts REAL, created_ts REAL NOT NULL,
  sort_ts REAL GENERATED ALWAYS AS (COALESCE(start_ts, end_ts)) VIRTUAL,
  start_soc REAL NOT NULL, end_soc REAL NOT NULL,
  energy_added_kwh REAL NOT NULL, max_power_kw REAL NOT NULL, avg_power_kw REAL NOT NULL,
  is_dcfc INTEGER NOT NULL DEFAULT 1,
  sample_count INTEGER NOT NULL DEFAULT 0,
  samples_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE(vin, session_id)
);
CREATE INDEX IF NOT EXISTS ix_dcfc_recent ON dcfc_sessions(vin, start_ts DESC);

CREATE TABLE IF NOT EXISTS drive_tracks (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  sort_ts REAL, point_count INTEGER NOT NULL,
  min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
  source TEXT NOT NULL DEFAULT 'live',
  detail TEXT NOT NULL DEFAULT 'full',
  track_json TEXT NOT NULL, preview_json TEXT NOT NULL,
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL,
  UNIQUE(vin, drive_id)
);
CREATE INDEX IF NOT EXISTS ix_tracks_sort ON drive_tracks(vin, sort_ts);

CREATE TABLE IF NOT EXISTS active_drive (
  vin TEXT PRIMARY KEY, drive_id TEXT NOT NULL,
  state_json TEXT NOT NULL, updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS active_track_chunks (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  seq INTEGER NOT NULL, points_json TEXT NOT NULL,
  UNIQUE(vin, drive_id, seq)
);

CREATE TABLE IF NOT EXISTS vehicle_pictures (
  vin TEXT PRIMARY KEY, status TEXT NOT NULL,
  content_type TEXT, image BLOB, source_url TEXT,
  options_json TEXT NOT NULL DEFAULT '[]', fetched_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS road_heat (
  vin TEXT NOT NULL, month TEXT NOT NULL,
  level INTEGER NOT NULL, version INTEGER NOT NULL,
  cell_count INTEGER NOT NULL, drive_count INTEGER NOT NULL,
  data BLOB NOT NULL, updated_ts REAL NOT NULL,
  PRIMARY KEY (vin, month)
);

CREATE TABLE IF NOT EXISTS road_heat_drives (
  vin TEXT NOT NULL, drive_id TEXT NOT NULL, month TEXT NOT NULL,
  PRIMARY KEY (vin, drive_id)
);
"""


def _make_track(
    n: int = 5,
    start_t: float = 1_700_000_000.0,
    lat0: float = 37.0,
    lon0: float = -122.0,
    step: float = 0.001,
) -> DriveTrack:
    """Build a simple monotonically-increasing GPS track for tests."""
    track = DriveTrack()
    for i in range(n):
        track.append(
            TrackPoint(t=start_t + i * 10, lat=lat0 + i * step, lon=lon0 + i * step)
        )
    return track


def _make_drive(
    drive_id: str,
    distance_miles: float,
    energy_kwh: float,
    start_time: str | None,
    end_time: str | None,
    is_micro_drive: bool = False,
    start_lat: float | None = None,
    start_lon: float | None = None,
    end_lat: float | None = None,
    end_lon: float | None = None,
) -> DriveRecord:
    """Build a minimal DriveRecord for storage-layer tests.

    The coordinate arguments default to None (unset), so every existing
    caller that doesn't pass them is unaffected.
    """
    return DriveRecord(
        vin=TEST_VIN,
        drive_id=drive_id,
        start_time=start_time or "",
        end_time=end_time or "",
        distance_miles=distance_miles,
        duration_seconds=600.0,
        start_soc=80.0,
        end_soc=75.0,
        battery_capacity_kwh=135.0,
        energy_kwh=energy_kwh,
        is_micro_drive=is_micro_drive,
        start_lat=start_lat,
        start_lon=start_lon,
        end_lat=end_lat,
        end_lon=end_lon,
    )


class TestSchemaAndMeta:
    """Schema creation, versioning, and meta key/value helpers."""

    def test_schema_creates_tables_and_user_version(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """setup() creates all expected tables and stamps PRAGMA user_version."""
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            with db._lock:
                tables = {
                    row[0]
                    for row in db._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                user_version = db._conn.execute("PRAGMA user_version").fetchone()[0]
            assert {"meta", "drives", "vampire_events", "dcfc_sessions"} <= tables
            assert user_version == SCHEMA_VERSION
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        finally:
            db.close()

    def test_setup_is_idempotent(self, mock_hass: Any, analytics_db_path: str) -> None:
        """Calling setup() twice on an already-open connection is a safe no-op."""
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        conn_before = db._conn
        db.setup()
        assert db._conn is conn_before
        db.close()

    def test_get_set_meta_roundtrip(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """set_meta/get_meta round-trip, and an unknown key returns None."""
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            assert db.get_meta("does_not_exist") is None
            db.set_meta("foo", "bar")
            assert db.get_meta("foo") == "bar"
            db.set_meta("foo", "baz")
            assert db.get_meta("foo") == "baz"
        finally:
            db.close()

    def test_executor_thread_guard_raises_on_loop_thread(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """Calling an executor-bound method from the (simulated) loop thread raises."""
        # Point loop_thread_id at *this* thread, the opposite of the usual
        # test setup, to specifically exercise the guard's failure path.
        mock_hass.loop_thread_id = threading.get_ident()
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        with pytest.raises(RuntimeError, match="executor"):
            db.setup()


class TestLegacyJsonMigration:
    """One-time legacy JSON -> SQLite import: lossless, idempotent, non-destructive."""

    def test_migration_lossless_idempotent_and_preserves_json_file(
        self, mock_hass: Any, analytics_db_path: str, tmp_path: Any
    ) -> None:
        """Importing legacy JSON twice keeps counts stable and never deletes the source file."""
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            legacy_path = tmp_path / "legacy_drives.json"
            legacy_payload = {
                "drives": [
                    _make_drive(
                        "legacy_1",
                        12.0,
                        4.0,
                        "2026-08-01T10:00:00Z",
                        "2026-08-01T10:20:00Z",
                    ).to_dict(),
                    _make_drive(
                        "legacy_2",
                        8.0,
                        3.0,
                        "2026-08-02T10:00:00Z",
                        "2026-08-02T10:20:00Z",
                    ).to_dict(),
                ],
                "vampire_events": [
                    VampireDrainRecord(
                        start_time="2026-08-01T20:00:00Z",
                        end_time="2026-08-02T06:00:00Z",
                        idle_hours=10.0,
                        start_soc=80.0,
                        end_soc=78.0,
                        drain_soc=2.0,
                        drain_kwh=2.7,
                        rate_pct_per_day=4.8,
                        avg_watts=112.5,
                    ).to_dict()
                ],
                "dcfc_sessions": [
                    ChargingSessionRecord(
                        session_id="legacy_dcfc_1",
                        start_time="2026-08-01T09:00:00Z",
                        end_time="2026-08-01T09:30:00Z",
                        start_soc=20.0,
                        end_soc=80.0,
                        energy_added_kwh=81.0,
                        max_power_kw=150.0,
                        avg_power_kw=120.0,
                        samples=[
                            ChargingSample(
                                timestamp="2026-08-01T09:00:00Z",
                                soc=20.0,
                                power_kw=150.0,
                            )
                        ],
                    ).to_dict()
                ],
            }
            legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

            # Parse the real on-disk JSON exactly as DriveStore would.
            with legacy_path.open(encoding="utf-8") as f:
                raw = json.load(f)
            drives, vampire_events, dcfc_sessions = DriveStore._parse_legacy_payload(
                raw
            )

            counts_1 = db.migrate_legacy_json(
                TEST_VIN, drives, vampire_events, dcfc_sessions
            )
            assert counts_1 == {"drives": 2, "vampire_events": 1, "dcfc_sessions": 1}
            assert db.get_meta(f"json_migrated_{TEST_VIN}") == json.dumps(counts_1)

            stats_after_first = db.window_stats(
                TEST_VIN, None, datetime.now(timezone.utc).timestamp()
            )
            assert stats_after_first.drive_count == 2
            assert stats_after_first.total_miles == 20.0

            # Re-running the import (as if the marker check were bypassed)
            # must not create duplicate rows: same drive_ids/keys upsert.
            counts_2 = db.migrate_legacy_json(
                TEST_VIN, drives, vampire_events, dcfc_sessions
            )
            assert counts_2 == {"drives": 0, "vampire_events": 0, "dcfc_sessions": 1}
            stats_after_second = db.window_stats(
                TEST_VIN, None, datetime.now(timezone.utc).timestamp()
            )
            assert stats_after_second.drive_count == 2
            assert stats_after_second.total_miles == 20.0

            # The legacy JSON file itself is never touched/deleted by import.
            assert legacy_path.exists()
            with legacy_path.open(encoding="utf-8") as f:
                assert json.load(f) == legacy_payload
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_drive_store_skips_reimport_after_marker_set(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """DriveStore.async_load() only imports legacy JSON once per VIN."""
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        # Simulate a legacy JSON store with one drive, without touching disk.
        store._legacy_store._data = {
            "drives": [
                _make_drive(
                    "legacy_x", 5.0, 2.0, "2026-08-01T10:00:00Z", "2026-08-01T10:10:00Z"
                ).to_dict()
            ]
        }
        await store.async_load()
        assert store.drive_count == 1

        # A second DriveStore for the same VIN against the same db must not
        # re-import (the meta marker short-circuits it), even though its own
        # legacy_store mock is empty.
        store2 = DriveStore(mock_hass, TEST_VIN, analytics_db)
        await store2.async_load()
        assert store2.drive_count == 1


class TestAggregateParitySQLvsLegacy:
    """SQL-computed aggregates must match a plain-Python aggregate over the same drives."""

    def test_empirical_fixture_aggregate_parity(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """window_stats() over imported fixture drives matches the empirical baseline
        (370.93 mi, 131.05 kWh, 2.83 mi/kWh, 4 micro-drives) and a Python-side sum."""
        drives, _meta = reconstruct_drives_from_sqlite(
            db_path=FIXTURE_DB_PATH, vin=EMPIRICAL_VIN, vehicle_id="r1s_test"
        )
        assert len(drives) == 62

        analytics_db.upsert_drives(EMPIRICAL_VIN, drives)
        now_ts = datetime.now(timezone.utc).timestamp()
        sql_stats = analytics_db.window_stats(EMPIRICAL_VIN, None, now_ts)

        # Legacy-style plain-Python aggregate over the same in-memory records.
        valid = [d for d in drives if not d.is_micro_drive]
        micro = [d for d in drives if d.is_micro_drive]
        py_total_miles = round(sum(d.distance_miles for d in valid), 2)
        py_total_kwh = round(sum(d.energy_kwh for d in valid), 2)
        py_efficiency = round(py_total_miles / py_total_kwh, 2)
        py_mpge = round(py_efficiency * 33.705, 1)

        assert sql_stats.total_miles == py_total_miles == 370.93
        assert sql_stats.total_kwh == py_total_kwh == 131.05
        assert sql_stats.efficiency_mi_kwh == py_efficiency == 2.83
        assert round(sql_stats.mpge, 1) == py_mpge == 95.4
        assert sql_stats.drive_count == len(valid) == 58
        assert sql_stats.total_micro_drives == len(micro) == 4


class TestNullSortTsHandling:
    """Rows with unparseable timestamps (NULL sort_ts) are excluded from windows,
    included in all-time, and retained (not deleted) by prune()."""

    def test_null_sort_ts_excluded_from_window_included_in_all_time(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        now_dt = datetime.now(timezone.utc)
        now_ts = now_dt.timestamp()
        recent_iso = (now_dt - timedelta(days=2)).isoformat()
        valid_drive = _make_drive("valid_1", 10.0, 4.0, recent_iso, recent_iso)
        bad_ts_drive = _make_drive(
            "bad_ts_1", 20.0, 5.0, "not-a-timestamp", "also-not-a-timestamp"
        )
        analytics_db.upsert_drives(TEST_VIN, [valid_drive, bad_ts_drive])

        window_stats = analytics_db.window_stats(TEST_VIN, 30, now_ts)
        assert window_stats.drive_count == 1
        assert window_stats.total_miles == 10.0

        all_time_stats = analytics_db.window_stats(TEST_VIN, None, now_ts)
        assert all_time_stats.drive_count == 2
        assert all_time_stats.total_miles == 30.0

    def test_null_sort_ts_retained_by_prune(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        bad_ts_drive = _make_drive("bad_ts_prune", 5.0, 2.0, "garbage", "garbage-end")
        analytics_db.upsert_drives(TEST_VIN, [bad_ts_drive])

        # A cutoff far in the future would delete everything with a real
        # sort_ts, but the NULL-sort_ts row is retained regardless: SQL's
        # `NULL < cutoff_ts` is never true.
        far_future_cutoff = datetime.now(timezone.utc).timestamp() + 999_999_999
        removed = analytics_db.prune(TEST_VIN, far_future_cutoff)
        assert removed == 0

        stats = analytics_db.window_stats(
            TEST_VIN, None, datetime.now(timezone.utc).timestamp()
        )
        assert stats.drive_count == 1


class TestPruneBoundaryConditions:
    """Prune's cutoff comparison is a strict `<`, not `<=`."""

    def test_prune_boundary_retains_row_exactly_at_cutoff(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        cutoff_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        cutoff_ts = cutoff_dt.timestamp()

        at_cutoff = _make_drive(
            "at_cutoff",
            10.0,
            4.0,
            cutoff_dt.isoformat(),
            cutoff_dt.isoformat(),
        )
        just_before = _make_drive(
            "just_before",
            10.0,
            4.0,
            (cutoff_dt - timedelta(seconds=1)).isoformat(),
            (cutoff_dt - timedelta(seconds=1)).isoformat(),
        )
        just_after = _make_drive(
            "just_after",
            10.0,
            4.0,
            (cutoff_dt + timedelta(seconds=1)).isoformat(),
            (cutoff_dt + timedelta(seconds=1)).isoformat(),
        )
        analytics_db.upsert_drives(TEST_VIN, [at_cutoff, just_before, just_after])

        removed = analytics_db.prune(TEST_VIN, cutoff_ts)
        # Only the strictly-older-than-cutoff row is removed.
        assert removed == 1

        now_ts = datetime.now(timezone.utc).timestamp()
        stats = analytics_db.window_stats(TEST_VIN, None, now_ts)
        assert stats.drive_count == 2

    def test_prune_removes_nothing_when_all_rows_newer_than_cutoff(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        recent = _make_drive(
            "recent_1",
            10.0,
            4.0,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        )
        analytics_db.upsert_drives(TEST_VIN, [recent])
        ancient_cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
        removed = analytics_db.prune(TEST_VIN, ancient_cutoff)
        assert removed == 0


class _PoisonedAnalyticsDatabase(AnalyticsDatabase):
    """An AnalyticsDatabase stand-in whose every executor-bound method raises.

    Used to prove DriveStore's synchronous cache-only surface (and the
    sensor entities built on top of it) never falls through to SQLite.
    """

    def _boom(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "AnalyticsDatabase method was called from a supposedly cache-only path"
        )

    get_meta = _boom
    set_meta = _boom
    upsert_drives = _boom
    insert_vampire_event = _boom
    merge_vampire_events = _boom
    upsert_dcfc_sessions = _boom
    migrate_legacy_json = _boom
    delete_vin = _boom
    prune = _boom
    window_stats = _boom
    build_cache = _boom
    close = _boom


class _FakeTracker:
    """Minimal stand-in exposing only what RivianDriveSensorEntity reads from a tracker."""

    def __init__(self) -> None:
        from custom_components.rivian.drive_models import DriveState, DriveStatus

        self.drive_state = DriveState(is_driving=False, status=DriveStatus.PARKED.value)
        self.is_debouncing_park = False

    def async_add_listener(self, _callback: Any) -> Any:
        return lambda: None


class TestCachePurity:
    """Regression guard: DriveStore's sync surface and sensor entities must never touch SQLite."""

    @pytest.mark.asyncio
    async def test_all_sync_accessors_and_sensor_properties_survive_poisoned_db(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """With every AnalyticsDatabase method poisoned to raise, all DriveStore sync
        properties and the sensor entity's native_value/extra_state_attributes must
        still work without ever invoking the (poisoned) database."""
        # Populate real data first, using a *working* database...
        real_db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        real_db.setup()
        store = DriveStore(mock_hass, TEST_VIN, real_db)
        await store.async_load()

        drive = _make_drive(
            "purity_1", 20.0, 6.0, "2026-08-20T14:30:00Z", "2026-08-20T15:05:00Z"
        )
        await store.async_save_drive(drive)
        await store.async_save_vampire_events(
            [
                VampireDrainRecord(
                    start_time="2026-08-20T10:00:00Z",
                    end_time="2026-08-20T14:30:00Z",
                    idle_hours=4.5,
                    start_soc=83.0,
                    end_soc=82.5,
                    drain_soc=0.5,
                    drain_kwh=0.68,
                    rate_pct_per_day=2.67,
                    avg_watts=150.0,
                )
            ]
        )
        await store.async_save_dcfc_sessions(
            [
                ChargingSessionRecord(
                    session_id="purity_dcfc_1",
                    start_time="2026-08-20T13:00:00Z",
                    end_time="2026-08-20T13:30:00Z",
                    start_soc=20.0,
                    end_soc=80.0,
                    energy_added_kwh=81.0,
                    max_power_kw=180.0,
                    avg_power_kw=120.0,
                    samples=[
                        ChargingSample(
                            timestamp="2026-08-20T13:00:00Z", soc=20.0, power_kw=180.0
                        )
                    ],
                )
            ]
        )
        real_db.close()

        # ...then swap in a poisoned database that raises on every method, and
        # rebuild the DriveStore's cache manually (bypassing async_refresh_cache,
        # which itself would call the poisoned build_cache).
        poisoned_db = _PoisonedAnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        store._db = poisoned_db

        # Exercise every documented sync, cache-only accessor.
        assert store.last_drive is not None
        assert store.last_drive.drive_id == "purity_1"
        assert store.drive_count == 1
        assert isinstance(store.recent_drives, list) and len(store.recent_drives) == 1
        assert (
            isinstance(store.recent_vampire_events, list)
            and len(store.recent_vampire_events) == 1
        )
        assert store.revision > 0
        assert store.is_loaded is True
        dcfc_sessions = store.get_dcfc_sessions(limit=50)
        assert len(dcfc_sessions) == 1
        for stats_getter in (
            store.get_stats_30d,
            store.get_stats_90d,
            store.get_stats_365d,
            store.get_stats_all_time,
        ):
            stats = stats_getter()
            assert stats is not None

        # Exercise the sensor entity properties built on top of this store;
        # neither native_value nor extra_state_attributes may touch SQLite.
        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=None)
        vehicle = {
            "id": "veh-1",
            "vin": TEST_VIN,
            "name": "r1s_adventure",
            "model": "R1S",
        }
        tracker = _FakeTracker()
        for desc in DRIVE_SENSORS:
            entity = RivianDriveSensorEntity(
                coordinator=coordinator,
                config_entry=MagicMock(),
                description=desc,
                vehicle=vehicle,
                tracker=tracker,  # type: ignore[arg-type]
                store=store,
            )
            # Must not raise -- if any property fell through to sqlite, the
            # poisoned method's AssertionError would propagate here.
            _ = entity.native_value
            _ = entity.extra_state_attributes


class TestSpeedBinTotals:
    """Speed-bin totals cover the whole storage window, not the 90-day chart window."""

    def test_totals_sum_every_retained_non_micro_drive(self, analytics_db: Any) -> None:
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)

        def drive(
            n: int, days_ago: int, miles: float, micro: bool = False
        ) -> DriveRecord:
            start = (now - timedelta(days=days_ago)).isoformat()
            record = _make_drive(
                f"d{n}", miles, max(miles / 3, 0.1), start, start, is_micro_drive=micro
            )
            record.speed_bins = {"30-39": SpeedBinData(miles=miles, seconds=miles * 90)}
            return record

        analytics_db.upsert_drives(
            TEST_VIN,
            [drive(1, 5, 4.0), drive(2, 200, 6.0), drive(3, 1, 0.3, micro=True)],
        )
        cache = analytics_db.build_cache(TEST_VIN, now.timestamp())

        assert list(cache.speed_bin_totals) == list(STANDARD_SPEED_BINS)
        # The 200-day-old drive counts; the micro-drive doesn't.
        assert cache.speed_bin_totals["30-39"] == {"miles": 10.0, "seconds": 900.0}
        assert cache.speed_bin_totals["0-9"] == {"miles": 0.0, "seconds": 0.0}
        # The 90-day chart window is unaffected.
        assert [d.drive_id for d in cache.recent_drives] == ["d1"]


class TestSchemaMigrationV1ToV2:
    """Schema upgrades (v2 track tables, v3 vehicle pictures) via both paths."""

    def test_fresh_db_creates_v2_schema_directly(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """A brand-new database runs only `_SCHEMA_SQL` and lands on v2 directly."""
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            with db._lock:
                tables = {
                    row[0]
                    for row in db._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                user_version = db._conn.execute("PRAGMA user_version").fetchone()[0]
            assert {
                "drive_tracks",
                "active_drive",
                "active_track_chunks",
                "vehicle_pictures",
                "road_heat",
                "road_heat_drives",
            } <= tables
            assert user_version == SCHEMA_VERSION == 14
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        finally:
            db.close()

    def test_migrating_v1_db_adds_track_tables_and_preserves_data(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """Opening a v1 database upgrades it to v2 without losing existing rows."""
        raw = sqlite3.connect(analytics_db_path)
        try:
            raw.executescript(_V1_SCHEMA_SQL)
            raw.execute(
                "INSERT INTO drives (vin, drive_id, start_time, end_time, "
                "start_ts, end_ts, created_ts, distance_miles, duration_seconds, "
                "energy_kwh) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    TEST_VIN,
                    "pre_migration_1",
                    "2026-08-01T10:00:00Z",
                    "2026-08-01T10:20:00Z",
                    1754042400.0,
                    1754043600.0,
                    time.time(),
                    12.0,
                    1200.0,
                    4.0,
                ),
            )
            raw.execute("PRAGMA user_version = 1")
            raw.commit()
        finally:
            raw.close()

        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            with db._lock:
                tables = {
                    row[0]
                    for row in db._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                user_version = db._conn.execute("PRAGMA user_version").fetchone()[0]
                drive_row = db._conn.execute(
                    "SELECT drive_id, distance_miles FROM drives WHERE vin = ?",
                    (TEST_VIN,),
                ).fetchone()
            assert {
                "drive_tracks",
                "active_drive",
                "active_track_chunks",
                "vehicle_pictures",
                "road_heat",
                "road_heat_drives",
            } <= tables
            assert user_version == SCHEMA_VERSION
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
            assert drive_row["drive_id"] == "pre_migration_1"
            assert drive_row["distance_miles"] == 12.0
        finally:
            db.close()

    def test_migrating_v3_db_renames_segments_json_and_preserves_chunks(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """Opening a v3 database renames segments_json to chunks_json in place."""
        chunk_payload = json.dumps(
            [
                {
                    "start_time": "2026-08-01T10:00:00Z",
                    "duration_seconds": 180.0,
                    "distance_miles": 2.0,
                    "energy_kwh": 0.7,
                    "efficiency_mi_kwh": 2.86,
                    "mpge": 96.4,
                    "avg_speed_mph": 40.0,
                    "speed_bin": "40-49",
                    "elevation_change_ft": 12.0,
                    "temp_f": 55.0,
                }
            ]
        )
        raw = sqlite3.connect(analytics_db_path)
        try:
            raw.executescript(_V3_SCHEMA_SQL)
            raw.execute(
                "INSERT INTO drives (vin, drive_id, start_time, end_time, "
                "start_ts, end_ts, created_ts, distance_miles, duration_seconds, "
                "energy_kwh, segments_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    TEST_VIN,
                    "pre_migration_v3",
                    "2026-08-01T10:00:00Z",
                    "2026-08-01T10:20:00Z",
                    1754042400.0,
                    1754043600.0,
                    time.time(),
                    12.0,
                    1200.0,
                    4.0,
                    chunk_payload,
                ),
            )
            raw.execute("PRAGMA user_version = 3")
            raw.commit()
        finally:
            raw.close()

        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            with db._lock:
                columns = {
                    row[1]
                    for row in db._conn.execute("PRAGMA table_info(drives)").fetchall()
                }
                user_version = db._conn.execute("PRAGMA user_version").fetchone()[0]
            assert "chunks_json" in columns
            assert "segments_json" not in columns
            assert user_version == SCHEMA_VERSION == 14

            record = db.list_drives(TEST_VIN, include_micro=True)
            assert [d["drive_id"] for d in record] == ["pre_migration_v3"]

            detail = db.get_drive_detail(TEST_VIN, "pre_migration_v3")
            assert detail is not None
            chunks = detail["drive"]["chunks"]
            assert len(chunks) == 1
            assert chunks[0]["distance_miles"] == pytest.approx(2.0)
            assert chunks[0]["speed_bin"] == "40-49"
        finally:
            db.close()

    def test_migrating_v4_db_adds_road_heat_tables_and_preserves_data(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """Opening a v4 database adds road_heat/road_heat_drives without losing data."""
        raw = sqlite3.connect(analytics_db_path)
        try:
            raw.executescript(_V4_SCHEMA_SQL)
            raw.execute(
                "INSERT INTO drives (vin, drive_id, start_time, end_time, "
                "start_ts, end_ts, created_ts, distance_miles, duration_seconds, "
                "energy_kwh) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    TEST_VIN,
                    "pre_migration_v4",
                    "2026-08-01T10:00:00Z",
                    "2026-08-01T10:20:00Z",
                    1754042400.0,
                    1754043600.0,
                    time.time(),
                    12.0,
                    1200.0,
                    4.0,
                ),
            )
            raw.execute("PRAGMA user_version = 4")
            raw.commit()
        finally:
            raw.close()

        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            with db._lock:
                tables = {
                    row[0]
                    for row in db._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                user_version = db._conn.execute("PRAGMA user_version").fetchone()[0]
                drive_row = db._conn.execute(
                    "SELECT drive_id, distance_miles FROM drives WHERE vin = ?",
                    (TEST_VIN,),
                ).fetchone()
            assert {"road_heat", "road_heat_drives"} <= tables
            assert user_version == SCHEMA_VERSION == 14
            assert drive_row["drive_id"] == "pre_migration_v4"
            assert drive_row["distance_miles"] == 12.0
        finally:
            db.close()

    def test_migrating_v5_db_adds_stats_and_context_columns_and_preserves_data(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """Opening a v5 database adds the v6 columns as NULL, keeping old rows."""
        raw = sqlite3.connect(analytics_db_path)
        try:
            raw.executescript(_V5_SCHEMA_SQL)
            raw.execute(
                "INSERT INTO drives (vin, drive_id, start_time, end_time, "
                "start_ts, end_ts, created_ts, distance_miles, duration_seconds, "
                "energy_kwh) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    TEST_VIN,
                    "pre_migration_v5",
                    "2026-08-01T10:00:00Z",
                    "2026-08-01T10:20:00Z",
                    1754042400.0,
                    1754043600.0,
                    time.time(),
                    12.0,
                    1200.0,
                    4.0,
                ),
            )
            raw.execute("PRAGMA user_version = 5")
            raw.commit()
        finally:
            raw.close()

        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            with db._lock:
                columns = {
                    row[1]
                    for row in db._conn.execute("PRAGMA table_info(drives)").fetchall()
                }
                user_version = db._conn.execute("PRAGMA user_version").fetchone()[0]
                drive_row = db._conn.execute(
                    "SELECT drive_id, distance_miles, moving_seconds, drive_modes_json, "
                    "trailer, driver FROM drives WHERE vin = ?",
                    (TEST_VIN,),
                ).fetchone()
            assert {
                "moving_seconds",
                "stopped_seconds",
                "stop_count",
                "climb_ft",
                "descent_ft",
                "track_max_speed_mph",
                "pct_distance_over_70mph",
                "start_range_mi",
                "end_range_mi",
                "drive_modes_json",
                "trailer",
                "driver",
            } <= columns
            assert user_version == SCHEMA_VERSION == 14
            assert drive_row["drive_id"] == "pre_migration_v5"
            assert drive_row["distance_miles"] == 12.0
            assert drive_row["moving_seconds"] is None
            assert drive_row["drive_modes_json"] is None
            assert drive_row["trailer"] is None
            assert drive_row["driver"] is None

            # The read path tolerates NULL drive_modes_json (old row) as [].
            summary = db.list_drives(TEST_VIN, include_micro=True)
            assert summary[0]["drive_modes"] == []
            assert summary[0]["trailer"] is None
            assert summary[0]["range_used_mi"] is None
        finally:
            db.close()

    def test_migrating_v6_db_adds_osm_tables_and_gaps_scanned_column(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """Opening a v6 database adds the v7 osm_roads/track_fills tables and column."""
        raw = sqlite3.connect(analytics_db_path)
        try:
            raw.executescript(_V6_SCHEMA_SQL)
            raw.execute(
                "INSERT INTO drive_tracks (vin, drive_id, sort_ts, point_count, "
                "track_json, preview_json, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (TEST_VIN, "pre_migration_v6", 1754042400.0, 2, "{}", "{}", 1.0, 1.0),
            )
            raw.execute("PRAGMA user_version = 6")
            raw.commit()
        finally:
            raw.close()

        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            with db._lock:
                tables = {
                    row[0]
                    for row in db._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                track_columns = {
                    row[1]
                    for row in db._conn.execute(
                        "PRAGMA table_info(drive_tracks)"
                    ).fetchall()
                }
                user_version = db._conn.execute("PRAGMA user_version").fetchone()[0]
                track_row = db._conn.execute(
                    "SELECT drive_id, gaps_scanned FROM drive_tracks WHERE vin = ?",
                    (TEST_VIN,),
                ).fetchone()
            assert {"osm_roads", "track_fills"} <= tables
            assert "gaps_scanned" in track_columns
            assert user_version == SCHEMA_VERSION == 14
            assert track_row["drive_id"] == "pre_migration_v6"
            assert track_row["gaps_scanned"] == 0
        finally:
            db.close()

    def test_migrating_v7_db_adds_places_table_and_drive_place_columns(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """Opening a v7 database adds the v8 places table and drive place-id columns."""
        raw = sqlite3.connect(analytics_db_path)
        try:
            raw.executescript(_V6_SCHEMA_SQL)
            raw.execute(
                "CREATE TABLE IF NOT EXISTS osm_roads (bbox_key TEXT PRIMARY KEY, "
                "fetched_ts REAL NOT NULL, data BLOB NOT NULL)"
            )
            raw.execute(
                "CREATE TABLE IF NOT EXISTS track_fills (vin TEXT NOT NULL, "
                "drive_id TEXT NOT NULL, after_t REAL NOT NULL, points_json TEXT "
                "NOT NULL, source TEXT NOT NULL DEFAULT 'osm', created_ts REAL "
                "NOT NULL, PRIMARY KEY (vin, drive_id, after_t))"
            )
            raw.execute(
                "ALTER TABLE drive_tracks ADD COLUMN gaps_scanned INTEGER "
                "NOT NULL DEFAULT 0"
            )
            raw.execute(
                "INSERT INTO drives (vin, drive_id, start_time, end_time, "
                "distance_miles, duration_seconds, energy_kwh, created_ts, "
                "start_lat, start_lon, end_lat, end_lon) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    TEST_VIN,
                    "pre_migration_v7",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:10:00Z",
                    5.0,
                    600.0,
                    2.0,
                    time.time(),
                    37.0,
                    -122.0,
                    37.1,
                    -122.1,
                ),
            )
            raw.execute("PRAGMA user_version = 7")
            raw.commit()
        finally:
            raw.close()

        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            with db._lock:
                tables = {
                    row[0]
                    for row in db._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                drive_columns = {
                    row[1]
                    for row in db._conn.execute("PRAGMA table_info(drives)").fetchall()
                }
                user_version = db._conn.execute("PRAGMA user_version").fetchone()[0]
                drive_row = db._conn.execute(
                    "SELECT drive_id, start_place_id, end_place_id FROM drives "
                    "WHERE vin = ?",
                    (TEST_VIN,),
                ).fetchone()
            assert "places" in tables
            assert {"start_place_id", "end_place_id"} <= drive_columns
            assert user_version == SCHEMA_VERSION == 14
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
            assert drive_row["drive_id"] == "pre_migration_v7"
            assert drive_row["start_place_id"] is None
            assert drive_row["end_place_id"] is None

            # The existing drive's distance/coords survived the migration.
            summary = db.list_drives(TEST_VIN, include_micro=True)
            assert summary[0]["distance_miles"] == 5.0
            assert summary[0]["start_lat"] == 37.0
        finally:
            db.close()


class TestUpsertTracks:
    """upsert_tracks: writes, short-track skip, and live-vs-backfill precedence."""

    def test_writes_and_skips_short_tracks(self, analytics_db: Any) -> None:
        analytics_db.upsert_drives(TEST_VIN, [_make_drive("d1", 5.0, 2.0, None, None)])
        good_track = _make_track(n=5)
        short_track = DriveTrack([TrackPoint(t=1.0, lat=1.0, lon=1.0)])

        written = analytics_db.upsert_tracks(
            TEST_VIN, [("d1", good_track), ("d2", short_track)]
        )
        assert written == 1
        assert analytics_db.get_track(TEST_VIN, "d1") is not None
        assert analytics_db.get_track(TEST_VIN, "d2") is None

    def test_backfill_does_not_replace_live(self, analytics_db: Any) -> None:
        live_track = _make_track(n=5, lat0=10.0)
        analytics_db.upsert_tracks(TEST_VIN, [("d1", live_track)], source="live")

        backfill_track = _make_track(n=8, lat0=99.0)
        written = analytics_db.upsert_tracks(
            TEST_VIN, [("d1", backfill_track)], source="backfill"
        )
        assert written == 0
        stored = analytics_db.get_track(TEST_VIN, "d1")
        assert len(stored) == 5
        assert stored.points[0].lat == pytest.approx(10.0)

    def test_live_replaces_backfill(self, analytics_db: Any) -> None:
        backfill_track = _make_track(n=4, lat0=20.0)
        analytics_db.upsert_tracks(
            TEST_VIN, [("d1", backfill_track)], source="backfill"
        )

        live_track = _make_track(n=7, lat0=50.0)
        written = analytics_db.upsert_tracks(
            TEST_VIN, [("d1", live_track)], source="live"
        )
        assert written == 1
        stored = analytics_db.get_track(TEST_VIN, "d1")
        assert len(stored) == 7
        assert stored.points[0].lat == pytest.approx(50.0)

    def test_sort_ts_uses_drive_row_when_present(self, analytics_db: Any) -> None:
        drive = _make_drive(
            "d1", 5.0, 2.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        track = _make_track(n=3, start_t=1_600_000_000.0)
        analytics_db.upsert_tracks(TEST_VIN, [("d1", track)])

        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT sort_ts FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "d1"),
            ).fetchone()
        expected = analytics_db._conn.execute(
            "SELECT sort_ts FROM drives WHERE vin = ? AND drive_id = ?",
            (TEST_VIN, "d1"),
        ).fetchone()["sort_ts"]
        assert row["sort_ts"] == expected
        assert row["sort_ts"] != track.points[0].t

    def test_sort_ts_uses_first_point_when_no_drive_row(
        self, analytics_db: Any
    ) -> None:
        track = _make_track(n=3, start_t=1_650_000_000.0)
        analytics_db.upsert_tracks(TEST_VIN, [("orphan_drive", track)])
        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT sort_ts FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "orphan_drive"),
            ).fetchone()
        assert row["sort_ts"] == track.points[0].t


class TestFinalizeDrive:
    """finalize_drive: one atomic transaction covering drive + track + checkpoint clear."""

    def test_finalize_writes_drive_and_track_and_clears_checkpoint(
        self, analytics_db: Any
    ) -> None:
        analytics_db.save_active_checkpoint(
            TEST_VIN, "d1", {"foo": "bar"}, _make_track(n=2), 0
        )
        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        track = _make_track(n=6)

        is_new = analytics_db.finalize_drive(TEST_VIN, record, track)
        assert is_new is True

        stored_track = analytics_db.get_track(TEST_VIN, "d1")
        assert stored_track is not None
        assert len(stored_track) == 6
        assert analytics_db.load_active_checkpoint(TEST_VIN) is None

        # A second finalize of the same drive_id is an update, not a new row.
        is_new_2 = analytics_db.finalize_drive(TEST_VIN, record, track)
        assert is_new_2 is False

    def test_finalize_atomic_on_track_encode_failure(
        self, analytics_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        analytics_db.save_active_checkpoint(
            TEST_VIN, "d1", {"foo": "bar"}, _make_track(n=2), 0
        )

        def _boom(self: DriveTrack) -> str:
            raise RuntimeError("encode boom")

        monkeypatch.setattr(DriveTrack, "encode", _boom)

        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        track = _make_track(n=6)
        with pytest.raises(RuntimeError, match="encode boom"):
            analytics_db.finalize_drive(TEST_VIN, record, track)

        with analytics_db._lock:
            drive_row = analytics_db._conn.execute(
                "SELECT 1 FROM drives WHERE vin = ? AND drive_id = ?", (TEST_VIN, "d1")
            ).fetchone()
        assert drive_row is None
        checkpoint = analytics_db.load_active_checkpoint(TEST_VIN)
        assert checkpoint is not None
        assert checkpoint.drive_id == "d1"


class TestActiveCheckpoint:
    """save/load/clear_active_checkpoint round-trip and drive-switch chunk cleanup."""

    def test_save_load_roundtrip_concatenates_chunks_in_order(
        self, analytics_db: Any
    ) -> None:
        chunk0 = _make_track(n=2, start_t=1_700_000_000.0)
        chunk1 = _make_track(n=2, start_t=1_700_000_100.0, lat0=40.0)
        chunk2 = _make_track(n=2, start_t=1_700_000_200.0, lat0=50.0)

        analytics_db.save_active_checkpoint(TEST_VIN, "d1", {"step": 0}, chunk0, 0)
        analytics_db.save_active_checkpoint(TEST_VIN, "d1", {"step": 1}, chunk1, 1)
        analytics_db.save_active_checkpoint(TEST_VIN, "d1", {"step": 2}, chunk2, 2)

        checkpoint = analytics_db.load_active_checkpoint(TEST_VIN)
        assert isinstance(checkpoint, ActiveCheckpoint)
        assert checkpoint.drive_id == "d1"
        assert checkpoint.state == {"step": 2}
        assert checkpoint.next_seq == 3
        assert len(checkpoint.track) == 6
        assert [p.lat for p in checkpoint.track.points[:2]] == [
            pytest.approx(chunk0.points[0].lat),
            pytest.approx(chunk0.points[1].lat),
        ]
        assert checkpoint.track.points[-1].lat == pytest.approx(chunk2.points[-1].lat)

        analytics_db.clear_active_checkpoint(TEST_VIN)
        assert analytics_db.load_active_checkpoint(TEST_VIN) is None

    def test_save_for_different_drive_wipes_old_chunks(self, analytics_db: Any) -> None:
        analytics_db.save_active_checkpoint(
            TEST_VIN, "d1", {"a": 1}, _make_track(n=2), 0
        )
        analytics_db.save_active_checkpoint(
            TEST_VIN, "d2", {"b": 2}, _make_track(n=3, lat0=60.0), 0
        )

        checkpoint = analytics_db.load_active_checkpoint(TEST_VIN)
        assert checkpoint.drive_id == "d2"
        assert len(checkpoint.track) == 3

        with analytics_db._lock:
            old_chunks = analytics_db._conn.execute(
                "SELECT COUNT(*) AS c FROM active_track_chunks WHERE vin = ? "
                "AND drive_id = ?",
                (TEST_VIN, "d1"),
            ).fetchone()["c"]
        assert old_chunks == 0

    def test_load_returns_none_when_absent(self, analytics_db: Any) -> None:
        assert analytics_db.load_active_checkpoint(TEST_VIN) is None


class TestListDrivesAndDetail:
    """list_drives ordering/paging and get_drive_detail payload shape."""

    def test_list_drives_ordering_paging_and_fields(self, analytics_db: Any) -> None:
        base = datetime(2026, 9, 1, tzinfo=timezone.utc)
        drives = [
            _make_drive(
                f"d{i}",
                10.0,
                4.0,
                (base + timedelta(hours=i)).isoformat(),
                (base + timedelta(hours=i)).isoformat(),
            )
            for i in range(5)
        ]
        analytics_db.upsert_drives(TEST_VIN, drives)
        analytics_db.upsert_tracks(TEST_VIN, [("d3", _make_track(n=4))])

        page1 = analytics_db.list_drives(TEST_VIN, limit=3)
        assert [d["drive_id"] for d in page1] == ["d4", "d3", "d2"]
        assert page1[0]["has_track"] is False
        d3_summary = page1[1]
        assert d3_summary["has_track"] is True
        assert d3_summary["track_source"] == "live"
        assert d3_summary["track_detail"] == "full"
        assert d3_summary["point_count"] == 4

        expected_keys = {
            "drive_id",
            "start_time",
            "end_time",
            "start_ts",
            "end_ts",
            "distance_miles",
            "duration_seconds",
            "energy_kwh",
            "efficiency_mi_kwh",
            "mpge",
            "avg_speed_mph",
            "max_speed_mph",
            "temp_f",
            "elevation_change_ft",
            "start_soc",
            "end_soc",
            "is_micro_drive",
            "start_lat",
            "start_lon",
            "end_lat",
            "end_lon",
            "has_track",
            "track_source",
            "track_detail",
            "point_count",
            "sort_ts",
            "moving_seconds",
            "stopped_seconds",
            "stop_count",
            "climb_ft",
            "descent_ft",
            "track_max_speed_mph",
            "pct_distance_over_70mph",
            "start_range_mi",
            "end_range_mi",
            "range_used_mi",
            "drive_modes",
            "trailer",
            "driver",
        }
        assert set(page1[0]) == expected_keys

        page2 = analytics_db.list_drives(
            TEST_VIN, before_ts=page1[-1]["sort_ts"], limit=3
        )
        assert [d["drive_id"] for d in page2] == ["d1", "d0"]

    def test_list_drives_include_micro(self, analytics_db: Any) -> None:
        normal = _make_drive(
            "normal", 10.0, 4.0, "2026-08-20T10:00:00Z", "2026-08-20T10:20:00Z"
        )
        micro = _make_drive(
            "micro",
            0.2,
            0.1,
            "2026-08-20T11:00:00Z",
            "2026-08-20T11:05:00Z",
            is_micro_drive=True,
        )
        analytics_db.upsert_drives(TEST_VIN, [normal, micro])

        default_result = analytics_db.list_drives(TEST_VIN)
        assert [d["drive_id"] for d in default_result] == ["normal"]

        with_micro = analytics_db.list_drives(TEST_VIN, include_micro=True)
        assert {d["drive_id"] for d in with_micro} == {"normal", "micro"}

    def test_get_drive_detail_with_track(self, analytics_db: Any) -> None:
        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        track = _make_track(n=4)
        analytics_db.finalize_drive(TEST_VIN, record, track)

        detail = analytics_db.get_drive_detail(TEST_VIN, "d1")
        assert detail is not None
        assert detail["drive"]["drive_id"] == "d1"
        assert "speed_bins" in detail["drive"]
        assert "chunks" in detail["drive"]
        assert detail["track"] is not None
        assert len(detail["track"]["lat"]) == 4

    def test_get_drive_detail_without_track(self, analytics_db: Any) -> None:
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive(
                    "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
                )
            ],
        )
        detail = analytics_db.get_drive_detail(TEST_VIN, "d1")
        assert detail is not None
        assert detail["track"] is None

    def test_get_drive_detail_unknown_returns_none(self, analytics_db: Any) -> None:
        assert analytics_db.get_drive_detail(TEST_VIN, "does-not-exist") is None


class TestTrackFillsMerge:
    """Gap fills merge into day()/get_drive_detail() payloads with a `filled` column."""

    def test_get_drive_detail_merges_fill_in_time_order(
        self, analytics_db: Any
    ) -> None:
        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        track = _make_track(n=3, start_t=1_700_000_000.0, step=0.001)
        analytics_db.finalize_drive(TEST_VIN, record, track)
        # A fill point landing strictly between the track's 2nd and 3rd points.
        fill_point = TrackPoint(t=1_700_000_015.0, lat=37.0015, lon=-121.9985)
        analytics_db.save_track_fill(
            TEST_VIN, "d1", after_t=track.points[1].t, points=[fill_point]
        )

        detail = analytics_db.get_drive_detail(TEST_VIN, "d1")
        payload = detail["track"]
        assert payload["t"] == sorted(payload["t"])
        assert payload["filled"] == [False, False, True, False]
        assert payload["t"].index(fill_point.t) == 2

    def test_day_merges_fill_in_time_order(self, analytics_db: Any) -> None:
        d1_start = datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO)
        record = _make_drive("d1", 8.0, 3.0, d1_start.isoformat(), d1_start.isoformat())
        track = _make_track(n=3, start_t=d1_start.timestamp(), step=0.001)
        analytics_db.finalize_drive(TEST_VIN, record, track)
        fill_point = TrackPoint(t=track.points[0].t + 5.0, lat=37.0005, lon=-121.9995)
        analytics_db.save_track_fill(
            TEST_VIN, "d1", after_t=track.points[0].t, points=[fill_point]
        )

        day_payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))
        track_payload = day_payload["segments"][0]["track"]
        assert track_payload["filled"] == [False, True, False, False]

    def test_track_without_fills_has_no_filled_key(self, analytics_db: Any) -> None:
        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        analytics_db.finalize_drive(TEST_VIN, record, _make_track(n=3))
        detail = analytics_db.get_drive_detail(TEST_VIN, "d1")
        assert "filled" not in detail["track"]

    def test_none_source_fill_is_not_merged(self, analytics_db: Any) -> None:
        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        track = _make_track(n=3)
        analytics_db.finalize_drive(TEST_VIN, record, track)
        analytics_db.save_track_fill(
            TEST_VIN, "d1", after_t=track.points[0].t, points=[], source="none"
        )
        detail = analytics_db.get_drive_detail(TEST_VIN, "d1")
        assert "filled" not in detail["track"]


class TestRoadSnapStorage:
    """gaps_to_snap, the OSM road cache, track_fills, and their cascading deletes."""

    def test_gaps_to_snap_returns_unresolved_gaps_then_marks_scanned(
        self, analytics_db: Any
    ) -> None:
        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        track = DriveTrack()
        track.append(TrackPoint(t=1_700_000_000.0, lat=37.0, lon=-122.0))
        track.append(
            TrackPoint(t=1_700_000_100.0, lat=37.01, lon=-122.0)
        )  # ~1.1km/100s
        analytics_db.finalize_drive(TEST_VIN, record, track)

        pending = analytics_db.gaps_to_snap(TEST_VIN, limit=10)
        assert len(pending) == 1
        drive_id, gap = pending[0]
        assert drive_id == "d1"

        # Still unresolved (no track_fills row yet): a second call returns it again.
        assert len(analytics_db.gaps_to_snap(TEST_VIN, limit=10)) == 1

        analytics_db.save_track_fill(TEST_VIN, drive_id, gap.start.t, [], "none")
        # Now resolved: the drive is marked scanned and no longer returned.
        assert analytics_db.gaps_to_snap(TEST_VIN, limit=10) == []
        with analytics_db._lock:
            scanned = analytics_db._conn.execute(
                "SELECT gaps_scanned FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "d1"),
            ).fetchone()["gaps_scanned"]
        assert scanned == 1

    def test_gapless_track_is_marked_scanned_without_any_fill_rows(
        self, analytics_db: Any
    ) -> None:
        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        analytics_db.finalize_drive(TEST_VIN, record, _make_track(n=3))
        assert analytics_db.gaps_to_snap(TEST_VIN, limit=10) == []
        assert analytics_db.get_track_fills(TEST_VIN, "d1") == []

    def test_replacing_a_track_resets_gaps_scanned(self, analytics_db: Any) -> None:
        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        analytics_db.finalize_drive(TEST_VIN, record, _make_track(n=3))
        analytics_db.gaps_to_snap(TEST_VIN, limit=10)  # marks it scanned
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=5))], source="live")
        with analytics_db._lock:
            scanned = analytics_db._conn.execute(
                "SELECT gaps_scanned FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "d1"),
            ).fetchone()["gaps_scanned"]
        assert scanned == 0

    def test_cached_roads_roundtrip_and_ttl_expiry(self, analytics_db: Any) -> None:
        ways = [road_snap.Way(nodes=[(37.0, -122.0), (37.001, -122.0)], oneway=False)]
        analytics_db.save_cached_roads("k1", ways)
        cached = analytics_db.get_cached_roads("k1")
        assert cached is not None
        assert cached[0].nodes == ways[0].nodes

        # Expire it by rewinding fetched_ts past the TTL.
        with analytics_db._lock:
            analytics_db._conn.execute(
                "UPDATE osm_roads SET fetched_ts = 0 WHERE bbox_key = 'k1'"
            )
        assert analytics_db.get_cached_roads("k1") is None

    def test_get_cached_roads_missing_key_returns_none(self, analytics_db: Any) -> None:
        assert analytics_db.get_cached_roads("does-not-exist") is None

    def test_delete_vin_removes_track_fills(self, analytics_db: Any) -> None:
        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        track = _make_track(n=3)
        analytics_db.finalize_drive(TEST_VIN, record, track)
        analytics_db.save_track_fill(TEST_VIN, "d1", track.points[0].t, [], "none")
        analytics_db.delete_vin(TEST_VIN)
        assert analytics_db.get_track_fills(TEST_VIN, "d1") == []

    def test_prune_tracks_removes_fills_of_deleted_tracks(
        self, analytics_db: Any
    ) -> None:
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        record = _make_drive(
            "old", 8.0, 3.0, old_time.isoformat(), old_time.isoformat()
        )
        track = _make_track(n=3, start_t=old_time.timestamp())
        analytics_db.finalize_drive(TEST_VIN, record, track)
        analytics_db.save_track_fill(TEST_VIN, "old", track.points[0].t, [], "none")

        cutoff = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
        analytics_db.prune_tracks(
            TEST_VIN, delete_before_ts=cutoff, thin_before_ts=None
        )

        assert analytics_db.get_track_fills(TEST_VIN, "old") == []


CHICAGO = ZoneInfo("America/Chicago")


class TestCalendar:
    """AnalyticsDatabase.calendar(): the All time -> years -> months -> days tree."""

    def test_groups_by_local_calendar_day_across_a_1h_gap(
        self, analytics_db: Any
    ) -> None:
        """23:30 and 00:30 local, 1 hour apart, land on different calendar days."""
        late = datetime(2026, 9, 23, 23, 30, tzinfo=CHICAGO)
        early = datetime(2026, 9, 24, 0, 30, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive("late", 10.0, 4.0, late.isoformat(), late.isoformat()),
                _make_drive("early", 12.0, 5.0, early.isoformat(), early.isoformat()),
            ],
        )
        cal = analytics_db.calendar(TEST_VIN, CHICAGO, year=2026, month=9)
        days = {d["key"]: d for d in cal["days"]}
        assert days["2026-09-23"]["drives"] == 1
        assert days["2026-09-24"]["drives"] == 1

    def test_drive_crossing_midnight_belongs_to_start_day(
        self, analytics_db: Any
    ) -> None:
        start = datetime(2026, 9, 23, 23, 50, tzinfo=CHICAGO)
        end = datetime(2026, 9, 24, 0, 20, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [_make_drive("d1", 5.0, 2.0, start.isoformat(), end.isoformat())],
        )
        cal = analytics_db.calendar(TEST_VIN, CHICAGO, year=2026, month=9)
        assert [d["key"] for d in cal["days"]] == ["2026-09-23"]

    def test_dst_spring_forward_day_groups_both_times(self, analytics_db: Any) -> None:
        """2026-03-08 (spring forward): 00:30 and 23:30 local both land on it."""
        early = datetime(2026, 3, 8, 0, 30, tzinfo=CHICAGO)
        late = datetime(2026, 3, 8, 23, 30, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive("a", 3.0, 1.0, early.isoformat(), early.isoformat()),
                _make_drive("b", 4.0, 1.5, late.isoformat(), late.isoformat()),
            ],
        )
        cal = analytics_db.calendar(TEST_VIN, CHICAGO, year=2026, month=3)
        assert [d["key"] for d in cal["days"]] == ["2026-03-08"]
        assert cal["days"][0]["drives"] == 2

    def test_dst_fall_back_day_groups_both_times(self, analytics_db: Any) -> None:
        """2026-11-01 (fall back): 00:30 and 23:30 local both land on it."""
        early = datetime(2026, 11, 1, 0, 30, tzinfo=CHICAGO)
        late = datetime(2026, 11, 1, 23, 30, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive("a", 3.0, 1.0, early.isoformat(), early.isoformat()),
                _make_drive("b", 4.0, 1.5, late.isoformat(), late.isoformat()),
            ],
        )
        cal = analytics_db.calendar(TEST_VIN, CHICAGO, year=2026, month=11)
        assert [d["key"] for d in cal["days"]] == ["2026-11-01"]
        assert cal["days"][0]["drives"] == 2

    def test_year_month_day_aggregates_equal_sum_of_drives(
        self, analytics_db: Any
    ) -> None:
        base = datetime(2026, 9, 1, 12, 0, tzinfo=CHICAGO)
        drives = [
            _make_drive(
                f"d{i}",
                10.0 + i,
                4.0 + i,
                (base + timedelta(days=i)).isoformat(),
                (base + timedelta(days=i)).isoformat(),
            )
            for i in range(3)
        ]
        analytics_db.upsert_drives(TEST_VIN, drives)
        cal = analytics_db.calendar(TEST_VIN, CHICAGO, year=2026, month=9)

        assert cal["totals"]["drives"] == 3
        assert cal["totals"]["miles"] == round(sum(10.0 + i for i in range(3)), 1)
        assert cal["years"][0]["drives"] == 3
        assert cal["months"][0]["drives"] == 3
        assert sum(d["drives"] for d in cal["days"]) == 3
        assert sum(d["miles"] for d in cal["days"]) == cal["months"][0]["miles"]

    def test_months_and_days_ordered_newest_first(self, analytics_db: Any) -> None:
        drives = []
        for month in (6, 7, 9):
            dt_ = datetime(2026, month, 5, 10, 0, tzinfo=CHICAGO)
            drives.append(
                _make_drive(f"m{month}", 5.0, 2.0, dt_.isoformat(), dt_.isoformat())
            )
        for day in (2, 15, 20):
            dt_ = datetime(2026, 9, day, 10, 0, tzinfo=CHICAGO)
            drives.append(
                _make_drive(f"d{day}", 5.0, 2.0, dt_.isoformat(), dt_.isoformat())
            )
        analytics_db.upsert_drives(TEST_VIN, drives)

        cal_year = analytics_db.calendar(TEST_VIN, CHICAGO, year=2026)
        assert [m["key"] for m in cal_year["months"]] == [
            "2026-09",
            "2026-07",
            "2026-06",
        ]

        cal_month = analytics_db.calendar(TEST_VIN, CHICAGO, year=2026, month=9)
        assert [d["key"] for d in cal_month["days"]] == [
            "2026-09-20",
            "2026-09-15",
            "2026-09-05",
            "2026-09-02",
        ]

    def test_micro_drives_excluded_unless_requested(self, analytics_db: Any) -> None:
        dt_ = datetime(2026, 9, 10, 10, 0, tzinfo=CHICAGO)
        normal = _make_drive("normal", 5.0, 2.0, dt_.isoformat(), dt_.isoformat())
        micro = _make_drive(
            "micro", 0.2, 0.1, dt_.isoformat(), dt_.isoformat(), is_micro_drive=True
        )
        analytics_db.upsert_drives(TEST_VIN, [normal, micro])

        default_cal = analytics_db.calendar(TEST_VIN, CHICAGO, year=2026, month=9)
        assert default_cal["totals"]["drives"] == 1

        with_micro_cal = analytics_db.calendar(
            TEST_VIN, CHICAGO, year=2026, month=9, include_micro=True
        )
        assert with_micro_cal["totals"]["drives"] == 2

    def test_with_route_counts_only_drives_with_a_stored_track(
        self, analytics_db: Any
    ) -> None:
        dt1 = datetime(2026, 9, 10, 9, 0, tzinfo=CHICAGO)
        dt2 = datetime(2026, 9, 10, 15, 0, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive("has_track", 5.0, 2.0, dt1.isoformat(), dt1.isoformat()),
                _make_drive("no_track", 6.0, 2.5, dt2.isoformat(), dt2.isoformat()),
            ],
        )
        analytics_db.upsert_tracks(TEST_VIN, [("has_track", _make_track(n=3))])

        cal = analytics_db.calendar(TEST_VIN, CHICAGO, year=2026, month=9)
        assert cal["totals"]["with_route"] == 1
        assert cal["days"][0]["with_route"] == 1

    def test_month_without_year_raises(self, analytics_db: Any) -> None:
        with pytest.raises(ValueError, match="year"):
            analytics_db.calendar(TEST_VIN, CHICAGO, month=9)

    def test_months_and_days_omitted_when_not_requested(
        self, analytics_db: Any
    ) -> None:
        dt_ = datetime(2026, 9, 10, 10, 0, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN, [_make_drive("d1", 5.0, 2.0, dt_.isoformat(), dt_.isoformat())]
        )
        totals_only = analytics_db.calendar(TEST_VIN, CHICAGO)
        assert "months" not in totals_only
        assert "days" not in totals_only

        year_only = analytics_db.calendar(TEST_VIN, CHICAGO, year=2026)
        assert "months" in year_only
        assert "days" not in year_only

    def test_efficiency_none_when_no_energy(self, analytics_db: Any) -> None:
        dt_ = datetime(2026, 9, 10, 10, 0, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN, [_make_drive("d1", 5.0, 0.0, dt_.isoformat(), dt_.isoformat())]
        )
        cal = analytics_db.calendar(TEST_VIN, CHICAGO, year=2026, month=9)
        assert cal["totals"]["efficiency_mi_kwh"] is None


class TestDay:
    """AnalyticsDatabase.day(): one local calendar day's segments, stops, endpoints."""

    def test_empty_day_shape(self, analytics_db: Any) -> None:
        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))
        assert payload["date"] == "2026-09-10"
        assert payload["segments"] == []
        assert payload["stops"] == []
        assert payload["start"] is None
        assert payload["end"] is None
        assert payload["totals"]["drives"] == 0
        assert payload["totals"]["efficiency_mi_kwh"] is None

    def test_segments_carry_chunks_and_battery_capacity_for_charts(
        self, analytics_db: Any
    ) -> None:
        d1_start = datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO)
        d2_start = datetime(2026, 9, 10, 12, 0, tzinfo=CHICAGO)
        with_chunks = _make_drive(
            "d1", 3.0, 1.0, d1_start.isoformat(), d1_start.isoformat()
        )
        with_chunks.battery_capacity_kwh = 135.0
        with_chunks.chunks = [
            DriveChunk(
                start_time=d1_start.isoformat(),
                duration_seconds=180.0,
                distance_miles=1.5,
                energy_kwh=0.6,
                efficiency_mi_kwh=2.5,
                avg_speed_mph=30.0,
                speed_bin="30-39",
            )
        ]
        without = _make_drive(
            "d2", 3.0, 1.0, d2_start.isoformat(), d2_start.isoformat()
        )
        analytics_db.upsert_drives(TEST_VIN, [with_chunks, without])

        segments = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))["segments"]

        assert segments[0]["battery_capacity_kwh"] == 135.0
        assert segments[0]["chunks"] == [
            {
                "start_ts": pytest.approx(d1_start.timestamp()),
                "duration_seconds": 180.0,
                "efficiency_mi_kwh": 2.5,
            }
        ]
        assert segments[1]["chunks"] == []

    def test_unreadable_route_does_not_hide_the_day(self, analytics_db: Any) -> None:
        d1_start = datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO)
        d2_start = datetime(2026, 9, 10, 12, 0, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive("d1", 3.0, 1.0, d1_start.isoformat(), d1_start.isoformat()),
                _make_drive("d2", 5.0, 2.0, d2_start.isoformat(), d2_start.isoformat()),
            ],
        )
        analytics_db.upsert_tracks(
            TEST_VIN, [("d1", _make_track(n=3)), ("d2", _make_track(n=3))]
        )
        with analytics_db._lock:
            analytics_db._conn.execute(
                "UPDATE drive_tracks SET track_json = 'not json' WHERE drive_id = 'd1'"
            )
            analytics_db._conn.commit()

        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))

        assert [s["drive_id"] for s in payload["segments"]] == ["d1", "d2"]
        assert payload["segments"][0]["track"] is None
        assert payload["segments"][1]["track"] is not None

    def test_segments_chronological_with_index_and_tracks(
        self, analytics_db: Any
    ) -> None:
        d1_start = datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO)
        d2_start = datetime(2026, 9, 10, 12, 0, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive("d2", 5.0, 2.0, d2_start.isoformat(), d2_start.isoformat()),
                _make_drive("d1", 3.0, 1.0, d1_start.isoformat(), d1_start.isoformat()),
            ],
        )
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=3))])

        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))
        segments = payload["segments"]
        assert [s["drive_id"] for s in segments] == ["d1", "d2"]
        assert [s["index"] for s in segments] == [0, 1]
        assert segments[0]["track"] is not None
        assert len(segments[0]["track"]["lat"]) == 3
        assert segments[1]["track"] is None
        assert payload["totals"]["drives"] == 2

    def test_stops_between_segments_track_and_fallback(self, analytics_db: Any) -> None:
        d1_start = datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO)
        d1_end = datetime(2026, 9, 10, 8, 30, tzinfo=CHICAGO)
        d2_start = datetime(2026, 9, 10, 9, 0, tzinfo=CHICAGO)
        d2_end = datetime(2026, 9, 10, 9, 30, tzinfo=CHICAGO)
        d3_start = datetime(2026, 9, 10, 10, 0, tzinfo=CHICAGO)
        d3_end = datetime(2026, 9, 10, 10, 30, tzinfo=CHICAGO)
        drive1 = _make_drive("d1", 3.0, 1.0, d1_start.isoformat(), d1_end.isoformat())
        drive1.start_lat, drive1.start_lon = 40.0, -105.0
        drive1.end_lat, drive1.end_lon = 40.5, -105.5
        drive2 = _make_drive("d2", 3.0, 1.0, d2_start.isoformat(), d2_end.isoformat())
        drive2.end_lat, drive2.end_lon = 41.0, -106.0
        drive3 = _make_drive("d3", 3.0, 1.0, d3_start.isoformat(), d3_end.isoformat())
        analytics_db.upsert_drives(TEST_VIN, [drive1, drive2, drive3])
        track1 = _make_track(n=3, lat0=42.0, lon0=-107.0)
        analytics_db.upsert_tracks(TEST_VIN, [("d1", track1)])

        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))
        stops = payload["stops"]
        assert len(stops) == 2

        # d1 has a track: the stop after it sits at the track's last point.
        stop0 = stops[0]
        assert stop0["after_index"] == 0
        assert stop0["lat"] == track1.points[-1].lat
        assert stop0["lon"] == track1.points[-1].lon
        assert stop0["arrive_ts"] == pytest.approx(d1_end.timestamp())
        assert stop0["depart_ts"] == pytest.approx(d2_start.timestamp())
        assert stop0["duration_seconds"] == pytest.approx(
            d2_start.timestamp() - d1_end.timestamp()
        )

        # d2 has no track: the stop after it falls back to end_lat/end_lon.
        stop1 = stops[1]
        assert stop1["after_index"] == 1
        assert stop1["lat"] == 41.0
        assert stop1["lon"] == -106.0

    @staticmethod
    def _drive_between(
        drive_id: str,
        start: datetime,
        start_pos: tuple[float, float],
        end_pos: tuple[float, float],
    ) -> DriveRecord:
        end = start + timedelta(minutes=20)
        drive = _make_drive(drive_id, 3.0, 1.0, start.isoformat(), end.isoformat())
        drive.start_lat, drive.start_lon = start_pos
        drive.end_lat, drive.end_lon = end_pos
        return drive

    def test_day_starts_where_the_car_was_parked_with_the_unrecorded_gap(
        self, analytics_db: Any
    ) -> None:
        """The car's first report arrives ~1.2 km from home: start at home anyway."""
        home, first_fix, work = (
            (39.6785, -104.9085),
            (39.6861, -104.9192),
            (39.7242, -104.9880),
        )
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                self._drive_between(
                    "prev", datetime(2026, 9, 9, 18, 0, tzinfo=CHICAGO), work, home
                ),
                self._drive_between(
                    "d1", datetime(2026, 9, 10, 7, 27, tzinfo=CHICAGO), first_fix, work
                ),
            ],
        )

        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))

        assert (payload["start"]["lat"], payload["start"]["lon"]) == home
        assert payload["gaps"] == [
            {
                "after_index": -1,
                "from": [home[0], home[1]],
                "to": [first_fix[0], first_fix[1]],
                "distance_m": pytest.approx(1246, abs=15),  # metres at ~39.7 N,
            }
        ]

    def test_no_gap_for_parking_drift_or_an_implausibly_long_jump(
        self, analytics_db: Any
    ) -> None:
        home = (39.6785, -104.9085)
        near_home = (39.6792, -104.9085)  # ~80 m: GPS drift at a parking spot
        far_away = (39.8242, -105.1880)  # ~27 km: straight line would mislead
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                self._drive_between(
                    "prev", datetime(2026, 9, 9, 18, 0, tzinfo=CHICAGO), far_away, home
                ),
                self._drive_between(
                    "d1",
                    datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO),
                    near_home,
                    far_away,
                ),
                self._drive_between(
                    "d2", datetime(2026, 9, 10, 12, 0, tzinfo=CHICAGO), home, home
                ),
            ],
        )

        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))

        assert payload["gaps"] == []
        assert (payload["start"]["lat"], payload["start"]["lon"]) == near_home

    def test_gap_between_segments_when_the_next_drive_resumes_elsewhere(
        self, analytics_db: Any
    ) -> None:
        a, b, c = (
            (39.6742, -104.9080),
            (39.7242, -104.9880),
            (39.7292, -104.9980),
        )  # b->c ~0.9 km
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                self._drive_between(
                    "d1", datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO), a, b
                ),
                self._drive_between(
                    "d2", datetime(2026, 9, 10, 12, 0, tzinfo=CHICAGO), c, a
                ),
            ],
        )

        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))

        assert [g["after_index"] for g in payload["gaps"]] == [0]
        assert payload["gaps"][0]["from"] == [b[0], b[1]]
        assert payload["gaps"][0]["to"] == [c[0], c[1]]
        assert payload["stops"][0]["lat"] == b[0]

    def test_start_end_from_track_points_and_lat_lon_fallback(
        self, analytics_db: Any
    ) -> None:
        d1_start = datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO)
        d2_start = datetime(2026, 9, 10, 12, 0, tzinfo=CHICAGO)
        drive1 = _make_drive("d1", 3.0, 1.0, d1_start.isoformat(), d1_start.isoformat())
        drive2 = _make_drive("d2", 3.0, 1.0, d2_start.isoformat(), d2_start.isoformat())
        drive2.end_lat, drive2.end_lon = 44.0, -108.0
        analytics_db.upsert_drives(TEST_VIN, [drive1, drive2])
        track1 = _make_track(n=3, lat0=50.0, lon0=-110.0)
        analytics_db.upsert_tracks(TEST_VIN, [("d1", track1)])

        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))
        assert payload["start"]["lat"] == track1.points[0].lat
        assert payload["start"]["lon"] == track1.points[0].lon
        assert payload["start"]["ts"] == track1.points[0].t
        # d2 (last segment) has no track: falls back to end_lat/end_lon/end_ts.
        assert payload["end"]["lat"] == 44.0
        assert payload["end"]["lon"] == -108.0

    def test_excludes_drive_from_adjacent_day(self, analytics_db: Any) -> None:
        in_day = datetime(2026, 9, 10, 12, 0, tzinfo=CHICAGO)
        next_day = datetime(2026, 9, 11, 0, 30, tzinfo=CHICAGO)
        prev_day = datetime(2026, 9, 9, 23, 30, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive("in", 5.0, 2.0, in_day.isoformat(), in_day.isoformat()),
                _make_drive(
                    "next", 5.0, 2.0, next_day.isoformat(), next_day.isoformat()
                ),
                _make_drive(
                    "prev", 5.0, 2.0, prev_day.isoformat(), prev_day.isoformat()
                ),
            ],
        )
        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))
        assert [s["drive_id"] for s in payload["segments"]] == ["in"]

    def test_fall_back_25h_day_includes_late_local_drive(
        self, analytics_db: Any
    ) -> None:
        """2026-11-01 fall-back day is 25h long; a 23:30 local drive is inside it."""
        late = datetime(2026, 11, 1, 23, 30, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [_make_drive("late", 5.0, 2.0, late.isoformat(), late.isoformat())],
        )
        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 11, 1))
        assert [s["drive_id"] for s in payload["segments"]] == ["late"]

    def test_micro_drives_excluded_unless_requested(self, analytics_db: Any) -> None:
        dt_ = datetime(2026, 9, 10, 10, 0, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive("normal", 5.0, 2.0, dt_.isoformat(), dt_.isoformat()),
                _make_drive(
                    "micro",
                    0.2,
                    0.1,
                    dt_.isoformat(),
                    dt_.isoformat(),
                    is_micro_drive=True,
                ),
            ],
        )
        default_payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))
        assert [s["drive_id"] for s in default_payload["segments"]] == ["normal"]

        with_micro_payload = analytics_db.day(
            TEST_VIN, CHICAGO, date(2026, 9, 10), include_micro=True
        )
        assert {s["drive_id"] for s in with_micro_payload["segments"]} == {
            "normal",
            "micro",
        }

    def test_segments_with_a_track_include_the_anchored_model(
        self, analytics_db: Any
    ) -> None:
        d1_start = datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO)
        drive = _make_drive("d1", 5.0, 2.0, d1_start.isoformat(), d1_start.isoformat())
        analytics_db.upsert_drives(TEST_VIN, [drive])
        track = DriveTrack(
            [
                TrackPoint(
                    t=d1_start.timestamp() + i * 10,
                    lat=40.0 + i * 0.001,
                    lon=-105.0,
                    speed_mps=15.0,
                )
                for i in range(20)
            ]
        )
        analytics_db.upsert_tracks(TEST_VIN, [("d1", track)])

        segments = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))["segments"]
        model = segments[0]["model"]
        assert model is not None
        assert len(model["eff"]) == 20
        assert isinstance(model["points"], list)

    def test_segments_without_a_track_have_no_model(self, analytics_db: Any) -> None:
        d1_start = datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [_make_drive("d1", 3.0, 1.0, d1_start.isoformat(), d1_start.isoformat())],
        )
        segments = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))["segments"]
        assert segments[0]["model"] is None

    def test_prior_tail_none_without_an_earlier_drive(self, analytics_db: Any) -> None:
        d1_start = datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [_make_drive("d1", 3.0, 1.0, d1_start.isoformat(), d1_start.isoformat())],
        )
        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))
        assert payload["prior_tail"] is None

    def test_prior_tail_none_when_earlier_drive_has_no_track(
        self, analytics_db: Any
    ) -> None:
        prior_start = datetime(2026, 9, 9, 22, 0, tzinfo=CHICAGO)
        d1_start = datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO)
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive(
                    "d0", 3.0, 1.0, prior_start.isoformat(), prior_start.isoformat()
                ),
                _make_drive("d1", 3.0, 1.0, d1_start.isoformat(), d1_start.isoformat()),
            ],
        )
        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))
        assert payload["prior_tail"] is None

    def test_prior_tail_trims_to_the_soc_drop_and_carries_its_own_capacity(
        self, analytics_db: Any
    ) -> None:
        prior_start = datetime(2026, 9, 9, 22, 0, tzinfo=CHICAGO)
        d1_start = datetime(2026, 9, 10, 8, 0, tzinfo=CHICAGO)
        prior = _make_drive(
            "d0", 3.0, 1.0, prior_start.isoformat(), prior_start.isoformat()
        )
        prior.battery_capacity_kwh = 100.0
        today = _make_drive("d1", 3.0, 1.0, d1_start.isoformat(), d1_start.isoformat())
        today.battery_capacity_kwh = 135.0
        analytics_db.upsert_drives(TEST_VIN, [prior, today])
        # Five points, 1.0-point SoC drop per step (small distance apart), so
        # the trim stops one point before the last -- the tail should be a
        # strict subset of the stored track, not the whole thing.
        track = DriveTrack(
            [
                TrackPoint(
                    t=prior_start.timestamp() + i * 10,
                    lat=40.0 + i * 0.0001,
                    lon=-105.0,
                    soc=80.0 - i,
                )
                for i in range(5)
            ]
        )
        analytics_db.upsert_tracks(TEST_VIN, [("d0", track)])

        payload = analytics_db.day(TEST_VIN, CHICAGO, date(2026, 9, 10))
        prior_tail = payload["prior_tail"]
        assert prior_tail is not None
        assert prior_tail["battery_capacity_kwh"] == 100.0
        tail_track = prior_tail["track"]
        assert 0 < len(tail_track["t"]) < 5
        # Ends at the earlier drive's own last point.
        assert tail_track["soc"][-1] == 76.0


class TestEnergyModel:
    """AnalyticsDatabase.fit_energy_model()/get_energy_model(): the anchored energy model."""

    @staticmethod
    def _routed_drive(drive_id: str, when: datetime) -> tuple[DriveRecord, DriveTrack]:
        """A drive record + a track long enough to qualify for fitting."""
        drive = _make_drive(
            drive_id,
            5.0,
            2.0,
            when.isoformat(),
            (when + timedelta(minutes=15)).isoformat(),
        )
        track = DriveTrack(
            [
                TrackPoint(
                    t=when.timestamp() + i * 10,
                    lat=40.0 + i * 0.001,
                    lon=-105.0,
                    speed_mps=20.0,
                    alt_m=1600.0,
                )
                for i in range(80)
            ]
        )
        return drive, track

    def _seed_routed_drives(self, analytics_db: Any, count: int) -> None:
        now = datetime.now(timezone.utc)
        for i in range(count):
            when = now - timedelta(days=i + 1, hours=1)
            drive, track = self._routed_drive(f"e{i}", when)
            analytics_db.upsert_drives(TEST_VIN, [drive])
            analytics_db.upsert_tracks(TEST_VIN, [(f"e{i}", track)])

    def test_get_energy_model_none_when_unset(self, analytics_db: Any) -> None:
        assert analytics_db.get_energy_model(TEST_VIN) is None

    def test_fit_energy_model_stores_meta_and_is_retrievable(
        self, analytics_db: Any
    ) -> None:
        self._seed_routed_drives(analytics_db, 6)
        result = analytics_db.fit_energy_model(TEST_VIN, window_days=90, min_drives=5)
        assert result["fitted"] is True
        assert result["n_drives"] == 6
        params = analytics_db.get_energy_model(TEST_VIN)
        assert params is not None
        assert params.cda_m2 == result["params"]["cda_m2"]

    def test_fit_energy_model_too_few_drives_keeps_existing(
        self, analytics_db: Any
    ) -> None:
        self._seed_routed_drives(analytics_db, 2)
        first = analytics_db.fit_energy_model(TEST_VIN, window_days=90, min_drives=1)
        assert first["fitted"] is True
        existing = analytics_db.get_energy_model(TEST_VIN)
        assert existing is not None

        result = analytics_db.fit_energy_model(TEST_VIN, window_days=90, min_drives=10)
        assert result == {
            "fitted": False,
            "reason": "too_few_drives",
            "n_drives": 2,
            "min_drives": 10,
            "has_existing": True,
        }
        assert analytics_db.get_energy_model(TEST_VIN) == existing

    def test_fit_energy_model_no_drives_and_no_existing(
        self, analytics_db: Any
    ) -> None:
        result = analytics_db.fit_energy_model(TEST_VIN, window_days=90, min_drives=1)
        assert result == {
            "fitted": False,
            "reason": "too_few_drives",
            "n_drives": 0,
            "min_drives": 1,
            "has_existing": False,
        }

    def test_fit_energy_model_ignores_drives_outside_window(
        self, analytics_db: Any
    ) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=200)
        drive, track = self._routed_drive("old", old)
        analytics_db.upsert_drives(TEST_VIN, [drive])
        analytics_db.upsert_tracks(TEST_VIN, [("old", track)])
        result = analytics_db.fit_energy_model(TEST_VIN, window_days=90, min_drives=1)
        assert result["fitted"] is False
        assert result["n_drives"] == 0

    def test_fit_energy_model_read_only_skips(self, analytics_db: Any) -> None:
        self._seed_routed_drives(analytics_db, 6)
        analytics_db.read_only = True
        result = analytics_db.fit_energy_model(TEST_VIN, window_days=90, min_drives=1)
        assert result == {"fitted": False, "reason": "read_only"}


class TestTrackPreviewsAndMissing:
    """get_track_previews and drives_missing_tracks."""

    def test_get_track_previews(self, analytics_db: Any) -> None:
        analytics_db.upsert_tracks(
            TEST_VIN, [("d1", _make_track(n=4)), ("d2", _make_track(n=3, lat0=5.0))]
        )
        previews = analytics_db.get_track_previews(TEST_VIN, ["d1", "d2", "missing"])
        assert set(previews) == {"d1", "d2"}
        assert len(previews["d1"]["lat"]) == 4
        assert len(previews["d1"]["lon"]) == 4

    def test_drives_missing_tracks_filters(self, analytics_db: Any) -> None:
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        has_track = _make_drive("has_track", 5.0, 2.0, now.isoformat(), now.isoformat())
        missing_recent = _make_drive(
            "missing_recent",
            5.0,
            2.0,
            (now + timedelta(hours=1)).isoformat(),
            (now + timedelta(hours=1)).isoformat(),
        )
        missing_old = _make_drive(
            "missing_old",
            5.0,
            2.0,
            (now - timedelta(days=30)).isoformat(),
            (now - timedelta(days=30)).isoformat(),
        )
        null_ts = _make_drive("null_ts", 5.0, 2.0, "garbage", "garbage")
        analytics_db.upsert_drives(
            TEST_VIN, [has_track, missing_recent, missing_old, null_ts]
        )
        analytics_db.upsert_tracks(TEST_VIN, [("has_track", _make_track(n=3))])

        since_ts = (now - timedelta(days=1)).timestamp()
        missing = analytics_db.drives_missing_tracks(TEST_VIN, since_ts)
        drive_ids = [m[0] for m in missing]
        assert drive_ids == ["missing_recent"]


class TestPruneWithTracks:
    """prune() removes orphan tracks; delete_vin clears tracks and checkpoints."""

    def test_prune_removes_orphan_tracks(self, analytics_db: Any) -> None:
        old = _make_drive(
            "old", 5.0, 2.0, "2020-01-01T00:00:00Z", "2020-01-01T00:10:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [old])
        analytics_db.upsert_tracks(TEST_VIN, [("old", _make_track(n=3))])

        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        analytics_db.prune(TEST_VIN, cutoff)

        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT 1 FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "old"),
            ).fetchone()
        assert row is None

    def test_delete_vin_removes_tracks_and_checkpoints(self, analytics_db: Any) -> None:
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive(
                    "d1", 5.0, 2.0, "2026-08-20T10:00:00Z", "2026-08-20T10:10:00Z"
                )
            ],
        )
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=3))])
        analytics_db.save_active_checkpoint(TEST_VIN, "d2", {}, _make_track(n=2), 0)

        analytics_db.delete_vin(TEST_VIN)

        assert analytics_db.get_track(TEST_VIN, "d1") is None
        assert analytics_db.load_active_checkpoint(TEST_VIN) is None
        with analytics_db._lock:
            remaining = analytics_db._conn.execute(
                "SELECT COUNT(*) AS c FROM drive_tracks WHERE vin = ?", (TEST_VIN,)
            ).fetchone()["c"]
        assert remaining == 0


class TestPruneTracks:
    """prune_tracks: delete/thin cutoffs, idempotency, and storage_stats."""

    def test_delete_before_cutoff_keeps_drive_row(self, analytics_db: Any) -> None:
        drive = _make_drive(
            "d1", 5.0, 2.0, "2020-01-01T00:00:00Z", "2020-01-01T00:10:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=3))])

        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        result = analytics_db.prune_tracks(TEST_VIN, cutoff, None)
        assert result == {"deleted": 1, "thinned": 0}
        assert analytics_db.get_track(TEST_VIN, "d1") is None

        with analytics_db._lock:
            drive_row = analytics_db._conn.execute(
                "SELECT 1 FROM drives WHERE vin = ? AND drive_id = ?", (TEST_VIN, "d1")
            ).fetchone()
        assert drive_row is not None

    def test_thin_reduces_points_keeps_endpoints_and_is_idempotent(
        self, analytics_db: Any
    ) -> None:
        drive = _make_drive(
            "d1", 5.0, 2.0, "2020-01-01T00:00:00Z", "2020-01-01T00:10:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])

        # A track with a long straight run of points simplifies down heavily
        # under Douglas-Peucker, but the endpoints are always kept.
        track = DriveTrack()
        for i in range(40):
            track.append(
                TrackPoint(t=1_700_000_000.0 + i, lat=37.0 + i * 1e-6, lon=-122.0)
            )
        analytics_db.upsert_tracks(TEST_VIN, [("d1", track)])

        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        result = analytics_db.prune_tracks(
            TEST_VIN, None, cutoff, thin_tolerance_m=50.0
        )
        assert result["thinned"] == 1
        assert result["deleted"] == 0

        thinned_track = analytics_db.get_track(TEST_VIN, "d1")
        assert len(thinned_track) < 40
        assert thinned_track.points[0].t == track.points[0].t
        assert thinned_track.points[-1].t == track.points[-1].t

        with analytics_db._lock:
            detail = analytics_db._conn.execute(
                "SELECT detail FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "d1"),
            ).fetchone()["detail"]
        assert detail == "thinned"

        # Second run: nothing left at detail='full' before the cutoff.
        result2 = analytics_db.prune_tracks(
            TEST_VIN, None, cutoff, thin_tolerance_m=50.0
        )
        assert result2 == {"deleted": 0, "thinned": 0}

    def test_unreadable_track_is_dropped_not_retried_forever(
        self, analytics_db: Any
    ) -> None:
        """A corrupt row must not abort thinning or be re-selected endlessly."""
        for drive_id in ("bad", "good"):
            analytics_db.upsert_drives(
                TEST_VIN,
                [
                    _make_drive(
                        drive_id,
                        5.0,
                        2.0,
                        "2020-01-01T00:00:00Z",
                        "2020-01-01T00:10:00Z",
                    )
                ],
            )
            analytics_db.upsert_tracks(TEST_VIN, [(drive_id, _make_track(n=5))])
        with analytics_db._lock:
            analytics_db._conn.execute(
                "UPDATE drive_tracks SET track_json = '{\"v\": 99}' WHERE drive_id = ?",
                ("bad",),
            )

        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        result = analytics_db.prune_tracks(TEST_VIN, None, cutoff)

        assert result["thinned"] == 1
        assert analytics_db.get_track(TEST_VIN, "bad") is None
        assert analytics_db.get_track(TEST_VIN, "good") is not None

    def test_none_cutoffs_skip_both_steps(self, analytics_db: Any) -> None:
        drive = _make_drive(
            "d1", 5.0, 2.0, "2020-01-01T00:00:00Z", "2020-01-01T00:10:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=3))])

        result = analytics_db.prune_tracks(TEST_VIN, None, None)
        assert result == {"deleted": 0, "thinned": 0}
        assert analytics_db.get_track(TEST_VIN, "d1") is not None

    def test_storage_stats_counts_full_and_thinned_and_bytes(
        self, analytics_db: Any
    ) -> None:
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive(
                    "d1", 5.0, 2.0, "2020-01-01T00:00:00Z", "2020-01-01T00:10:00Z"
                ),
                _make_drive(
                    "d2", 5.0, 2.0, "2026-08-01T00:00:00Z", "2026-08-01T00:10:00Z"
                ),
            ],
        )
        analytics_db.upsert_tracks(
            TEST_VIN, [("d1", _make_track(n=3)), ("d2", _make_track(n=3, lat0=20.0))]
        )
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        analytics_db.prune_tracks(TEST_VIN, None, cutoff, thin_tolerance_m=50.0)

        stats = analytics_db.storage_stats(TEST_VIN)
        assert stats["drive_count"] == 2
        assert stats["track_count"] == 2
        assert stats["full_count"] == 1
        assert stats["thinned_count"] == 1
        assert stats["track_bytes"] > 0
        assert stats["db_bytes"] > 0


class TestRecomputeDriveStats:
    """recompute_drive_stats: batching, capacity fallback, context columns untouched."""

    @staticmethod
    def _track_with_telemetry(n: int = 6) -> DriveTrack:
        track = DriveTrack()
        for i in range(n):
            track.append(
                TrackPoint(
                    t=1_700_000_000.0 + i * 10,
                    lat=37.0 + i * 0.001,
                    lon=-122.0 + i * 0.001,
                    speed_mps=15.0,
                    alt_m=10.0 * i,
                    soc=80.0 - i,
                )
            )
        return track

    def test_recomputes_null_stats_from_stored_track(self, analytics_db: Any) -> None:
        drive = _make_drive(
            "d1", 5.0, 2.0, "2026-08-01T10:00:00Z", "2026-08-01T10:20:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        analytics_db.upsert_tracks(TEST_VIN, [("d1", self._track_with_telemetry())])

        assert analytics_db.has_unrecomputed_drive_stats(TEST_VIN) is True

        result = analytics_db.recompute_drive_stats(TEST_VIN)
        assert result == {"updated": 1}
        assert analytics_db.has_unrecomputed_drive_stats(TEST_VIN) is False

        detail = analytics_db.get_drive_detail(TEST_VIN, "d1")
        summary = detail["drive"]
        assert summary["moving_seconds"] == pytest.approx(50.0)
        assert summary["stopped_seconds"] == pytest.approx(0.0)

    def test_never_touches_live_only_context_columns(self, analytics_db: Any) -> None:
        drive = DriveRecord(
            vin=TEST_VIN,
            drive_id="d1",
            start_time="2026-08-01T10:00:00Z",
            end_time="2026-08-01T10:20:00Z",
            distance_miles=5.0,
            duration_seconds=600.0,
            start_soc=80.0,
            end_soc=75.0,
            battery_capacity_kwh=135.0,
            energy_kwh=2.0,
            start_range_mi=200.0,
            end_range_mi=190.0,
            drive_modes=["Sport"],
            trailer=True,
            driver="Kelly",
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        analytics_db.upsert_tracks(TEST_VIN, [("d1", self._track_with_telemetry())])

        analytics_db.recompute_drive_stats(TEST_VIN)

        detail = analytics_db.get_drive_detail(TEST_VIN, "d1")
        summary = detail["drive"]
        assert summary["start_range_mi"] == pytest.approx(200.0)
        assert summary["end_range_mi"] == pytest.approx(190.0)
        assert summary["drive_modes"] == ["Sport"]
        assert summary["trailer"] is True
        assert summary["driver"] == "Kelly"

    def test_batches_across_many_drives(self, analytics_db: Any) -> None:
        for i in range(120):
            drive = _make_drive(
                f"d{i}",
                5.0,
                2.0,
                f"2026-08-{(i % 28) + 1:02d}T10:00:00Z",
                f"2026-08-{(i % 28) + 1:02d}T10:20:00Z",
            )
            analytics_db.upsert_drives(TEST_VIN, [drive])
            analytics_db.upsert_tracks(
                TEST_VIN, [(f"d{i}", self._track_with_telemetry())]
            )

        result = analytics_db.recompute_drive_stats(TEST_VIN)
        assert result == {"updated": 120}
        assert analytics_db.has_unrecomputed_drive_stats(TEST_VIN) is False

    def test_unreadable_track_is_skipped_not_fatal(self, analytics_db: Any) -> None:
        drive = _make_drive(
            "bad", 5.0, 2.0, "2026-08-01T10:00:00Z", "2026-08-01T10:20:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        analytics_db.upsert_tracks(TEST_VIN, [("bad", self._track_with_telemetry())])
        with analytics_db._lock:
            analytics_db._conn.execute(
                "UPDATE drive_tracks SET track_json = '{\"v\": 99}' WHERE drive_id = ?",
                ("bad",),
            )

        result = analytics_db.recompute_drive_stats(TEST_VIN)
        assert result == {"updated": 0}
        # Its stats stay NULL, but that mustn't retrigger a full recompute on
        # every restart.
        assert analytics_db.has_unrecomputed_drive_stats(TEST_VIN) is False

    def test_read_only_skips(self, analytics_db: Any) -> None:
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive(
                    "d1", 5.0, 2.0, "2026-08-01T10:00:00Z", "2026-08-01T10:20:00Z"
                )
            ],
        )
        analytics_db.upsert_tracks(TEST_VIN, [("d1", self._track_with_telemetry())])
        analytics_db.read_only = True
        result = analytics_db.recompute_drive_stats(TEST_VIN)
        assert result == {"updated": 0}


class TestSeriesWindow:
    """series_window: an arbitrary-length window beyond the 90-day hot cache."""

    def test_series_window_beyond_90_days_ascending_excludes_micro(
        self, analytics_db: Any
    ) -> None:
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        old = _make_drive(
            "old_200d",
            10.0,
            4.0,
            (now - timedelta(days=200)).isoformat(),
            (now - timedelta(days=200)).isoformat(),
        )
        recent = _make_drive(
            "recent_5d",
            10.0,
            4.0,
            (now - timedelta(days=5)).isoformat(),
            (now - timedelta(days=5)).isoformat(),
        )
        micro = _make_drive(
            "micro",
            0.2,
            0.1,
            (now - timedelta(days=10)).isoformat(),
            (now - timedelta(days=10)).isoformat(),
            is_micro_drive=True,
        )
        analytics_db.upsert_drives(TEST_VIN, [old, recent, micro])

        drives, _vampire = analytics_db.series_window(TEST_VIN, 365, now.timestamp())
        assert [d.drive_id for d in drives] == ["old_200d", "recent_5d"]


class TestLoopThreadGuardOnNewMethods:
    """Every new public method must refuse to run on the (simulated) loop thread."""

    def test_new_public_methods_raise_on_loop_thread(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        record = _make_drive(
            "d1", 5.0, 2.0, "2026-08-20T10:00:00Z", "2026-08-20T10:10:00Z"
        )
        track = _make_track(n=2)

        # _loop_thread_id is captured once at construction time, so flip it
        # directly (mirroring what a real loop-thread call would compare
        # against) rather than mutating mock_hass after the fact.
        db._loop_thread_id = threading.get_ident()
        calls = [
            (db.upsert_tracks, (TEST_VIN, [("d1", track)])),
            (db.finalize_drive, (TEST_VIN, record, track)),
            (db.get_track, (TEST_VIN, "d1")),
            (db.list_drives, (TEST_VIN,)),
            (db.get_track_previews, (TEST_VIN, ["d1"])),
            (db.get_drive_detail, (TEST_VIN, "d1")),
            (db.drives_missing_tracks, (TEST_VIN, 0.0)),
            (db.save_active_checkpoint, (TEST_VIN, "d1", {}, track, 0)),
            (db.load_active_checkpoint, (TEST_VIN,)),
            (db.clear_active_checkpoint, (TEST_VIN,)),
            (db.prune_tracks, (TEST_VIN, None, None)),
            (db.storage_stats, (TEST_VIN,)),
            (db.series_window, (TEST_VIN, 30, time.time())),
        ]
        for method, args in calls:
            with pytest.raises(RuntimeError, match="executor"):
                method(*args)

        db._loop_thread_id = -1
        db.close()


class TestVehiclePicture:
    """The configurator picture is stored once per VIN and read back intact."""

    def test_round_trip_and_replace(self, analytics_db: Any) -> None:
        from custom_components.rivian.analytics_db import VehiclePicture

        assert analytics_db.get_vehicle_picture(TEST_VIN) is None
        failed = VehiclePicture("failed", None, None, None, [], 1.0)
        analytics_db.save_vehicle_picture(TEST_VIN, failed)
        assert analytics_db.get_vehicle_picture(TEST_VIN) == failed

        ok = VehiclePicture(
            "ok", "image/webp", b"\x00RIFF\xff", "https://x", ["EXP-LGR"], 2.0
        )
        analytics_db.save_vehicle_picture(TEST_VIN, ok)
        assert analytics_db.get_vehicle_picture(TEST_VIN) == ok

    def test_picture_methods_reject_the_loop_thread(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db._loop_thread_id = threading.get_ident()
        with pytest.raises(RuntimeError):
            db.get_vehicle_picture(TEST_VIN)


class TestRoadHeat:
    """update_heat/rebuild_heat/heat_info/heat_tile: counting, caching, retention."""

    def test_update_heat_counts_each_track_once(self, analytics_db: Any) -> None:
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=5))])
        assert analytics_db.update_heat(TEST_VIN, timezone.utc) == 1
        assert analytics_db.update_heat(TEST_VIN, timezone.utc) == 0

    def test_update_heat_counts_new_track_incrementally(
        self, analytics_db: Any
    ) -> None:
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=5))])
        assert analytics_db.update_heat(TEST_VIN, timezone.utc) == 1
        analytics_db.upsert_tracks(TEST_VIN, [("d2", _make_track(n=5, lat0=40.0))])
        assert analytics_db.update_heat(TEST_VIN, timezone.utc) == 1

    def test_month_assignment_uses_local_zone_across_a_utc_day_boundary(
        self, analytics_db: Any
    ) -> None:
        """23:30 local on Aug 31 in America/Denver is already Sep 1 in UTC."""
        tz = ZoneInfo("America/Denver")
        local_dt = datetime(2026, 8, 31, 23, 30, tzinfo=tz)
        assert local_dt.astimezone(timezone.utc).month == 9  # sanity: UTC disagrees

        track = DriveTrack()
        track.append(TrackPoint(t=local_dt.timestamp(), lat=40.0, lon=-105.0))
        track.append(TrackPoint(t=local_dt.timestamp() + 10, lat=40.001, lon=-105.001))
        analytics_db.upsert_tracks(TEST_VIN, [("d1", track)])
        analytics_db.update_heat(TEST_VIN, tz)

        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT month FROM road_heat_drives WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "d1"),
            ).fetchone()
        assert row["month"] == "2026-08"

    def test_undecodable_track_is_recorded_and_not_retried(
        self, analytics_db: Any
    ) -> None:
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=3))])
        with analytics_db._lock, analytics_db._transaction():
            analytics_db._conn.execute(
                "UPDATE drive_tracks SET track_json = 'not json' "
                "WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "d1"),
            )

        assert analytics_db.update_heat(TEST_VIN, timezone.utc) == 1
        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT month FROM road_heat_drives WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "d1"),
            ).fetchone()
        assert row["month"] == ""
        # Never retried: the second call finds nothing left to count.
        assert analytics_db.update_heat(TEST_VIN, timezone.utc) == 0

    def test_update_heat_read_only_returns_zero(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=3))])
            db.read_only = True
            assert db.update_heat(TEST_VIN, timezone.utc) == 0
        finally:
            db.close()

    def test_heat_methods_reject_the_loop_thread(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db._loop_thread_id = threading.get_ident()
        with pytest.raises(RuntimeError):
            db.update_heat(TEST_VIN, timezone.utc)
        with pytest.raises(RuntimeError):
            db.heat_info(TEST_VIN, "all")
        with pytest.raises(RuntimeError):
            db.rebuild_heat(TEST_VIN, timezone.utc)

    def test_year_and_all_sum_their_months(self, analytics_db: Any) -> None:
        tz = timezone.utc

        def track_at(dt: datetime, lat0: float) -> DriveTrack:
            track = DriveTrack()
            track.append(TrackPoint(t=dt.timestamp(), lat=lat0, lon=-100.0))
            track.append(
                TrackPoint(t=dt.timestamp() + 10, lat=lat0 + 0.01, lon=-100.01)
            )
            return track

        analytics_db.upsert_tracks(
            TEST_VIN,
            [
                ("jan1", track_at(datetime(2026, 1, 15, tzinfo=tz), 10.0)),
                ("feb1", track_at(datetime(2026, 2, 15, tzinfo=tz), 20.0)),
                ("old1", track_at(datetime(2025, 1, 15, tzinfo=tz), 30.0)),
            ],
        )
        analytics_db.update_heat(TEST_VIN, tz)

        year_info = analytics_db.heat_info(TEST_VIN, "year", "2026")
        all_info = analytics_db.heat_info(TEST_VIN, "all")
        assert year_info["drives"] == 2
        assert all_info["drives"] == 3

        jan_grid, *_ = analytics_db._get_heat_grid(TEST_VIN, "month", "2026-01")
        feb_grid, *_ = analytics_db._get_heat_grid(TEST_VIN, "month", "2026-02")
        old_grid, *_ = analytics_db._get_heat_grid(TEST_VIN, "month", "2025-01")
        year_grid, *_ = analytics_db._get_heat_grid(TEST_VIN, "year", "2026")
        all_grid, *_ = analytics_db._get_heat_grid(TEST_VIN, "all", None)

        assert year_grid.to_counts() == HeatGrid.merge([jan_grid, feb_grid]).to_counts()
        assert (
            all_grid.to_counts()
            == HeatGrid.merge([jan_grid, feb_grid, old_grid]).to_counts()
        )

    def test_heat_info_shape(self, analytics_db: Any) -> None:
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=5))])
        analytics_db.update_heat(TEST_VIN, timezone.utc)
        info = analytics_db.heat_info(TEST_VIN, "all")
        assert info["period"] == "all"
        assert info["drives"] == 1
        assert info["cells"] > 0
        assert info["bbox"] is not None and len(info["bbox"]) == 4
        assert info["scale_max"] >= 1

    def test_heat_tile_cells_within_tile_and_carry_scale_max(
        self, analytics_db: Any
    ) -> None:
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=5))])
        analytics_db.update_heat(TEST_VIN, timezone.utc)
        grid, *_ = analytics_db._get_heat_grid(TEST_VIN, "all", None)
        cx, cy = split_key(next(iter(grid.to_counts())))

        tile = analytics_db.heat_tile(TEST_VIN, "all", None, BASE_LEVEL, cx, cy)
        assert tile["scale_max"] >= 1
        assert len(tile["cells"]) >= 1
        for count in (c[2] for c in tile["cells"]):
            assert count >= 1

    def test_unknown_period_key_returns_empty_not_an_error(
        self, analytics_db: Any
    ) -> None:
        info = analytics_db.heat_info(TEST_VIN, "month", "2099-01")
        assert info == {
            "period": "month",
            "key": "2099-01",
            "bbox": None,
            "scale_max": 2,
            "cells": 0,
            "drives": 0,
        }
        tile = analytics_db.heat_tile(TEST_VIN, "month", "2099-01", 5, 10, 10)
        assert tile["cells"] == []

    @pytest.mark.parametrize(
        ("period", "key"),
        [
            ("bogus", None),
            ("year", "abcd"),
            ("year", None),
            ("month", "2026-13"),
            ("month", "not-a-month"),
            ("month", None),
        ],
    )
    def test_bad_period_or_key_raises(
        self, analytics_db: Any, period: str, key: str | None
    ) -> None:
        with pytest.raises(ValueError):
            analytics_db.heat_info(TEST_VIN, period, key)

    def test_heat_survives_prune_and_prune_tracks(self, analytics_db: Any) -> None:
        tz = timezone.utc
        old_time = datetime(2020, 1, 1, tzinfo=tz)
        analytics_db.upsert_drives(
            TEST_VIN,
            [_make_drive("old1", 5.0, 2.0, old_time.isoformat(), old_time.isoformat())],
        )
        analytics_db.upsert_tracks(
            TEST_VIN, [("old1", _make_track(n=5, start_t=old_time.timestamp()))]
        )
        analytics_db.update_heat(TEST_VIN, tz)
        before = analytics_db.heat_info(TEST_VIN, "all")
        assert before["drives"] == 1

        cutoff = datetime(2026, 1, 1, tzinfo=tz).timestamp()
        analytics_db.prune(TEST_VIN, cutoff)
        assert analytics_db.heat_info(TEST_VIN, "all") == before

        analytics_db.prune_tracks(
            TEST_VIN, delete_before_ts=None, thin_before_ts=cutoff
        )
        assert analytics_db.heat_info(TEST_VIN, "all") == before

    def test_delete_vin_removes_heat(self, analytics_db: Any) -> None:
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=5))])
        analytics_db.update_heat(TEST_VIN, timezone.utc)
        assert analytics_db.heat_info(TEST_VIN, "all")["drives"] == 1

        analytics_db.delete_vin(TEST_VIN)
        info = analytics_db.heat_info(TEST_VIN, "all")
        assert info["drives"] == 0
        assert info["cells"] == 0

    def test_rebuild_reproduces_the_same_grid(self, analytics_db: Any) -> None:
        tz = timezone.utc
        analytics_db.upsert_tracks(
            TEST_VIN,
            [
                ("d1", _make_track(n=5, lat0=10.0)),
                ("d2", _make_track(n=5, lat0=20.0)),
            ],
        )
        analytics_db.update_heat(TEST_VIN, tz)
        before, *_ = analytics_db._get_heat_grid(TEST_VIN, "all", None)
        before_counts = before.to_counts()

        result = analytics_db.rebuild_heat(TEST_VIN, tz)
        assert result["drives_counted"] == 2
        assert result["months_rebuilt"] >= 1

        after, *_ = analytics_db._get_heat_grid(TEST_VIN, "all", None)
        assert after.to_counts() == before_counts

    def test_rebuild_keeps_heat_for_a_month_whose_tracks_were_all_pruned(
        self, analytics_db: Any
    ) -> None:
        tz = timezone.utc
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=5))])
        analytics_db.update_heat(TEST_VIN, tz)
        before, *_ = analytics_db._get_heat_grid(TEST_VIN, "all", None)
        assert len(before) > 0

        # Prune the only stored track entirely.
        analytics_db.prune_tracks(
            TEST_VIN, delete_before_ts=time.time() + 86400, thin_before_ts=None
        )

        result = analytics_db.rebuild_heat(TEST_VIN, tz)
        assert result == {"months_rebuilt": 0, "drives_counted": 0}
        after, *_ = analytics_db._get_heat_grid(TEST_VIN, "all", None)
        assert after.to_counts() == before.to_counts()

    def test_rebuild_drops_heat_for_a_drive_whose_track_was_pruned_in_rebuilt_month(
        self, analytics_db: Any
    ) -> None:
        tz = timezone.utc
        keep_track = _make_track(n=5, lat0=10.0)
        analytics_db.upsert_tracks(
            TEST_VIN,
            [("keep", keep_track), ("gone", _make_track(n=5, lat0=50.0))],
        )
        analytics_db.update_heat(TEST_VIN, tz)

        with analytics_db._lock, analytics_db._transaction():
            analytics_db._conn.execute(
                "DELETE FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "gone"),
            )

        result = analytics_db.rebuild_heat(TEST_VIN, tz)
        assert result == {"months_rebuilt": 1, "drives_counted": 1}

        grid, *_ = analytics_db._get_heat_grid(TEST_VIN, "all", None)
        expected = RoadHeat.empty().add_drives([track_passes(keep_track)]).display()
        assert grid.to_counts() == expected.to_counts()

    def test_heat_counted_by_an_older_format_is_recounted_once(
        self, analytics_db: Any
    ) -> None:
        """Rows from before corridor/pass counting are rebuilt on the next update."""
        tz = timezone.utc
        track = _make_track(n=5)
        analytics_db.upsert_tracks(TEST_VIN, [("d1", track)])
        month = datetime.fromtimestamp(track.points[0].t, tz).strftime("%Y-%m")
        old_grid = HeatGrid.empty().add_drive(track_cells(track))
        with analytics_db._lock, analytics_db._transaction():
            analytics_db._conn.execute(
                "INSERT INTO road_heat (vin, month, level, version, cell_count, "
                "drive_count, data, updated_ts) VALUES (?, ?, ?, 1, ?, 1, ?, 0)",
                (TEST_VIN, month, BASE_LEVEL, len(old_grid), old_grid.encode()),
            )
            analytics_db._conn.execute(
                "INSERT INTO road_heat_drives (vin, drive_id, month) VALUES (?, ?, ?)",
                (TEST_VIN, "d1", month),
            )

        assert analytics_db.update_heat(TEST_VIN, tz) == 1
        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT version, drive_count FROM road_heat WHERE vin = ?", (TEST_VIN,)
            ).fetchone()
        assert (row["version"], row["drive_count"]) == (HEAT_FORMAT_VERSION, 1)
        # Recorded as upgraded: later updates count only new routes.
        assert analytics_db.update_heat(TEST_VIN, tz) == 0

    def test_cache_invalidation_after_update_heat_adds_a_drive(
        self, analytics_db: Any
    ) -> None:
        tz = timezone.utc
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=5))])
        analytics_db.update_heat(TEST_VIN, tz)

        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT month FROM road_heat_drives WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "d1"),
            ).fetchone()
        month_key = row["month"]
        year_key = month_key[:4]

        assert analytics_db.heat_info(TEST_VIN, "month", month_key)["drives"] == 1
        assert analytics_db.heat_info(TEST_VIN, "year", year_key)["drives"] == 1
        assert analytics_db.heat_info(TEST_VIN, "all")["drives"] == 1

        analytics_db.upsert_tracks(TEST_VIN, [("d2", _make_track(n=5, lat0=60.0))])
        analytics_db.update_heat(TEST_VIN, tz)

        assert analytics_db.heat_info(TEST_VIN, "month", month_key)["drives"] == 2
        assert analytics_db.heat_info(TEST_VIN, "year", year_key)["drives"] == 2
        assert analytics_db.heat_info(TEST_VIN, "all")["drives"] == 2

    def test_grid_read_during_a_concurrent_update_is_not_cached(
        self, analytics_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An update committing mid-read must not leave the stale grid cached."""
        tz = timezone.utc
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=5))])
        analytics_db.update_heat(TEST_VIN, tz)

        real_merge = analytics_db_module.RoadHeat.merge

        def merge_then_invalidate(heats):
            # Stands in for update_heat committing on another thread while this
            # read is between its DB query and storing the result.
            analytics_db._invalidate_heat_cache_all(TEST_VIN)
            return real_merge(heats)

        monkeypatch.setattr(
            analytics_db_module.RoadHeat, "merge", staticmethod(merge_then_invalidate)
        )
        assert analytics_db.heat_info(TEST_VIN, "all")["drives"] == 1
        assert (TEST_VIN, "all") not in analytics_db._heat_cache

        monkeypatch.setattr(
            analytics_db_module.RoadHeat, "merge", staticmethod(real_merge)
        )
        analytics_db.heat_info(TEST_VIN, "all")
        assert (TEST_VIN, "all") in analytics_db._heat_cache


OTHER_VIN = "7PDSGABA8NN999999"


class TestMultiVinReads:
    """calendar()/_get_heat_grid() over several VINs (`vin IN (...)`)."""

    def test_calendar_combines_vins_with_by_vin_on_every_node(
        self, analytics_db: Any
    ) -> None:
        when = datetime(2026, 9, 23, 9, 0, tzinfo=CHICAGO).isoformat()
        other = _make_drive("o1", 7.0, 2.0, when, when)
        other.vin = OTHER_VIN
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive("a1", 10.0, 4.0, when, when),
                _make_drive("a2", 5.0, 2.0, when, when),
            ],
        )
        analytics_db.upsert_drives(OTHER_VIN, [other])

        cal = analytics_db.calendar([TEST_VIN, OTHER_VIN], CHICAGO, year=2026, month=9)

        assert cal["totals"]["drives"] == 3
        assert cal["totals"]["miles"] == 22.0
        assert cal["totals"]["by_vin"] == {
            TEST_VIN: {"drives": 2, "miles": 15.0},
            OTHER_VIN: {"drives": 1, "miles": 7.0},
        }
        for node in (cal["years"][0], cal["months"][0], cal["days"][0]):
            assert node["drives"] == 3
            assert node["by_vin"][OTHER_VIN] == {"drives": 1, "miles": 7.0}

    def test_calendar_lists_requested_vins_with_no_drives_as_zero(
        self, analytics_db: Any
    ) -> None:
        when = datetime(2026, 9, 23, 9, 0, tzinfo=CHICAGO).isoformat()
        analytics_db.upsert_drives(TEST_VIN, [_make_drive("a1", 10.0, 4.0, when, when)])
        cal = analytics_db.calendar([TEST_VIN, OTHER_VIN], CHICAGO)
        assert cal["totals"]["by_vin"][OTHER_VIN] == {"drives": 0, "miles": 0.0}

    def test_single_vin_calendar_has_no_by_vin(self, analytics_db: Any) -> None:
        when = datetime(2026, 9, 23, 9, 0, tzinfo=CHICAGO).isoformat()
        analytics_db.upsert_drives(TEST_VIN, [_make_drive("a1", 10.0, 4.0, when, when)])
        assert "by_vin" not in analytics_db.calendar(TEST_VIN, CHICAGO)["totals"]

    def test_combined_heat_is_the_cell_wise_sum(self, analytics_db: Any) -> None:
        tz = timezone.utc
        shared = _make_track(n=5)
        analytics_db.upsert_tracks(TEST_VIN, [("d1", shared)])
        analytics_db.upsert_tracks(
            OTHER_VIN, [("d1", shared), ("d2", _make_track(n=5, lat0=45.0))]
        )
        analytics_db.update_heat(TEST_VIN, tz)
        analytics_db.update_heat(OTHER_VIN, tz)

        a, *_ = analytics_db._get_heat_grid(TEST_VIN, "all", None)
        b, *_ = analytics_db._get_heat_grid(OTHER_VIN, "all", None)
        both, _bbox, _scale, drives = analytics_db._get_heat_grid(
            [TEST_VIN, OTHER_VIN], "all", None
        )

        expected: dict[int, int] = dict(a.to_counts())
        for key, count in b.to_counts().items():
            expected[key] = expected.get(key, 0) + count
        assert both.to_counts() == expected
        assert drives == 3
        assert analytics_db.heat_info([TEST_VIN, OTHER_VIN], "all")["drives"] == 3

    def test_updating_one_vin_drops_combined_cache_entries_containing_it(
        self, analytics_db: Any
    ) -> None:
        tz = timezone.utc
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=5))])
        analytics_db.upsert_tracks(OTHER_VIN, [("d1", _make_track(n=5, lat0=45.0))])
        analytics_db.update_heat(TEST_VIN, tz)
        analytics_db.update_heat(OTHER_VIN, tz)

        pair = [TEST_VIN, OTHER_VIN]
        assert analytics_db.heat_info(pair, "all")["drives"] == 2
        combined_key = (frozenset(pair), "all")
        assert combined_key in analytics_db._heat_cache

        analytics_db.upsert_tracks(OTHER_VIN, [("d2", _make_track(n=5, lat0=50.0))])
        analytics_db.update_heat(OTHER_VIN, tz)

        assert combined_key not in analytics_db._heat_cache
        assert analytics_db.heat_info(pair, "all")["drives"] == 3

    def test_unrelated_vin_update_keeps_the_combined_entry(
        self, analytics_db: Any
    ) -> None:
        tz = timezone.utc
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=5))])
        analytics_db.upsert_tracks(OTHER_VIN, [("d1", _make_track(n=5, lat0=45.0))])
        analytics_db.update_heat(TEST_VIN, tz)
        analytics_db.update_heat(OTHER_VIN, tz)
        analytics_db.heat_info([TEST_VIN, OTHER_VIN], "all")
        third = "7PDSGABA8NN555555"
        analytics_db.upsert_tracks(third, [("d1", _make_track(n=5, lat0=50.0))])
        analytics_db.update_heat(third, tz)

        assert (frozenset([TEST_VIN, OTHER_VIN]), "all") in analytics_db._heat_cache


HOME = (37.0, -122.0)
WORK = (37.5, -122.5)


def _timed(base: str, hour: int, minute: int = 0) -> str:
    return f"{base}T{hour:02d}:{minute:02d}:00Z"


class TestPlaces:
    """AnalyticsDatabase places: rebuild/sync/assign/CRUD, and day()/delete_vin integration."""

    def _seed_commute_drives(self, db: AnalyticsDatabase, days: int = 4) -> None:
        """Write `days` round trips Home -> Work -> Home, enough for a cluster."""
        drives: list[DriveRecord] = []
        for day in range(1, days + 1):
            base = f"2026-01-{day:02d}"
            drives.append(
                _make_drive(
                    f"to_work_{day}",
                    20.0,
                    6.0,
                    _timed(base, 8),
                    _timed(base, 8, 30),
                    start_lat=HOME[0],
                    start_lon=HOME[1],
                    end_lat=WORK[0],
                    end_lon=WORK[1],
                )
            )
            drives.append(
                _make_drive(
                    f"to_home_{day}",
                    20.0,
                    6.0,
                    _timed(base, 17),
                    _timed(base, 17, 30),
                    start_lat=WORK[0],
                    start_lon=WORK[1],
                    end_lat=HOME[0],
                    end_lon=HOME[1],
                )
            )
        db.upsert_drives(TEST_VIN, drives)

    def test_rebuild_places_clusters_frequent_endpoints(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        result = analytics_db.rebuild_places("real")
        assert result["places"] == 2
        places = analytics_db.list_places("real")
        assert len(places) == 2
        assert {p["source"] for p in places} == {"auto"}
        assert all(p["visits"] >= 3 for p in places)

    def test_rebuild_places_is_idempotent_and_ids_are_stable(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        first_ids = sorted(p["id"] for p in analytics_db.list_places("real"))

        analytics_db.rebuild_places("real")
        second_ids = sorted(p["id"] for p in analytics_db.list_places("real"))
        assert first_ids == second_ids

    def test_rebuild_places_assigns_drive_start_end_place_ids(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT start_place_id, end_place_id FROM drives "
                "WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "to_work_1"),
            ).fetchone()
        assert row["start_place_id"] is not None
        assert row["end_place_id"] is not None
        assert row["start_place_id"] != row["end_place_id"]

    def test_assign_drive_places_is_incremental_and_does_not_recluster(
        self, analytics_db: Any
    ) -> None:
        """assign_drive_places only looks up existing places: a 4th round trip
        alone (below MIN_VISITS) gets no place until a full rebuild."""
        self._seed_commute_drives(analytics_db, days=1)
        analytics_db.assign_drive_places(TEST_VIN, "to_work_1")
        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT start_place_id, end_place_id FROM drives WHERE drive_id = ?",
                ("to_work_1",),
            ).fetchone()
        assert row["start_place_id"] is None
        assert row["end_place_id"] is None
        assert analytics_db.list_places("real") == []

    def test_sync_zones_seeds_zone_place_and_takes_priority_over_clustering(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        zones = [
            {
                "entity_id": "zone.home",
                "name": "Home",
                "latitude": HOME[0],
                "longitude": HOME[1],
                "radius": 100,
            }
        ]
        analytics_db.sync_zones("real", zones)
        places = analytics_db.list_places("real")
        zone_places = [p for p in places if p["source"] == "zone"]
        assert len(zone_places) == 1
        assert zone_places[0]["label"] == "Home"
        # Home's points went to the zone place, not a new auto cluster.
        assert not any(
            p["source"] == "auto"
            and abs(p["lat"] - HOME[0]) < 0.01
            and abs(p["lon"] - HOME[1]) < 0.01
            for p in places
        )

    def test_sync_zones_removes_place_for_a_deleted_zone(
        self, analytics_db: Any
    ) -> None:
        zones = [
            {
                "entity_id": "zone.home",
                "name": "Home",
                "latitude": HOME[0],
                "longitude": HOME[1],
                "radius": 100,
            }
        ]
        work_zone = {
            "entity_id": "zone.work",
            "name": "Work",
            "latitude": WORK[0],
            "longitude": WORK[1],
            "radius": 100,
        }
        analytics_db.sync_zones("real", [*zones, work_zone])
        assert len(analytics_db.list_places("real")) == 2
        analytics_db.sync_zones("real", zones)
        assert [p["zone_entity_id"] for p in analytics_db.list_places("real")] == [
            "zone.home"
        ]

    def test_sync_zones_with_no_zones_loaded_deletes_nothing(
        self, analytics_db: Any
    ) -> None:
        zones = [
            {
                "entity_id": "zone.home",
                "name": "Home",
                "latitude": HOME[0],
                "longitude": HOME[1],
                "radius": 100,
            }
        ]
        analytics_db.sync_zones("real", zones)
        analytics_db.sync_zones("real", [])
        assert len(analytics_db.list_places("real")) == 1

    def test_sync_zones_clamps_radius_to_bounds(self, analytics_db: Any) -> None:
        zones = [
            {
                "entity_id": "zone.tiny",
                "name": "Tiny",
                "latitude": HOME[0],
                "longitude": HOME[1],
                "radius": 1,
            },
            {
                "entity_id": "zone.huge",
                "name": "Huge",
                "latitude": WORK[0],
                "longitude": WORK[1],
                "radius": 10000,
            },
        ]
        analytics_db.sync_zones("real", zones)
        places = {p["zone_entity_id"]: p for p in analytics_db.list_places("real")}
        assert places["zone.tiny"]["radius_m"] == 50
        assert places["zone.huge"]["radius_m"] == 500

    def test_update_place_rename_converts_auto_to_user(self, analytics_db: Any) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        place = analytics_db.list_places("real")[0]
        analytics_db.update_place("real", place["id"], name="Home")
        updated = next(
            p for p in analytics_db.list_places("real") if p["id"] == place["id"]
        )
        assert updated["source"] == "user"
        assert updated["name"] == "Home"
        assert updated["label"] == "Home"

    def test_update_place_hide_leaves_drives_unlabeled_in_day(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        places = analytics_db.list_places("real")
        home_place = next(p for p in places if abs(p["lat"] - HOME[0]) < 0.01)
        analytics_db.update_place("real", home_place["id"], hidden=True)

        payload = analytics_db.day(TEST_VIN, timezone.utc, date(2026, 1, 1))
        for segment in payload["segments"]:
            if segment["start_place"] is not None:
                assert segment["start_place"]["id"] != home_place["id"]
            if segment["end_place"] is not None:
                assert segment["end_place"]["id"] != home_place["id"]

    def test_update_place_unknown_id_raises(self, analytics_db: Any) -> None:
        with pytest.raises(ValueError):
            analytics_db.update_place("real", 999999, name="X")

    def test_update_place_unsupported_field_raises(self, analytics_db: Any) -> None:
        place_id = analytics_db.create_place("real", HOME[0], HOME[1], "Home")
        with pytest.raises(ValueError):
            analytics_db.update_place("real", place_id, bogus_field=1)

    def test_create_place_is_user_sourced_and_assigns_drives(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db, days=1)
        place_id = analytics_db.create_place("real", HOME[0], HOME[1], "Home")
        places = analytics_db.list_places("real")
        created = next(p for p in places if p["id"] == place_id)
        assert created["source"] == "user"
        assert created["name"] == "Home"
        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT start_place_id FROM drives WHERE drive_id = ?",
                ("to_work_1",),
            ).fetchone()
        assert row["start_place_id"] == place_id

    def test_merge_places_moves_visits_and_deletes_merged(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db, days=1)
        home_a = analytics_db.create_place("real", HOME[0], HOME[1], "Home A")
        home_b_lat, home_b_lon = HOME[0] + 0.01, HOME[1] + 0.01
        home_b = analytics_db.create_place("real", home_b_lat, home_b_lon, "Home B")
        # Manually park one drive's start at home_b so it has a visit to move.
        with analytics_db._lock, analytics_db._transaction():
            analytics_db._conn.execute(
                "UPDATE drives SET start_place_id = ? WHERE drive_id = ?",
                (home_b, "to_work_1"),
            )

        analytics_db.merge_places("real", home_a, [home_b])

        remaining_ids = {p["id"] for p in analytics_db.list_places("real")}
        assert home_b not in remaining_ids
        assert home_a in remaining_ids
        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT start_place_id FROM drives WHERE drive_id = ?",
                ("to_work_1",),
            ).fetchone()
        assert row["start_place_id"] == home_a

    def test_merged_auto_place_is_not_redetected_by_a_rebuild(
        self, analytics_db: Any
    ) -> None:
        # Two auto places 250 m apart (just past the 200 m cluster radius).
        self._seed_commute_drives(analytics_db)
        near_work = (WORK[0] + 0.00225, WORK[1])
        drives = [
            _make_drive(
                f"to_near_{day}",
                20.0,
                6.0,
                _timed(f"2026-01-{day:02d}", 12),
                _timed(f"2026-01-{day:02d}", 12, 10),
                start_lat=WORK[0],
                start_lon=WORK[1],
                end_lat=near_work[0],
                end_lon=near_work[1],
            )
            for day in range(1, 5)
        ]
        analytics_db.upsert_drives(TEST_VIN, drives)
        analytics_db.rebuild_places("real")
        places = analytics_db.list_places("real")
        work = next(p for p in places if abs(p["lat"] - WORK[0]) < 0.001)
        near = next(p for p in places if abs(p["lat"] - near_work[0]) < 0.0005)

        analytics_db.merge_places("real", work["id"], [near["id"]])
        analytics_db.rebuild_places("real")

        after = analytics_db.list_places("real")
        assert near["id"] not in {p["id"] for p in after}
        merged = next(p for p in after if p["id"] == work["id"])
        assert merged["source"] == "user"
        assert merged["radius_m"] >= 250
        # No new auto place re-appeared at the merged spot.
        assert not any(
            p["source"] == "auto" and abs(p["lat"] - near_work[0]) < 0.0005
            for p in after
        )

    def test_merge_refuses_to_merge_away_a_zone_place(self, analytics_db: Any) -> None:
        analytics_db.sync_zones(
            "real",
            [
                {
                    "entity_id": "zone.home",
                    "name": "Home",
                    "latitude": HOME[0],
                    "longitude": HOME[1],
                    "radius": 100,
                }
            ],
        )
        zone_id = analytics_db.list_places("real")[0]["id"]
        other = analytics_db.create_place("real", WORK[0], WORK[1], "Work")
        with pytest.raises(ValueError):
            analytics_db.merge_places("real", other, [zone_id])

    def test_moving_or_resizing_an_auto_place_pins_it_as_user(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        place = analytics_db.list_places("real")[0]
        analytics_db.update_place("real", place["id"], radius_m=300)
        updated = next(
            p for p in analytics_db.list_places("real") if p["id"] == place["id"]
        )
        assert updated["source"] == "user"
        assert updated["radius_m"] == 300

    def test_visits_count_a_stop_once_not_as_arrival_plus_departure(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db, days=4)
        analytics_db.rebuild_places("real")
        for place in analytics_db.list_places("real"):
            assert place["visits"] == 4

    def test_places_needing_geocode_only_unnamed_auto_with_min_visits(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        candidates = analytics_db.places_needing_geocode("real")
        assert len(candidates) == 2

        # Naming one removes it from the candidate list.
        analytics_db.update_place("real", candidates[0]["place_id"], name="Named")
        candidates_after = analytics_db.places_needing_geocode("real")
        assert len(candidates_after) == 1

    def test_resized_unnamed_place_is_still_geocoded(self, analytics_db: Any) -> None:
        # Resizing before naming pins the place as "user" but leaves it
        # unnamed; it should still get an OSM name.
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        place_id = analytics_db.places_needing_geocode("real")[0]["place_id"]
        analytics_db.update_place("real", place_id, radius_m=250)
        assert place_id in {
            c["place_id"] for c in analytics_db.places_needing_geocode("real")
        }

    def test_save_geocode_retried_only_after_cooldown(self, analytics_db: Any) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        candidate = analytics_db.places_needing_geocode("real")[0]
        now = time.time()
        analytics_db.save_geocode("real", candidate["place_id"], None, now)

        # Just attempted: not due again yet.
        assert candidate["place_id"] not in {
            c["place_id"] for c in analytics_db.places_needing_geocode("real")
        }

        with analytics_db._lock, analytics_db._transaction():
            analytics_db._conn.execute(
                "UPDATE places SET geocoded_ts = ? WHERE place_id = ?",
                (now - 8 * 86400.0, candidate["place_id"]),
            )
        assert candidate["place_id"] in {
            c["place_id"] for c in analytics_db.places_needing_geocode("real")
        }

    def test_save_geocode_success_sets_geocode_name(self, analytics_db: Any) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        candidate = analytics_db.places_needing_geocode("real")[0]
        analytics_db.save_geocode("real", candidate["place_id"], "Main St", time.time())
        place = next(
            p
            for p in analytics_db.list_places("real")
            if p["id"] == candidate["place_id"]
        )
        assert place["geocode_name"] == "Main St"
        assert place["label"] == "Main St"

    def test_has_unbuilt_places_until_rebuilt(self, analytics_db: Any) -> None:
        assert analytics_db.has_unbuilt_places("real") is True
        analytics_db.rebuild_places("real")
        assert analytics_db.has_unbuilt_places("real") is False

    def test_day_carries_place_labels_for_segments_stops_and_endpoints(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db, days=1)
        analytics_db.create_place("real", HOME[0], HOME[1], "Home")
        analytics_db.create_place("real", WORK[0], WORK[1], "Work")

        payload = analytics_db.day(TEST_VIN, timezone.utc, date(2026, 1, 1))
        assert len(payload["segments"]) == 2
        first, second = payload["segments"]
        assert first["start_place"]["label"] == "Home"
        assert first["end_place"]["label"] == "Work"
        assert second["start_place"]["label"] == "Work"
        assert second["end_place"]["label"] == "Home"

        assert len(payload["stops"]) == 1
        assert payload["stops"][0]["place"]["label"] == "Work"
        assert payload["start"]["place"]["label"] == "Home"
        assert payload["end"]["place"]["label"] == "Home"

    def test_get_drive_detail_carries_place_labels(self, analytics_db: Any) -> None:
        self._seed_commute_drives(analytics_db, days=1)
        analytics_db.create_place("real", HOME[0], HOME[1], "Home")
        analytics_db.create_place("real", WORK[0], WORK[1], "Work")

        detail = analytics_db.get_drive_detail(TEST_VIN, "to_work_1")
        assert detail["drive"]["start_place"]["label"] == "Home"
        assert detail["drive"]["end_place"]["label"] == "Work"

    def test_delete_vin_clears_places(self, analytics_db: Any) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        assert analytics_db.list_places("real") != []
        before = analytics_db.list_places("real")
        analytics_db.delete_vin(TEST_VIN)
        # Places belong to no vehicle: deleting a car's history keeps them
        # (an unvisited, unnamed auto place goes on the next rebuild).
        assert analytics_db.list_places("real") == [
            {
                **p,
                "visits": 0,
                "visits_by_vin": {},
                "arrivals": 0,
                "departures": 0,
                "last_visit_ts": None,
            }
            for p in before
        ]
        analytics_db.rebuild_places("real")
        assert [
            p for p in analytics_db.list_places("real") if p["source"] == "auto"
        ] == []


class TestRoutes:
    """AnalyticsDatabase routes: schema v9 migration, rebuild/list/detail/rename."""

    def _seed_commute_drives(self, db: AnalyticsDatabase, days: int = 4) -> None:
        drives: list[DriveRecord] = []
        for day in range(1, days + 1):
            base = f"2026-01-{day:02d}"
            drives.append(
                _make_drive(
                    f"to_work_{day}",
                    20.0,
                    6.0,
                    _timed(base, 8),
                    _timed(base, 8, 30),
                    start_lat=HOME[0],
                    start_lon=HOME[1],
                    end_lat=WORK[0],
                    end_lon=WORK[1],
                )
            )
            drives.append(
                _make_drive(
                    f"to_home_{day}",
                    20.0,
                    6.0,
                    _timed(base, 17),
                    _timed(base, 17, 30),
                    start_lat=WORK[0],
                    start_lon=WORK[1],
                    end_lat=HOME[0],
                    end_lon=HOME[1],
                )
            )
        db.upsert_drives(TEST_VIN, drives)

    def _set_duration(
        self, db: AnalyticsDatabase, drive_id: str, seconds: float
    ) -> None:
        with db._lock, db._transaction():
            db._conn.execute(
                "UPDATE drives SET duration_seconds = ? WHERE vin = ? AND drive_id = ?",
                (seconds, TEST_VIN, drive_id),
            )

    def test_migrating_v8_db_adds_routes_table_and_drive_route_column(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """Opening a v8 database adds the v9 routes table and drives.route_id."""
        raw = sqlite3.connect(analytics_db_path)
        try:
            raw.executescript(_V6_SCHEMA_SQL)
            raw.execute(
                "CREATE TABLE IF NOT EXISTS osm_roads (bbox_key TEXT PRIMARY KEY, "
                "fetched_ts REAL NOT NULL, data BLOB NOT NULL)"
            )
            raw.execute(
                "CREATE TABLE IF NOT EXISTS track_fills (vin TEXT NOT NULL, "
                "drive_id TEXT NOT NULL, after_t REAL NOT NULL, points_json TEXT "
                "NOT NULL, source TEXT NOT NULL DEFAULT 'osm', created_ts REAL "
                "NOT NULL, PRIMARY KEY (vin, drive_id, after_t))"
            )
            raw.execute(
                "ALTER TABLE drive_tracks ADD COLUMN gaps_scanned INTEGER "
                "NOT NULL DEFAULT 0"
            )
            raw.execute(
                "CREATE TABLE IF NOT EXISTS places (place_id INTEGER PRIMARY KEY, "
                "vin TEXT NOT NULL, name TEXT, category TEXT, lat REAL NOT NULL, "
                "lon REAL NOT NULL, radius_m REAL NOT NULL DEFAULT 150, "
                "source TEXT NOT NULL DEFAULT 'auto', zone_entity_id TEXT, "
                "hidden INTEGER NOT NULL DEFAULT 0, geocode_name TEXT, "
                "geocoded_ts REAL, created_ts REAL NOT NULL, updated_ts REAL NOT NULL)"
            )
            raw.execute("ALTER TABLE drives ADD COLUMN start_place_id INTEGER")
            raw.execute("ALTER TABLE drives ADD COLUMN end_place_id INTEGER")
            raw.execute(
                "INSERT INTO drives (vin, drive_id, start_time, end_time, "
                "distance_miles, duration_seconds, energy_kwh, created_ts, "
                "start_lat, start_lon, end_lat, end_lon) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    TEST_VIN,
                    "pre_migration_v8",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:10:00Z",
                    5.0,
                    600.0,
                    2.0,
                    time.time(),
                    37.0,
                    -122.0,
                    37.1,
                    -122.1,
                ),
            )
            raw.execute("PRAGMA user_version = 8")
            raw.commit()
        finally:
            raw.close()

        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            with db._lock:
                tables = {
                    row[0]
                    for row in db._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                drive_columns = {
                    row[1]
                    for row in db._conn.execute("PRAGMA table_info(drives)").fetchall()
                }
                user_version = db._conn.execute("PRAGMA user_version").fetchone()[0]
                drive_row = db._conn.execute(
                    "SELECT drive_id, route_id FROM drives WHERE vin = ?",
                    (TEST_VIN,),
                ).fetchone()
            assert "routes" in tables
            assert "route_id" in drive_columns
            assert user_version == SCHEMA_VERSION == 14
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
            assert drive_row["drive_id"] == "pre_migration_v8"
            assert drive_row["route_id"] is None

            summary = db.list_drives(TEST_VIN, include_micro=True)
            assert summary[0]["distance_miles"] == 5.0
        finally:
            db.close()

    def test_rebuild_routes_groups_commute_into_two_routes(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        result = analytics_db.rebuild_routes("real")
        assert result["routes"] == 2
        routes = analytics_db.list_routes("real")
        assert len(routes) == 2
        assert {r["drive_count"] for r in routes} == {4}
        assert all(r["variant"] == 1 for r in routes)

    def test_rebuild_routes_below_threshold_is_not_a_route(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db, days=2)
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        assert analytics_db.list_routes("real") == []

    def test_rebuild_routes_is_idempotent_and_ids_stable(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        first_ids = sorted(r["id"] for r in analytics_db.list_routes("real"))

        analytics_db.rebuild_routes("real")
        second_ids = sorted(r["id"] for r in analytics_db.list_routes("real"))
        assert first_ids == second_ids

    def test_rebuild_routes_assigns_drive_route_id(self, analytics_db: Any) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT route_id FROM drives WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "to_work_1"),
            ).fetchone()
        assert row["route_id"] is not None

    def test_list_routes_sorted_by_drive_count_desc_with_stats(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db, days=4)
        # Add a smaller route (3 drives) so the two routes have different sizes.
        extra = [
            _make_drive(
                "to_gym_1",
                5.0,
                1.5,
                _timed("2026-02-01", 9),
                _timed("2026-02-01", 9, 10),
                start_lat=HOME[0],
                start_lon=HOME[1],
                end_lat=HOME[0] + 0.05,
                end_lon=HOME[1] + 0.05,
            ),
            _make_drive(
                "to_gym_2",
                5.0,
                1.5,
                _timed("2026-02-02", 9),
                _timed("2026-02-02", 9, 10),
                start_lat=HOME[0],
                start_lon=HOME[1],
                end_lat=HOME[0] + 0.05,
                end_lon=HOME[1] + 0.05,
            ),
            _make_drive(
                "to_gym_3",
                5.0,
                1.5,
                _timed("2026-02-03", 9),
                _timed("2026-02-03", 9, 10),
                start_lat=HOME[0],
                start_lon=HOME[1],
                end_lat=HOME[0] + 0.05,
                end_lon=HOME[1] + 0.05,
            ),
        ]
        analytics_db.upsert_drives(TEST_VIN, extra)
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        routes = analytics_db.list_routes("real")
        assert len(routes) == 3
        counts = [r["drive_count"] for r in routes]
        assert counts == sorted(counts, reverse=True)
        for route in routes:
            assert "stats" in route
            assert route["stats"]["count"] == route["drive_count"]
            assert route["label"]

    def test_route_detail_returns_rank_vs_avg_and_preview_only(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        self._set_duration(analytics_db, "to_work_1", 500.0)
        self._set_duration(analytics_db, "to_work_2", 600.0)
        self._set_duration(analytics_db, "to_work_3", 700.0)
        self._set_duration(analytics_db, "to_work_4", 600.0)
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")

        # Only one drive gets a stored GPS route.
        analytics_db.upsert_tracks(TEST_VIN, [("to_work_1", _make_track(n=5))])

        route = next(
            r
            for r in analytics_db.list_routes("real")
            if any(
                d["drive_id"] == "to_work_1"
                for d in analytics_db.route_detail("real", r["id"])["drives"]
            )
        )
        detail = analytics_db.route_detail("real", route["id"])
        assert detail["stats"]["count"] == 4
        by_id = {d["drive_id"]: d for d in detail["drives"]}
        assert by_id["to_work_1"]["rank"] == 1
        assert by_id["to_work_3"]["rank"] == 4
        assert by_id["to_work_1"]["preview"] is not None
        assert by_id["to_work_2"]["preview"] is None
        assert "track_json" not in by_id["to_work_1"]
        assert isinstance(by_id["to_work_1"]["preview"]["lat"], list)

    def test_rename_route_sets_name_and_label(self, analytics_db: Any) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        route = analytics_db.list_routes("real")[0]
        analytics_db.rename_route("real", route["id"], "My Commute")
        updated = next(
            r for r in analytics_db.list_routes("real") if r["id"] == route["id"]
        )
        assert updated["name"] == "My Commute"
        assert updated["label"] == "My Commute"

    def test_rename_route_unknown_id_raises(self, analytics_db: Any) -> None:
        with pytest.raises(ValueError):
            analytics_db.rename_route("real", 999999, "X")

    def test_has_unbuilt_routes_until_rebuilt(self, analytics_db: Any) -> None:
        assert analytics_db.has_unbuilt_routes("real") is True
        analytics_db.rebuild_routes("real")
        assert analytics_db.has_unbuilt_routes("real") is False

    def test_rebuild_routes_after_place_merge_reflects_new_grouping(
        self, analytics_db: Any
    ) -> None:
        # Two work-adjacent spots 250 m apart (just past the 200 m cluster
        # radius), each with exactly MIN_ROUTE_DRIVES Home-> drives: two
        # separate, barely-qualifying routes until they're merged into one.
        near_work = (WORK[0] + 0.00225, WORK[1])
        drives = [
            _make_drive(
                f"to_work_{day}",
                20.0,
                6.0,
                _timed(f"2026-01-{day:02d}", 8),
                _timed(f"2026-01-{day:02d}", 8, 30),
                start_lat=HOME[0],
                start_lon=HOME[1],
                end_lat=WORK[0],
                end_lon=WORK[1],
            )
            for day in range(1, 4)
        ] + [
            _make_drive(
                f"to_near_work_{day}",
                20.0,
                6.0,
                _timed(f"2026-02-{day:02d}", 8),
                _timed(f"2026-02-{day:02d}", 8, 30),
                start_lat=HOME[0],
                start_lon=HOME[1],
                end_lat=near_work[0],
                end_lon=near_work[1],
            )
            for day in range(1, 4)
        ]
        analytics_db.upsert_drives(TEST_VIN, drives)
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        before = analytics_db.list_routes("real")
        assert len(before) == 2
        assert {r["drive_count"] for r in before} == {3}

        places = analytics_db.list_places("real")
        work_place = next(p for p in places if abs(p["lat"] - WORK[0]) < 0.001)
        near_place = next(p for p in places if abs(p["lat"] - near_work[0]) < 0.0005)
        # merge_places rebuilds routes itself -- no explicit rebuild_routes call.
        analytics_db.merge_places("real", work_place["id"], [near_place["id"]])

        after = analytics_db.list_routes("real")
        assert len(after) == 1
        assert after[0]["drive_count"] == 6

    def test_create_and_update_place_also_rebuild_routes_automatically(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        assert len(analytics_db.list_routes("real")) == 2

        # Hiding the Home place (a geometry-affecting update) removes it from
        # assignment, which collapses both routes -- with no explicit
        # rebuild_routes call, update_place must have triggered it itself.
        places = analytics_db.list_places("real")
        home_place = next(p for p in places if abs(p["lat"] - HOME[0]) < 0.01)
        analytics_db.update_place("real", home_place["id"], hidden=True)
        assert analytics_db.list_routes("real") == []

    def test_day_carries_route_info_for_segments(self, analytics_db: Any) -> None:
        self._seed_commute_drives(analytics_db, days=4)
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")

        payload = analytics_db.day(TEST_VIN, timezone.utc, date(2026, 1, 1))
        first = payload["segments"][0]
        assert first["route"] is not None
        assert first["route"]["count"] == 4
        assert first["route"]["rank"] is not None
        assert "→" in first["route"]["label"]

    def test_delete_vin_clears_routes(self, analytics_db: Any) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        assert analytics_db.list_routes("real") != []
        analytics_db.delete_vin(TEST_VIN)
        # Routes belong to no vehicle either: they stay until a rebuild
        # finds no drive left to support them.
        assert analytics_db.list_routes("real") != []
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        assert analytics_db.list_routes("real") == []


class TestDeleteDrivesDayPlaceSession:
    """delete_drives/delete_day/delete_place/delete_dcfc_session, and delete_vin's cleanup."""

    def test_delete_drives_removes_rows_tracks_and_rebuilds(
        self, analytics_db: Any
    ) -> None:
        # places.MIN_VISITS is 3, so 3 "keep" drives (plus 1 "gone" one) are
        # needed for auto places to still exist after the delete.
        drives = [
            _make_drive(
                f"keep{i}",
                5.0,
                2.0,
                _timed(f"2026-02-0{i}", 8),
                _timed(f"2026-02-0{i}", 8, 15),
                start_lat=HOME[0],
                start_lon=HOME[1],
                end_lat=WORK[0],
                end_lon=WORK[1],
            )
            for i in range(1, 4)
        ]
        drives.append(
            _make_drive(
                "gone",
                5.0,
                2.0,
                _timed("2026-02-09", 8),
                _timed("2026-02-09", 8, 15),
                start_lat=HOME[0],
                start_lon=HOME[1],
                end_lat=WORK[0],
                end_lon=WORK[1],
            )
        )
        analytics_db.upsert_drives(TEST_VIN, drives)
        analytics_db.upsert_tracks(
            TEST_VIN,
            [
                *((f"keep{i}", _make_track(n=5, lat0=10.0 + i)) for i in range(1, 4)),
                ("gone", _make_track(n=5, lat0=20.0)),
            ],
        )
        analytics_db.update_heat(TEST_VIN, timezone.utc)
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        assert analytics_db.heat_info(TEST_VIN, "all")["drives"] == 4

        result = analytics_db.delete_drives(TEST_VIN, ["gone"])
        assert result["deleted"] == 1
        assert len(result["affected_hours"]) == 1

        with analytics_db._lock:
            drive_count = analytics_db._conn.execute(
                "SELECT COUNT(*) FROM drives WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "gone"),
            ).fetchone()[0]
            track_count = analytics_db._conn.execute(
                "SELECT COUNT(*) FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "gone"),
            ).fetchone()[0]
        assert drive_count == 0
        assert track_count == 0
        assert analytics_db.heat_info(TEST_VIN, "all")["drives"] == 3
        # The remaining "keep" drives are still assigned to their places.
        assert analytics_db.list_places("real") != []

    def test_delete_drives_drops_month_heat_when_no_route_left(
        self, analytics_db: Any
    ) -> None:
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive(
                    "only",
                    5.0,
                    2.0,
                    _timed("2026-02-01", 8),
                    _timed("2026-02-01", 8, 15),
                )
            ],
        )
        analytics_db.upsert_tracks(TEST_VIN, [("only", _make_track(n=5))])
        analytics_db.update_heat(TEST_VIN, timezone.utc)
        assert analytics_db.heat_info(TEST_VIN, "all")["drives"] == 1

        analytics_db.delete_drives(TEST_VIN, ["only"])

        info = analytics_db.heat_info(TEST_VIN, "all")
        assert info["drives"] == 0
        assert info["cells"] == 0
        with analytics_db._lock:
            count = analytics_db._conn.execute(
                "SELECT COUNT(*) FROM road_heat WHERE vin = ?", (TEST_VIN,)
            ).fetchone()[0]
        assert count == 0

    def test_delete_drives_empty_list_is_a_noop(self, analytics_db: Any) -> None:
        assert analytics_db.delete_drives(TEST_VIN, []) == {
            "deleted": 0,
            "affected_hours": [],
        }

    def test_delete_day_uses_local_day_window(self, analytics_db: Any) -> None:
        tz = ZoneInfo("America/Denver")
        # Just after local midnight on day 1, and on day 2.
        day1_early = datetime(2026, 2, 1, 0, 10, tzinfo=tz)
        day2 = datetime(2026, 2, 2, 8, 0, tzinfo=tz)
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive(
                    "day1",
                    5.0,
                    2.0,
                    day1_early.isoformat(),
                    (day1_early + timedelta(minutes=10)).isoformat(),
                ),
                _make_drive(
                    "day2",
                    5.0,
                    2.0,
                    day2.isoformat(),
                    (day2 + timedelta(minutes=10)).isoformat(),
                ),
            ],
        )
        result = analytics_db.delete_day(TEST_VIN, tz, date(2026, 2, 1))
        assert result["deleted"] == 1
        with analytics_db._lock:
            remaining = {
                r["drive_id"]
                for r in analytics_db._conn.execute(
                    "SELECT drive_id FROM drives WHERE vin = ?", (TEST_VIN,)
                ).fetchall()
            }
        assert remaining == {"day2"}

    def test_delete_place_user_place_is_deleted(self, analytics_db: Any) -> None:
        place_id = analytics_db.create_place("real", HOME[0], HOME[1], "Home")
        result = analytics_db.delete_place("real", place_id)
        assert result == {"action": "deleted"}
        assert analytics_db.list_places("real") == []

    def test_delete_place_auto_suggestion_is_hidden_not_deleted(
        self, analytics_db: Any
    ) -> None:
        self._seed_commute_drives(analytics_db)
        analytics_db.rebuild_places("real")
        places = analytics_db.list_places("real")
        auto_place = next(p for p in places if p["source"] == "auto")

        result = analytics_db.delete_place("real", auto_place["id"])
        assert result == {"action": "hidden"}
        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT hidden FROM places WHERE dataset = ? AND place_id = ?",
                ("real", auto_place["id"]),
            ).fetchone()
        assert row["hidden"] == 1

    def _seed_commute_drives(self, db: AnalyticsDatabase, days: int = 4) -> None:
        drives: list[DriveRecord] = []
        for day in range(1, days + 1):
            base = f"2026-03-{day:02d}"
            drives.append(
                _make_drive(
                    f"to_work_{day}",
                    20.0,
                    6.0,
                    _timed(base, 8),
                    _timed(base, 8, 30),
                    start_lat=HOME[0],
                    start_lon=HOME[1],
                    end_lat=WORK[0],
                    end_lon=WORK[1],
                )
            )
        db.upsert_drives(TEST_VIN, drives)

    def test_delete_place_zone_place_raises(self, analytics_db: Any) -> None:
        analytics_db.sync_zones(
            "real",
            [
                {
                    "entity_id": "zone.home",
                    "name": "Home",
                    "latitude": HOME[0],
                    "longitude": HOME[1],
                    "radius": 100,
                }
            ],
        )
        places = analytics_db.list_places("real")
        zone_place = next(p for p in places if p["source"] == "zone")
        with pytest.raises(ValueError):
            analytics_db.delete_place("real", zone_place["id"])

    def test_delete_place_unknown_id_raises(self, analytics_db: Any) -> None:
        with pytest.raises(ValueError):
            analytics_db.delete_place("real", 999999)

    def test_delete_dcfc_session(self, analytics_db: Any) -> None:
        session = ChargingSessionRecord(
            session_id="sess1",
            start_time=_timed("2026-02-01", 10),
            end_time=_timed("2026-02-01", 10, 30),
            start_soc=35.0,
            end_soc=75.0,
            energy_added_kwh=50.0,
            max_power_kw=150.0,
            avg_power_kw=100.0,
        )
        analytics_db.upsert_dcfc_sessions(TEST_VIN, [session])
        assert analytics_db.delete_dcfc_session(TEST_VIN, "sess1") == 1
        assert analytics_db.delete_dcfc_session(TEST_VIN, "sess1") == 0
        with analytics_db._lock:
            count = analytics_db._conn.execute(
                "SELECT COUNT(*) FROM dcfc_sessions WHERE vin = ?", (TEST_VIN,)
            ).fetchone()[0]
        assert count == 0

    def test_delete_vin_clears_pictures_and_meta(self, analytics_db: Any) -> None:
        from custom_components.rivian.analytics_db import VehiclePicture

        analytics_db.save_vehicle_picture(
            TEST_VIN,
            VehiclePicture(
                status="ok",
                content_type="image/png",
                image=b"fake",
                source_url="https://example.com/x.png",
                options=[],
                fetched_ts=time.time(),
            ),
        )
        meta_keys = [
            f"drive_stats_version:{TEST_VIN}",
            f"energy_model:{TEST_VIN}",
            f"road_heat_format:{TEST_VIN}",
        ]
        for key in meta_keys:
            analytics_db.set_meta(key, "1")

        analytics_db.delete_vin(TEST_VIN)

        assert analytics_db.get_vehicle_picture(TEST_VIN) is None
        for key in meta_keys:
            assert analytics_db.get_meta(key) is None
