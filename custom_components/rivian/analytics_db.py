"""SQLite-backed analytics store for Rivian drive, vampire-drain and charging history.

One :class:`AnalyticsDatabase` instance owns a single ``sqlite3`` connection shared
by every vehicle (VIN) known to this Home Assistant instance. All public methods
perform blocking I/O and therefore must be invoked via
``hass.async_add_executor_job`` -- never from the event loop thread.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
import contextlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
import json
import logging
import os
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Final
import zlib

from homeassistant.util import dt as dt_util

from . import places, road_snap, routes as routes_mod
from .drive_conditions import compute_condition_columns
from .drive_models import (
    MICRO_DRIVE_THRESHOLD_MILES,
    MPGE_FACTOR,
    STANDARD_SPEED_BINS,
    AggregatedDriveStats,
    ChargingSample,
    ChargingSessionRecord,
    DriveChunk,
    DriveRecord,
    SpeedBinData,
    VampireDrainRecord,
)
from .drive_stats import compute_track_stats
from .drive_track import DriveTrack, TrackPoint, haversine_m
from .energy_model import (
    DEFAULT_PARAMS as ENERGY_MODEL_DEFAULT_PARAMS,
    MIN_DRIVE_DISTANCE_MI,
    MIN_DRIVE_ENERGY_KWH,
    EnergyModelParams,
    anchored_efficiency,
    fit_params,
    interval_features,
)
from .road_heat import (
    BASE_LEVEL,
    HEAT_FORMAT_VERSION,
    HeatGrid,
    RoadHeat,
    track_cells,
    track_passes,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION: Final[int] = 14
# A session with no recorded position is placed at the end of the drive that
# finished at most this long before it (the car charges where it parked).
SESSION_LOCATION_MAX_GAP_S: Final[float] = 2 * 3600.0
# Unit-conversion constants for recompute_drive_stats (mirrors drive_tracker.py).
_METERS_TO_FEET: Final[float] = 3.28084
_MPS_TO_MPH: Final[float] = 2.23693629
RECOMPUTE_STATS_BATCH_SIZE: Final[int] = 50
DEFAULT_DB_RELATIVE_PATH: Final[str] = ".storage/rivian_analytics.db"
DCFC_CACHE_LIMIT: Final[int] = 50
DCFC_SAMPLE_HYDRATE_LIMIT: Final[int] = 10
DRIVE_CACHE_WINDOW_DAYS: Final[int] = 90
DRIVE_CACHE_LIMIT: Final[int] = 500
DRIVE_HYDRATE_LIMIT: Final[int] = 60
VAMPIRE_CACHE_LIMIT: Final[int] = 250
SECONDS_PER_DAY: Final[float] = 86400.0
TRACK_PREVIEW_MAX_POINTS: Final[int] = 150
TRACK_THIN_BATCH_SIZE: Final[int] = 50
SERIES_WINDOW_ROW_CAP: Final[int] = 5000
HEAT_UPDATE_BATCH_SIZE: Final[int] = 50
# A day view draws a dashed "not recorded" line where the car's position jumps
# between parking and the next recorded point by more than GPS drift at a
# parking spot, but not so far that a straight line would mislead.
# Bump when a drive_stats definition changes: every VIN's stored drives are
# then recomputed once, in the background, after the next start.
DRIVE_STATS_VERSION: Final[int] = 1
DAY_GAP_MIN_M: Final[float] = 150.0
DAY_GAP_MAX_M: Final[float] = 3000.0
# How much of the drive before a day's first segment to carry forward as
# `prior_tail`, so the Efficiency chart's rolling average and 3-min chunks
# have context for the first minutes of the day's first drive instead of a
# blank start.
PRIOR_TAIL_MIN_SOC_DROP_PCT: Final[float] = 1.0
PRIOR_TAIL_MIN_DISTANCE_M: Final[float] = 10_000.0
PRIOR_TAIL_MAX_POINTS: Final[int] = 400
HEAT_CACHE_LIMIT: Final[int] = 24
ENERGY_MODEL_WINDOW_DAYS: Final[int] = 90
ENERGY_MODEL_MIN_DRIVES: Final[int] = 15
# How long a cached OSM road-network fetch (keyed by a coarse bbox grid cell)
# is reused before a stale road edit would need a fresh Overpass fetch.
OSM_ROADS_TTL_SECONDS: Final[float] = 90 * 86400.0
GAPS_TO_SNAP_DEFAULT_LIMIT: Final[int] = 20
# Bump when a places definition changes (mirrors DRIVE_STATS_VERSION): every
# VIN then gets one deterministic rebuild_places() in the background.
PLACES_VERSION: Final[int] = 1
# The ``meta`` row listing the synthetic demo vehicles (written by demo.py).
# Which dataset a VIN's places/routes belong to is derived from it.
DEMO_VEHICLES_META_KEY: Final[str] = "demo_vehicles"
# A place keeps its numbered label and is retried no sooner than this after a
# failed (or no-name) geocode attempt.
GEOCODE_RETRY_SECONDS: Final[float] = 7 * 86400.0
PLACES_GEOCODE_DEFAULT_LIMIT: Final[int] = 10
# Bump to re-run the one-time weather/conditions backfill for every VIN.
WEATHER_VERSION: Final[int] = 1
# The conditions columns the weather backfill fills when NULL (add-only).
_CONDITION_COLUMNS: Final[tuple[str, ...]] = (
    "wind_speed_mph",
    "wind_dir_deg",
    "headwind_mph",
    "precip_mm",
    "pressure_hpa",
    "humidity_pct",
    "air_density",
    "expected_kwh",
)
# Bump when a routes definition changes (mirrors PLACES_VERSION): every VIN
# then gets one deterministic rebuild_routes() in the background.
ROUTES_VERSION: Final[int] = 1

_SCHEMA_SQL: Final[str] = """
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
  drive_modes_json TEXT, trailer INTEGER, driver TEXT,
  start_place_id INTEGER, end_place_id INTEGER,
  wind_speed_mph REAL, wind_dir_deg REAL, headwind_mph REAL, precip_mm REAL,
  pressure_hpa REAL, humidity_pct REAL, air_density REAL, expected_kwh REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_drives ON drives(vin, drive_id);
CREATE INDEX IF NOT EXISTS ix_drives_window ON drives(vin, is_micro_drive, sort_ts);
CREATE INDEX IF NOT EXISTS ix_drives_recent ON drives(vin, sort_ts DESC, created_ts DESC);
CREATE INDEX IF NOT EXISTS ix_drives_start_place ON drives(vin, start_place_id);
CREATE INDEX IF NOT EXISTS ix_drives_end_place ON drives(vin, end_place_id);

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

-- "Charging sessions": the table keeps its original name. ``kind`` is 'dc'
-- (fast charge, with a power curve) or 'ac' (home/Level 2, coarse SoC points).
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
  lat REAL, lon REAL, place_id INTEGER,
  kind TEXT NOT NULL DEFAULT 'dc',
  source TEXT NOT NULL DEFAULT 'live',
  vendor TEXT, network TEXT, station_name TEXT, station_version TEXT,
  charger_max_kw REAL, is_home INTEGER, rivian_txn_id TEXT,
  outside_temp_f REAL, battery_temp_f REAL,
  UNIQUE(vin, session_id)
);
CREATE INDEX IF NOT EXISTS ix_dcfc_recent ON dcfc_sessions(vin, start_ts DESC);
CREATE INDEX IF NOT EXISTS ix_dcfc_kind ON dcfc_sessions(vin, kind, start_ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dcfc_txn ON dcfc_sessions(vin, rivian_txn_id)
  WHERE rivian_txn_id IS NOT NULL;

-- One row per vehicle per local day, kept forever (never pruned by
-- retention): the battery-health chart's source. ``temp_source`` is
-- 'battery' (DC session samples) or 'outside' (drives' weather).
CREATE TABLE IF NOT EXISTS capacity_history (
  vin TEXT NOT NULL, day TEXT NOT NULL, kwh REAL NOT NULL,
  temp_f REAL, temp_source TEXT, source TEXT NOT NULL DEFAULT 'statistics',
  PRIMARY KEY (vin, day)
);

CREATE TABLE IF NOT EXISTS drive_tracks (
  id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
  sort_ts REAL, point_count INTEGER NOT NULL,
  min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
  source TEXT NOT NULL DEFAULT 'live',
  detail TEXT NOT NULL DEFAULT 'full',
  track_json TEXT NOT NULL, preview_json TEXT NOT NULL,
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL,
  gaps_scanned INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS osm_roads (
  bbox_key TEXT PRIMARY KEY, fetched_ts REAL NOT NULL, data BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS track_fills (
  vin TEXT NOT NULL, drive_id TEXT NOT NULL, after_t REAL NOT NULL,
  points_json TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'osm',
  created_ts REAL NOT NULL,
  PRIMARY KEY (vin, drive_id, after_t)
);
CREATE INDEX IF NOT EXISTS ix_track_fills_drive ON track_fills(vin, drive_id);

CREATE TABLE IF NOT EXISTS places (
  place_id INTEGER PRIMARY KEY, dataset TEXT NOT NULL DEFAULT 'real',
  name TEXT, category TEXT,
  lat REAL NOT NULL, lon REAL NOT NULL,
  radius_m REAL NOT NULL DEFAULT 150,
  source TEXT NOT NULL DEFAULT 'auto',
  zone_entity_id TEXT,
  hidden INTEGER NOT NULL DEFAULT 0,
  geocode_name TEXT, geocoded_ts REAL,
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_places_dataset ON places(dataset);
CREATE UNIQUE INDEX IF NOT EXISTS ux_places_zone ON places(dataset, zone_entity_id)
  WHERE zone_entity_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS routes (
  route_id INTEGER PRIMARY KEY, dataset TEXT NOT NULL DEFAULT 'real',
  start_place_id INTEGER NOT NULL, end_place_id INTEGER NOT NULL,
  variant INTEGER NOT NULL, name TEXT,
  drive_count INTEGER NOT NULL DEFAULT 0,
  stats_json TEXT NOT NULL DEFAULT '{}',
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_routes_variant
  ON routes(dataset, start_place_id, end_place_id, variant);
CREATE INDEX IF NOT EXISTS ix_routes_dataset ON routes(dataset);

ALTER TABLE drives ADD COLUMN route_id INTEGER;
CREATE INDEX IF NOT EXISTS ix_drives_route ON drives(vin, route_id);
"""

_UPSERT_DRIVE_SQL: Final[str] = """
INSERT INTO drives (
  vin, drive_id, start_time, end_time, start_ts, end_ts, created_ts,
  distance_miles, duration_seconds, start_soc, end_soc, battery_capacity_kwh,
  energy_kwh, efficiency_mi_kwh, mpge, start_altitude_ft, end_altitude_ft,
  elevation_change_ft, avg_speed_mph, max_speed_mph, integrated_temperature_f,
  is_micro_drive, start_odometer_mi, end_odometer_mi, start_lat, start_lon,
  end_lat, end_lon, speed_bins_json, weather_json, chunks_json,
  moving_seconds, stopped_seconds, stop_count, climb_ft, descent_ft,
  track_max_speed_mph, pct_distance_over_70mph,
  start_range_mi, end_range_mi, drive_modes_json, trailer, driver,
  wind_speed_mph, wind_dir_deg, headwind_mph, precip_mm, pressure_hpa,
  humidity_pct, air_density, expected_kwh
) VALUES (
  :vin, :drive_id, :start_time, :end_time, :start_ts, :end_ts, :created_ts,
  :distance_miles, :duration_seconds, :start_soc, :end_soc, :battery_capacity_kwh,
  :energy_kwh, :efficiency_mi_kwh, :mpge, :start_altitude_ft, :end_altitude_ft,
  :elevation_change_ft, :avg_speed_mph, :max_speed_mph, :integrated_temperature_f,
  :is_micro_drive, :start_odometer_mi, :end_odometer_mi, :start_lat, :start_lon,
  :end_lat, :end_lon, :speed_bins_json, :weather_json, :chunks_json,
  :moving_seconds, :stopped_seconds, :stop_count, :climb_ft, :descent_ft,
  :track_max_speed_mph, :pct_distance_over_70mph,
  :start_range_mi, :end_range_mi, :drive_modes_json, :trailer, :driver,
  :wind_speed_mph, :wind_dir_deg, :headwind_mph, :precip_mm, :pressure_hpa,
  :humidity_pct, :air_density, :expected_kwh
)
ON CONFLICT(vin, drive_id) DO UPDATE SET
  start_time=excluded.start_time, end_time=excluded.end_time,
  start_ts=excluded.start_ts, end_ts=excluded.end_ts,
  distance_miles=excluded.distance_miles, duration_seconds=excluded.duration_seconds,
  start_soc=excluded.start_soc, end_soc=excluded.end_soc,
  battery_capacity_kwh=excluded.battery_capacity_kwh, energy_kwh=excluded.energy_kwh,
  efficiency_mi_kwh=excluded.efficiency_mi_kwh, mpge=excluded.mpge,
  start_altitude_ft=excluded.start_altitude_ft, end_altitude_ft=excluded.end_altitude_ft,
  elevation_change_ft=excluded.elevation_change_ft, avg_speed_mph=excluded.avg_speed_mph,
  max_speed_mph=excluded.max_speed_mph,
  integrated_temperature_f=excluded.integrated_temperature_f,
  is_micro_drive=excluded.is_micro_drive, start_odometer_mi=excluded.start_odometer_mi,
  end_odometer_mi=excluded.end_odometer_mi, start_lat=excluded.start_lat,
  start_lon=excluded.start_lon, end_lat=excluded.end_lat, end_lon=excluded.end_lon,
  speed_bins_json=excluded.speed_bins_json, weather_json=excluded.weather_json,
  chunks_json=excluded.chunks_json,
  moving_seconds=excluded.moving_seconds, stopped_seconds=excluded.stopped_seconds,
  stop_count=excluded.stop_count, climb_ft=excluded.climb_ft,
  descent_ft=excluded.descent_ft, track_max_speed_mph=excluded.track_max_speed_mph,
  pct_distance_over_70mph=excluded.pct_distance_over_70mph,
  start_range_mi=excluded.start_range_mi, end_range_mi=excluded.end_range_mi,
  drive_modes_json=excluded.drive_modes_json, trailer=excluded.trailer,
  driver=excluded.driver,
  -- Conditions are add-only: a re-upsert from a record that never computed
  -- them (a backfill, a legacy import) must not blank what's stored.
  wind_speed_mph=COALESCE(excluded.wind_speed_mph, drives.wind_speed_mph),
  wind_dir_deg=COALESCE(excluded.wind_dir_deg, drives.wind_dir_deg),
  headwind_mph=COALESCE(excluded.headwind_mph, drives.headwind_mph),
  precip_mm=COALESCE(excluded.precip_mm, drives.precip_mm),
  pressure_hpa=COALESCE(excluded.pressure_hpa, drives.pressure_hpa),
  humidity_pct=COALESCE(excluded.humidity_pct, drives.humidity_pct),
  air_density=COALESCE(excluded.air_density, drives.air_density),
  expected_kwh=COALESCE(excluded.expected_kwh, drives.expected_kwh)
"""

_UPSERT_VAMPIRE_SQL: Final[str] = """
INSERT INTO vampire_events (
  vin, start_time, end_time, start_ts, end_ts, created_ts,
  idle_hours, start_soc, end_soc, drain_soc, drain_kwh,
  rate_pct_per_day, avg_watts, avg_temp_f, latitude, longitude
) VALUES (
  :vin, :start_time, :end_time, :start_ts, :end_ts, :created_ts,
  :idle_hours, :start_soc, :end_soc, :drain_soc, :drain_kwh,
  :rate_pct_per_day, :avg_watts, :avg_temp_f, :latitude, :longitude
)
ON CONFLICT(vin, start_time, end_time) DO UPDATE SET
  start_ts=excluded.start_ts, end_ts=excluded.end_ts,
  idle_hours=excluded.idle_hours, start_soc=excluded.start_soc, end_soc=excluded.end_soc,
  drain_soc=excluded.drain_soc, drain_kwh=excluded.drain_kwh,
  rate_pct_per_day=excluded.rate_pct_per_day, avg_watts=excluded.avg_watts,
  avg_temp_f=excluded.avg_temp_f, latitude=excluded.latitude, longitude=excluded.longitude
"""

_UPSERT_DCFC_SQL: Final[str] = """
INSERT INTO dcfc_sessions (
  vin, session_id, start_time, end_time, start_ts, end_ts, created_ts,
  start_soc, end_soc, energy_added_kwh, max_power_kw, avg_power_kw,
  is_dcfc, sample_count, samples_json, lat, lon, place_id, kind, source,
  vendor, network, station_name, station_version, charger_max_kw, is_home,
  rivian_txn_id, outside_temp_f, battery_temp_f
) VALUES (
  :vin, :session_id, :start_time, :end_time, :start_ts, :end_ts, :created_ts,
  :start_soc, :end_soc, :energy_added_kwh, :max_power_kw, :avg_power_kw,
  :is_dcfc, :sample_count, :samples_json, :lat, :lon, :place_id, :kind, :source,
  :vendor, :network, :station_name, :station_version, :charger_max_kw, :is_home,
  :rivian_txn_id, :outside_temp_f, :battery_temp_f
)
ON CONFLICT(vin, session_id) DO UPDATE SET
  start_time=excluded.start_time, end_time=excluded.end_time,
  start_ts=excluded.start_ts, end_ts=excluded.end_ts,
  start_soc=excluded.start_soc, end_soc=excluded.end_soc,
  energy_added_kwh=excluded.energy_added_kwh, max_power_kw=excluded.max_power_kw,
  avg_power_kw=excluded.avg_power_kw, is_dcfc=excluded.is_dcfc,
  sample_count=excluded.sample_count, samples_json=excluded.samples_json,
  lat=excluded.lat, lon=excluded.lon, place_id=excluded.place_id,
  kind=excluded.kind, source=excluded.source,
  vendor=COALESCE(excluded.vendor, vendor),
  network=COALESCE(excluded.network, network),
  station_name=COALESCE(excluded.station_name, station_name),
  station_version=COALESCE(excluded.station_version, station_version),
  charger_max_kw=COALESCE(excluded.charger_max_kw, charger_max_kw),
  is_home=COALESCE(excluded.is_home, is_home),
  rivian_txn_id=COALESCE(excluded.rivian_txn_id, rivian_txn_id),
  outside_temp_f=COALESCE(excluded.outside_temp_f, outside_temp_f),
  battery_temp_f=COALESCE(excluded.battery_temp_f, battery_temp_f)
"""

_UPSERT_TRACK_SQL: Final[str] = """
INSERT INTO drive_tracks (
  vin, drive_id, sort_ts, point_count, min_lat, min_lon, max_lat, max_lon,
  source, detail, track_json, preview_json, created_ts, updated_ts, gaps_scanned
) VALUES (
  :vin, :drive_id, :sort_ts, :point_count, :min_lat, :min_lon, :max_lat, :max_lon,
  :source, :detail, :track_json, :preview_json, :created_ts, :updated_ts, 0
)
ON CONFLICT(vin, drive_id) DO UPDATE SET
  sort_ts=excluded.sort_ts, point_count=excluded.point_count,
  min_lat=excluded.min_lat, min_lon=excluded.min_lon,
  max_lat=excluded.max_lat, max_lon=excluded.max_lon,
  source=excluded.source, detail=excluded.detail,
  track_json=excluded.track_json, preview_json=excluded.preview_json,
  updated_ts=excluded.updated_ts, gaps_scanned=0
"""


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    """Add the per-drive summary-stats and vehicle-context columns (schema v6).

    Individual ``ALTER TABLE ... ADD COLUMN`` statements only -- this runs
    inside the caller's already-open ``BEGIN IMMEDIATE`` transaction, and
    ``executescript`` would implicitly COMMIT that transaction early. Every
    added column is nullable with no default, so existing rows read back as
    NULL/None until a recompute (or a live/backfilled drive) fills them in.
    """
    for column_sql in (
        "moving_seconds REAL",
        "stopped_seconds REAL",
        "stop_count INTEGER",
        "climb_ft REAL",
        "descent_ft REAL",
        "track_max_speed_mph REAL",
        "pct_distance_over_70mph REAL",
        "start_range_mi REAL",
        "end_range_mi REAL",
        "drive_modes_json TEXT",
        "trailer INTEGER",
        "driver TEXT",
    ):
        conn.execute(f"ALTER TABLE drives ADD COLUMN {column_sql}")


def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    """Add the OSM road cache and gap-fill tables, plus drive_tracks.gaps_scanned (v7).

    Individual statements only -- this runs inside the caller's already-open
    ``BEGIN IMMEDIATE`` transaction, and ``executescript`` would implicitly
    COMMIT that transaction early.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS osm_roads (
          bbox_key TEXT PRIMARY KEY, fetched_ts REAL NOT NULL, data BLOB NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS track_fills (
          vin TEXT NOT NULL, drive_id TEXT NOT NULL, after_t REAL NOT NULL,
          points_json TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'osm',
          created_ts REAL NOT NULL,
          PRIMARY KEY (vin, drive_id, after_t)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_track_fills_drive ON track_fills(vin, drive_id)"
    )
    conn.execute(
        "ALTER TABLE drive_tracks ADD COLUMN gaps_scanned INTEGER NOT NULL DEFAULT 0"
    )


def _migrate_to_v8(conn: sqlite3.Connection) -> None:
    """Add the ``places`` table and drives.start_place_id/end_place_id (schema v8).

    Individual statements only -- this runs inside the caller's already-open
    ``BEGIN IMMEDIATE`` transaction, and ``executescript`` would implicitly
    COMMIT that transaction early.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS places (
          place_id INTEGER PRIMARY KEY, vin TEXT NOT NULL,
          name TEXT, category TEXT,
          lat REAL NOT NULL, lon REAL NOT NULL,
          radius_m REAL NOT NULL DEFAULT 150,
          source TEXT NOT NULL DEFAULT 'auto',
          zone_entity_id TEXT,
          hidden INTEGER NOT NULL DEFAULT 0,
          geocode_name TEXT, geocoded_ts REAL,
          created_ts REAL NOT NULL, updated_ts REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_places_vin ON places(vin)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_places_zone ON places(vin, zone_entity_id) "
        "WHERE zone_entity_id IS NOT NULL"
    )
    conn.execute("ALTER TABLE drives ADD COLUMN start_place_id INTEGER")
    conn.execute("ALTER TABLE drives ADD COLUMN end_place_id INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_drives_start_place ON drives(vin, start_place_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_drives_end_place ON drives(vin, end_place_id)"
    )


def _migrate_to_v9(conn: sqlite3.Connection) -> None:
    """Add the ``routes`` table and drives.route_id (schema v9).

    Individual statements only -- this runs inside the caller's already-open
    ``BEGIN IMMEDIATE`` transaction, and ``executescript`` would implicitly
    COMMIT that transaction early.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routes (
          route_id INTEGER PRIMARY KEY, vin TEXT NOT NULL,
          start_place_id INTEGER NOT NULL, end_place_id INTEGER NOT NULL,
          variant INTEGER NOT NULL, name TEXT,
          drive_count INTEGER NOT NULL DEFAULT 0,
          stats_json TEXT NOT NULL DEFAULT '{}',
          created_ts REAL NOT NULL, updated_ts REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_routes_variant "
        "ON routes(vin, start_place_id, end_place_id, variant)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_routes_vin ON routes(vin)")
    conn.execute("ALTER TABLE drives ADD COLUMN route_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_drives_route ON drives(vin, route_id)")


def parse_demo_vins(raw: str | None) -> set[str]:
    """Return the VINs listed in the ``demo_vehicles`` meta value (``[]`` if unreadable)."""
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except ValueError:
        return set()
    if not isinstance(data, list):
        return set()
    return {str(e["vin"]) for e in data if isinstance(e, dict) and e.get("vin")}


def _migrate_to_v10(conn: sqlite3.Connection) -> None:
    """Make places and routes belong to no vehicle (schema v10).

    ``places.vin`` / ``routes.vin`` become a ``dataset`` ('real' | 'demo'; demo
    VINs come from the ``demo_vehicles`` meta row), the per-VIN duplicates
    within a dataset are merged, ``drives.start_place_id`` / ``end_place_id`` /
    ``route_id`` are remapped to the survivors, and the places/routes rebuild
    stamps are cleared so the background rebuild re-clusters and re-groups.
    SQLite can only drop a column by rebuilding the table, so each table is
    created anew, filled and swapped in (this works on every SQLite version).

    Individual statements only -- this runs inside the caller's already-open
    ``BEGIN IMMEDIATE`` transaction, and ``executescript`` would implicitly
    COMMIT that transaction early.
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (DEMO_VEHICLES_META_KEY,)
    ).fetchone()
    demo_vins = parse_demo_vins(row[0] if row is not None else None)

    def dataset_of(vin: str) -> str:
        return places.DATASET_DEMO if vin in demo_vins else places.DATASET_REAL

    old_places = [
        dict(r)
        for r in conn.execute(
            "SELECT place_id, vin, name, category, lat, lon, radius_m, source, "
            "zone_entity_id, hidden, geocode_name, geocoded_ts, created_ts, "
            "updated_ts FROM places ORDER BY place_id"
        ).fetchall()
    ]
    for p in old_places:
        p["dataset"] = dataset_of(p["vin"])

    # Survivor preference: zone, then user (named first), then named auto,
    # then unnamed auto; earliest created, lowest id within a tier.
    def rank(p: dict[str, Any]) -> tuple[int, float, int]:
        if p["source"] == "zone":
            tier = 0
        elif p["source"] == "user":
            tier = 1 if p["name"] else 2
        else:
            tier = 3 if p["name"] else 4
        return (tier, p["created_ts"] or 0.0, p["place_id"])

    place_map: dict[int, int] = {}
    survivors: dict[int, dict[str, Any]] = {}
    zone_survivor: dict[tuple[str, str], int] = {}
    merged_zone = merged_proximity = 0
    for p in sorted(old_places, key=rank):
        pid = p["place_id"]
        match: int | None = None
        if p["source"] == "zone" and p["zone_entity_id"]:
            match = zone_survivor.get((p["dataset"], p["zone_entity_id"]))
        elif p["source"] != "zone":
            best: float | None = None
            for sid, sv in survivors.items():
                if sv["dataset"] != p["dataset"] or sv["source"] == "zone":
                    continue
                dist = haversine_m(sv["lat"], sv["lon"], p["lat"], p["lon"])
                if dist <= max(sv["radius_m"], p["radius_m"]) and (
                    best is None or dist < best
                ):
                    match, best = sid, dist
        if match is None:
            survivors[pid] = dict(p)
            place_map[pid] = pid
            if p["source"] == "zone" and p["zone_entity_id"]:
                zone_survivor[(p["dataset"], p["zone_entity_id"])] = pid
            continue
        place_map[pid] = match
        sv = survivors[match]
        if p["source"] == "zone":
            merged_zone += 1
        else:
            merged_proximity += 1
        if not sv["name"] and p["name"]:
            sv["name"] = p["name"]
            sv["category"] = sv["category"] or p["category"]
        if not sv["category"] and p["category"]:
            sv["category"] = p["category"]
        if not sv["geocode_name"] and p["geocode_name"]:
            sv["geocode_name"] = p["geocode_name"]
            sv["geocoded_ts"] = p["geocoded_ts"]
        # Hidden only when every merged copy was hidden.
        sv["hidden"] = int(bool(sv["hidden"]) and bool(p["hidden"]))
        sv["created_ts"] = min(sv["created_ts"], p["created_ts"])

    conn.execute(
        """
        CREATE TABLE places_v10 (
          place_id INTEGER PRIMARY KEY, dataset TEXT NOT NULL DEFAULT 'real',
          name TEXT, category TEXT,
          lat REAL NOT NULL, lon REAL NOT NULL,
          radius_m REAL NOT NULL DEFAULT 150,
          source TEXT NOT NULL DEFAULT 'auto',
          zone_entity_id TEXT,
          hidden INTEGER NOT NULL DEFAULT 0,
          geocode_name TEXT, geocoded_ts REAL,
          created_ts REAL NOT NULL, updated_ts REAL NOT NULL
        )
        """
    )
    for sv in survivors.values():
        conn.execute(
            "INSERT INTO places_v10 (place_id, dataset, name, category, lat, lon, "
            "radius_m, source, zone_entity_id, hidden, geocode_name, geocoded_ts, "
            "created_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sv["place_id"],
                sv["dataset"],
                sv["name"],
                sv["category"],
                sv["lat"],
                sv["lon"],
                sv["radius_m"],
                sv["source"],
                sv["zone_entity_id"],
                sv["hidden"],
                sv["geocode_name"],
                sv["geocoded_ts"],
                sv["created_ts"],
                sv["updated_ts"],
            ),
        )
    conn.execute("DROP TABLE places")
    conn.execute("ALTER TABLE places_v10 RENAME TO places")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_places_dataset ON places(dataset)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_places_zone "
        "ON places(dataset, zone_entity_id) WHERE zone_entity_id IS NOT NULL"
    )

    # Remap every drive's start/end place to the surviving place.
    for old_id, new_id in place_map.items():
        if old_id != new_id:
            conn.execute(
                "UPDATE drives SET start_place_id = ? WHERE start_place_id = ?",
                (new_id, old_id),
            )
            conn.execute(
                "UPDATE drives SET end_place_id = ? WHERE end_place_id = ?",
                (new_id, old_id),
            )

    old_routes = [
        dict(r)
        for r in conn.execute(
            "SELECT route_id, vin, start_place_id, end_place_id, variant, name, "
            "drive_count, stats_json, created_ts, updated_ts FROM routes "
            "ORDER BY route_id"
        ).fetchall()
    ]
    route_survivor: dict[tuple[str, int, int, int], dict[str, Any]] = {}
    route_map: dict[int, int] = {}
    merged_routes = 0
    # Fullest route first, so it survives; ties by lowest id.
    for r in sorted(
        old_routes, key=lambda r: (-(r["drive_count"] or 0), r["route_id"])
    ):
        r["dataset"] = dataset_of(r["vin"])
        r["start_place_id"] = place_map.get(r["start_place_id"], r["start_place_id"])
        r["end_place_id"] = place_map.get(r["end_place_id"], r["end_place_id"])
        key = (r["dataset"], r["start_place_id"], r["end_place_id"], r["variant"])
        keeper = route_survivor.get(key)
        if keeper is None:
            route_survivor[key] = r
            route_map[r["route_id"]] = r["route_id"]
            continue
        merged_routes += 1
        route_map[r["route_id"]] = keeper["route_id"]
        keeper["name"] = keeper["name"] or r["name"]
        keeper["drive_count"] = (keeper["drive_count"] or 0) + (r["drive_count"] or 0)
        keeper["created_ts"] = min(keeper["created_ts"], r["created_ts"])

    conn.execute(
        """
        CREATE TABLE routes_v10 (
          route_id INTEGER PRIMARY KEY, dataset TEXT NOT NULL DEFAULT 'real',
          start_place_id INTEGER NOT NULL, end_place_id INTEGER NOT NULL,
          variant INTEGER NOT NULL, name TEXT,
          drive_count INTEGER NOT NULL DEFAULT 0,
          stats_json TEXT NOT NULL DEFAULT '{}',
          created_ts REAL NOT NULL, updated_ts REAL NOT NULL
        )
        """
    )
    for r in route_survivor.values():
        # The old stats were per vehicle (per_drive keyed by drive id); the
        # rebuild that follows recomputes them with the new shape.
        conn.execute(
            "INSERT INTO routes_v10 (route_id, dataset, start_place_id, "
            "end_place_id, variant, name, drive_count, stats_json, created_ts, "
            "updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)",
            (
                r["route_id"],
                r["dataset"],
                r["start_place_id"],
                r["end_place_id"],
                r["variant"],
                r["name"],
                r["drive_count"],
                r["created_ts"],
                r["updated_ts"],
            ),
        )
    conn.execute("DROP TABLE routes")
    conn.execute("ALTER TABLE routes_v10 RENAME TO routes")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_routes_variant "
        "ON routes(dataset, start_place_id, end_place_id, variant)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_routes_dataset ON routes(dataset)")
    for old_id, new_id in route_map.items():
        if old_id != new_id:
            conn.execute(
                "UPDATE drives SET route_id = ? WHERE route_id = ?", (new_id, old_id)
            )

    # The rebuild stamps were per VIN; clear them so the background rebuild
    # re-clusters and re-groups each dataset once.
    conn.execute(
        "DELETE FROM meta WHERE key LIKE 'places_version:%' "
        "OR key LIKE 'routes_version:%'"
    )
    _LOGGER.info(
        "Analytics schema v10: places and routes no longer belong to a vehicle -- "
        "%d places -> %d (%d zone duplicates + %d nearby duplicates merged), "
        "%d routes -> %d (%d merged)",
        len(old_places),
        len(survivors),
        merged_zone,
        merged_proximity,
        len(old_routes),
        len(route_survivor),
        merged_routes,
    )
    _V10_MERGE_COUNTS.update(
        places_before=len(old_places),
        places_after=len(survivors),
        zone_merged=merged_zone,
        proximity_merged=merged_proximity,
        routes_before=len(old_routes),
        routes_after=len(route_survivor),
        routes_merged=merged_routes,
    )


# The last v10 migration's merge counts, for tests and the dry-run script.
_V10_MERGE_COUNTS: dict[str, int] = {}


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Add the GPS drive-track tables (schema v2). Individual statements only:

    this runs inside the caller's already-open ``BEGIN IMMEDIATE`` transaction,
    and ``executescript`` would implicitly COMMIT that transaction early.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drive_tracks (
          id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
          sort_ts REAL, point_count INTEGER NOT NULL,
          min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
          source TEXT NOT NULL DEFAULT 'live',
          detail TEXT NOT NULL DEFAULT 'full',
          track_json TEXT NOT NULL, preview_json TEXT NOT NULL,
          created_ts REAL NOT NULL, updated_ts REAL NOT NULL,
          UNIQUE(vin, drive_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_tracks_sort ON drive_tracks(vin, sort_ts)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS active_drive (
          vin TEXT PRIMARY KEY, drive_id TEXT NOT NULL,
          state_json TEXT NOT NULL, updated_ts REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS active_track_chunks (
          id INTEGER PRIMARY KEY, vin TEXT NOT NULL, drive_id TEXT NOT NULL,
          seq INTEGER NOT NULL, points_json TEXT NOT NULL,
          UNIQUE(vin, drive_id, seq)
        )
        """
    )


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """Add the per-vehicle configurator picture table (schema v3)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vehicle_pictures (
          vin TEXT PRIMARY KEY, status TEXT NOT NULL,
          content_type TEXT, image BLOB, source_url TEXT,
          options_json TEXT NOT NULL DEFAULT '[]', fetched_ts REAL NOT NULL
        )
        """
    )


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """Rename drives.segments_json to chunks_json (schema v4).

    A single ``ALTER TABLE ... RENAME COLUMN`` via ``conn.execute`` -- not
    ``executescript``, which would implicitly COMMIT the caller's already-open
    ``BEGIN IMMEDIATE`` transaction early.
    """
    conn.execute("ALTER TABLE drives RENAME COLUMN segments_json TO chunks_json")


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    """Add the road-heat map tables (schema v5)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS road_heat (
          vin TEXT NOT NULL, month TEXT NOT NULL,
          level INTEGER NOT NULL, version INTEGER NOT NULL,
          cell_count INTEGER NOT NULL, drive_count INTEGER NOT NULL,
          data BLOB NOT NULL, updated_ts REAL NOT NULL,
          PRIMARY KEY (vin, month)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS road_heat_drives (
          vin TEXT NOT NULL, drive_id TEXT NOT NULL, month TEXT NOT NULL,
          PRIMARY KEY (vin, drive_id)
        )
        """
    )


def _migrate_to_v11(conn: sqlite3.Connection) -> None:
    """Make ``dcfc_sessions`` hold every charging session (schema v11).

    Adds ``lat``/``lon``/``place_id`` (where it charged), ``kind`` ('dc' | 'ac',
    from ``is_dcfc``) and ``source`` ('live' | 'backfill' | 'demo' | 'rivian' | 'inferred'). Existing
    rows become 'live' (a backfilled row can't be told apart from a live one),
    except a registered demo VIN's, which become 'demo'. Individual statements
    only (see ``_migrate_to_v9``).
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(dcfc_sessions)").fetchall()}
    for column, ddl in (
        ("lat", "REAL"),
        ("lon", "REAL"),
        ("place_id", "INTEGER"),
        ("kind", "TEXT NOT NULL DEFAULT 'dc'"),
        ("source", "TEXT NOT NULL DEFAULT 'live'"),
    ):
        if column not in have:
            conn.execute(f"ALTER TABLE dcfc_sessions ADD COLUMN {column} {ddl}")
    conn.execute(
        "UPDATE dcfc_sessions SET kind = CASE WHEN is_dcfc = 1 THEN 'dc' ELSE 'ac' END"
    )
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (DEMO_VEHICLES_META_KEY,)
    ).fetchone()
    for vin in sorted(parse_demo_vins(row[0] if row is not None else None)):
        conn.execute("UPDATE dcfc_sessions SET source = 'demo' WHERE vin = ?", (vin,))
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_dcfc_kind ON dcfc_sessions(vin, kind, start_ts)"
    )


def _migrate_to_v12(conn: sqlite3.Connection) -> None:
    """Add the per-drive driving-conditions columns (schema v12).

    Wind, precipitation, pressure, humidity, air density, headwind and the
    energy model's expected kWh. All nullable with no default: existing drives
    read back NULL until the weather backfill (or a live finalize) fills them.
    Individual statements only (see ``_migrate_to_v9``); idempotent.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(drives)").fetchall()}
    for column in (
        "wind_speed_mph",
        "wind_dir_deg",
        "headwind_mph",
        "precip_mm",
        "pressure_hpa",
        "humidity_pct",
        "air_density",
        "expected_kwh",
    ):
        if column not in have:
            conn.execute(f"ALTER TABLE drives ADD COLUMN {column} REAL")


def _migrate_to_v13(conn: sqlite3.Connection) -> None:
    """Add station details to charging sessions and the capacity history (schema v13).

    ``dcfc_sessions`` gains nullable ``vendor``, ``network``, ``station_name``,
    ``station_version``, ``charger_max_kw``, ``is_home`` and ``rivian_txn_id``
    (unique per VIN when set); ``capacity_history`` is new. Individual
    statements only (see ``_migrate_to_v9``); idempotent.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(dcfc_sessions)").fetchall()}
    for column, ddl in (
        ("vendor", "TEXT"),
        ("network", "TEXT"),
        ("station_name", "TEXT"),
        ("station_version", "TEXT"),
        ("charger_max_kw", "REAL"),
        ("is_home", "INTEGER"),
        ("rivian_txn_id", "TEXT"),
    ):
        if column not in have:
            conn.execute(f"ALTER TABLE dcfc_sessions ADD COLUMN {column} {ddl}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_dcfc_txn ON dcfc_sessions"
        "(vin, rivian_txn_id) WHERE rivian_txn_id IS NOT NULL"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS capacity_history ("
        "vin TEXT NOT NULL, day TEXT NOT NULL, kwh REAL NOT NULL, "
        "temp_f REAL, temp_source TEXT, source TEXT NOT NULL DEFAULT 'statistics', "
        "PRIMARY KEY (vin, day))"
    )


def _migrate_to_v14(conn: sqlite3.Connection) -> None:
    """Add charging-session temperatures (schema v14).

    ``dcfc_sessions`` gains nullable ``outside_temp_f`` (Open-Meteo, filled
    after the session) and ``battery_temp_f`` (when the vehicle reports one).
    Individual statements only; idempotent.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(dcfc_sessions)").fetchall()}
    for column in ("outside_temp_f", "battery_temp_f"):
        if column not in have:
            conn.execute(f"ALTER TABLE dcfc_sessions ADD COLUMN {column} REAL")


# Ordered schema migrations, keyed by the version they upgrade *to*. A fresh
# database (user_version == 0) runs only `_SCHEMA_SQL` and never touches this
# dict; an existing database upgrades by running each version's migration in
# turn inside one transaction.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
    5: _migrate_to_v5,
    6: _migrate_to_v6,
    7: _migrate_to_v7,
    8: _migrate_to_v8,
    9: _migrate_to_v9,
    10: _migrate_to_v10,
    11: _migrate_to_v11,
    12: _migrate_to_v12,
    13: _migrate_to_v13,
    14: _migrate_to_v14,
}


def _parse_iso_to_epoch(ts_str: str | None) -> float | None:
    """Parse an ISO-8601 timestamp string into a POSIX epoch float, or None."""
    if not ts_str:
        return None
    try:
        normalized = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _trim_track_tail(
    track: DriveTrack,
    min_soc_drop_pct: float = PRIOR_TAIL_MIN_SOC_DROP_PCT,
    min_distance_m: float = PRIOR_TAIL_MIN_DISTANCE_M,
    max_points: int = PRIOR_TAIL_MAX_POINTS,
) -> DriveTrack:
    """Return the last points of ``track`` covering a SoC drop or distance.

    Walks backward from the track's last point until either the SoC has
    dropped at least ``min_soc_drop_pct`` (relative to the last point) or the
    cumulative distance has reached ``min_distance_m``, whichever comes
    first, then hard-caps the result to the last ``max_points`` points.
    """
    pts = track.points
    if len(pts) < 2:
        return DriveTrack(list(pts))

    last_soc = pts[-1].soc
    cum_distance = 0.0
    start_idx = len(pts) - 1
    for i in range(len(pts) - 2, -1, -1):
        point = pts[i]
        nxt = pts[i + 1]
        cum_distance += haversine_m(point.lat, point.lon, nxt.lat, nxt.lon)
        start_idx = i
        soc_drop = (
            point.soc - last_soc
            if point.soc is not None and last_soc is not None
            else None
        )
        if (soc_drop is not None and soc_drop >= min_soc_drop_pct) or (
            cum_distance >= min_distance_m
        ):
            break

    tail_points = pts[start_idx:]
    if len(tail_points) > max_points:
        tail_points = tail_points[-max_points:]
    return DriveTrack(tail_points)


@dataclass(frozen=True)
class HotCache:
    """Immutable, atomically-swapped snapshot of a VIN's analytics used by entities."""

    stats_30d: AggregatedDriveStats
    stats_90d: AggregatedDriveStats
    stats_365d: AggregatedDriveStats
    stats_all_time: AggregatedDriveStats
    last_drive: DriveRecord | None
    recent_drives: list[DriveRecord] = field(default_factory=list)
    recent_vampire_events: list[VampireDrainRecord] = field(default_factory=list)
    dcfc_sessions: list[ChargingSessionRecord] = field(default_factory=list)
    speed_bin_totals: dict[str, dict[str, float]] = field(default_factory=dict)
    drive_count: int = 0
    revision: int = 0
    generated_at: float = 0.0


@dataclass(frozen=True)
class ActiveCheckpoint:
    """A resumable in-progress drive: live state plus its captured track so far."""

    drive_id: str
    state: dict[str, Any]
    track: DriveTrack
    next_seq: int
    updated_ts: float


@dataclass(frozen=True)
class VehiclePicture:
    """A vehicle's saved configurator picture, or the record of a failed fetch."""

    status: str  # "ok" | "failed"
    content_type: str | None
    image: bytes | None
    source_url: str | None
    options: list[str]
    fetched_ts: float


class AnalyticsDatabase:
    """Owns the single shared SQLite connection backing all vehicles' drive analytics."""

    def __init__(self, hass: HomeAssistant, db_path: str | None = None) -> None:
        """Configure the database wrapper without touching disk.

        Construction performs no I/O so it is safe on the event loop; call
        ``setup()`` from an executor job to actually open and migrate.
        """
        self._hass = hass
        self.db_path = db_path or hass.config.path(DEFAULT_DB_RELATIVE_PATH)
        self._lock = threading.RLock()
        # Serializes whole update_heat()/rebuild_heat() runs (never self._lock,
        # which is held only for individual batches) so two runs can never
        # double-count the same drive.
        self._heat_run_lock = threading.Lock()
        # Serializes whole rebuild_places()/rebuild_routes() runs: places and
        # routes are shared by every vehicle, so two stores seeding at once
        # must not both insert the same new cluster.
        self._rebuild_lock = threading.RLock()
        self._claims_lock = threading.Lock()
        self._seed_claims: set[str] = set()
        self._heat_cache_lock = threading.Lock()
        self._heat_cache: OrderedDict[
            tuple[str, str],
            tuple[HeatGrid, tuple[float, float, float, float] | None, int, int],
        ] = OrderedDict()
        # Bumped per VIN on every invalidation, so a grid read from the DB just
        # before a concurrent update commits is never cached after it.
        self._heat_generation: dict[str, int] = {}
        # Prefer the loop thread id hass reports over whichever thread happened
        # to construct us, so the executor-thread guard stays correct even if a
        # test or future caller builds this off-loop.
        self._loop_thread_id = (
            getattr(hass, "loop_thread_id", None) or threading.get_ident()
        )
        self.read_only = False
        self.was_repaired = False
        self._conn: sqlite3.Connection | None = None

    def setup(self) -> None:
        """Open (or create/repair/migrate) the database. Executor-bound."""
        self._assert_thread_only()
        with self._lock:
            if self._conn is not None:
                return
            db_dir = os.path.dirname(self.db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            self._conn = self._connect(self.db_path)
            self._initialize()

    # -- connection / schema lifecycle --------------------------------------

    @staticmethod
    def _connect(path: str) -> sqlite3.Connection:
        """Open a SQLite connection with the pragmas this store depends on."""
        conn = sqlite3.connect(
            path, timeout=15.0, check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        return conn

    @staticmethod
    def _quick_check_ok(conn: sqlite3.Connection) -> bool:
        """Return whether PRAGMA quick_check reports a healthy database."""
        row = conn.execute("PRAGMA quick_check").fetchone()
        return bool(row) and str(row[0]).lower() == "ok"

    def _quarantine_corrupt_file(self) -> None:
        """Rename a corrupt database (and its WAL/SHM siblings) aside, then recreate empty."""
        self._conn.close()
        corrupt_path = f"{self.db_path}.corrupt-{int(time.time())}"
        try:
            os.replace(self.db_path, corrupt_path)
            _LOGGER.error(
                "Analytics database at %s failed integrity check; quarantined to %s",
                self.db_path,
                corrupt_path,
            )
        except OSError as err:
            _LOGGER.error(
                "Analytics database at %s failed integrity check and could not be "
                "quarantined: %s",
                self.db_path,
                err,
            )
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(f"{self.db_path}{suffix}")
        self.was_repaired = True
        self._conn = self._connect(self.db_path)

    def _initialize(self) -> None:
        """Run integrity check, schema creation/migration, and version reconciliation."""
        if not self._quick_check_ok(self._conn):
            self._quarantine_corrupt_file()

        user_version = self._conn.execute("PRAGMA user_version").fetchone()[0]

        if user_version == 0:
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._set_meta_locked("schema_version", str(SCHEMA_VERSION))
        elif user_version < SCHEMA_VERSION:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for version in range(user_version + 1, SCHEMA_VERSION + 1):
                    migration = _MIGRATIONS.get(version)
                    if migration is not None:
                        migration(self._conn)
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        elif user_version > SCHEMA_VERSION:
            _LOGGER.error(
                "Analytics database schema version %d is newer than this integration "
                "supports (%d); it looks like the integration was downgraded. Opening "
                "read-only to avoid corrupting data",
                user_version,
                SCHEMA_VERSION,
            )
            self.read_only = True

    def _assert_thread_only(self) -> None:
        """Guard against accidental blocking I/O on the event loop thread."""
        if threading.get_ident() == self._loop_thread_id:
            raise RuntimeError(
                "AnalyticsDatabase methods perform blocking I/O and must be called "
                "via hass.async_add_executor_job, not on the event loop thread"
            )

    def _assert_executor_thread(self) -> None:
        """Guard the executor thread and that ``setup()`` has opened the connection."""
        self._assert_thread_only()
        if self._conn is None:
            raise RuntimeError("AnalyticsDatabase.setup() must run before use")

    @contextlib.contextmanager
    def _transaction(self):
        """Wrap a block of writes in an explicit BEGIN IMMEDIATE / COMMIT."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def close(self) -> None:
        """Close the underlying SQLite connection, if it was ever opened."""
        self._assert_thread_only()
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- row <-> dataclass conversion ----------------------------------------

    @staticmethod
    def _drive_row_params(
        vin: str, record: DriveRecord, created_ts: float
    ) -> dict[str, Any]:
        serialized = record.to_dict()
        return {
            "vin": vin,
            "drive_id": record.drive_id,
            "start_time": record.start_time,
            "end_time": record.end_time,
            "start_ts": _parse_iso_to_epoch(record.start_time),
            "end_ts": _parse_iso_to_epoch(record.end_time),
            "created_ts": created_ts,
            "distance_miles": record.distance_miles,
            "duration_seconds": record.duration_seconds,
            "start_soc": record.start_soc,
            "end_soc": record.end_soc,
            "battery_capacity_kwh": record.battery_capacity_kwh,
            "energy_kwh": record.energy_kwh,
            "efficiency_mi_kwh": record.efficiency_mi_kwh,
            "mpge": record.mpge,
            "start_altitude_ft": record.start_altitude_ft,
            "end_altitude_ft": record.end_altitude_ft,
            "elevation_change_ft": record.elevation_change_ft,
            "avg_speed_mph": record.avg_speed_mph,
            "max_speed_mph": record.max_speed_mph,
            "integrated_temperature_f": record.integrated_temperature_f,
            "is_micro_drive": int(record.is_micro_drive),
            "start_odometer_mi": record.start_odometer_mi,
            "end_odometer_mi": record.end_odometer_mi,
            "start_lat": record.start_lat,
            "start_lon": record.start_lon,
            "end_lat": record.end_lat,
            "end_lon": record.end_lon,
            "speed_bins_json": json.dumps(serialized.get("speed_bins", {})),
            "weather_json": json.dumps(serialized.get("weather_samples", [])),
            "chunks_json": json.dumps(serialized.get("chunks", [])),
            "moving_seconds": record.moving_seconds,
            "stopped_seconds": record.stopped_seconds,
            "stop_count": record.stop_count,
            "climb_ft": record.climb_ft,
            "descent_ft": record.descent_ft,
            "track_max_speed_mph": record.track_max_speed_mph,
            "pct_distance_over_70mph": record.pct_distance_over_70mph,
            "start_range_mi": record.start_range_mi,
            "end_range_mi": record.end_range_mi,
            "drive_modes_json": json.dumps(record.drive_modes),
            "trailer": (int(record.trailer) if record.trailer is not None else None),
            "driver": record.driver,
            "wind_speed_mph": record.wind_speed_mph,
            "wind_dir_deg": record.wind_dir_deg,
            "headwind_mph": record.headwind_mph,
            "precip_mm": record.precip_mm,
            "pressure_hpa": record.pressure_hpa,
            "humidity_pct": record.humidity_pct,
            "air_density": record.air_density,
            "expected_kwh": record.expected_kwh,
        }

    @staticmethod
    def _row_to_drive(row: sqlite3.Row, hydrate: bool) -> DriveRecord:
        speed_bins_raw = json.loads(row["speed_bins_json"] or "{}")
        speed_bins = {k: SpeedBinData.from_dict(v) for k, v in speed_bins_raw.items()}
        weather_samples = json.loads(row["weather_json"] or "[]") if hydrate else []
        chunks_raw = json.loads(row["chunks_json"] or "[]") if hydrate else []
        chunks = [DriveChunk.from_dict(c) for c in chunks_raw if isinstance(c, dict)]
        return DriveRecord(
            vin=row["vin"],
            drive_id=row["drive_id"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            distance_miles=row["distance_miles"],
            duration_seconds=row["duration_seconds"],
            start_soc=row["start_soc"] or 0.0,
            end_soc=row["end_soc"] or 0.0,
            battery_capacity_kwh=row["battery_capacity_kwh"] or 0.0,
            energy_kwh=row["energy_kwh"],
            efficiency_mi_kwh=row["efficiency_mi_kwh"] or 0.0,
            mpge=row["mpge"] or 0.0,
            start_altitude_ft=row["start_altitude_ft"] or 0.0,
            end_altitude_ft=row["end_altitude_ft"] or 0.0,
            elevation_change_ft=row["elevation_change_ft"] or 0.0,
            avg_speed_mph=row["avg_speed_mph"] or 0.0,
            max_speed_mph=row["max_speed_mph"] or 0.0,
            integrated_temperature_f=row["integrated_temperature_f"],
            speed_bins=speed_bins,
            is_micro_drive=bool(row["is_micro_drive"]),
            start_odometer_mi=row["start_odometer_mi"],
            end_odometer_mi=row["end_odometer_mi"],
            start_lat=row["start_lat"],
            start_lon=row["start_lon"],
            end_lat=row["end_lat"],
            end_lon=row["end_lon"],
            weather_samples=weather_samples,
            chunks=chunks,
            moving_seconds=row["moving_seconds"],
            stopped_seconds=row["stopped_seconds"],
            stop_count=row["stop_count"],
            climb_ft=row["climb_ft"],
            descent_ft=row["descent_ft"],
            track_max_speed_mph=row["track_max_speed_mph"],
            pct_distance_over_70mph=row["pct_distance_over_70mph"],
            start_range_mi=row["start_range_mi"],
            end_range_mi=row["end_range_mi"],
            drive_modes=json.loads(row["drive_modes_json"] or "[]"),
            trailer=(bool(row["trailer"]) if row["trailer"] is not None else None),
            driver=row["driver"],
            wind_speed_mph=row["wind_speed_mph"],
            wind_dir_deg=row["wind_dir_deg"],
            headwind_mph=row["headwind_mph"],
            precip_mm=row["precip_mm"],
            pressure_hpa=row["pressure_hpa"],
            humidity_pct=row["humidity_pct"],
            air_density=row["air_density"],
            expected_kwh=row["expected_kwh"],
        )

    @staticmethod
    def _vampire_row_params(
        vin: str, event: VampireDrainRecord, created_ts: float
    ) -> dict[str, Any]:
        return {
            "vin": vin,
            "start_time": event.start_time,
            "end_time": event.end_time,
            "start_ts": _parse_iso_to_epoch(event.start_time),
            "end_ts": _parse_iso_to_epoch(event.end_time),
            "created_ts": created_ts,
            "idle_hours": event.idle_hours,
            "start_soc": event.start_soc,
            "end_soc": event.end_soc,
            "drain_soc": event.drain_soc,
            "drain_kwh": event.drain_kwh,
            "rate_pct_per_day": event.rate_pct_per_day,
            "avg_watts": event.avg_watts,
            "avg_temp_f": event.avg_temp_f,
            "latitude": event.latitude,
            "longitude": event.longitude,
        }

    @staticmethod
    def _row_to_vampire(row: sqlite3.Row) -> VampireDrainRecord:
        return VampireDrainRecord(
            start_time=row["start_time"],
            end_time=row["end_time"],
            idle_hours=row["idle_hours"],
            start_soc=row["start_soc"],
            end_soc=row["end_soc"],
            drain_soc=row["drain_soc"],
            drain_kwh=row["drain_kwh"],
            rate_pct_per_day=row["rate_pct_per_day"],
            avg_watts=row["avg_watts"],
            avg_temp_f=row["avg_temp_f"],
            latitude=row["latitude"],
            longitude=row["longitude"],
        )

    @staticmethod
    def _dcfc_row_params(
        vin: str, session: ChargingSessionRecord, created_ts: float
    ) -> dict[str, Any]:
        serialized = session.to_dict()
        return {
            "vin": vin,
            "session_id": session.session_id,
            "start_time": session.start_time,
            "end_time": session.end_time,
            "start_ts": _parse_iso_to_epoch(session.start_time),
            "end_ts": _parse_iso_to_epoch(session.end_time),
            "created_ts": created_ts,
            "start_soc": session.start_soc,
            "end_soc": session.end_soc,
            "energy_added_kwh": session.energy_added_kwh,
            "max_power_kw": session.max_power_kw,
            "avg_power_kw": session.avg_power_kw,
            "is_dcfc": int(session.is_dcfc),
            "sample_count": len(session.samples),
            "samples_json": json.dumps(serialized.get("samples", [])),
            "lat": session.lat,
            "lon": session.lon,
            "place_id": session.place_id,
            "kind": session.kind,
            "source": session.source,
            "vendor": session.vendor,
            "network": session.network,
            "station_name": session.station_name,
            "station_version": session.station_version,
            "charger_max_kw": session.charger_max_kw,
            "is_home": None if session.is_home is None else int(session.is_home),
            "rivian_txn_id": session.rivian_txn_id,
            "outside_temp_f": session.outside_temp_f,
            "battery_temp_f": session.battery_temp_f,
        }

    @staticmethod
    def _row_to_dcfc(row: sqlite3.Row, hydrate: bool) -> ChargingSessionRecord:
        samples_raw = json.loads(row["samples_json"] or "[]") if hydrate else []
        samples = [
            ChargingSample.from_dict(s) for s in samples_raw if isinstance(s, dict)
        ]
        return ChargingSessionRecord(
            session_id=row["session_id"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            start_soc=row["start_soc"],
            end_soc=row["end_soc"],
            energy_added_kwh=row["energy_added_kwh"],
            max_power_kw=row["max_power_kw"],
            avg_power_kw=row["avg_power_kw"],
            is_dcfc=bool(row["is_dcfc"]),
            kind=row["kind"],
            lat=row["lat"],
            lon=row["lon"],
            place_id=row["place_id"],
            source=row["source"],
            vendor=row["vendor"],
            network=row["network"],
            station_name=row["station_name"],
            station_version=row["station_version"],
            charger_max_kw=row["charger_max_kw"],
            is_home=None if row["is_home"] is None else bool(row["is_home"]),
            rivian_txn_id=row["rivian_txn_id"],
            outside_temp_f=row["outside_temp_f"],
            battery_temp_f=row["battery_temp_f"],
            samples=samples,
        )

    # -- meta helpers ---------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        """Return a value from the ``meta`` key/value table, or None if absent."""
        self._assert_executor_thread()
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        """Set a value in the ``meta`` key/value table."""
        self._assert_executor_thread()
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; set_meta(%s) skipped", key
                )
                return
            self._set_meta_locked(key, value)

    def _set_meta_locked(self, key: str, value: str) -> None:
        """Set a meta value; caller must already hold ``self._lock``."""
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- executor-side write API -----------------------------------------------

    def upsert_drives(self, vin: str, records: list[DriveRecord]) -> int:
        """Insert or update drive records for a VIN; return the count of NEW rows."""
        self._assert_executor_thread()
        if not records:
            return 0
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; upsert_drives skipped"
                )
                return 0
            created_ts = time.time()
            distinct_ids = list({r.drive_id for r in records})
            with self._transaction():
                existing: set[str] = set()
                if distinct_ids:
                    placeholders = ",".join("?" for _ in distinct_ids)
                    cur = self._conn.execute(
                        f"SELECT drive_id FROM drives WHERE vin = ? "
                        f"AND drive_id IN ({placeholders})",
                        [vin, *distinct_ids],
                    )
                    existing = {r["drive_id"] for r in cur.fetchall()}

                new_count = 0
                seen: set[str] = set()
                for record in records:
                    if record.drive_id not in existing and record.drive_id not in seen:
                        new_count += 1
                    seen.add(record.drive_id)
                    self._conn.execute(
                        _UPSERT_DRIVE_SQL,
                        self._drive_row_params(vin, record, created_ts),
                    )
            return new_count

    def insert_vampire_event(self, vin: str, event: VampireDrainRecord) -> None:
        """Insert or update a single vampire drain event."""
        self._assert_executor_thread()
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; insert_vampire_event skipped"
                )
                return
            created_ts = time.time()
            with self._transaction():
                self._conn.execute(
                    _UPSERT_VAMPIRE_SQL,
                    self._vampire_row_params(vin, event, created_ts),
                )

    def merge_vampire_events(self, vin: str, events: list[VampireDrainRecord]) -> int:
        """Upsert-merge vampire drain events; return the count of NEW rows."""
        self._assert_executor_thread()
        if not events:
            return 0
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; merge_vampire_events skipped"
                )
                return 0
            created_ts = time.time()
            with self._transaction():
                cur = self._conn.execute(
                    "SELECT start_time, end_time FROM vampire_events WHERE vin = ?",
                    (vin,),
                )
                existing = {(r["start_time"], r["end_time"]) for r in cur.fetchall()}

                new_count = 0
                seen: set[tuple[str, str]] = set()
                for event in events:
                    key = (event.start_time, event.end_time)
                    if key not in existing and key not in seen:
                        new_count += 1
                    seen.add(key)
                    self._conn.execute(
                        _UPSERT_VAMPIRE_SQL,
                        self._vampire_row_params(vin, event, created_ts),
                    )
            return new_count

    def upsert_dcfc_sessions(
        self, vin: str, sessions: list[ChargingSessionRecord]
    ) -> None:
        """Insert or update DC fast charging session records."""
        self._assert_executor_thread()
        if not sessions:
            return
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; upsert_dcfc_sessions skipped"
                )
                return
            created_ts = time.time()
            dataset = (
                places.DATASET_DEMO
                if vin in self._demo_vins_locked()
                else places.DATASET_REAL
            )
            with self._transaction():
                for session in sessions:
                    if (
                        session.place_id is None
                        and session.lat is not None
                        and session.lon is not None
                    ):
                        session.place_id = self._place_id_at_locked(
                            dataset, session.lat, session.lon
                        )
                    self._conn.execute(
                        _UPSERT_DCFC_SQL,
                        self._dcfc_row_params(vin, session, created_ts),
                    )

    def _place_id_at_locked(self, dataset: str, lat: float, lon: float) -> int | None:
        """Return the nearest non-hidden place of ``dataset`` containing the point.

        Caller must hold ``self._lock``.
        """
        rows = self._conn.execute(
            "SELECT place_id, lat, lon, radius_m, hidden FROM places WHERE dataset = ?",
            (dataset,),
        ).fetchall()
        return places.assign(
            (lat, lon),
            [
                places.ExistingPlace(
                    place_id=r["place_id"],
                    lat=r["lat"],
                    lon=r["lon"],
                    radius_m=r["radius_m"],
                    source="",
                    hidden=bool(r["hidden"]),
                )
                for r in rows
            ],
        )

    def _reassign_session_places_locked(self, dataset: str) -> None:
        """Re-point every located charging session of ``dataset`` at its place.

        Caller must hold ``self._lock`` and be inside a transaction.
        """
        clause, clause_params = self._dataset_drive_clause(dataset)
        rows = self._conn.execute(
            f"SELECT id, lat, lon, place_id FROM dcfc_sessions "
            f"WHERE lat IS NOT NULL AND lon IS NOT NULL AND {clause}",
            clause_params,
        ).fetchall()
        for r in rows:
            new_id = self._place_id_at_locked(dataset, r["lat"], r["lon"])
            if new_id != r["place_id"]:
                self._conn.execute(
                    "UPDATE dcfc_sessions SET place_id = ? WHERE id = ?",
                    (new_id, r["id"]),
                )

    def list_charging_sessions(
        self,
        vin: str,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return a VIN's charging sessions (DC and AC), ascending, hydrated.

        Each item is the record's ``to_dict()`` plus ``start_ts``/``end_ts``
        (epoch seconds) and ``place`` (``{"id", "label", "category",
        "zone_entity_id"}`` or None; a hidden or deleted place labels nothing).
        """
        self._assert_executor_thread()
        with self._lock:
            dataset = (
                places.DATASET_DEMO
                if vin in self._demo_vins_locked()
                else places.DATASET_REAL
            )
            query = "SELECT * FROM dcfc_sessions WHERE vin = ?"
            params: list[Any] = [vin]
            if since_ts is not None:
                query += " AND COALESCE(end_ts, start_ts) >= ?"
                params.append(since_ts)
            if until_ts is not None:
                query += " AND COALESCE(start_ts, end_ts) <= ?"
                params.append(until_ts)
            query += " ORDER BY sort_ts ASC, id ASC"
            rows = self._conn.execute(query, params).fetchall()
            place_rows = self._conn.execute(
                "SELECT place_id, name, geocode_name, hidden, category, "
                "zone_entity_id FROM places WHERE dataset = ?",
                (dataset,),
            ).fetchall()
        labels = {
            r["place_id"]: {
                "id": r["place_id"],
                "label": places.place_label(
                    r["name"], r["geocode_name"], r["place_id"]
                ),
                "category": r["category"],
                "zone_entity_id": r["zone_entity_id"],
            }
            for r in place_rows
            if not r["hidden"]
        }
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._row_to_dcfc(row, hydrate=True).to_dict()
            item["start_ts"] = row["start_ts"]
            item["end_ts"] = row["end_ts"]
            pid = row["place_id"]
            item["place"] = dict(labels[pid]) if pid in labels else None
            result.append(item)
        return result

    def charging_session_intervals(
        self, vin: str, exclude_inferred: bool = False
    ) -> list[tuple[float, float]]:
        """Return every stored session's ``(start_ts, end_ts)``, for backfill dedupe.

        ``exclude_inferred`` leaves out the sessions inferred from the battery
        level (the inference re-derives those itself).
        """
        self._assert_executor_thread()
        query = (
            "SELECT start_ts, end_ts FROM dcfc_sessions WHERE vin = ? "
            "AND start_ts IS NOT NULL"
        )
        if exclude_inferred:
            query += " AND source != 'inferred'"
        with self._lock:
            rows = self._conn.execute(query, (vin,)).fetchall()
        return [(r["start_ts"], r["end_ts"] or r["start_ts"]) for r in rows]

    def drive_end_times(self, vin: str, start_ts: float, end_ts: float) -> list[float]:
        """Return the end times of a VIN's drives (micro drives too) ending in a window."""
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                "SELECT end_ts FROM drives WHERE vin = ? AND end_ts IS NOT NULL "
                "AND end_ts >= ? AND end_ts <= ? ORDER BY end_ts",
                (vin, start_ts, end_ts),
            ).fetchall()
        return [r["end_ts"] for r in rows]

    def soc_events(self, vin: str, start_ts: float, end_ts: float) -> dict[str, Any]:
        """Return the drives and charging sessions overlapping a window.

        Feeds the synthesized battery-% timeline of vehicles without recorder
        statistics (the demo cars). A drive carries its stored route preview's
        SoC points when it has a route. Reaches a few days before ``start_ts``
        so the series has a value at the window's start.
        """
        self._assert_executor_thread()
        lo = start_ts - 3 * SECONDS_PER_DAY
        with self._lock:
            drive_rows = self._conn.execute(
                "SELECT d.start_ts, d.end_ts, d.start_soc, d.end_soc, "
                "t.preview_json AS preview_json FROM drives d "
                "LEFT JOIN drive_tracks t ON t.vin = d.vin AND t.drive_id = d.drive_id "
                "WHERE d.vin = ? AND d.start_ts IS NOT NULL AND d.end_ts IS NOT NULL "
                "AND d.end_ts >= ? AND d.start_ts <= ? ORDER BY d.start_ts ASC",
                (vin, lo, end_ts),
            ).fetchall()
            session_rows = self._conn.execute(
                "SELECT start_ts, end_ts, start_soc, end_soc, kind, samples_json "
                "FROM dcfc_sessions WHERE vin = ? AND start_ts IS NOT NULL "
                "AND end_ts IS NOT NULL AND end_ts >= ? AND start_ts <= ? "
                "ORDER BY start_ts ASC",
                (vin, lo, end_ts),
            ).fetchall()
        drives: list[dict[str, Any]] = []
        for r in drive_rows:
            points: list[tuple[float, float]] = []
            if r["preview_json"]:
                with contextlib.suppress(ValueError, KeyError, TypeError):
                    points = [
                        (p.t, p.soc)
                        for p in DriveTrack.decode(r["preview_json"]).points
                        if p.soc is not None
                    ]
            drives.append(
                {
                    "start_ts": r["start_ts"],
                    "end_ts": r["end_ts"],
                    "start_soc": r["start_soc"],
                    "end_soc": r["end_soc"],
                    "points": points,
                }
            )
        sessions: list[dict[str, Any]] = []
        for r in session_rows:
            spoints: list[tuple[float, float]] = []
            with contextlib.suppress(ValueError, TypeError):
                for sample in json.loads(r["samples_json"] or "[]"):
                    ts = _parse_iso_to_epoch(sample.get("timestamp"))
                    if ts is not None and sample.get("soc") is not None:
                        spoints.append((ts, float(sample["soc"])))
            sessions.append(
                {
                    "start_ts": r["start_ts"],
                    "end_ts": r["end_ts"],
                    "start_soc": r["start_soc"],
                    "end_soc": r["end_soc"],
                    "kind": r["kind"],
                    "points": spoints,
                }
            )
        return {"drives": drives, "sessions": sessions}

    def capacity_rows(self, vin: str) -> list[dict[str, Any]]:
        """Return each drive's capacity/range readings, ascending, for the health chart.

        Only drives that recorded a ``battery_capacity_kwh``; ``end_range_mi``
        and ``end_soc`` may be None.
        """
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                "SELECT sort_ts, battery_capacity_kwh, end_soc, end_range_mi "
                "FROM drives WHERE vin = ? AND sort_ts IS NOT NULL "
                "AND battery_capacity_kwh IS NOT NULL AND battery_capacity_kwh > 0 "
                "ORDER BY sort_ts ASC",
                (vin,),
            ).fetchall()
        return [
            {
                "ts": r["sort_ts"],
                "capacity_kwh": r["battery_capacity_kwh"],
                "end_soc": r["end_soc"],
                "end_range_mi": r["end_range_mi"],
            }
            for r in rows
        ]

    # -- capacity history (kept forever) --------------------------------------

    def capacity_history_rows(self, vin: str) -> list[dict[str, Any]]:
        """Return a VIN's ``capacity_history`` rows, oldest day first."""
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, kwh, temp_f, temp_source, source FROM capacity_history "
                "WHERE vin = ? ORDER BY day ASC",
                (vin,),
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_capacity_history(self, vin: str, rows: list[dict[str, Any]]) -> int:
        """Insert or replace per-day capacity rows (``day``, ``kwh``, ``temp_f``,
        ``temp_source``, ``source``); returns how many were written. Never pruned."""
        self._assert_executor_thread()
        if not rows:
            return 0
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; upsert_capacity_history skipped"
                )
                return 0
            with self._transaction():
                for row in rows:
                    self._conn.execute(
                        "INSERT INTO capacity_history "
                        "(vin, day, kwh, temp_f, temp_source, source) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(vin, day) DO UPDATE SET kwh=excluded.kwh, "
                        "temp_f=excluded.temp_f, temp_source=excluded.temp_source, "
                        "source=excluded.source",
                        (
                            vin,
                            row["day"],
                            float(row["kwh"]),
                            row.get("temp_f"),
                            row.get("temp_source"),
                            row.get("source") or "statistics",
                        ),
                    )
        return len(rows)

    def capacity_day_inputs(self, vin: str, tz: tzinfo) -> dict[str, dict[str, Any]]:
        """Return per local day (``YYYY-MM-DD``) the capacity inputs stored here.

        ``kwh``: the day's largest drive-reported battery capacity;
        ``outside_f``: mean of the day's drives' temperature;
        ``battery_f``: mean battery temperature over the day's DC session samples.
        Any key is absent when there is nothing for it.
        """
        self._assert_executor_thread()
        with self._lock:
            drive_rows = self._conn.execute(
                "SELECT sort_ts, battery_capacity_kwh, integrated_temperature_f "
                "FROM drives WHERE vin = ? AND sort_ts IS NOT NULL",
                (vin,),
            ).fetchall()
            session_rows = self._conn.execute(
                "SELECT start_ts, samples_json FROM dcfc_sessions WHERE vin = ? "
                "AND kind = 'dc' AND start_ts IS NOT NULL AND sample_count > 0",
                (vin,),
            ).fetchall()
        days: dict[str, dict[str, Any]] = {}

        def day_of(ts: float) -> str:
            return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d")

        outside: dict[str, list[float]] = {}
        battery: dict[str, list[float]] = {}
        for r in drive_rows:
            day = day_of(r["sort_ts"])
            entry = days.setdefault(day, {})
            kwh = r["battery_capacity_kwh"]
            if kwh and kwh > 0:
                entry["kwh"] = max(entry.get("kwh", 0.0), float(kwh))
            if r["integrated_temperature_f"] is not None:
                outside.setdefault(day, []).append(float(r["integrated_temperature_f"]))
        for r in session_rows:
            with contextlib.suppress(ValueError, TypeError):
                temps = [
                    float(smp["battery_temp_f"])
                    for smp in json.loads(r["samples_json"] or "[]")
                    if isinstance(smp, dict) and smp.get("battery_temp_f") is not None
                ]
                if temps:
                    battery.setdefault(day_of(r["start_ts"]), []).extend(temps)
        for day, vals in outside.items():
            days.setdefault(day, {})["outside_f"] = sum(vals) / len(vals)
        for day, vals in battery.items():
            days.setdefault(day, {})["battery_f"] = sum(vals) / len(vals)
        return days

    # -- Rivian-app history import / station lookup ----------------------------

    def sessions_for_history_match(self, vin: str) -> list[dict[str, Any]]:
        """Return every stored session's identity fields (no samples) for matching."""
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, kind, start_ts, end_ts, start_soc, end_soc, "
                "energy_added_kwh, vendor, network, is_home, rivian_txn_id, source "
                "FROM dcfc_sessions WHERE vin = ? AND start_ts IS NOT NULL",
                (vin,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_session_fields(
        self, vin: str, session_id: str, fields: dict[str, Any]
    ) -> None:
        """Set a whitelisted subset of one session's station/energy fields."""
        self._assert_executor_thread()
        allowed = {
            "vendor",
            "network",
            "station_name",
            "station_version",
            "charger_max_kw",
            "is_home",
            "rivian_txn_id",
            "energy_added_kwh",
            "lat",
            "lon",
            "place_id",
            "outside_temp_f",
            "battery_temp_f",
        }
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return
        with self._lock:
            if self.read_only:
                return
            assignments = ", ".join(f"{c} = ?" for c in cols)
            with self._transaction():
                self._conn.execute(
                    f"UPDATE dcfc_sessions SET {assignments} "
                    "WHERE vin = ? AND session_id = ?",
                    [*cols.values(), vin, session_id],
                )

    def fill_session_locations_from_drives(self, vin: str) -> int:
        """Locate sessions recorded without a position from the drive before them.

        A car charges where it parked, so a session with no lat/lon takes the
        end point of the drive that finished within ``SESSION_LOCATION_MAX_GAP_S``
        before it (sessions recorded before v11 captured no location). Returns
        how many sessions were updated. Demo sessions are left alone.
        """
        self._assert_executor_thread()
        prior = (
            "SELECT {col} FROM drives d WHERE d.vin = dcfc_sessions.vin "
            "AND d.end_ts IS NOT NULL AND d.end_ts <= dcfc_sessions.start_ts "
            "AND d.end_ts >= dcfc_sessions.start_ts - ? "
            "AND d.end_lat IS NOT NULL AND d.end_lon IS NOT NULL "
            "ORDER BY d.end_ts DESC LIMIT 1"
        )
        gap = SESSION_LOCATION_MAX_GAP_S
        with self._lock, self._transaction():
            cur = self._conn.execute(
                f"UPDATE dcfc_sessions SET lat = ({prior.format(col='d.end_lat')}), "
                f"lon = ({prior.format(col='d.end_lon')}) "
                "WHERE vin = ? AND lat IS NULL AND source != 'demo' "
                f"AND EXISTS ({prior.format(col='1')})",
                (gap, gap, vin, gap),
            )
            return cur.rowcount or 0

    def sessions_needing_outside_temp(
        self, vin: str, before_ts: float, limit: int = 60
    ) -> list[dict[str, Any]]:
        """Return located sessions (DC and AC) with no outside temperature yet.

        Only sessions that ended before ``before_ts`` (the weather service needs
        the hours to have happened), newest first, never demo rows.
        """
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, start_ts, end_ts, lat, lon FROM dcfc_sessions "
                "WHERE vin = ? AND lat IS NOT NULL AND lon IS NOT NULL "
                "AND start_ts IS NOT NULL AND outside_temp_f IS NULL "
                "AND COALESCE(end_ts, start_ts) <= ? AND source != 'demo' "
                "ORDER BY start_ts DESC LIMIT ?",
                (vin, before_ts, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def dc_sessions_needing_station(
        self, vin: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return located DC sessions with no station info yet, newest first."""
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, lat, lon FROM dcfc_sessions WHERE vin = ? "
                "AND kind = 'dc' AND lat IS NOT NULL AND lon IS NOT NULL "
                "AND station_name IS NULL AND network IS NULL AND vendor IS NULL "
                "AND source != 'demo' ORDER BY start_ts DESC LIMIT ?",
                (vin, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def level_before(self, vin: str, ts: float) -> tuple[float, float | None] | None:
        """Return ``(soc, battery_capacity_kwh)`` the car had just before ``ts``.

        From the latest drive or session ending before ``ts`` (its end SoC);
        the capacity is the newest drive-reported one. None when there is
        nothing earlier.
        """
        self._assert_executor_thread()
        with self._lock:
            drive = self._conn.execute(
                "SELECT end_ts, end_soc FROM drives "
                "WHERE vin = ? AND end_ts IS NOT NULL AND end_ts <= ? "
                "AND end_soc IS NOT NULL ORDER BY end_ts DESC LIMIT 1",
                (vin, ts),
            ).fetchone()
            session = self._conn.execute(
                "SELECT end_ts, end_soc FROM dcfc_sessions WHERE vin = ? "
                "AND end_ts IS NOT NULL AND end_ts <= ? ORDER BY end_ts DESC LIMIT 1",
                (vin, ts),
            ).fetchone()
            cap = self._conn.execute(
                "SELECT battery_capacity_kwh FROM drives WHERE vin = ? "
                "AND battery_capacity_kwh > 0 ORDER BY sort_ts DESC LIMIT 1",
                (vin,),
            ).fetchone()
        capacity = float(cap[0]) if cap is not None else None
        best: tuple[float, float] | None = None
        for row in (drive, session):
            if row is not None and (best is None or row["end_ts"] > best[0]):
                best = (row["end_ts"], row["end_soc"])
        return None if best is None else (float(best[1]), capacity)

    def get_cached_osm(self, key: str) -> Any | None:
        """Return a cached JSON value (any OSM lookup) for ``key``, or None if
        absent, older than 90 days or corrupt. Shares the ``osm_roads`` table."""
        self._assert_executor_thread()
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_ts, data FROM osm_roads WHERE bbox_key = ?", (key,)
            ).fetchone()
        if row is None or time.time() - row["fetched_ts"] > OSM_ROADS_TTL_SECONDS:
            return None
        try:
            return json.loads(zlib.decompress(row["data"]))
        except (zlib.error, ValueError, TypeError):
            return None

    def save_cached_osm(self, key: str, value: Any) -> None:
        """Cache a JSON-able OSM lookup result under ``key`` for 90 days."""
        self._assert_executor_thread()
        if self.read_only:
            return
        data = zlib.compress(json.dumps(value, separators=(",", ":")).encode())
        with self._lock, self._transaction():
            self._conn.execute(
                "INSERT INTO osm_roads (bbox_key, fetched_ts, data) VALUES (?, ?, ?) "
                "ON CONFLICT(bbox_key) DO UPDATE SET "
                "fetched_ts=excluded.fetched_ts, data=excluded.data",
                (key, time.time(), data),
            )

    def migrate_legacy_json(
        self,
        vin: str,
        drives: list[DriveRecord],
        vampire_events: list[VampireDrainRecord],
        dcfc_sessions: list[ChargingSessionRecord],
    ) -> dict[str, int]:
        """Import legacy per-VIN JSON records into SQLite in a single transaction.

        Also writes the ``json_migrated_<vin>`` meta marker atomically with the
        data so the import is idempotent even if interrupted mid-way.
        """
        self._assert_executor_thread()
        counts = {"drives": 0, "vampire_events": 0, "dcfc_sessions": 0}
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; migrate_legacy_json skipped"
                )
                return counts
            created_ts = time.time()
            with self._transaction():
                if drives:
                    distinct_ids = list({d.drive_id for d in drives})
                    existing_d: set[str] = set()
                    if distinct_ids:
                        placeholders = ",".join("?" for _ in distinct_ids)
                        cur = self._conn.execute(
                            f"SELECT drive_id FROM drives WHERE vin = ? "
                            f"AND drive_id IN ({placeholders})",
                            [vin, *distinct_ids],
                        )
                        existing_d = {r["drive_id"] for r in cur.fetchall()}
                    seen_d: set[str] = set()
                    for d in drives:
                        if d.drive_id not in existing_d and d.drive_id not in seen_d:
                            counts["drives"] += 1
                        seen_d.add(d.drive_id)
                        self._conn.execute(
                            _UPSERT_DRIVE_SQL,
                            self._drive_row_params(vin, d, created_ts),
                        )

                if vampire_events:
                    cur = self._conn.execute(
                        "SELECT start_time, end_time FROM vampire_events WHERE vin = ?",
                        (vin,),
                    )
                    existing_v = {
                        (r["start_time"], r["end_time"]) for r in cur.fetchall()
                    }
                    seen_v: set[tuple[str, str]] = set()
                    for v in vampire_events:
                        key = (v.start_time, v.end_time)
                        if key not in existing_v and key not in seen_v:
                            counts["vampire_events"] += 1
                        seen_v.add(key)
                        self._conn.execute(
                            _UPSERT_VAMPIRE_SQL,
                            self._vampire_row_params(vin, v, created_ts),
                        )

                if dcfc_sessions:
                    for s in dcfc_sessions:
                        self._conn.execute(
                            _UPSERT_DCFC_SQL, self._dcfc_row_params(vin, s, created_ts)
                        )
                    counts["dcfc_sessions"] = len(dcfc_sessions)

                self._set_meta_locked(f"json_migrated_{vin}", json.dumps(counts))
        return counts

    def delete_vin(self, vin: str) -> None:
        """Delete all analytics rows (drives, vampire events, DCFC sessions) for a VIN.

        Never deletes places or routes: they belong to no vehicle.
        """
        self._assert_executor_thread()
        with self._lock:
            if self.read_only:
                _LOGGER.warning("Analytics database is read-only; delete_vin skipped")
                return
            with self._transaction():
                self._conn.execute("DELETE FROM drives WHERE vin = ?", (vin,))
                self._conn.execute("DELETE FROM vampire_events WHERE vin = ?", (vin,))
                self._conn.execute("DELETE FROM dcfc_sessions WHERE vin = ?", (vin,))
                self._conn.execute("DELETE FROM capacity_history WHERE vin = ?", (vin,))
                self._conn.execute("DELETE FROM drive_tracks WHERE vin = ?", (vin,))
                self._conn.execute("DELETE FROM active_drive WHERE vin = ?", (vin,))
                self._conn.execute(
                    "DELETE FROM active_track_chunks WHERE vin = ?", (vin,)
                )
                self._conn.execute("DELETE FROM road_heat WHERE vin = ?", (vin,))
                self._conn.execute("DELETE FROM road_heat_drives WHERE vin = ?", (vin,))
                self._conn.execute("DELETE FROM track_fills WHERE vin = ?", (vin,))
                # Places and routes belong to no vehicle, so they stay; a
                # place no remaining drive visits (and nobody named) goes away
                # on the next rebuild_places().
                self._conn.execute("DELETE FROM vehicle_pictures WHERE vin = ?", (vin,))
                self._conn.execute(
                    "DELETE FROM meta WHERE key IN (?, ?, ?, ?)",
                    (
                        f"json_migrated_{vin}",
                        self._drive_stats_meta_key(vin),
                        self._energy_model_meta_key(vin),
                        self._heat_format_meta_key(vin),
                    ),
                )
            self._invalidate_heat_cache_all(vin)

    def delete_drives(self, vin: str, drive_ids: list[str]) -> dict[str, Any]:
        """Delete one or more drives and everything derived from them.

        Removes the ``drives``, ``drive_tracks`` and ``track_fills`` rows in
        one transaction. Then recounts road heat for every local month (in
        HA's configured time zone) that any deleted drive was counted into:
        unlike ``rebuild_heat()``, a month with no route left afterward has
        its ``road_heat`` row dropped rather than kept -- this is the one
        place that happens. Finally rebuilds places and routes, since a
        deleted drive's endpoints no longer count toward clustering or route
        stats.

        Returns ``{"deleted": n, "affected_hours": [...]}``: the hour-aligned
        (UTC epoch) start timestamps of the deleted drives, for the caller to
        rewrite long-term statistics from the earliest one forward.
        """
        self._assert_executor_thread()
        if not drive_ids:
            return {"deleted": 0, "affected_hours": []}
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; delete_drives skipped")
            return {"deleted": 0, "affected_hours": []}

        placeholders = ",".join("?" for _ in drive_ids)
        with self._lock:
            drive_rows = self._conn.execute(
                f"SELECT sort_ts FROM drives WHERE vin = ? "
                f"AND drive_id IN ({placeholders})",
                [vin, *drive_ids],
            ).fetchall()
            month_rows = self._conn.execute(
                f"SELECT DISTINCT month FROM road_heat_drives WHERE vin = ? "
                f"AND drive_id IN ({placeholders})",
                [vin, *drive_ids],
            ).fetchall()

        affected_hours = sorted(
            {
                (row["sort_ts"] // 3600) * 3600
                for row in drive_rows
                if row["sort_ts"] is not None
            }
        )
        months = {r["month"] for r in month_rows if r["month"]}

        with self._lock, self._transaction():
            deleted = (
                self._conn.execute(
                    f"DELETE FROM drives WHERE vin = ? AND drive_id IN ({placeholders})",
                    [vin, *drive_ids],
                ).rowcount
                or 0
            )
            self._conn.execute(
                f"DELETE FROM drive_tracks WHERE vin = ? "
                f"AND drive_id IN ({placeholders})",
                [vin, *drive_ids],
            )
            self._conn.execute(
                f"DELETE FROM track_fills WHERE vin = ? "
                f"AND drive_id IN ({placeholders})",
                [vin, *drive_ids],
            )
            self._conn.execute(
                f"DELETE FROM road_heat_drives WHERE vin = ? "
                f"AND drive_id IN ({placeholders})",
                [vin, *drive_ids],
            )

        if months:
            tz = dt_util.get_default_time_zone()
            with self._heat_run_lock:
                self._rebuild_heat_months_locked(vin, tz, months)

        dataset = self.dataset_for_vin(vin)
        self.rebuild_places(dataset)
        self.rebuild_routes(dataset)

        return {"deleted": deleted, "affected_hours": affected_hours}

    def delete_day(self, vin: str, tz: tzinfo, day: date) -> dict[str, Any]:
        """Delete every drive on one local calendar day (see ``day()``'s window)."""
        self._assert_executor_thread()
        midnight_time = datetime.min.time()
        start_ts = datetime.combine(day, midnight_time, tzinfo=tz).timestamp()
        end_ts = datetime.combine(
            day + timedelta(days=1), midnight_time, tzinfo=tz
        ).timestamp()
        with self._lock:
            rows = self._conn.execute(
                "SELECT drive_id FROM drives WHERE vin = ? AND sort_ts IS NOT NULL "
                "AND sort_ts >= ? AND sort_ts < ?",
                (vin, start_ts, end_ts),
            ).fetchall()
        drive_ids = [r["drive_id"] for r in rows]
        return self.delete_drives(vin, drive_ids)

    def delete_place(self, dataset: str, place_id: int) -> dict[str, Any]:
        """Delete or hide a place, depending on its source.

        A ``user`` place is deleted outright. An ``auto`` suggestion is only
        hidden (so it won't be suggested again, but can be restored from
        Hidden). A ``zone`` place can't be deleted here -- it's managed in
        Home Assistant zones -- and raises ``ValueError``.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; delete_place skipped")
            return {"action": "none"}
        with self._lock:
            row = self._conn.execute(
                "SELECT source FROM places WHERE dataset = ? AND place_id = ?",
                (dataset, place_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"No place {place_id}")
        source = row["source"]
        if source == "zone":
            raise ValueError(
                "Zone places are managed in Home Assistant zones, not here"
            )
        now = time.time()
        if source == "auto":
            with self._lock, self._transaction():
                self._conn.execute(
                    "UPDATE places SET hidden = 1, updated_ts = ? "
                    "WHERE dataset = ? AND place_id = ?",
                    (now, dataset, place_id),
                )
            action = "hidden"
        else:  # user
            with self._lock, self._transaction():
                self._conn.execute(
                    "DELETE FROM places WHERE dataset = ? AND place_id = ?",
                    (dataset, place_id),
                )
            action = "deleted"
        self.rebuild_places(dataset)
        self.rebuild_routes(dataset)
        return {"action": action}

    def delete_dcfc_session(self, vin: str, session_id: str) -> int:
        """Delete one DC fast-charge session; return the number of rows removed (0 or 1)."""
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning(
                "Analytics database is read-only; delete_dcfc_session skipped"
            )
            return 0
        with self._lock, self._transaction():
            row = self._conn.execute(
                "SELECT source FROM dcfc_sessions WHERE vin = ? AND session_id = ?",
                (vin, session_id),
            ).fetchone()
            cur = self._conn.execute(
                "DELETE FROM dcfc_sessions WHERE vin = ? AND session_id = ?",
                (vin, session_id),
            )
            if row is not None and row["source"] == "inferred":
                # Remember the delete: the nightly inference would re-add it.
                tombstones = self._inferred_tombstones_locked(vin)
                if session_id not in tombstones:
                    tombstones.append(session_id)
                    self._set_meta_locked(
                        f"inferred_deleted:{vin}", json.dumps(tombstones)
                    )
            return cur.rowcount or 0

    def _inferred_tombstones_locked(self, vin: str) -> list[str]:
        """Return the VIN's deleted inferred session ids; caller holds the lock."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"inferred_deleted:{vin}",)
        ).fetchone()
        if row is None:
            return []
        try:
            data = json.loads(row["value"])
        except (TypeError, ValueError):
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    def earliest_activity_ts(self, vin: str) -> float | None:
        """Return the start of the VIN's earliest stored drive or session."""
        self._assert_executor_thread()
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(t) FROM (SELECT MIN(start_ts) AS t FROM drives "
                "WHERE vin = ?1 UNION ALL SELECT MIN(start_ts) FROM dcfc_sessions "
                "WHERE vin = ?1 AND source != 'inferred')",
                (vin,),
            ).fetchone()
        return row[0] if row is not None and row[0] is not None else None

    def last_drive_end_location(
        self, vin: str, ts: float, max_age_s: float
    ) -> tuple[float, float] | None:
        """Return where the latest drive ending at/before ``ts`` (within ``max_age_s``) ended."""
        self._assert_executor_thread()
        with self._lock:
            row = self._conn.execute(
                "SELECT end_lat, end_lon FROM drives WHERE vin = ? "
                "AND end_ts IS NOT NULL AND end_ts <= ? AND end_ts >= ? "
                "ORDER BY end_ts DESC LIMIT 1",
                (vin, ts, ts - max_age_s),
            ).fetchone()
        if row is None or row["end_lat"] is None or row["end_lon"] is None:
            return None
        return (row["end_lat"], row["end_lon"])

    def get_inferred_scan(self, vin: str) -> dict[str, Any] | None:
        """The VIN's last inference scan stamp (``{version, through}``), or None."""
        self._assert_executor_thread()
        raw = self.get_meta(f"inferred_scan:{vin}")
        try:
            value = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def set_inferred_scan(self, vin: str, stamp: dict[str, Any]) -> None:
        """Record the VIN's inference scan stamp (see ``get_inferred_scan``)."""
        self._assert_executor_thread()
        if self.read_only:
            return
        self.set_meta(f"inferred_scan:{vin}", json.dumps(stamp))

    def replace_inferred_sessions(
        self,
        vin: str,
        sessions: list[ChargingSessionRecord],
        since_ts: float | None = None,
    ) -> bool:
        """Replace the VIN's ``source='inferred'`` sessions with ``sessions``.

        With ``since_ts`` only the inferred rows starting at or after it are
        replaced (an incremental re-check); older ones are kept as stored.

        One transaction: the old inferred rows go, the new set comes in except
        ids the user deleted (``inferred_deleted:<vin>`` in ``meta``) and any
        that overlap a stored non-inferred session. Rows of other sources are
        never touched. Returns True when the stored inferred set changed.
        """
        self._assert_executor_thread()
        if self.read_only:
            return False
        with self._lock:
            dataset = (
                places.DATASET_DEMO
                if vin in self._demo_vins_locked()
                else places.DATASET_REAL
            )
            tombstones = set(self._inferred_tombstones_locked(vin))
            before = self._inferred_signature_locked(vin)
            real = [
                (r["start_ts"], r["end_ts"] or r["start_ts"])
                for r in self._conn.execute(
                    "SELECT start_ts, end_ts FROM dcfc_sessions WHERE vin = ? "
                    "AND start_ts IS NOT NULL AND source != 'inferred'",
                    (vin,),
                ).fetchall()
            ]
            created_ts = time.time()
            with self._transaction():
                if since_ts is None:
                    self._conn.execute(
                        "DELETE FROM dcfc_sessions WHERE vin = ? AND source = 'inferred'",
                        (vin,),
                    )
                else:
                    self._conn.execute(
                        "DELETE FROM dcfc_sessions WHERE vin = ? AND source = 'inferred' "
                        "AND start_ts >= ?",
                        (vin, since_ts),
                    )
                for session in sessions:
                    if session.source != "inferred" or session.session_id in tombstones:
                        continue
                    start = _parse_iso_to_epoch(session.start_time)
                    end = _parse_iso_to_epoch(session.end_time) or start
                    if start is not None and any(
                        a <= end and start <= b for a, b in real
                    ):
                        continue
                    if (
                        session.place_id is None
                        and session.lat is not None
                        and session.lon is not None
                    ):
                        session.place_id = self._place_id_at_locked(
                            dataset, session.lat, session.lon
                        )
                    self._conn.execute(
                        _UPSERT_DCFC_SQL,
                        self._dcfc_row_params(vin, session, created_ts),
                    )
            return self._inferred_signature_locked(vin) != before

    def _inferred_signature_locked(self, vin: str) -> list[tuple[Any, ...]]:
        """Comparable summary of the VIN's inferred rows; caller holds the lock."""
        return [
            tuple(r)
            for r in self._conn.execute(
                "SELECT session_id, start_ts, end_ts, start_soc, end_soc, place_id "
                "FROM dcfc_sessions WHERE vin = ? AND source = 'inferred' "
                "ORDER BY session_id",
                (vin,),
            ).fetchall()
        ]

    def prune(self, vin: str, cutoff_ts: float) -> int:
        """Delete drives/vampire events older than cutoff_ts; return rows removed.

        Rows with both start_ts and end_ts NULL are retained (COALESCE yields NULL,
        and `NULL < cutoff_ts` is neither true nor false in SQL, so the DELETE
        predicate never matches them). DC fast-charge sessions outside the newest 50 have
        their sample curve blanked (not deleted) so session summaries persist.
        """
        self._assert_executor_thread()
        with self._lock:
            if self.read_only:
                _LOGGER.warning("Analytics database is read-only; prune skipped")
                return 0
            with self._transaction():
                d_cur = self._conn.execute(
                    "DELETE FROM drives WHERE vin = ? AND COALESCE(end_ts, start_ts) < ?",
                    (vin, cutoff_ts),
                )
                drives_deleted = d_cur.rowcount or 0
                v_cur = self._conn.execute(
                    "DELETE FROM vampire_events WHERE vin = ? "
                    "AND COALESCE(end_ts, start_ts) < ?",
                    (vin, cutoff_ts),
                )
                vampire_deleted = v_cur.rowcount or 0
                self._conn.execute(
                    """
                    UPDATE dcfc_sessions SET samples_json = '[]', sample_count = 0
                     WHERE vin = ? AND kind = 'dc' AND id NOT IN (
                        SELECT id FROM dcfc_sessions WHERE vin = ? AND kind = 'dc'
                         ORDER BY sort_ts DESC, id DESC LIMIT ?
                     )
                    """,
                    (vin, vin, DCFC_CACHE_LIMIT),
                )
                self._conn.execute(
                    """
                    DELETE FROM drive_tracks WHERE vin = ? AND NOT EXISTS (
                        SELECT 1 FROM drives d
                         WHERE d.vin = drive_tracks.vin AND d.drive_id = drive_tracks.drive_id
                    )
                    """,
                    (vin,),
                )
                self._conn.execute(
                    """
                    DELETE FROM track_fills WHERE vin = ? AND NOT EXISTS (
                        SELECT 1 FROM drive_tracks t
                         WHERE t.vin = track_fills.vin AND t.drive_id = track_fills.drive_id
                    )
                    """,
                    (vin,),
                )
            self._conn.execute("PRAGMA incremental_vacuum(200)")
        return drives_deleted + vampire_deleted

    # -- executor-side read API -------------------------------------------------

    def window_stats(
        self, vin: str, days: int | None, now_ts: float
    ) -> AggregatedDriveStats:
        """Compute aggregated drive stats over a rolling window (None = all-time)."""
        self._assert_executor_thread()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS drive_count,
                       COALESCE(SUM(distance_miles),0) AS total_miles,
                       COALESCE(SUM(energy_kwh),0)     AS total_kwh,
                       COALESCE(SUM(duration_seconds),0) AS total_duration
                  FROM drives
                 WHERE vin = :vin AND is_micro_drive = 0 AND distance_miles >= :threshold
                   AND (:days IS NULL OR (sort_ts IS NOT NULL
                        AND sort_ts >= :now_ts - :days * :seconds_per_day
                        AND sort_ts <= :now_ts))
                """,
                {
                    "vin": vin,
                    "threshold": MICRO_DRIVE_THRESHOLD_MILES,
                    "days": days,
                    "now_ts": now_ts,
                    "seconds_per_day": SECONDS_PER_DAY,
                },
            ).fetchone()
            # Intentional: total_micro_drives is an ALL-TIME count injected into
            # every window, reproducing the legacy in-memory implementation's
            # behavior (it counted over the whole store, not the window slice).
            micro_count = self._conn.execute(
                "SELECT COUNT(*) AS c FROM drives WHERE vin = ? AND is_micro_drive = 1",
                (vin,),
            ).fetchone()["c"]
        return self._build_stats(row, micro_count)

    @staticmethod
    def _build_stats(row: sqlite3.Row, micro_count: int) -> AggregatedDriveStats:
        total_miles = float(row["total_miles"] or 0.0)
        total_kwh = float(row["total_kwh"] or 0.0)
        total_duration = float(row["total_duration"] or 0.0)
        drive_count = int(row["drive_count"] or 0)

        efficiency = total_miles / total_kwh if total_kwh > 0.0 else 0.0
        mpge = efficiency * MPGE_FACTOR
        avg_distance = total_miles / drive_count if drive_count > 0 else 0.0

        return AggregatedDriveStats(
            total_miles=round(total_miles, 2),
            total_kwh=round(total_kwh, 2),
            efficiency_mi_kwh=round(efficiency, 2),
            mpge=round(mpge, 2),
            drive_count=drive_count,
            total_duration_seconds=round(total_duration, 1),
            avg_distance_miles=round(avg_distance, 2),
            total_micro_drives=micro_count,
        )

    def build_cache(self, vin: str, now_ts: float) -> HotCache:
        """Build a fresh HotCache snapshot for a VIN from SQLite."""
        self._assert_executor_thread()
        with self._lock:
            stats_30d = self.window_stats(vin, 30, now_ts)
            stats_90d = self.window_stats(vin, 90, now_ts)
            stats_365d = self.window_stats(vin, 365, now_ts)
            stats_all_time = self.window_stats(vin, None, now_ts)

            last_row = self._conn.execute(
                # id DESC breaks ties between drives sharing a timestamp, so
                # "most recent" is stable across cache rebuilds.
                "SELECT * FROM drives WHERE vin = ? "
                "ORDER BY sort_ts DESC, created_ts DESC, id DESC LIMIT 1",
                (vin,),
            ).fetchone()
            last_drive = (
                self._row_to_drive(last_row, hydrate=True) if last_row else None
            )

            cutoff = now_ts - DRIVE_CACHE_WINDOW_DAYS * SECONDS_PER_DAY
            drive_rows = self._conn.execute(
                """
                SELECT * FROM drives
                 WHERE vin = ? AND is_micro_drive = 0
                   AND sort_ts IS NOT NULL AND sort_ts >= ? AND sort_ts <= ?
                 ORDER BY sort_ts DESC, created_ts DESC, id DESC LIMIT ?
                """,
                (vin, cutoff, now_ts, DRIVE_CACHE_LIMIT),
            ).fetchall()
            recent_drives = [
                self._row_to_drive(r, hydrate=idx < DRIVE_HYDRATE_LIMIT)
                for idx, r in enumerate(drive_rows)
            ]
            recent_drives.reverse()  # ascending time order for entities/charts

            vampire_rows = self._conn.execute(
                """
                SELECT * FROM vampire_events
                 WHERE vin = ? AND sort_ts IS NOT NULL AND sort_ts >= ? AND sort_ts <= ?
                 ORDER BY sort_ts DESC, id DESC LIMIT ?
                """,
                (vin, cutoff, now_ts, VAMPIRE_CACHE_LIMIT),
            ).fetchall()
            recent_vampire_events = [
                self._row_to_vampire(r) for r in reversed(vampire_rows)
            ]

            dcfc_rows = self._conn.execute(
                "SELECT * FROM dcfc_sessions WHERE vin = ? AND kind = 'dc' "
                "ORDER BY sort_ts DESC, id DESC LIMIT ?",
                (vin, DCFC_CACHE_LIMIT),
            ).fetchall()
            dcfc_sessions = [
                self._row_to_dcfc(r, hydrate=idx < DCFC_SAMPLE_HYDRATE_LIMIT)
                for idx, r in enumerate(dcfc_rows)
            ]
            dcfc_sessions.reverse()  # ascending, matching legacy FIFO-capped ordering

            # Summed over every retained drive (the whole storage window), not the
            # 90-day chart window: this is the vehicle's overall speed profile.
            bin_rows = self._conn.execute(
                """
                SELECT je.key AS bin,
                       COALESCE(SUM(json_extract(je.value, '$.miles')), 0) AS miles,
                       COALESCE(SUM(json_extract(je.value, '$.seconds')), 0) AS seconds
                  FROM drives, json_each(drives.speed_bins_json) AS je
                 WHERE drives.vin = ? AND drives.is_micro_drive = 0
                   AND drives.distance_miles >= ?
                 GROUP BY je.key
                """,
                (vin, MICRO_DRIVE_THRESHOLD_MILES),
            ).fetchall()
            found = {r["bin"]: r for r in bin_rows}
            speed_bin_totals = {
                b: {
                    "miles": round(found[b]["miles"], 2) if b in found else 0.0,
                    "seconds": round(found[b]["seconds"], 0) if b in found else 0.0,
                }
                for b in STANDARD_SPEED_BINS
            }

        return HotCache(
            stats_30d=stats_30d,
            stats_90d=stats_90d,
            stats_365d=stats_365d,
            stats_all_time=stats_all_time,
            last_drive=last_drive,
            recent_drives=recent_drives,
            recent_vampire_events=recent_vampire_events,
            dcfc_sessions=dcfc_sessions,
            speed_bin_totals=speed_bin_totals,
            drive_count=stats_all_time.drive_count,
            revision=0,  # caller (DriveStore) stamps its own monotonic revision
            generated_at=now_ts,
        )

    # -- GPS drive tracks -----------------------------------------------------

    def _track_sort_ts(
        self, vin: str, drive_id: str, track: DriveTrack
    ) -> float | None:
        """Return the sort_ts a track row should use: caller must hold ``self._lock``.

        Matches the parent drive's COALESCE(start_ts, end_ts) if a drive row
        exists, else falls back to the track's first point timestamp.
        """
        row = self._conn.execute(
            "SELECT sort_ts FROM drives WHERE vin = ? AND drive_id = ?",
            (vin, drive_id),
        ).fetchone()
        if row is not None:
            return row["sort_ts"]
        return track.points[0].t if track.points else None

    def _upsert_track_locked(
        self, vin: str, drive_id: str, track: DriveTrack, source: str, now_ts: float
    ) -> bool:
        """Upsert one track row inside an already-open transaction.

        Caller must hold ``self._lock`` and have an active ``BEGIN``. Tracks
        with fewer than 2 points are skipped. A 'backfill' track never
        replaces an existing 'live' track. Returns True if the row was
        written.
        """
        if len(track) < 2:
            return False
        existing = self._conn.execute(
            "SELECT source FROM drive_tracks WHERE vin = ? AND drive_id = ?",
            (vin, drive_id),
        ).fetchone()
        if (
            existing is not None
            and existing["source"] == "live"
            and source == "backfill"
        ):
            return False

        bbox = track.bbox()
        params = {
            "vin": vin,
            "drive_id": drive_id,
            "sort_ts": self._track_sort_ts(vin, drive_id, track),
            "point_count": len(track),
            "min_lat": bbox[0] if bbox else None,
            "min_lon": bbox[1] if bbox else None,
            "max_lat": bbox[2] if bbox else None,
            "max_lon": bbox[3] if bbox else None,
            "source": source,
            "detail": "full",
            "track_json": track.encode(),
            "preview_json": track.preview(TRACK_PREVIEW_MAX_POINTS).encode(),
            "created_ts": now_ts,
            "updated_ts": now_ts,
        }
        self._conn.execute(_UPSERT_TRACK_SQL, params)
        # A replaced route invalidates any prior gap-fill attempts against it
        # (different points, different gaps); gaps_scanned=0 above means it
        # will be rescanned and refilled from scratch.
        self._conn.execute(
            "DELETE FROM track_fills WHERE vin = ? AND drive_id = ?",
            (vin, drive_id),
        )
        return True

    def upsert_tracks(
        self, vin: str, items: list[tuple[str, DriveTrack]], source: str = "live"
    ) -> int:
        """Insert or update GPS tracks for a batch of drives; return count written."""
        self._assert_executor_thread()
        if not items:
            return 0
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; upsert_tracks skipped"
                )
                return 0
            now_ts = time.time()
            written = 0
            with self._transaction():
                for drive_id, track in items:
                    if self._upsert_track_locked(vin, drive_id, track, source, now_ts):
                        written += 1
            return written

    def finalize_drive(
        self, vin: str, record: DriveRecord, track: DriveTrack | None
    ) -> bool:
        """Upsert a completed drive and its track, and clear the live checkpoint.

        Runs as one transaction: the drive upsert, the track upsert (if the
        track has at least 2 points, written as source='live'), and clearing
        this VIN's active_drive/active_track_chunks rows all succeed or all
        roll back together. Returns True if the drive row is new.
        """
        self._assert_executor_thread()
        # Pure CPU over the route, so done before taking the lock.
        self._apply_conditions(vin, record, track)
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; finalize_drive skipped"
                )
                return False
            created_ts = time.time()
            with self._transaction():
                existing = self._conn.execute(
                    "SELECT 1 FROM drives WHERE vin = ? AND drive_id = ?",
                    (vin, record.drive_id),
                ).fetchone()
                is_new = existing is None
                self._conn.execute(
                    _UPSERT_DRIVE_SQL,
                    self._drive_row_params(vin, record, created_ts),
                )
                if track is not None:
                    self._upsert_track_locked(
                        vin, record.drive_id, track, "live", created_ts
                    )
                self._conn.execute("DELETE FROM active_drive WHERE vin = ?", (vin,))
                self._conn.execute(
                    "DELETE FROM active_track_chunks WHERE vin = ?", (vin,)
                )
            return is_new

    def _apply_conditions(
        self, vin: str, record: DriveRecord, track: DriveTrack | None
    ) -> None:
        """Fill the record's still-empty driving-condition fields; never raises.

        Uses the drive's own weather samples (live Open-Meteo ``current``
        readings) and this VIN's fitted energy model (the default parameters
        until a fit exists). A failure just leaves the fields ``None`` for the
        weather backfill to fill later.
        """
        if track is None or len(track.points) < 2:
            return
        try:
            params = self.get_energy_model(vin) or ENERGY_MODEL_DEFAULT_PARAMS
            cols = compute_condition_columns(
                track,
                record.weather_samples,
                params,
                duration_s=record.duration_seconds,
                distance_miles=record.distance_miles,
                temp_f=record.integrated_temperature_f,
            )
        except Exception:
            _LOGGER.exception("Could not compute driving conditions (non-fatal)")
            return
        for column in _CONDITION_COLUMNS:
            if getattr(record, column) is None and cols.get(column) is not None:
                setattr(record, column, cols[column])

    # -- driving conditions (weather backfill) ----------------------------------

    @staticmethod
    def _weather_meta_key(vin: str) -> str:
        """Meta key recording the weather-backfill version a VIN was filled with."""
        return f"weather_version:{vin}"

    def has_unseeded_weather(self, vin: str) -> bool:
        """True until the one-time weather backfill has run for this VIN."""
        return self.get_meta(self._weather_meta_key(vin)) != str(WEATHER_VERSION)

    def mark_weather_seeded(self, vin: str) -> None:
        """Stamp the one-time weather backfill as done for this VIN."""
        self._assert_executor_thread()
        with self._lock, self._transaction():
            self._set_meta_locked(self._weather_meta_key(vin), str(WEATHER_VERSION))

    def drives_for_weather_backfill(
        self, vin: str, since_ts: float
    ) -> list[dict[str, Any]]:
        """Routed drives since ``since_ts`` with any condition column still NULL.

        Oldest first. Empty for a demo VIN: demo drives get their conditions
        from the fixture and must never reach the network.
        """
        self._assert_executor_thread()
        null_clause = " OR ".join(f"d.{c} IS NULL" for c in _CONDITION_COLUMNS)
        with self._lock:
            if vin in self._demo_vins_locked():
                return []
            rows = self._conn.execute(
                f"""
                SELECT d.drive_id AS drive_id, d.start_ts AS start_ts,
                       d.end_ts AS end_ts, d.start_lat AS lat, d.start_lon AS lon
                  FROM drives d
                  JOIN drive_tracks t ON t.vin = d.vin AND t.drive_id = d.drive_id
                 WHERE d.vin = ? AND d.sort_ts IS NOT NULL AND d.sort_ts >= ?
                   AND d.start_lat IS NOT NULL AND d.start_lon IS NOT NULL
                   AND ({null_clause})
                 ORDER BY d.sort_ts ASC
                """,
                (vin, since_ts),
            ).fetchall()
        return [dict(r) for r in rows]

    def apply_weather_backfill(
        self, vin: str, items: list[tuple[str, list[dict[str, Any]]]]
    ) -> int:
        """Fill NULL condition columns from per-drive weather samples; returns drives updated.

        ``items`` is ``[(drive_id, samples)]``; a drive with no samples still
        gets its ``expected_kwh``. Add-only: a column that already holds a
        value is never overwritten. Skips a demo VIN. Tracks are decoded and
        the physics run outside the write lock, one batch write at the end.
        """
        self._assert_executor_thread()
        if self.read_only:
            return 0
        with self._lock:
            if vin in self._demo_vins_locked():
                return 0
        params = self.get_energy_model(vin) or ENERGY_MODEL_DEFAULT_PARAMS
        updates: list[tuple[str, dict[str, float | None]]] = []
        for drive_id, samples in items:
            with self._lock:
                row = self._conn.execute(
                    """
                    SELECT d.duration_seconds AS duration_seconds,
                           d.distance_miles AS distance_miles,
                           d.integrated_temperature_f AS temp_f,
                           t.track_json AS track_json
                      FROM drives d
                      JOIN drive_tracks t ON t.vin = d.vin AND t.drive_id = d.drive_id
                     WHERE d.vin = ? AND d.drive_id = ?
                    """,
                    (vin, drive_id),
                ).fetchone()
            if row is None:
                continue
            try:
                track = DriveTrack.decode(row["track_json"])
            except ValueError:
                continue
            cols = compute_condition_columns(
                track,
                samples,
                params,
                duration_s=row["duration_seconds"],
                distance_miles=row["distance_miles"],
                temp_f=row["temp_f"],
            )
            updates.append((drive_id, cols))
        if not updates:
            return 0
        assignments = ", ".join(f"{c} = COALESCE({c}, ?)" for c in _CONDITION_COLUMNS)
        with self._lock, self._transaction():
            for drive_id, cols in updates:
                self._conn.execute(
                    f"UPDATE drives SET {assignments} WHERE vin = ? AND drive_id = ?",
                    (*(cols.get(c) for c in _CONDITION_COLUMNS), vin, drive_id),
                )
        return len(updates)

    # -- efficiency page ---------------------------------------------------------

    def efficiency_data(
        self,
        vin: str,
        since_ts: float | None,
        tz: tzinfo,
        include_micro: bool = False,
    ) -> dict[str, Any]:
        """Per-drive rows, speed-band aggregates and weekly/monthly trends for one VIN.

        ``since_ts`` bounds the window (``None`` = everything retained). Micro
        drives (and any drive with no energy) are excluded unless
        ``include_micro``. Drives older than the retention window are gone, so
        the trend only reaches as far back as the retained drives.
        """
        self._assert_executor_thread()
        clauses = [
            "drives.vin = ?",
            "drives.sort_ts IS NOT NULL",
            "drives.energy_kwh > 0",
        ]
        args: list[Any] = [vin]
        if since_ts is not None:
            clauses.append("drives.sort_ts >= ?")
            args.append(since_ts)
        if not include_micro:
            clauses.append("drives.is_micro_drive = 0 AND drives.distance_miles >= ?")
            args.append(MICRO_DRIVE_THRESHOLD_MILES)
        where = " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM drives WHERE {where} ORDER BY sort_ts ASC, id ASC",
                args,
            ).fetchall()
            band_rows = self._conn.execute(
                f"""
                SELECT json_extract(je.value, '$.speed_bin') AS band,
                       COALESCE(SUM(json_extract(je.value, '$.distance_miles')), 0) AS miles,
                       COALESCE(SUM(json_extract(je.value, '$.energy_kwh')), 0) AS kwh
                  FROM drives, json_each(drives.chunks_json) AS je
                 WHERE {where}
                 GROUP BY band
                """,
                args,
            ).fetchall()

        drives_out: list[dict[str, Any]] = []
        weekly: dict[date, list[float]] = {}
        monthly: dict[date, list[float]] = {}
        for row in rows:
            distance = row["distance_miles"] or 0.0
            energy = row["energy_kwh"]
            eff = distance / energy if energy > 0 else None
            expected = row["expected_kwh"]
            climb = row["climb_ft"]
            modes = json.loads(row["drive_modes_json"] or "[]")
            drives_out.append(
                {
                    "drive_id": row["drive_id"],
                    "date_ts": row["sort_ts"],
                    "distance_mi": round(distance, 2),
                    "duration_s": round(row["duration_seconds"] or 0.0, 1),
                    "avg_speed_mph": row["avg_speed_mph"],
                    "temp_f": row["integrated_temperature_f"],
                    "headwind_mph": row["headwind_mph"],
                    "wind_speed_mph": row["wind_speed_mph"],
                    "precip_mm": row["precip_mm"],
                    "air_density": row["air_density"],
                    "climb_ft_per_mi": (
                        round(climb / distance, 1)
                        if climb is not None and distance > 0
                        else None
                    ),
                    "trip_length_mi": round(distance, 2),
                    "drive_mode": modes[0] if modes else None,
                    "trailer": (
                        bool(row["trailer"]) if row["trailer"] is not None else None
                    ),
                    "efficiency_mi_kwh": round(eff, 3) if eff is not None else None,
                    "mpge": round(eff * MPGE_FACTOR, 1) if eff is not None else None,
                    "expected_eff_mi_kwh": (
                        round(distance / expected, 3)
                        if expected is not None and expected > 0
                        else None
                    ),
                    "score": (
                        round(expected / energy, 3)
                        if expected is not None and energy > 0
                        else None
                    ),
                }
            )
            local = datetime.fromtimestamp(row["sort_ts"], tz).date()
            for bucket, key in (
                (weekly, local - timedelta(days=local.weekday())),
                (monthly, local.replace(day=1)),
            ):
                agg = bucket.setdefault(key, [0.0, 0.0])
                agg[0] += distance
                agg[1] += energy

        def _series(bucket: dict[date, list[float]]) -> list[list[float]]:
            out: list[list[float]] = []
            for start in sorted(bucket):
                miles, kwh = bucket[start]
                eff = miles / kwh if kwh > 0 else 0.0
                start_ts = datetime(
                    start.year, start.month, start.day, tzinfo=tz
                ).timestamp()
                out.append(
                    [
                        start_ts,
                        round(eff, 3),
                        round(eff * MPGE_FACTOR, 1),
                        round(miles, 1),
                    ]
                )
            return out

        found = {r["band"]: r for r in band_rows if r["band"] is not None}
        speed_bands = [
            {
                "band": band,
                "miles": round(found[band]["miles"], 2),
                "kwh": round(found[band]["kwh"], 3),
                "efficiency": round(found[band]["miles"] / found[band]["kwh"], 3),
            }
            for band in STANDARD_SPEED_BINS
            if band in found and found[band]["kwh"] > 0
        ]
        return {
            "drives": drives_out,
            "speed_bands": speed_bands,
            "trend": {"weekly": _series(weekly), "monthly": _series(monthly)},
        }

    def get_track(self, vin: str, drive_id: str) -> DriveTrack | None:
        """Return the full-detail (or thinned) GPS track for one drive, if any."""
        self._assert_executor_thread()
        with self._lock:
            row = self._conn.execute(
                "SELECT track_json FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (vin, drive_id),
            ).fetchone()
        return DriveTrack.decode(row["track_json"]) if row is not None else None

    @staticmethod
    def _row_to_drive_summary(row: sqlite3.Row) -> dict[str, Any]:
        """Build the DRIVE_SUMMARY dict (plus ``sort_ts``) from a joined drive row."""
        temp_f = row["integrated_temperature_f"]
        start_range_mi = row["start_range_mi"]
        end_range_mi = row["end_range_mi"]
        range_used_mi = (
            round(start_range_mi - end_range_mi, 1)
            if start_range_mi is not None and end_range_mi is not None
            else None
        )
        return {
            "drive_id": row["drive_id"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "start_ts": row["start_ts"],
            "end_ts": row["end_ts"],
            "distance_miles": round(row["distance_miles"] or 0.0, 2),
            "duration_seconds": row["duration_seconds"],
            "energy_kwh": row["energy_kwh"],
            "efficiency_mi_kwh": round(row["efficiency_mi_kwh"] or 0.0, 2),
            "mpge": round(row["mpge"] or 0.0, 1),
            "avg_speed_mph": row["avg_speed_mph"],
            "max_speed_mph": row["max_speed_mph"],
            "temp_f": round(temp_f, 1) if temp_f is not None else None,
            "elevation_change_ft": row["elevation_change_ft"],
            "start_soc": row["start_soc"],
            "end_soc": row["end_soc"],
            "is_micro_drive": bool(row["is_micro_drive"]),
            "start_lat": row["start_lat"],
            "start_lon": row["start_lon"],
            "end_lat": row["end_lat"],
            "end_lon": row["end_lon"],
            "has_track": row["track_source"] is not None,
            "track_source": row["track_source"],
            "track_detail": row["track_detail"],
            "point_count": row["track_point_count"] or 0,
            "sort_ts": row["sort_ts"],
            "moving_seconds": row["moving_seconds"],
            "stopped_seconds": row["stopped_seconds"],
            "stop_count": row["stop_count"],
            "climb_ft": row["climb_ft"],
            "descent_ft": row["descent_ft"],
            "track_max_speed_mph": row["track_max_speed_mph"],
            "pct_distance_over_70mph": row["pct_distance_over_70mph"],
            "start_range_mi": start_range_mi,
            "end_range_mi": end_range_mi,
            "range_used_mi": range_used_mi,
            "drive_modes": json.loads(row["drive_modes_json"] or "[]"),
            "trailer": (bool(row["trailer"]) if row["trailer"] is not None else None),
            "driver": row["driver"],
        }

    def list_drives(
        self,
        vin: str,
        before_ts: float | None = None,
        limit: int = 50,
        include_micro: bool = False,
    ) -> list[dict[str, Any]]:
        """Return a page of drive summaries, newest first, LEFT JOINed with tracks."""
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT d.*, t.source AS track_source, t.detail AS track_detail,
                       t.point_count AS track_point_count
                  FROM drives d
                  LEFT JOIN drive_tracks t
                    ON t.vin = d.vin AND t.drive_id = d.drive_id
                 WHERE d.vin = :vin
                   AND (:include_micro OR d.is_micro_drive = 0)
                   AND (:before_ts IS NULL
                        OR (d.sort_ts IS NOT NULL AND d.sort_ts < :before_ts))
                 ORDER BY d.sort_ts DESC, d.created_ts DESC, d.id DESC
                 LIMIT :limit
                """,
                {
                    "vin": vin,
                    "include_micro": include_micro,
                    "before_ts": before_ts,
                    "limit": limit,
                },
            ).fetchall()
        return [self._row_to_drive_summary(r) for r in rows]

    def get_track_previews(
        self, vin: str, drive_ids: list[str]
    ) -> dict[str, dict[str, list]]:
        """Return {drive_id: {"lat": [...], "lon": [...]}} from stored previews."""
        self._assert_executor_thread()
        if not drive_ids:
            return {}
        with self._lock:
            placeholders = ",".join("?" for _ in drive_ids)
            rows = self._conn.execute(
                f"SELECT drive_id, preview_json FROM drive_tracks "
                f"WHERE vin = ? AND drive_id IN ({placeholders})",
                [vin, *drive_ids],
            ).fetchall()
        result: dict[str, dict[str, list]] = {}
        for row in rows:
            preview = DriveTrack.decode(row["preview_json"])
            result[row["drive_id"]] = {
                "lat": [p.lat for p in preview.points],
                "lon": [p.lon for p in preview.points],
            }
        return result

    def get_drive_detail(self, vin: str, drive_id: str) -> dict[str, Any] | None:
        """Return the full drive detail payload (summary + speed bins/chunks + track)."""
        self._assert_executor_thread()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT d.*, t.source AS track_source, t.detail AS track_detail,
                       t.point_count AS track_point_count, t.track_json AS track_json
                  FROM drives d
                  LEFT JOIN drive_tracks t
                    ON t.vin = d.vin AND t.drive_id = d.drive_id
                 WHERE d.vin = ? AND d.drive_id = ?
                """,
                (vin, drive_id),
            ).fetchone()
        if row is None:
            return None
        summary = self._row_to_drive_summary(row)
        record = self._row_to_drive(row, hydrate=True)
        summary["speed_bins"] = {
            k: {"miles": v.miles, "seconds": v.seconds}
            for k, v in record.speed_bins.items()
        }
        summary["chunks"] = [c.to_dict() for c in record.chunks]
        place_rows = self._place_refs_for_ids(
            [row["start_place_id"], row["end_place_id"]]
        )
        summary["start_place"] = self._place_ref_from_row(
            place_rows.get(row["start_place_id"])
        )
        summary["end_place"] = self._place_ref_from_row(
            place_rows.get(row["end_place_id"])
        )
        track_payload = (
            self._merge_fills_into_track(
                vin, drive_id, DriveTrack.decode(row["track_json"])
            )
            if row["track_json"] is not None
            else None
        )
        return {"drive": summary, "track": track_payload}

    # -- calendar grouping ----------------------------------------------------

    @staticmethod
    def _aggregate_drive_rows(entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate a list of drive-like dicts into one AGG payload.

        Each entry must carry ``distance_miles``, ``duration_seconds``,
        ``energy_kwh``, ``has_track`` and ``sort_ts``. Efficiency is computed
        the same way as ``_build_stats``/``window_stats`` (miles / kWh summed
        across the group, None when no energy was recorded).
        """
        if not entries:
            return {
                "drives": 0,
                "miles": 0.0,
                "hours": 0.0,
                "energy_kwh": 0.0,
                "efficiency_mi_kwh": None,
                "with_route": 0,
                "first_ts": None,
                "last_ts": None,
            }
        total_miles = sum(float(e["distance_miles"] or 0.0) for e in entries)
        total_duration = sum(float(e["duration_seconds"] or 0.0) for e in entries)
        total_energy = sum(float(e["energy_kwh"] or 0.0) for e in entries)
        with_route = sum(1 for e in entries if e["has_track"])
        sort_values = [e["sort_ts"] for e in entries]
        efficiency = round(total_miles / total_energy, 2) if total_energy > 0 else None
        return {
            "drives": len(entries),
            "miles": round(total_miles, 1),
            "hours": round(total_duration / 3600.0, 2),
            "energy_kwh": round(total_energy, 2),
            "efficiency_mi_kwh": efficiency,
            "with_route": with_route,
            "first_ts": min(sort_values),
            "last_ts": max(sort_values),
        }

    def calendar(
        self,
        vin: str | Sequence[str],
        tz: tzinfo,
        year: int | None = None,
        month: int | None = None,
        include_micro: bool = False,
    ) -> dict[str, Any]:
        """Group a VIN's drives into an All time -> years -> months -> days tree.

        Grouping happens in Python (from one flat query, no schema change), by
        each drive's ``sort_ts`` converted to a local calendar day in ``tz``
        (the caller passes Home Assistant's configured zone). ``month``
        requires ``year``. The "months" key is present only when ``year`` is
        given, and "days" only when both ``year`` and ``month`` are given
        (this store omits the key entirely rather than returning an empty
        list, so callers can tell "not requested" from "requested but empty").

        ``vin`` may be a sequence of VINs (``vin IN (...)``): the tree is then
        combined and every node also carries ``by_vin: {vin: {drives, miles}}``
        (every requested VIN listed, zeros included). A bare string keeps the
        single-VIN payload unchanged.
        """
        if month is not None and year is None:
            raise ValueError("calendar: month requires year")
        self._assert_executor_thread()
        multi = not isinstance(vin, str)
        vins = list(dict.fromkeys(vin)) if multi else [vin]
        marks = ",".join("?" for _ in vins)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT d.vin AS vin, d.sort_ts AS sort_ts, d.distance_miles AS distance_miles,
                       d.duration_seconds AS duration_seconds,
                       d.energy_kwh AS energy_kwh,
                       (t.drive_id IS NOT NULL) AS has_track
                  FROM drives d
                  LEFT JOIN drive_tracks t
                    ON t.vin = d.vin AND t.drive_id = d.drive_id
                 WHERE d.vin IN ({marks}) AND d.sort_ts IS NOT NULL
                   AND (? OR d.is_micro_drive = 0)
                """,
                (*vins, 1 if include_micro else 0),
            ).fetchall()

        entries: list[dict[str, Any]] = []
        for row in rows:
            local_dt = datetime.fromtimestamp(row["sort_ts"], tz)
            entries.append(
                {
                    "vin": row["vin"],
                    "sort_ts": row["sort_ts"],
                    "distance_miles": row["distance_miles"],
                    "duration_seconds": row["duration_seconds"],
                    "energy_kwh": row["energy_kwh"],
                    "has_track": bool(row["has_track"]),
                    "year": local_dt.year,
                    "month": local_dt.month,
                    "day": local_dt.day,
                }
            )

        def agg(group: list[dict[str, Any]]) -> dict[str, Any]:
            node = self._aggregate_drive_rows(group)
            if multi:
                by_vin = {v: {"drives": 0, "miles": 0.0} for v in vins}
                for e in group:
                    slot = by_vin[e["vin"]]
                    slot["drives"] += 1
                    slot["miles"] += float(e["distance_miles"] or 0.0)
                for slot in by_vin.values():
                    slot["miles"] = round(slot["miles"], 1)
                node["by_vin"] = by_vin
            return node

        result: dict[str, Any] = {"totals": agg(entries)}

        years_map: dict[int, list[dict[str, Any]]] = {}
        for entry in entries:
            years_map.setdefault(entry["year"], []).append(entry)
        result["years"] = [
            {"key": f"{y:04d}", **agg(years_map[y])}
            for y in sorted(years_map, reverse=True)
        ]

        if year is not None:
            year_entries = [e for e in entries if e["year"] == year]
            months_map: dict[int, list[dict[str, Any]]] = {}
            for entry in year_entries:
                months_map.setdefault(entry["month"], []).append(entry)
            result["months"] = [
                {
                    "key": f"{year:04d}-{m:02d}",
                    **agg(months_map[m]),
                }
                for m in sorted(months_map, reverse=True)
            ]

            if month is not None:
                month_entries = [e for e in year_entries if e["month"] == month]
                days_map: dict[int, list[dict[str, Any]]] = {}
                for entry in month_entries:
                    days_map.setdefault(entry["day"], []).append(entry)
                result["days"] = [
                    {
                        "key": f"{year:04d}-{month:02d}-{d:02d}",
                        **agg(days_map[d]),
                    }
                    for d in sorted(days_map, reverse=True)
                ]

        return result

    def day(
        self,
        vin: str,
        tz: tzinfo,
        day: date,
        include_micro: bool = False,
    ) -> dict[str, Any]:
        """Return one local calendar day's drives (segments), stops, and endpoints.

        The window is ``[local midnight of day, local midnight of next day)``
        computed in ``tz``, so it naturally stretches or shrinks on a DST
        transition day.
        """
        self._assert_executor_thread()
        midnight_time = datetime.min.time()
        start_ts = datetime.combine(day, midnight_time, tzinfo=tz).timestamp()
        end_ts = datetime.combine(
            day + timedelta(days=1), midnight_time, tzinfo=tz
        ).timestamp()

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT d.*, t.source AS track_source, t.detail AS track_detail,
                       t.point_count AS track_point_count, t.track_json AS track_json
                  FROM drives d
                  LEFT JOIN drive_tracks t
                    ON t.vin = d.vin AND t.drive_id = d.drive_id
                 WHERE d.vin = :vin AND d.sort_ts IS NOT NULL
                   AND d.sort_ts >= :start_ts AND d.sort_ts < :end_ts
                   AND (:include_micro OR d.is_micro_drive = 0)
                 ORDER BY d.sort_ts ASC, d.created_ts ASC, d.id ASC
                """,
                {
                    "vin": vin,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "include_micro": include_micro,
                },
            ).fetchall()

        # Parsed once per call (not per segment) so a busy day view doesn't
        # re-parse the stored JSON params for every drive.
        model_params = self.get_energy_model(vin) or ENERGY_MODEL_DEFAULT_PARAMS

        place_map = self._place_refs_for_ids(
            [r["start_place_id"] for r in rows] + [r["end_place_id"] for r in rows],
        )
        route_ids = {r["route_id"] for r in rows if r["route_id"] is not None}
        route_map: dict[int, sqlite3.Row] = {}
        if route_ids:
            placeholders = ",".join("?" for _ in route_ids)
            with self._lock:
                route_rows = self._conn.execute(
                    f"SELECT route_id, variant, name, drive_count, stats_json "
                    f"FROM routes WHERE route_id IN ({placeholders})",
                    list(route_ids),
                ).fetchall()
            route_map = {r["route_id"]: r for r in route_rows}

        segments: list[dict[str, Any]] = []
        tracks: list[DriveTrack | None] = []
        for idx, row in enumerate(rows):
            summary = self._row_to_drive_summary(row)
            track: DriveTrack | None = None
            if row["track_json"] is not None:
                # One unreadable route must not hide the rest of the day.
                try:
                    track = DriveTrack.decode(row["track_json"])
                except ValueError as err:
                    _LOGGER.warning(
                        "Skipping unreadable route for drive %s: %s",
                        row["drive_id"],
                        err,
                    )
            tracks.append(track)
            route_field: dict[str, Any] | None = None
            route_row = (
                route_map.get(row["route_id"]) if row["route_id"] is not None else None
            )
            if route_row is not None:
                start_ref = self._place_ref_from_row(
                    place_map.get(row["start_place_id"])
                )
                end_ref = self._place_ref_from_row(place_map.get(row["end_place_id"]))
                start_label = (
                    start_ref["label"]
                    if start_ref
                    else f"Place #{row['start_place_id']}"
                )
                end_label = (
                    end_ref["label"] if end_ref else f"Place #{row['end_place_id']}"
                )
                label = route_row["name"] or routes_mod.route_label(
                    start_label, end_label, route_row["variant"]
                )
                route_stats = json.loads(route_row["stats_json"] or "{}")
                drive_stat = route_stats.get("per_drive", {}).get(
                    routes_mod.drive_key(vin, row["drive_id"]), {}
                )
                # The drive's standing among its own car's drives on the route
                # (an R2 isn't ranked against an R1T); the car's own count.
                own = (route_stats.get("by_vin") or {}).get(vin) or {}
                route_field = {
                    "id": route_row["route_id"],
                    "label": label,
                    "rank": drive_stat.get("vin_rank"),
                    "count": own.get("count") or route_row["drive_count"],
                    "vs_avg_pct": drive_stat.get("vin_vs_avg_pct"),
                }
            model: dict[str, Any] | None = None
            if track is not None:
                try:
                    model = anchored_efficiency(
                        track,
                        model_params,
                        row["battery_capacity_kwh"],
                        drive_energy_kwh=row["energy_kwh"],
                    )
                except Exception:
                    _LOGGER.warning(
                        "Anchored efficiency model failed for drive %s",
                        row["drive_id"],
                        exc_info=True,
                    )
            segments.append(
                {
                    "index": idx,
                    **summary,
                    "battery_capacity_kwh": row["battery_capacity_kwh"],
                    # 3-minute efficiency chunks, for the day/drive charts.
                    "chunks": self._chart_chunks(row["chunks_json"]),
                    "track": (
                        self._merge_fills_into_track(vin, row["drive_id"], track)
                        if track is not None
                        else None
                    ),
                    "model": model,
                    "start_place": self._place_ref_from_row(
                        place_map.get(row["start_place_id"])
                    ),
                    "end_place": self._place_ref_from_row(
                        place_map.get(row["end_place_id"])
                    ),
                    "route": route_field,
                }
            )

        totals = self._aggregate_drive_rows(segments)

        # Where each segment's route begins and ends: its track's first/last
        # point, else the drive's own start/end coordinates.
        route_starts: list[tuple[float, float, float | None] | None] = []
        route_ends: list[tuple[float, float, float | None] | None] = []
        for seg, track in zip(segments, tracks, strict=True):
            if track is not None and track.points:
                first, last = track.points[0], track.points[-1]
                route_starts.append((first.lat, first.lon, first.t))
                route_ends.append((last.lat, last.lon, last.t))
                continue
            start_ts = seg.get("start_ts")
            if start_ts is None:
                start_ts = seg.get("sort_ts")
            route_starts.append(
                (seg["start_lat"], seg["start_lon"], start_ts)
                if seg.get("start_lat") is not None and seg.get("start_lon") is not None
                else None
            )
            route_ends.append(
                (seg["end_lat"], seg["end_lon"], seg.get("end_ts"))
                if seg.get("end_lat") is not None and seg.get("end_lon") is not None
                else None
            )

        def unrecorded_gap(
            parked: tuple[float, float], resumed: tuple[float, float, Any] | None
        ) -> float | None:
            """Metres between a parked spot and where recording resumed, if a gap."""
            if resumed is None:
                return None
            distance = haversine_m(parked[0], parked[1], resumed[0], resumed[1])
            return distance if DAY_GAP_MIN_M < distance <= DAY_GAP_MAX_M else None

        gaps: list[dict[str, Any]] = []
        stops: list[dict[str, Any]] = []
        for i in range(len(segments) - 1):
            seg = segments[i]
            nxt = segments[i + 1]
            parked_at = route_ends[i]
            if parked_at is None:
                continue
            arrive_ts = seg.get("end_ts")
            depart_ts = nxt.get("start_ts")
            if depart_ts is None:
                depart_ts = nxt.get("sort_ts")
            duration_seconds = None
            if arrive_ts is not None and depart_ts is not None:
                duration_seconds = max(0.0, depart_ts - arrive_ts)
            stops.append(
                {
                    "after_index": i,
                    "lat": parked_at[0],
                    "lon": parked_at[1],
                    "arrive_ts": arrive_ts,
                    "depart_ts": depart_ts,
                    "duration_seconds": duration_seconds,
                    "place": seg.get("end_place"),
                }
            )
            gap = unrecorded_gap(parked_at[:2], route_starts[i + 1])
            if gap is not None:
                resumed = route_starts[i + 1]
                gaps.append(
                    {
                        "after_index": i,
                        "from": [parked_at[0], parked_at[1]],
                        "to": [resumed[0], resumed[1]],
                        "distance_m": round(gap),
                    }
                )

        start_point: dict[str, Any] | None = None
        end_point: dict[str, Any] | None = None
        if segments:
            recorded_start = route_starts[0]
            if recorded_start is not None:
                start_point = {
                    "lat": recorded_start[0],
                    "lon": recorded_start[1],
                    "ts": recorded_start[2],
                    "place": segments[0].get("start_place"),
                }
                # The day starts where the car was parked before its first
                # drive. The car's first reports can arrive a minute or two after
                # it wakes and pulls away (a kilometre down the road), so start
                # there and return the unrecorded stretch as a gap to draw.
                with self._lock:
                    prev = self._conn.execute(
                        """
                        SELECT end_lat, end_lon FROM drives
                         WHERE vin = ? AND sort_ts IS NOT NULL AND sort_ts < ?
                           AND end_lat IS NOT NULL AND end_lon IS NOT NULL
                         ORDER BY sort_ts DESC, created_ts DESC, id DESC
                         LIMIT 1
                        """,
                        (vin, rows[0]["sort_ts"]),
                    ).fetchone()
                if prev is not None:
                    parked = (prev["end_lat"], prev["end_lon"])
                    gap = unrecorded_gap(parked, recorded_start)
                    if gap is not None:
                        start_point = {
                            "lat": parked[0],
                            "lon": parked[1],
                            "ts": recorded_start[2],
                            "place": segments[0].get("start_place"),
                        }
                        gaps.insert(
                            0,
                            {
                                "after_index": -1,
                                "from": [parked[0], parked[1]],
                                "to": [recorded_start[0], recorded_start[1]],
                                "distance_m": round(gap),
                            },
                        )
            recorded_end = route_ends[-1]
            if recorded_end is not None:
                end_point = {
                    "lat": recorded_end[0],
                    "lon": recorded_end[1],
                    "ts": recorded_end[2],
                    "place": segments[-1].get("end_place"),
                }

        # The tail of the most recent earlier drive with a stored track, so
        # the Efficiency chart's rolling average and 3-min chunks have
        # context for the first minutes of the day's first drive instead of
        # a blank start.
        prior_tail: dict[str, Any] | None = None
        if segments:
            with self._lock:
                prior_row = self._conn.execute(
                    """
                    SELECT d.drive_id, d.battery_capacity_kwh, t.track_json
                      FROM drives d
                      JOIN drive_tracks t ON t.vin = d.vin AND t.drive_id = d.drive_id
                     WHERE d.vin = ? AND d.sort_ts IS NOT NULL AND d.sort_ts < ?
                       AND t.track_json IS NOT NULL
                     ORDER BY d.sort_ts DESC, d.created_ts DESC, d.id DESC
                     LIMIT 1
                    """,
                    (vin, rows[0]["sort_ts"]),
                ).fetchone()
            if prior_row is not None:
                prior_track: DriveTrack | None = None
                try:
                    prior_track = DriveTrack.decode(prior_row["track_json"])
                except ValueError as err:
                    _LOGGER.warning(
                        "Skipping unreadable prior-tail route for drive %s: %s",
                        prior_row["drive_id"],
                        err,
                    )
                if prior_track is not None and prior_track.points:
                    tail = _trim_track_tail(prior_track)
                    prior_tail = {
                        "track": tail.to_payload(),
                        "battery_capacity_kwh": prior_row["battery_capacity_kwh"],
                    }

        return {
            "date": day.isoformat(),
            "totals": totals,
            "segments": segments,
            "prior_tail": prior_tail,
            "stops": stops,
            "gaps": gaps,
            "start": start_point,
            "end": end_point,
        }

    @staticmethod
    def _chart_chunks(chunks_json: str | None) -> list[dict[str, Any]]:
        """Return a drive's chunks as ``{start_ts, duration_seconds, efficiency_mi_kwh}``."""
        try:
            raw = json.loads(chunks_json or "[]")
        except ValueError:
            return []
        chunks: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            start_ts = _parse_iso_to_epoch(item.get("start_time"))
            if start_ts is None:
                continue
            chunks.append(
                {
                    "start_ts": start_ts,
                    "duration_seconds": item.get("duration_seconds"),
                    "efficiency_mi_kwh": item.get("efficiency_mi_kwh"),
                }
            )
        return chunks

    def drives_missing_tracks(
        self, vin: str, since_ts: float
    ) -> list[tuple[str, float, float]]:
        """Return (drive_id, start_ts, end_ts) for trackless drives, oldest first."""
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT d.drive_id, d.start_ts, d.end_ts
                  FROM drives d
                 WHERE d.vin = ? AND d.sort_ts IS NOT NULL AND d.sort_ts >= ?
                   AND d.start_ts IS NOT NULL AND d.end_ts IS NOT NULL
                   AND NOT EXISTS (
                        SELECT 1 FROM drive_tracks t
                         WHERE t.vin = d.vin AND t.drive_id = d.drive_id
                   )
                 ORDER BY d.sort_ts ASC
                """,
                (vin, since_ts),
            ).fetchall()
        return [(r["drive_id"], r["start_ts"], r["end_ts"]) for r in rows]

    # -- road-snapped gap filling ------------------------------------------------

    def gaps_to_snap(
        self, vin: str, limit: int = GAPS_TO_SNAP_DEFAULT_LIMIT
    ) -> list[tuple[str, road_snap.Gap]]:
        """Return up to `limit` (drive_id, Gap) pairs still needing a snap attempt.

        Only considers ``drive_tracks`` rows with ``gaps_scanned = 0``: a
        route is decoded and gap-scanned here, then marked scanned
        (``gaps_scanned = 1``) once every gap it contains has a
        ``track_fills`` row (a real fill or a 'none' record), so a route
        with no gaps -- the overwhelming majority -- is only ever decoded
        once, not on every call. Replacing a track's ``track_json`` (a
        backfill overwriting a live route, a thinning pass) resets
        ``gaps_scanned`` back to 0.
        """
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                "SELECT drive_id, track_json FROM drive_tracks "
                "WHERE vin = ? AND gaps_scanned = 0 ORDER BY sort_ts LIMIT ?",
                (vin, max(limit, 1) * 5),
            ).fetchall()

        result: list[tuple[str, road_snap.Gap]] = []
        fully_scanned: list[str] = []
        for row in rows:
            drive_id = row["drive_id"]
            try:
                track = DriveTrack.decode(row["track_json"])
            except ValueError as err:
                _LOGGER.debug(
                    "road_snap: undecodable track for drive %s (VIN %s): %s",
                    drive_id,
                    vin,
                    err,
                )
                fully_scanned.append(drive_id)
                continue
            gaps = road_snap.find_gaps(track)
            if not gaps:
                fully_scanned.append(drive_id)
                continue
            with self._lock:
                existing_after_t = {
                    r["after_t"]
                    for r in self._conn.execute(
                        "SELECT after_t FROM track_fills WHERE vin = ? AND drive_id = ?",
                        (vin, drive_id),
                    ).fetchall()
                }
            unresolved = [g for g in gaps if g.start.t not in existing_after_t]
            if not unresolved:
                fully_scanned.append(drive_id)
                continue
            for gap in unresolved:
                if len(result) >= limit:
                    break
                result.append((drive_id, gap))
            if len(result) >= limit:
                break

        if fully_scanned:
            with self._lock, self._transaction():
                placeholders = ",".join("?" for _ in fully_scanned)
                self._conn.execute(
                    "UPDATE drive_tracks SET gaps_scanned = 1 "
                    f"WHERE vin = ? AND drive_id IN ({placeholders})",
                    [vin, *fully_scanned],
                )
        return result

    def get_cached_roads(self, key: str) -> list[road_snap.Way] | None:
        """Return cached parsed OSM ways for `key`, or None if absent/stale/corrupt."""
        self._assert_executor_thread()
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_ts, data FROM osm_roads WHERE bbox_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        if time.time() - row["fetched_ts"] > OSM_ROADS_TTL_SECONDS:
            return None
        try:
            payload = json.loads(zlib.decompress(row["data"]))
        except (zlib.error, json.JSONDecodeError, UnicodeDecodeError, TypeError) as err:
            _LOGGER.debug("road_snap: corrupt cached roads for key %s: %s", key, err)
            return None
        return road_snap.ways_from_json(payload)

    def save_cached_roads(self, key: str, ways: list[road_snap.Way]) -> None:
        """Cache parsed OSM ways for `key` (zlib-compressed JSON), reused for 90 days."""
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning(
                "Analytics database is read-only; save_cached_roads skipped"
            )
            return
        data = zlib.compress(
            json.dumps(road_snap.ways_to_json(ways), separators=(",", ":")).encode()
        )
        with self._lock, self._transaction():
            self._conn.execute(
                "INSERT INTO osm_roads (bbox_key, fetched_ts, data) VALUES (?, ?, ?) "
                "ON CONFLICT(bbox_key) DO UPDATE SET "
                "fetched_ts=excluded.fetched_ts, data=excluded.data",
                (key, time.time(), data),
            )

    def save_track_fill(
        self,
        vin: str,
        drive_id: str,
        after_t: float,
        points: list[TrackPoint],
        source: str = "osm",
    ) -> None:
        """Save (upsert) one gap's fill; points=[] with source='none' records a tried,
        unfillable gap so it is never retried on every scan.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; save_track_fill skipped")
            return
        points_json = DriveTrack(points).to_points_json()
        with self._lock, self._transaction():
            self._conn.execute(
                "INSERT INTO track_fills "
                "(vin, drive_id, after_t, points_json, source, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(vin, drive_id, after_t) DO UPDATE SET "
                "points_json=excluded.points_json, source=excluded.source, "
                "created_ts=excluded.created_ts",
                (vin, drive_id, after_t, points_json, source, time.time()),
            )

    def get_track_fills(self, vin: str, drive_id: str) -> list[dict[str, Any]]:
        """Return this drive's track_fills rows as {after_t, points, source}, ordered."""
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                "SELECT after_t, points_json, source FROM track_fills "
                "WHERE vin = ? AND drive_id = ? ORDER BY after_t",
                (vin, drive_id),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                points = DriveTrack.from_points_json(row["points_json"]).points
            except (ValueError, TypeError, json.JSONDecodeError) as err:
                _LOGGER.debug(
                    "road_snap: corrupt fill points for drive %s (VIN %s): %s",
                    drive_id,
                    vin,
                    err,
                )
                points = []
            result.append(
                {"after_t": row["after_t"], "points": points, "source": row["source"]}
            )
        return result

    def drives_counted_in_heat(self, vin: str, drive_ids: list[str]) -> set[str]:
        """Return which of these drive_ids already have a road_heat_drives row."""
        self._assert_executor_thread()
        if not drive_ids:
            return set()
        with self._lock:
            placeholders = ",".join("?" for _ in drive_ids)
            rows = self._conn.execute(
                "SELECT drive_id FROM road_heat_drives WHERE vin = ? "
                f"AND drive_id IN ({placeholders})",
                [vin, *drive_ids],
            ).fetchall()
        return {r["drive_id"] for r in rows}

    def _merge_fills_into_track(
        self, vin: str, drive_id: str, track: DriveTrack
    ) -> dict[str, list]:
        """Return `track`'s payload with any stored gap fills merged in, time-ordered.

        Adds a parallel ``filled`` boolean column (True for an inserted
        point, False for a recorded one). Omitted entirely when the track
        has no real fills, so a payload with nothing to merge is unchanged.
        """
        fills = self.get_track_fills(vin, drive_id)
        fill_points = [p for f in fills if f["points"] for p in f["points"]]
        payload = track.to_payload()
        if not fill_points:
            return payload

        rows: list[tuple[float, dict[str, Any], bool]] = [
            (
                payload["t"][i],
                {
                    "lat": payload["lat"][i],
                    "lon": payload["lon"][i],
                    "t": payload["t"][i],
                    "speed_mps": payload["speed_mps"][i],
                    "alt_m": payload["alt_m"][i],
                    "soc": payload["soc"][i],
                    "odo_m": payload["odo_m"][i],
                },
                False,
            )
            for i in range(len(payload["t"]))
        ]
        for point in fill_points:
            rows.append(
                (
                    point.t,
                    {
                        "lat": round(point.lat, 5),
                        "lon": round(point.lon, 5),
                        "t": round(point.t, 1),
                        "speed_mps": (
                            round(point.speed_mps, 2)
                            if point.speed_mps is not None
                            else None
                        ),
                        "alt_m": (
                            round(point.alt_m, 1) if point.alt_m is not None else None
                        ),
                        "soc": None,
                        "odo_m": None,
                    },
                    True,
                )
            )
        rows.sort(key=lambda item: item[0])
        return {
            "t": [r[1]["t"] for r in rows],
            "lat": [r[1]["lat"] for r in rows],
            "lon": [r[1]["lon"] for r in rows],
            "speed_mps": [r[1]["speed_mps"] for r in rows],
            "alt_m": [r[1]["alt_m"] for r in rows],
            "soc": [r[1]["soc"] for r in rows],
            "odo_m": [r[1]["odo_m"] for r in rows],
            "filled": [r[2] for r in rows],
        }

    def _track_with_fills(
        self, vin: str, drive_id: str, track: DriveTrack
    ) -> DriveTrack:
        """Return `track` with any stored gap fills merged in as real points.

        Used only for road-heat counting (see ``_update_heat_locked``), so a
        filled gap's estimated path contributes cells too, instead of the
        straight-line gap it replaces.
        """
        fills = self.get_track_fills(vin, drive_id)
        fill_points = [p for f in fills if f["points"] for p in f["points"]]
        if not fill_points:
            return track
        merged = sorted([*track.points, *fill_points], key=lambda p: p.t)
        return DriveTrack(merged)

    # -- live-drive checkpoint --------------------------------------------------

    def save_active_checkpoint(
        self,
        vin: str,
        drive_id: str,
        state: dict[str, Any],
        new_points: DriveTrack | None,
        seq: int,
    ) -> None:
        """Persist live drive-tracker state and (optionally) a new track chunk.

        If this VIN already holds a checkpoint for a *different* drive_id,
        that other drive's chunks are deleted first (a drive can only have
        one in-progress checkpoint at a time).
        """
        self._assert_executor_thread()
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; save_active_checkpoint skipped"
                )
                return
            now_ts = time.time()
            state_json = json.dumps(state)
            with self._transaction():
                existing = self._conn.execute(
                    "SELECT drive_id FROM active_drive WHERE vin = ?", (vin,)
                ).fetchone()
                if existing is not None and existing["drive_id"] != drive_id:
                    self._conn.execute(
                        "DELETE FROM active_track_chunks WHERE vin = ? AND drive_id = ?",
                        (vin, existing["drive_id"]),
                    )
                self._conn.execute(
                    "INSERT INTO active_drive(vin, drive_id, state_json, updated_ts) "
                    "VALUES(?, ?, ?, ?) ON CONFLICT(vin) DO UPDATE SET "
                    "drive_id=excluded.drive_id, state_json=excluded.state_json, "
                    "updated_ts=excluded.updated_ts",
                    (vin, drive_id, state_json, now_ts),
                )
                if new_points is not None and len(new_points) > 0:
                    self._conn.execute(
                        "INSERT INTO active_track_chunks(vin, drive_id, seq, points_json) "
                        "VALUES(?, ?, ?, ?) ON CONFLICT(vin, drive_id, seq) "
                        "DO UPDATE SET points_json=excluded.points_json",
                        (vin, drive_id, seq, new_points.to_points_json()),
                    )

    def load_active_checkpoint(self, vin: str) -> ActiveCheckpoint | None:
        """Return the in-progress drive checkpoint for a VIN, or None."""
        self._assert_executor_thread()
        with self._lock:
            row = self._conn.execute(
                "SELECT drive_id, state_json, updated_ts FROM active_drive WHERE vin = ?",
                (vin,),
            ).fetchone()
            if row is None:
                return None
            chunk_rows = self._conn.execute(
                "SELECT seq, points_json FROM active_track_chunks "
                "WHERE vin = ? AND drive_id = ? ORDER BY seq ASC",
                (vin, row["drive_id"]),
            ).fetchall()

        track = DriveTrack()
        next_seq = 0
        for chunk_row in chunk_rows:
            chunk_track = DriveTrack.from_points_json(chunk_row["points_json"])
            track.extend(chunk_track.points)
            next_seq = max(next_seq, chunk_row["seq"] + 1)

        return ActiveCheckpoint(
            drive_id=row["drive_id"],
            state=json.loads(row["state_json"]),
            track=track,
            next_seq=next_seq,
            updated_ts=row["updated_ts"],
        )

    def clear_active_checkpoint(self, vin: str) -> None:
        """Delete the in-progress drive checkpoint (state + chunks) for a VIN."""
        self._assert_executor_thread()
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; clear_active_checkpoint skipped"
                )
                return
            with self._transaction():
                self._conn.execute("DELETE FROM active_drive WHERE vin = ?", (vin,))
                self._conn.execute(
                    "DELETE FROM active_track_chunks WHERE vin = ?", (vin,)
                )

    # -- track retention/thinning -------------------------------------------------

    def prune_tracks(
        self,
        vin: str,
        delete_before_ts: float | None,
        thin_before_ts: float | None,
        thin_tolerance_m: float = 10.0,
    ) -> dict[str, int]:
        """Delete old tracks and/or thin (simplify) tracks past a separate cutoff.

        Either cutoff may be None to skip that step. Thinning processes rows
        in batches of ``TRACK_THIN_BATCH_SIZE`` so memory stays bounded on
        large histories, and only touches rows still at detail='full', so
        re-running it is a no-op once every eligible row has been thinned.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; prune_tracks skipped")
            return {"deleted": 0, "thinned": 0}
        deleted = 0
        thinned = 0
        if delete_before_ts is not None:
            with self._lock, self._transaction():
                cur = self._conn.execute(
                    "DELETE FROM drive_tracks WHERE vin = ? "
                    "AND sort_ts IS NOT NULL AND sort_ts < ?",
                    (vin, delete_before_ts),
                )
                deleted = cur.rowcount or 0
                self._conn.execute(
                    """
                    DELETE FROM track_fills WHERE vin = ? AND NOT EXISTS (
                        SELECT 1 FROM drive_tracks t
                         WHERE t.vin = track_fills.vin AND t.drive_id = track_fills.drive_id
                    )
                    """,
                    (vin,),
                )

        # Lock and commit per batch: every DB call serializes on self._lock,
        # so holding it across a first-time thinning of a long history would
        # block drive saves and checkpoints for the whole run.
        while thin_before_ts is not None:
            with self._lock, self._transaction():
                batch = self._thin_track_batch_locked(
                    vin, thin_before_ts, thin_tolerance_m
                )
            if batch is None:
                break
            thinned += batch
        with self._lock:
            self._conn.execute("PRAGMA incremental_vacuum(200)")
        return {"deleted": deleted, "thinned": thinned}

    def _thin_track_batch_locked(
        self, vin: str, thin_before_ts: float, thin_tolerance_m: float
    ) -> int | None:
        """Thin one batch of full-detail tracks; None when none are left.

        Caller must hold ``self._lock`` inside an open transaction.
        """
        rows = self._conn.execute(
            "SELECT id, track_json FROM drive_tracks "
            "WHERE vin = ? AND detail = 'full' "
            "AND sort_ts IS NOT NULL AND sort_ts < ? "
            "LIMIT ?",
            (vin, thin_before_ts, TRACK_THIN_BATCH_SIZE),
        ).fetchall()
        if not rows:
            return None
        now_ts = time.time()
        thinned = 0
        for row in rows:
            try:
                track = DriveTrack.decode(row["track_json"])
            except ValueError as err:
                # Unreadable row: drop it, or it would be re-selected (and
                # fail) on every batch and every daily run.
                _LOGGER.warning(
                    "Deleting unreadable GPS track row %s for VIN %s: %s",
                    row["id"],
                    vin,
                    err,
                )
                self._conn.execute(
                    "DELETE FROM drive_tracks WHERE id = ?", (row["id"],)
                )
                continue
            thin_track = track.simplify(thin_tolerance_m)
            bbox = thin_track.bbox()
            self._conn.execute(
                "UPDATE drive_tracks SET track_json = ?, "
                "preview_json = ?, point_count = ?, min_lat = ?, "
                "min_lon = ?, max_lat = ?, max_lon = ?, "
                "detail = 'thinned', updated_ts = ? WHERE id = ?",
                (
                    thin_track.encode(),
                    thin_track.preview(TRACK_PREVIEW_MAX_POINTS).encode(),
                    len(thin_track),
                    bbox[0] if bbox else None,
                    bbox[1] if bbox else None,
                    bbox[2] if bbox else None,
                    bbox[3] if bbox else None,
                    now_ts,
                    row["id"],
                ),
            )
            thinned += 1
        return thinned

    def recompute_drive_stats(self, vin: str) -> dict[str, int]:
        """Recompute track-derived summary-stat columns for every drive with a track.

        Batches like ``prune_tracks``/``_thin_track_batch_locked``: each batch
        selects rows (drive id and its stored track) under ``self._lock``, computes
        ``compute_track_stats`` outside the lock (pure CPU, no I/O), then
        writes the batch's updates in one transaction. Only the track-derived
        columns are touched -- live-only vehicle-context columns (range,
        drive modes, trailer, driver) are never modified here. Returns
        ``{"updated": <count>}``.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning(
                "Analytics database is read-only; recompute_drive_stats skipped"
            )
            return {"updated": 0}

        updated = 0
        after_id = 0
        while True:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT d.id AS did, t.track_json AS track_json
                      FROM drives d
                      JOIN drive_tracks t ON t.vin = d.vin AND t.drive_id = d.drive_id
                     WHERE d.vin = ? AND d.id > ?
                     ORDER BY d.id ASC LIMIT ?
                    """,
                    (vin, after_id, RECOMPUTE_STATS_BATCH_SIZE),
                ).fetchall()
            if not rows:
                break

            computed: list[tuple[int, Any]] = []
            for row in rows:
                after_id = max(after_id, row["did"])
                try:
                    track = DriveTrack.decode(row["track_json"])
                except ValueError as err:
                    _LOGGER.warning(
                        "Skipping unreadable GPS track for drive id %d (VIN %s) "
                        "during stats recompute: %s",
                        row["did"],
                        vin,
                        err,
                    )
                    continue
                computed.append((row["did"], compute_track_stats(track)))

            if computed:
                with self._lock, self._transaction():
                    for did, stats in computed:
                        self._conn.execute(
                            """
                            UPDATE drives SET
                              moving_seconds = ?, stopped_seconds = ?, stop_count = ?,
                              climb_ft = ?, descent_ft = ?, track_max_speed_mph = ?,
                              pct_distance_over_70mph = ?
                             WHERE id = ?
                            """,
                            (
                                stats.moving_seconds,
                                stats.stopped_seconds,
                                stats.stop_count,
                                (
                                    round(stats.climb_m * _METERS_TO_FEET, 1)
                                    if stats.climb_m is not None
                                    else None
                                ),
                                (
                                    round(stats.descent_m * _METERS_TO_FEET, 1)
                                    if stats.descent_m is not None
                                    else None
                                ),
                                (
                                    round(stats.max_speed_mps * _MPS_TO_MPH, 1)
                                    if stats.max_speed_mps is not None
                                    else None
                                ),
                                stats.pct_distance_over_70mph,
                                did,
                            ),
                        )
                    updated += len(computed)

        with self._lock, self._transaction():
            self._set_meta_locked(
                self._drive_stats_meta_key(vin), str(DRIVE_STATS_VERSION)
            )
        return {"updated": updated}

    @staticmethod
    def _drive_stats_meta_key(vin: str) -> str:
        """Meta key recording the stats definitions a VIN's drives were computed with."""
        return f"drive_stats_version:{vin}"

    def has_unrecomputed_drive_stats(self, vin: str) -> bool:
        """Return True until this VIN's stats were recomputed with the current definitions.

        True right after the v6 schema migration (or a DRIVE_STATS_VERSION
        bump), before the one-time background recompute has run; False once
        it has. Recorded in ``meta`` rather than inferred from NULL columns, so
        a drive whose route can't be read never triggers a recompute of the
        whole history on every restart.
        """
        return self.get_meta(self._drive_stats_meta_key(vin)) != str(
            DRIVE_STATS_VERSION
        )

    def claim_once(self, key: str) -> bool:
        """Return True the first time ``key`` is claimed on this database instance.

        Lets the first of several stores sharing this database run a one-time
        background seed (places/routes are shared) while the rest skip it.
        """
        with self._claims_lock:
            if key in self._seed_claims:
                return False
            self._seed_claims.add(key)
            return True

    # -- datasets ------------------------------------------------------------
    #
    # Places and routes belong to no vehicle. The only partition is the
    # ``dataset`` ('real' | 'demo'), so the synthetic demo cars' made-up places
    # never label, or mix with, the household's real ones. Which VINs are demo
    # vehicles is the ``demo_vehicles`` meta row (see demo.py).

    def _demo_vins_locked(self) -> set[str]:
        """Return the registered demo VINs. Caller must hold ``self._lock``."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (DEMO_VEHICLES_META_KEY,)
        ).fetchone()
        return parse_demo_vins(row["value"] if row is not None else None)

    def dataset_for_vin(self, vin: str) -> str:
        """Return ``'demo'`` for a registered demo VIN, else ``'real'``."""
        self._assert_executor_thread()
        with self._lock:
            demo = self._demo_vins_locked()
        return places.DATASET_DEMO if vin in demo else places.DATASET_REAL

    def _dataset_drive_clause(
        self, dataset: str, column: str = "vin"
    ) -> tuple[str, list[str]]:
        """Return ``(sql, params)`` selecting the drives of ``dataset``.

        Caller must hold ``self._lock``.
        """
        demo = sorted(self._demo_vins_locked())
        placeholders = ",".join("?" for _ in demo)
        if dataset == places.DATASET_DEMO:
            if not demo:
                return "0", []
            return f"{column} IN ({placeholders})", demo
        if not demo:
            return "1", []
        return f"{column} NOT IN ({placeholders})", demo

    def clear_dataset(self, dataset: str) -> None:
        """Delete every place and route of a dataset (and its rebuild stamps).

        Used when the last demo vehicle is removed. Drives are untouched
        (``delete_vin`` removes those); their place/route ids are cleared.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; clear_dataset skipped")
            return
        with self._rebuild_lock, self._lock, self._transaction():
            clause, params = self._dataset_drive_clause(dataset)
            self._conn.execute("DELETE FROM places WHERE dataset = ?", (dataset,))
            self._conn.execute("DELETE FROM routes WHERE dataset = ?", (dataset,))
            self._conn.execute(
                f"UPDATE drives SET start_place_id = NULL, end_place_id = NULL, "
                f"route_id = NULL WHERE {clause}",
                params,
            )
            self._conn.execute(
                "DELETE FROM meta WHERE key IN (?, ?)",
                (self._places_meta_key(dataset), self._routes_meta_key(dataset)),
            )

    # -- favorite places -------------------------------------------------------

    @staticmethod
    def _places_meta_key(dataset: str) -> str:
        """Meta key recording the places definitions a dataset was last rebuilt with."""
        return f"places_version:{dataset}"

    def has_unbuilt_places(self, dataset: str) -> bool:
        """Return True until this dataset's places were built with the current definitions.

        Mirrors ``has_unrecomputed_drive_stats``: true right after the v8/v10
        schema migration (or a PLACES_VERSION bump) until the one-time
        background ``rebuild_places`` has run.
        """
        self._assert_executor_thread()
        return self.get_meta(self._places_meta_key(dataset)) != str(PLACES_VERSION)

    def _fetch_places_locked(self, dataset: str) -> list[sqlite3.Row]:
        """Return every place row of a dataset. Caller must hold ``self._lock``."""
        return self._conn.execute(
            "SELECT place_id, name, category, lat, lon, radius_m, source, "
            "zone_entity_id, hidden, geocode_name, geocoded_ts "
            "FROM places WHERE dataset = ?",
            (dataset,),
        ).fetchall()

    def sync_zones(self, dataset: str, zones: list[dict[str, Any]]) -> dict[str, int]:
        """Upsert HA zones as places (keyed by zone_entity_id), then rebuild.

        Only the ``real`` dataset ever receives zones -- the demo cars must
        never see the user's real home. A zone's radius is clamped to
        ``places.ZONE_RADIUS_BOUNDS``. A zone place's name is only set when
        the row is first created -- a later rename (via ``update_place``)
        survives a resync. A zone that no longer exists has its place row
        deleted; its drives are reassigned by the ``rebuild_places`` call that
        follows.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; sync_zones skipped")
            return {"places": 0, "assigned": 0}
        if dataset != places.DATASET_REAL:
            return {"places": 0, "assigned": 0}

        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT place_id, zone_entity_id FROM places "
                "WHERE dataset = ? AND source = 'zone'",
                (dataset,),
            ).fetchall()
            existing_by_entity = {r["zone_entity_id"]: r["place_id"] for r in existing}
            seen_entities: set[str] = set()
            with self._transaction():
                for zone in zones:
                    entity_id = zone.get("entity_id")
                    lat, lon = zone.get("latitude"), zone.get("longitude")
                    if not entity_id or lat is None or lon is None:
                        continue
                    seen_entities.add(entity_id)
                    radius = zone.get("radius") or places.DEFAULT_RADIUS_M
                    radius = max(
                        places.ZONE_RADIUS_BOUNDS[0],
                        min(places.ZONE_RADIUS_BOUNDS[1], float(radius)),
                    )
                    if entity_id in existing_by_entity:
                        self._conn.execute(
                            "UPDATE places SET lat = ?, lon = ?, radius_m = ?, "
                            "updated_ts = ? WHERE place_id = ?",
                            (lat, lon, radius, now, existing_by_entity[entity_id]),
                        )
                    else:
                        self._conn.execute(
                            "INSERT INTO places (dataset, name, category, lat, lon, "
                            "radius_m, source, zone_entity_id, hidden, created_ts, "
                            "updated_ts) VALUES (?, ?, NULL, ?, ?, ?, 'zone', ?, 0, ?, ?)",
                            (
                                dataset,
                                zone.get("name"),
                                lat,
                                lon,
                                radius,
                                entity_id,
                                now,
                                now,
                            ),
                        )
                # HA always has zone.home, so no zones at all means they
                # haven't loaded yet (an early startup sync) -- never treat
                # that as "every zone was deleted", which would drop their
                # places and any renames.
                stale_entities = (
                    set(existing_by_entity) - seen_entities if seen_entities else set()
                )
                for entity_id in stale_entities:
                    self._conn.execute(
                        "DELETE FROM places WHERE place_id = ?",
                        (existing_by_entity[entity_id],),
                    )
        result = self.rebuild_places(dataset)
        self.rebuild_routes(dataset)
        return result

    def rebuild_places(self, dataset: str) -> dict[str, int]:
        """Deterministically re-cluster a dataset's auto places and reassign its drives.

        Endpoints are computed per drive's own vehicle (a drive's start chains
        to the *same car's* previous drive's end), then pooled across the
        dataset's vehicles before clustering. Selects places/drives under
        ``self._lock``, computes clustering and assignment outside it (pure
        CPU, matching ``recompute_drive_stats``'s batching philosophy), then
        writes every change -- place upserts/deletes and every drive's
        start/end place id -- in one transaction. Whole rebuilds are
        serialized so two stores seeding at once can't both insert the same
        new cluster.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; rebuild_places skipped")
            return {"places": 0, "assigned": 0}

        with self._rebuild_lock:
            with self._lock:
                place_rows = self._fetch_places_locked(dataset)
                clause, clause_params = self._dataset_drive_clause(dataset)
                drive_rows = self._conn.execute(
                    f"SELECT vin, drive_id, start_lat, start_lon, start_ts, "
                    f"end_lat, end_lon, end_ts FROM drives WHERE {clause} "
                    f"ORDER BY vin ASC, sort_ts ASC, created_ts ASC, id ASC",
                    clause_params,
                ).fetchall()

            fixed_places = [
                places.ExistingPlace(
                    place_id=r["place_id"],
                    lat=r["lat"],
                    lon=r["lon"],
                    radius_m=r["radius_m"],
                    source=r["source"],
                    hidden=bool(r["hidden"]),
                )
                for r in place_rows
                if r["source"] in ("zone", "user")
            ]
            existing_auto = [
                places.AutoPlaceState(
                    place_id=r["place_id"],
                    lat=r["lat"],
                    lon=r["lon"],
                    radius_m=r["radius_m"],
                    hidden=bool(r["hidden"]),
                    name=r["name"],
                    category=r["category"],
                    geocode_name=r["geocode_name"],
                    geocoded_ts=r["geocoded_ts"],
                )
                for r in place_rows
                if r["source"] == "auto"
            ]
            rows_by_vin: dict[str, list[places.DriveEndpointInput]] = {}
            for r in drive_rows:
                rows_by_vin.setdefault(r["vin"], []).append(
                    places.DriveEndpointInput(
                        drive_id=r["drive_id"],
                        start_lat=r["start_lat"],
                        start_lon=r["start_lon"],
                        start_ts=r["start_ts"],
                        end_lat=r["end_lat"],
                        end_lon=r["end_lon"],
                        end_ts=r["end_ts"],
                        vin=r["vin"],
                    )
                )
            endpoints = places.pooled_endpoints(rows_by_vin)
            clusters = places.cluster_endpoints(endpoints, fixed_places, existing_auto)

            now = time.time()
            with self._lock, self._transaction():
                kept_auto_ids = {c.place_id for c in clusters if c.place_id is not None}
                stale_auto_ids = {ap.place_id for ap in existing_auto} - kept_auto_ids
                for place_id in stale_auto_ids:
                    self._conn.execute(
                        "DELETE FROM places WHERE place_id = ?", (place_id,)
                    )

                for cluster in clusters:
                    if cluster.place_id is not None:
                        self._conn.execute(
                            "UPDATE places SET lat = ?, lon = ?, updated_ts = ? "
                            "WHERE place_id = ?",
                            (cluster.lat, cluster.lon, now, cluster.place_id),
                        )
                    else:
                        self._conn.execute(
                            "INSERT INTO places (dataset, name, category, lat, lon, "
                            "radius_m, source, hidden, geocode_name, geocoded_ts, "
                            "created_ts, updated_ts) VALUES "
                            "(?, NULL, NULL, ?, ?, ?, 'auto', 0, NULL, NULL, ?, ?)",
                            (
                                dataset,
                                cluster.lat,
                                cluster.lon,
                                places.AUTO_RADIUS_M,
                                now,
                                now,
                            ),
                        )

                all_place_rows = self._conn.execute(
                    "SELECT place_id, lat, lon, radius_m, hidden FROM places "
                    "WHERE dataset = ?",
                    (dataset,),
                ).fetchall()
                all_places = [
                    places.ExistingPlace(
                        place_id=r["place_id"],
                        lat=r["lat"],
                        lon=r["lon"],
                        radius_m=r["radius_m"],
                        source="",
                        hidden=bool(r["hidden"]),
                    )
                    for r in all_place_rows
                ]
                assignment: dict[tuple[str, str, str], int | None] = {}
                for endpoint in endpoints:
                    assignment[(endpoint.vin, endpoint.drive_id, endpoint.kind)] = (
                        places.assign((endpoint.lat, endpoint.lon), all_places)
                    )

                assigned = 0
                for row in drive_rows:
                    start_place_id = assignment.get(
                        (row["vin"], row["drive_id"], "start")
                    )
                    end_place_id = assignment.get((row["vin"], row["drive_id"], "end"))
                    self._conn.execute(
                        "UPDATE drives SET start_place_id = ?, end_place_id = ? "
                        "WHERE vin = ? AND drive_id = ?",
                        (start_place_id, end_place_id, row["vin"], row["drive_id"]),
                    )
                    assigned += 1

                self._reassign_session_places_locked(dataset)

                self._set_meta_locked(
                    self._places_meta_key(dataset), str(PLACES_VERSION)
                )

        return {"places": len(clusters), "assigned": assigned}

    def assign_drive_places(self, vin: str, drive_id: str) -> None:
        """Incrementally assign one drive's start/end place ids against existing places.

        Unlike ``rebuild_places``, this never re-clusters auto places -- it
        only looks up the nearest existing place (in the drive's own dataset)
        for this one drive's (parked-position-adjusted) endpoints. Called after
        every finalized drive; a full ``rebuild_places`` periodically (service
        call, zone sync, post-backfill, first-load seed) is what lets a
        newly-frequent spot become its own auto place.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning(
                "Analytics database is read-only; assign_drive_places skipped"
            )
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT start_lat, start_lon, end_lat, end_lon, sort_ts "
                "FROM drives WHERE vin = ? AND drive_id = ?",
                (vin, drive_id),
            ).fetchone()
            if row is None:
                return
            dataset = (
                places.DATASET_DEMO
                if vin in self._demo_vins_locked()
                else places.DATASET_REAL
            )
            prev = None
            if row["sort_ts"] is not None:
                prev = self._conn.execute(
                    "SELECT end_lat, end_lon FROM drives WHERE vin = ? "
                    "AND sort_ts IS NOT NULL AND sort_ts < ? "
                    "AND end_lat IS NOT NULL AND end_lon IS NOT NULL "
                    "ORDER BY sort_ts DESC, created_ts DESC, id DESC LIMIT 1",
                    (vin, row["sort_ts"]),
                ).fetchone()
            place_rows = self._conn.execute(
                "SELECT place_id, lat, lon, radius_m, hidden FROM places "
                "WHERE dataset = ?",
                (dataset,),
            ).fetchall()
            all_places = [
                places.ExistingPlace(
                    place_id=r["place_id"],
                    lat=r["lat"],
                    lon=r["lon"],
                    radius_m=r["radius_m"],
                    source="",
                    hidden=bool(r["hidden"]),
                )
                for r in place_rows
            ]

            start_place_id = None
            if row["start_lat"] is not None and row["start_lon"] is not None:
                start_lat, start_lon = row["start_lat"], row["start_lon"]
                if prev is not None:
                    distance = haversine_m(
                        prev["end_lat"], prev["end_lon"], start_lat, start_lon
                    )
                    if distance <= places.DAY_GAP_MAX_M:
                        start_lat, start_lon = prev["end_lat"], prev["end_lon"]
                start_place_id = places.assign((start_lat, start_lon), all_places)

            end_place_id = None
            if row["end_lat"] is not None and row["end_lon"] is not None:
                end_place_id = places.assign(
                    (row["end_lat"], row["end_lon"]), all_places
                )

            with self._transaction():
                self._conn.execute(
                    "UPDATE drives SET start_place_id = ?, end_place_id = ? "
                    "WHERE vin = ? AND drive_id = ?",
                    (start_place_id, end_place_id, vin, drive_id),
                )

    def list_places(
        self, dataset: str, vins: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return a dataset's places with visit counts/last-visit from drives.

        ``visits_by_vin`` breaks each place's visits down per vehicle. With
        ``vins`` the counts cover only those vehicles and the list is filtered
        to the places they visit -- except hidden places (they label nothing
        and would otherwise be impossible to restore), named places and zone
        places, which are kept so a new zone or name shows up before any drive.
        """
        self._assert_executor_thread()
        vin_filter = ""
        vin_params: list[str] = []
        if vins is not None:
            vin_list = list(dict.fromkeys(vins))
            vin_filter = f" AND vin IN ({','.join('?' for _ in vin_list)})"
            vin_params = vin_list
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM places WHERE dataset = ? ORDER BY place_id",
                (dataset,),
            ).fetchall()
            if not rows:
                return []
            stat_rows = self._conn.execute(
                f"""
                SELECT vin, place_id, SUM(dep) AS dep, SUM(arr) AS arr,
                       MAX(ts) AS last_ts
                  FROM (
                      SELECT vin, start_place_id AS place_id, 1 AS dep, 0 AS arr,
                             start_ts AS ts
                        FROM drives
                       WHERE start_place_id IS NOT NULL{vin_filter}
                      UNION ALL
                      SELECT vin, end_place_id AS place_id, 0 AS dep, 1 AS arr,
                             end_ts AS ts
                        FROM drives
                       WHERE end_place_id IS NOT NULL{vin_filter}
                  )
                 GROUP BY vin, place_id
                """,
                [*vin_params, *vin_params],
            ).fetchall()
        stats: dict[int, dict[str, Any]] = {}
        for s in stat_rows:
            entry = stats.setdefault(
                s["place_id"],
                {"by_vin": {}, "arrivals": 0, "departures": 0, "last": None},
            )
            dep, arr = s["dep"] or 0, s["arr"] or 0
            # One stop is an arrival and the next departure, so the larger of
            # the two is a car's visit count.
            entry["by_vin"][s["vin"]] = max(dep, arr)
            entry["arrivals"] += arr
            entry["departures"] += dep
            if s["last_ts"] is not None and (
                entry["last"] is None or s["last_ts"] > entry["last"]
            ):
                entry["last"] = s["last_ts"]
        result: list[dict[str, Any]] = []
        for r in rows:
            entry = stats.get(r["place_id"])
            by_vin = entry["by_vin"] if entry else {}
            visits = sum(by_vin.values())
            # Unvisited places are dropped for a vehicle filter, except ones
            # someone deliberately made: hidden (else impossible to restore),
            # named, or an HA zone (e.g. a zone just created, not yet driven to).
            deliberate = r["hidden"] or r["name"] or r["source"] == "zone"
            if vins is not None and visits == 0 and not deliberate:
                continue
            result.append(
                {
                    "id": r["place_id"],
                    "label": places.place_label(
                        r["name"], r["geocode_name"], r["place_id"]
                    ),
                    "name": r["name"],
                    "geocode_name": r["geocode_name"],
                    "category": r["category"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "radius_m": r["radius_m"],
                    "source": r["source"],
                    "zone_entity_id": r["zone_entity_id"],
                    "hidden": bool(r["hidden"]),
                    "visits": visits,
                    "visits_by_vin": dict(by_vin),
                    "arrivals": entry["arrivals"] if entry else 0,
                    "departures": entry["departures"] if entry else 0,
                    "last_visit_ts": entry["last"] if entry else None,
                }
            )
        return result

    _UPDATE_PLACE_FIELDS: Final[frozenset[str]] = frozenset(
        {"name", "category", "radius_m", "hidden", "lat", "lon"}
    )

    def update_place(self, dataset: str, place_id: int, **fields: Any) -> None:
        """Update a place's editable fields.

        Renaming, moving or resizing an ``auto`` place makes it ``user``, so a
        rebuild never re-clusters the edit away. A change to radius/lat/lon/
        hidden changes which points the place covers, so it triggers a full
        ``rebuild_places`` afterward.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; update_place skipped")
            return
        unknown = set(fields) - self._UPDATE_PLACE_FIELDS
        if unknown:
            raise ValueError(f"update_place: unsupported fields {sorted(unknown)}")
        if not fields:
            return
        geometry_changed = bool({"radius_m", "lat", "lon", "hidden"} & set(fields))
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT source FROM places WHERE dataset = ? AND place_id = ?",
                (dataset, place_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"No place {place_id}")
            set_clauses: list[str] = []
            params: list[Any] = []
            for key, value in fields.items():
                if key == "hidden":
                    value = int(bool(value))
                set_clauses.append(f"{key} = ?")
                params.append(value)
            pinned = bool(fields.get("name")) or bool(
                {"radius_m", "lat", "lon"} & set(fields)
            )
            if pinned and row["source"] == "auto":
                set_clauses.append("source = ?")
                params.append("user")
            set_clauses.append("updated_ts = ?")
            params.append(now)
            params.extend([dataset, place_id])
            with self._transaction():
                self._conn.execute(
                    f"UPDATE places SET {', '.join(set_clauses)} "
                    f"WHERE dataset = ? AND place_id = ?",
                    params,
                )
        if geometry_changed:
            self.rebuild_places(dataset)
            self.rebuild_routes(dataset)

    def create_place(
        self,
        dataset: str,
        lat: float,
        lon: float,
        name: str,
        radius_m: float | None = None,
        category: str | None = None,
    ) -> int:
        """Create a user-defined place (e.g. naming a parked spot), then reassign drives."""
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; create_place skipped")
            return -1
        now = time.time()
        radius = radius_m if radius_m is not None else places.DEFAULT_RADIUS_M
        with self._lock, self._transaction():
            cur = self._conn.execute(
                "INSERT INTO places (dataset, name, category, lat, lon, radius_m, "
                "source, hidden, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, 'user', 0, ?, ?)",
                (dataset, name, category, lat, lon, radius, now, now),
            )
            place_id = cur.lastrowid
        self.rebuild_places(dataset)
        self.rebuild_routes(dataset)
        return place_id

    def merge_places(self, dataset: str, into: int, place_ids: list[int]) -> None:
        """Merge ``place_ids`` into ``into``, so their visits count toward it.

        ``into`` grows to cover each merged place (centroid distance plus its
        radius, capped at ``places.ZONE_RADIUS_BOUNDS[1]``) and becomes
        ``user`` if it was ``auto``, so the rebuild that follows keeps the
        merged spots assigned to it instead of re-detecting them. Zone places
        can't be merged away -- the next zone sync would recreate them.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; merge_places skipped")
            return
        merge_ids = [pid for pid in place_ids if pid != into]
        if not merge_ids:
            return
        placeholders = ",".join("?" for _ in merge_ids)
        now = time.time()
        with self._lock:
            target = self._conn.execute(
                "SELECT lat, lon, radius_m, source FROM places "
                "WHERE dataset = ? AND place_id = ?",
                (dataset, into),
            ).fetchone()
            if target is None:
                raise ValueError(f"No place {into}")
            merged = self._conn.execute(
                f"SELECT place_id, lat, lon, radius_m, source FROM places "
                f"WHERE dataset = ? AND place_id IN ({placeholders})",
                [dataset, *merge_ids],
            ).fetchall()
            if any(r["source"] == "zone" for r in merged):
                raise ValueError(
                    "A Home Assistant zone place can't be merged into another place"
                )
            radius = target["radius_m"]
            for r in merged:
                reach = (
                    haversine_m(target["lat"], target["lon"], r["lat"], r["lon"])
                    + r["radius_m"]
                )
                radius = max(radius, reach)
            radius = min(places.ZONE_RADIUS_BOUNDS[1], radius)
            source = "user" if target["source"] == "auto" else target["source"]
            with self._transaction():
                self._conn.execute(
                    "UPDATE places SET radius_m = ?, source = ?, updated_ts = ? "
                    "WHERE dataset = ? AND place_id = ?",
                    (radius, source, now, dataset, into),
                )
                self._conn.execute(
                    f"DELETE FROM places WHERE dataset = ? AND place_id IN ({placeholders})",
                    [dataset, *merge_ids],
                )
        self.rebuild_places(dataset)
        self.rebuild_routes(dataset)

    def places_needing_geocode(
        self, dataset: str, limit: int = PLACES_GEOCODE_DEFAULT_LIMIT
    ) -> list[dict[str, Any]]:
        """Return up to `limit` unnamed (auto, or user-pinned by a move/resize), >= MIN_VISITS places due for a geocode attempt."""
        self._assert_executor_thread()
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT place_id, lat, lon FROM places
                 WHERE dataset = ? AND source != 'zone' AND name IS NULL
                   AND geocode_name IS NULL
                   AND (geocoded_ts IS NULL OR geocoded_ts < ?)
                   AND place_id IN (
                       SELECT place_id FROM (
                           SELECT start_place_id AS place_id, 1 AS dep, 0 AS arr
                             FROM drives
                            WHERE start_place_id IS NOT NULL
                           UNION ALL
                           SELECT end_place_id AS place_id, 0 AS dep, 1 AS arr
                             FROM drives
                            WHERE end_place_id IS NOT NULL
                       ) GROUP BY place_id
                         HAVING MAX(SUM(dep), SUM(arr)) >= ?
                   )
                 ORDER BY place_id
                 LIMIT ?
                """,
                (
                    dataset,
                    now - GEOCODE_RETRY_SECONDS,
                    places.MIN_VISITS,
                    limit,
                ),
            ).fetchall()
        return [
            {"place_id": r["place_id"], "lat": r["lat"], "lon": r["lon"]} for r in rows
        ]

    def save_geocode(
        self, dataset: str, place_id: int, name: str | None, attempted_ts: float
    ) -> None:
        """Record a geocode attempt's result (or failure) for one place."""
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; save_geocode skipped")
            return
        with self._lock, self._transaction():
            self._conn.execute(
                "UPDATE places SET geocode_name = ?, geocoded_ts = ?, updated_ts = ? "
                "WHERE dataset = ? AND place_id = ?",
                (name, attempted_ts, attempted_ts, dataset, place_id),
            )

    def _place_refs_for_ids(
        self, place_ids: Iterable[int | None]
    ) -> dict[int, sqlite3.Row]:
        """Return {place_id: row} for the given ids (self-locking).

        Place ids are globally unique, so no dataset filter is needed: a
        drive's place ids always come from its own dataset.
        """
        ids = {pid for pid in place_ids if pid is not None}
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT place_id, name, category, geocode_name, hidden FROM places "
                f"WHERE place_id IN ({placeholders})",
                list(ids),
            ).fetchall()
        return {r["place_id"]: r for r in rows}

    @staticmethod
    def _place_ref_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        """Shape one place row as ``{id, label, category}``, or None if hidden/absent."""
        if row is None or row["hidden"]:
            return None
        return {
            "id": row["place_id"],
            "label": places.place_label(
                row["name"], row["geocode_name"], row["place_id"]
            ),
            "category": row["category"],
        }

    @staticmethod
    def _place_label_from_rows(
        place_rows: dict[int, sqlite3.Row], place_id: int
    ) -> str:
        """Return a place's label (name, geocode name, else "Place #N")."""
        row = place_rows.get(place_id)
        if row is None:
            return f"Place #{place_id}"
        return places.place_label(row["name"], row["geocode_name"], place_id)

    # -- favorite drives (repeated routes) --------------------------------------

    @staticmethod
    def _routes_meta_key(dataset: str) -> str:
        """Meta key recording the routes definitions a dataset was last rebuilt with."""
        return f"routes_version:{dataset}"

    def has_unbuilt_routes(self, dataset: str) -> bool:
        """Return True until this dataset's routes were built with the current definitions.

        Mirrors ``has_unbuilt_places``: true right after the v9/v10 schema
        migration (or a ``routes.MIN_ROUTE_DRIVES``/``ROUTES_VERSION`` bump)
        until the one-time background ``rebuild_routes`` has run.
        """
        self._assert_executor_thread()
        return self.get_meta(self._routes_meta_key(dataset)) != str(ROUTES_VERSION)

    @staticmethod
    def _route_stats_payload(stats: routes_mod.RouteStats) -> dict[str, Any]:
        """Serialize a ``routes.RouteStats`` to the JSON-friendly dict stored/served.

        The flat fields (and the identical ``overall`` block) are across every
        vehicle that drove the route; ``by_vin`` is each car's own
        count/best/average/slowest. Drive references are ``"<vin>|<drive_id>"``
        keys, and ``per_drive`` carries both the overall rank/vs-avg and the
        rank/vs-avg among the same car's own drives (``vin_*``).
        """
        overall = {
            "count": stats.count,
            "fastest_seconds": stats.fastest_seconds,
            "avg_seconds": stats.avg_seconds,
            "slowest_seconds": stats.slowest_seconds,
            "avg_efficiency_mi_kwh": stats.avg_efficiency_mi_kwh,
        }
        return {
            "count": stats.count,
            "fastest_seconds": stats.fastest_seconds,
            "fastest_drive_id": stats.fastest_drive_id,
            "slowest_seconds": stats.slowest_seconds,
            "slowest_drive_id": stats.slowest_drive_id,
            "avg_seconds": stats.avg_seconds,
            "fastest_moving_seconds": stats.fastest_moving_seconds,
            "fastest_moving_drive_id": stats.fastest_moving_drive_id,
            "slowest_moving_seconds": stats.slowest_moving_seconds,
            "slowest_moving_drive_id": stats.slowest_moving_drive_id,
            "avg_moving_seconds": stats.avg_moving_seconds,
            "avg_efficiency_mi_kwh": stats.avg_efficiency_mi_kwh,
            "avg_temp_f": stats.avg_temp_f,
            "last_ts": stats.last_ts,
            "overall": overall,
            "by_vin": {
                vin: {
                    "count": v.count,
                    "fastest_seconds": v.fastest_seconds,
                    "avg_seconds": v.avg_seconds,
                    "slowest_seconds": v.slowest_seconds,
                    "avg_efficiency_mi_kwh": v.avg_efficiency_mi_kwh,
                }
                for vin, v in stats.by_vin.items()
            },
            "per_drive": {
                key: {
                    "vin": stat.vin,
                    "rank": stat.rank,
                    "vs_avg_pct": stat.vs_avg_pct,
                    "vin_rank": stat.vin_rank,
                    "vin_vs_avg_pct": stat.vin_vs_avg_pct,
                    "outlier": stat.outlier,
                }
                for key, stat in stats.drive_stats.items()
            },
        }

    def rebuild_routes(self, dataset: str) -> dict[str, int]:
        """Deterministically re-group a dataset's routes/variants and reassign its drives.

        Routes belong to no vehicle: every drive in the dataset is grouped by
        place pair and path variant regardless of which car drove it. Mirrors
        ``rebuild_places``'s batching: selects drives/tracks under
        ``self._lock``, computes grouping and stats outside it (pure CPU),
        then writes every change -- route upserts/deletes and every drive's
        ``route_id`` -- in one transaction. Route ids are stable across a
        rebuild: the same (dataset, start_place_id, end_place_id, variant)
        reuses its existing row, so a user's rename survives.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; rebuild_routes skipped")
            return {"routes": 0, "assigned": 0}

        with self._rebuild_lock:
            with self._lock:
                clause, clause_params = self._dataset_drive_clause(dataset, "d.vin")
                plain_clause, plain_params = self._dataset_drive_clause(dataset)
                drive_rows = self._conn.execute(
                    f"""
                    SELECT d.vin, d.drive_id, d.start_place_id, d.end_place_id,
                           d.sort_ts, d.duration_seconds, d.moving_seconds,
                           d.distance_miles, d.energy_kwh,
                           d.integrated_temperature_f, t.track_json
                      FROM drives d
                      LEFT JOIN drive_tracks t
                        ON t.vin = d.vin AND t.drive_id = d.drive_id
                     WHERE {clause} AND d.is_micro_drive = 0
                       AND d.start_place_id IS NOT NULL AND d.end_place_id IS NOT NULL
                    """,
                    clause_params,
                ).fetchall()
                existing_routes = self._conn.execute(
                    "SELECT route_id, start_place_id, end_place_id, variant, name "
                    "FROM routes WHERE dataset = ?",
                    (dataset,),
                ).fetchall()

            inputs: list[routes_mod.RouteDriveInput] = []
            for row in drive_rows:
                cells = None
                if row["track_json"] is not None:
                    try:
                        track = DriveTrack.decode(row["track_json"])
                        cells = routes_mod.coarsen_cells(track_cells(track))
                    except ValueError as err:
                        _LOGGER.debug(
                            "Skipping unreadable route for drive %s (routes): %s",
                            row["drive_id"],
                            err,
                        )
                inputs.append(
                    routes_mod.RouteDriveInput(
                        drive_id=row["drive_id"],
                        start_place_id=row["start_place_id"],
                        end_place_id=row["end_place_id"],
                        sort_ts=row["sort_ts"],
                        duration_seconds=row["duration_seconds"],
                        moving_seconds=row["moving_seconds"],
                        distance_miles=row["distance_miles"],
                        energy_kwh=row["energy_kwh"],
                        temp_f=row["integrated_temperature_f"],
                        cells=cells,
                        vin=row["vin"],
                    )
                )

            groups = routes_mod.build_routes(inputs)
            existing_by_key = {
                (r["start_place_id"], r["end_place_id"], r["variant"]): r
                for r in existing_routes
            }

            now = time.time()
            with self._lock, self._transaction():
                kept_keys: set[tuple[int, int, int]] = set()
                drive_route_map: dict[tuple[str, str], int] = {}
                for group in groups:
                    key = (group.start_place_id, group.end_place_id, group.variant)
                    kept_keys.add(key)
                    stats_json = json.dumps(self._route_stats_payload(group.stats))
                    existing = existing_by_key.get(key)
                    if existing is not None:
                        route_id = existing["route_id"]
                        self._conn.execute(
                            "UPDATE routes SET drive_count = ?, stats_json = ?, "
                            "updated_ts = ? WHERE route_id = ?",
                            (len(group.drive_ids), stats_json, now, route_id),
                        )
                    else:
                        cur = self._conn.execute(
                            "INSERT INTO routes (dataset, start_place_id, "
                            "end_place_id, variant, name, drive_count, stats_json, "
                            "created_ts, updated_ts) "
                            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                            (
                                dataset,
                                group.start_place_id,
                                group.end_place_id,
                                group.variant,
                                len(group.drive_ids),
                                stats_json,
                                now,
                                now,
                            ),
                        )
                        route_id = cur.lastrowid
                    for drive_key in group.drive_ids:
                        vin, _, drive_id = drive_key.partition("|")
                        drive_route_map[(vin, drive_id)] = route_id

                stale_keys = set(existing_by_key) - kept_keys
                for key in stale_keys:
                    self._conn.execute(
                        "DELETE FROM routes WHERE route_id = ?",
                        (existing_by_key[key]["route_id"],),
                    )

                # Clear every candidate drive's route_id first (covers a pair that
                # dropped below threshold, or a drive that moved pairs), then set
                # it for the ones that are currently routed.
                self._conn.execute(
                    f"UPDATE drives SET route_id = NULL WHERE {plain_clause} "
                    f"AND is_micro_drive = 0 AND start_place_id IS NOT NULL "
                    f"AND end_place_id IS NOT NULL",
                    plain_params,
                )
                for (vin, drive_id), route_id in drive_route_map.items():
                    self._conn.execute(
                        "UPDATE drives SET route_id = ? WHERE vin = ? AND drive_id = ?",
                        (route_id, vin, drive_id),
                    )

                self._set_meta_locked(
                    self._routes_meta_key(dataset), str(ROUTES_VERSION)
                )

        return {"routes": len(groups), "assigned": len(drive_route_map)}

    @staticmethod
    def _selected_route_count(
        stats: dict[str, Any], vins: Sequence[str] | None
    ) -> int | None:
        """Return the selected vehicles' drive count on a route (None = no filter)."""
        if vins is None:
            return None
        by_vin = stats.get("by_vin") or {}
        return sum(int((by_vin.get(v) or {}).get("count") or 0) for v in vins)

    def list_routes(
        self, dataset: str, vins: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return a dataset's routes (summary + stored stats).

        Without ``vins``: by total drive_count desc. With ``vins``: only routes
        those vehicles have driven, ordered by *their* drive count on the
        route (then total), so each car's favorites are its most-driven routes.
        Each entry carries ``selected_count`` (the selected vehicles' drives;
        the total when unfiltered).
        """
        self._assert_executor_thread()
        with self._lock:
            route_rows = self._conn.execute(
                "SELECT * FROM routes WHERE dataset = ? "
                "ORDER BY drive_count DESC, route_id ASC",
                (dataset,),
            ).fetchall()
            if not route_rows:
                return []
            place_ids = {r["start_place_id"] for r in route_rows} | {
                r["end_place_id"] for r in route_rows
            }
            place_rows = self._place_refs_for_ids(place_ids)

        variant_counts: dict[tuple[int, int], int] = {}
        for r in route_rows:
            key = (r["start_place_id"], r["end_place_id"])
            variant_counts[key] = variant_counts.get(key, 0) + 1

        result: list[dict[str, Any]] = []
        for r in route_rows:
            stats = json.loads(r["stats_json"] or "{}")
            selected = self._selected_route_count(stats, vins)
            if selected is not None and selected <= 0:
                continue
            start_label = self._place_label_from_rows(place_rows, r["start_place_id"])
            end_label = self._place_label_from_rows(place_rows, r["end_place_id"])
            variant_count = variant_counts[(r["start_place_id"], r["end_place_id"])]
            label = r["name"] or routes_mod.route_label(
                start_label, end_label, r["variant"]
            )
            result.append(
                {
                    "id": r["route_id"],
                    "start_place": self._place_ref_from_row(
                        place_rows.get(r["start_place_id"])
                    ),
                    "end_place": self._place_ref_from_row(
                        place_rows.get(r["end_place_id"])
                    ),
                    "variant": r["variant"],
                    "variant_count": variant_count,
                    "name": r["name"],
                    "label": label,
                    "drive_count": r["drive_count"],
                    "selected_count": (
                        selected if selected is not None else r["drive_count"]
                    ),
                    "stats": stats,
                    "updated_ts": r["updated_ts"],
                }
            )
        if vins is not None:
            result.sort(
                key=lambda e: (-e["selected_count"], -e["drive_count"], e["id"])
            )
        return result

    def route_detail(
        self, dataset: str, route_id: int, vins: Sequence[str] | None = None
    ) -> dict[str, Any] | None:
        """Return one route's stats plus every drive's summary and preview polyline.

        Each drive is tagged with its ``vin`` and route-wide ``key``
        (``"<vin>|<drive_id>"``). Per-drive rank/vs_avg_pct/outlier come from
        the route's stored ``stats_json`` (computed at the last rebuild), never
        recomputed here. With ``vins`` only those vehicles' drives are listed
        (the stats still cover every car). The route's own full GPS track is
        never loaded -- only its stored ``drive_tracks.preview_json``
        (<= TRACK_PREVIEW_MAX_POINTS points).
        """
        self._assert_executor_thread()
        vin_clause = ""
        vin_params: list[str] = []
        if vins is not None:
            vin_list = list(dict.fromkeys(vins))
            vin_clause = f" AND d.vin IN ({','.join('?' for _ in vin_list)})"
            vin_params = vin_list
        with self._lock:
            route_row = self._conn.execute(
                "SELECT * FROM routes WHERE dataset = ? AND route_id = ?",
                (dataset, route_id),
            ).fetchone()
            if route_row is None:
                return None
            drive_rows = self._conn.execute(
                f"""
                SELECT d.vin, d.drive_id, d.start_ts, d.end_ts, d.sort_ts,
                       d.duration_seconds, d.moving_seconds, d.distance_miles,
                       d.efficiency_mi_kwh, d.integrated_temperature_f,
                       t.preview_json
                  FROM drives d
                  LEFT JOIN drive_tracks t
                    ON t.vin = d.vin AND t.drive_id = d.drive_id
                 WHERE d.route_id = ?{vin_clause}
                 ORDER BY d.sort_ts ASC
                """,
                [route_id, *vin_params],
            ).fetchall()
            variant_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM routes WHERE dataset = ? "
                "AND start_place_id = ? AND end_place_id = ?",
                (dataset, route_row["start_place_id"], route_row["end_place_id"]),
            ).fetchone()["n"]
            place_rows = self._place_refs_for_ids(
                [route_row["start_place_id"], route_row["end_place_id"]]
            )

        start_row = place_rows.get(route_row["start_place_id"])
        end_row = place_rows.get(route_row["end_place_id"])
        start_label = self._place_label_from_rows(
            place_rows, route_row["start_place_id"]
        )
        end_label = self._place_label_from_rows(place_rows, route_row["end_place_id"])
        label = route_row["name"] or routes_mod.route_label(
            start_label, end_label, route_row["variant"]
        )
        stats_payload = json.loads(route_row["stats_json"] or "{}")
        per_drive = stats_payload.get("per_drive", {})

        drives_payload: list[dict[str, Any]] = []
        for row in drive_rows:
            preview = None
            if row["preview_json"] is not None:
                try:
                    preview_track = DriveTrack.decode(row["preview_json"])
                    preview = {
                        "lat": [p.lat for p in preview_track.points],
                        "lon": [p.lon for p in preview_track.points],
                    }
                except ValueError as err:
                    _LOGGER.warning(
                        "Skipping unreadable preview for drive %s: %s",
                        row["drive_id"],
                        err,
                    )
            key = routes_mod.drive_key(row["vin"], row["drive_id"])
            drive_stat = per_drive.get(key, {})
            drives_payload.append(
                {
                    "vin": row["vin"],
                    "drive_id": row["drive_id"],
                    "key": key,
                    "start_ts": row["start_ts"],
                    "end_ts": row["end_ts"],
                    "sort_ts": row["sort_ts"],
                    "duration_seconds": row["duration_seconds"],
                    "moving_seconds": row["moving_seconds"],
                    "distance_miles": (
                        round(row["distance_miles"], 2)
                        if row["distance_miles"] is not None
                        else None
                    ),
                    "efficiency_mi_kwh": row["efficiency_mi_kwh"],
                    "temp_f": row["integrated_temperature_f"],
                    "rank": drive_stat.get("rank"),
                    "vs_avg_pct": drive_stat.get("vs_avg_pct"),
                    "vin_rank": drive_stat.get("vin_rank"),
                    "vin_vs_avg_pct": drive_stat.get("vin_vs_avg_pct"),
                    "outlier": drive_stat.get("outlier", False),
                    "preview": preview,
                }
            )

        return {
            "id": route_row["route_id"],
            "start_place": self._place_ref_from_row(start_row),
            "end_place": self._place_ref_from_row(end_row),
            "variant": route_row["variant"],
            "variant_count": variant_count,
            "name": route_row["name"],
            "label": label,
            "stats": stats_payload,
            "drives": drives_payload,
        }

    def rename_route(self, dataset: str, route_id: int, name: str | None) -> None:
        """Set (or clear, with None/empty) a route's display name override."""
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; rename_route skipped")
            return
        now = time.time()
        with self._lock, self._transaction():
            cur = self._conn.execute(
                "UPDATE routes SET name = ?, updated_ts = ? "
                "WHERE dataset = ? AND route_id = ?",
                (name or None, now, dataset, route_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"No route {route_id}")

    def fit_energy_model(
        self,
        vin: str,
        window_days: int = ENERGY_MODEL_WINDOW_DAYS,
        min_drives: int = ENERGY_MODEL_MIN_DRIVES,
    ) -> dict[str, Any]:
        """Refit this VIN's anchored energy-model coefficients from recent routed drives.

        Gathers routed drives (``energy_kwh`` > ``MIN_DRIVE_ENERGY_KWH``,
        ``distance_miles`` > ``MIN_DRIVE_DISTANCE_MI``) within the trailing
        ``window_days``, decodes their tracks and computes physics features
        outside ``self._lock`` (pure CPU work), then fits and stores the
        result under ``meta`` key ``energy_model:<vin>``. If fewer than
        ``min_drives`` qualify, any existing fit is left untouched and the
        result says so instead of overwriting it with a poorly-conditioned one.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; fit_energy_model skipped")
            return {"fitted": False, "reason": "read_only"}

        cutoff_ts = time.time() - window_days * SECONDS_PER_DAY
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT d.energy_kwh AS energy_kwh, t.track_json AS track_json
                  FROM drives d
                  JOIN drive_tracks t ON t.vin = d.vin AND t.drive_id = d.drive_id
                 WHERE d.vin = ? AND d.energy_kwh > ? AND d.distance_miles > ?
                   AND d.sort_ts IS NOT NULL AND d.sort_ts >= ?
                """,
                (vin, MIN_DRIVE_ENERGY_KWH, MIN_DRIVE_DISTANCE_MI, cutoff_ts),
            ).fetchall()

        drives: list[tuple[list[Any], float]] = []
        for row in rows:
            try:
                track = DriveTrack.decode(row["track_json"])
            except ValueError as err:
                _LOGGER.warning(
                    "Skipping unreadable route during energy-model fit for VIN %s: %s",
                    vin,
                    err,
                )
                continue
            features = interval_features(track)
            if features:
                drives.append((features, row["energy_kwh"]))

        if len(drives) < min_drives:
            existing = self.get_energy_model(vin)
            return {
                "fitted": False,
                "reason": "too_few_drives",
                "n_drives": len(drives),
                "min_drives": min_drives,
                "has_existing": existing is not None,
            }

        params, rmse_kwh, n = fit_params(drives)
        payload = {
            **params.to_dict(),
            "n_drives": n,
            "rmse_kwh": rmse_kwh,
            "fitted_at": time.time(),
        }
        with self._lock, self._transaction():
            self._set_meta_locked(self._energy_model_meta_key(vin), json.dumps(payload))
        return {
            "fitted": True,
            "n_drives": n,
            "rmse_kwh": rmse_kwh,
            "params": params.to_dict(),
        }

    def get_energy_model(self, vin: str) -> EnergyModelParams | None:
        """Return this VIN's stored fitted energy-model params, or None if never fitted."""
        self._assert_executor_thread()
        raw = self.get_meta(self._energy_model_meta_key(vin))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return EnergyModelParams.from_dict(data)

    @staticmethod
    def _energy_model_meta_key(vin: str) -> str:
        """Meta key holding a VIN's fitted energy-model params (JSON)."""
        return f"energy_model:{vin}"

    def storage_stats(self, vin: str) -> dict[str, Any]:
        """Return drive/track row counts and byte sizes for diagnostics."""
        self._assert_executor_thread()
        with self._lock:
            drive_count = self._conn.execute(
                "SELECT COUNT(*) AS c FROM drives WHERE vin = ?", (vin,)
            ).fetchone()["c"]
            track_row = self._conn.execute(
                """
                SELECT COUNT(*) AS c,
                       SUM(CASE WHEN detail = 'full' THEN 1 ELSE 0 END) AS full_c,
                       SUM(CASE WHEN detail = 'thinned' THEN 1 ELSE 0 END) AS thinned_c,
                       COALESCE(SUM(length(track_json) + length(preview_json)), 0)
                           AS track_bytes
                  FROM drive_tracks WHERE vin = ?
                """,
                (vin,),
            ).fetchone()

        db_bytes = 0
        try:
            db_bytes = os.path.getsize(self.db_path)
            wal_path = f"{self.db_path}-wal"
            if os.path.exists(wal_path):
                db_bytes += os.path.getsize(wal_path)
        except OSError:
            db_bytes = 0

        return {
            "drive_count": drive_count,
            "track_count": track_row["c"] or 0,
            "full_count": track_row["full_c"] or 0,
            "thinned_count": track_row["thinned_c"] or 0,
            "track_bytes": track_row["track_bytes"] or 0,
            "db_bytes": db_bytes,
        }

    def series_window(
        self, vin: str, days: int, now_ts: float
    ) -> tuple[list[DriveRecord], list[VampireDrainRecord]]:
        """Return non-micro drives and vampire events over an arbitrary window, ascending."""
        self._assert_executor_thread()
        cutoff = now_ts - days * SECONDS_PER_DAY
        with self._lock:
            drive_rows = self._conn.execute(
                """
                SELECT * FROM drives
                 WHERE vin = ? AND is_micro_drive = 0
                   AND sort_ts IS NOT NULL AND sort_ts >= ? AND sort_ts <= ?
                 ORDER BY sort_ts DESC, created_ts DESC, id DESC LIMIT ?
                """,
                (vin, cutoff, now_ts, SERIES_WINDOW_ROW_CAP),
            ).fetchall()
            drives = [
                self._row_to_drive(r, hydrate=idx < DRIVE_HYDRATE_LIMIT)
                for idx, r in enumerate(drive_rows)
            ]
            drives.reverse()

            vampire_rows = self._conn.execute(
                """
                SELECT * FROM vampire_events
                 WHERE vin = ? AND sort_ts IS NOT NULL AND sort_ts >= ? AND sort_ts <= ?
                 ORDER BY sort_ts DESC, id DESC LIMIT ?
                """,
                (vin, cutoff, now_ts, SERIES_WINDOW_ROW_CAP),
            ).fetchall()
            vampire_events = [self._row_to_vampire(r) for r in reversed(vampire_rows)]

        return drives, vampire_events

    def drives_since(
        self, vin: str, from_ts: float, now_ts: float
    ) -> list[DriveRecord]:
        """Return every drive (any distance, unhydrated) with sort_ts in [from_ts, now_ts].

        Used by the long-term-statistics rewrite after a delete. Unlike
        ``series_window()``/``build_cache()`` this is uncapped (a rewrite
        range is expected to be small) and includes micro-drives, so bucketing
        exactly matches what ``async_update_statistics`` would have written.
        """
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM drives
                 WHERE vin = ? AND sort_ts IS NOT NULL
                   AND sort_ts >= ? AND sort_ts <= ?
                 ORDER BY sort_ts ASC, created_ts ASC, id ASC
                """,
                (vin, from_ts, now_ts),
            ).fetchall()
        return [self._row_to_drive(r, hydrate=False) for r in rows]

    # -- vehicle picture --------------------------------------------------------

    def get_vehicle_picture(self, vin: str) -> VehiclePicture | None:
        """Return the saved picture (or failed-fetch record) for a VIN, if any."""
        self._assert_executor_thread()
        with self._lock:
            row = self._conn.execute(
                "SELECT status, content_type, image, source_url, options_json, "
                "fetched_ts FROM vehicle_pictures WHERE vin = ?",
                (vin,),
            ).fetchone()
        if row is None:
            return None
        return VehiclePicture(
            status=row["status"],
            content_type=row["content_type"],
            image=bytes(row["image"]) if row["image"] is not None else None,
            source_url=row["source_url"],
            options=json.loads(row["options_json"] or "[]"),
            fetched_ts=row["fetched_ts"],
        )

    def save_vehicle_picture(self, vin: str, picture: VehiclePicture) -> None:
        """Insert or replace a VIN's picture record."""
        self._assert_executor_thread()
        with self._lock:
            if self.read_only:
                _LOGGER.warning(
                    "Analytics database is read-only; save_vehicle_picture skipped"
                )
                return
            with self._transaction():
                self._conn.execute(
                    "INSERT INTO vehicle_pictures(vin, status, content_type, image, "
                    "source_url, options_json, fetched_ts) VALUES(?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(vin) DO UPDATE SET status=excluded.status, "
                    "content_type=excluded.content_type, image=excluded.image, "
                    "source_url=excluded.source_url, "
                    "options_json=excluded.options_json, "
                    "fetched_ts=excluded.fetched_ts",
                    (
                        vin,
                        picture.status,
                        picture.content_type,
                        picture.image,
                        picture.source_url,
                        json.dumps(picture.options),
                        picture.fetched_ts,
                    ),
                )

    # -- road heat map ------------------------------------------------------

    @staticmethod
    def _heat_cache_key(vin: str, period: str, key: str | None) -> tuple[str, str]:
        """Validate period/key and return the cache key for this VIN/period/key.

        Raises ValueError for an unrecognized period, or a malformed year
        (``YYYY``) or month (``YYYY-MM``) key. "all" ignores ``key`` entirely.
        """
        if period == "all":
            return (vin, "all")
        if period == "year":
            if key is None or len(key) != 4 or not key.isdigit():
                raise ValueError(f"road_heat: invalid year key {key!r}")
            return (vin, key)
        if period == "month":
            if (
                key is None
                or len(key) != 7
                or key[4] != "-"
                or not key[:4].isdigit()
                or not key[5:7].isdigit()
                or not 1 <= int(key[5:7]) <= 12
            ):
                raise ValueError(f"road_heat: invalid month key {key!r}")
            return (vin, key)
        raise ValueError(f"road_heat: invalid period {period!r}")

    @staticmethod
    def _cache_owner_has(owner: str | frozenset[str], vin: str) -> bool:
        """Whether a heat-cache key's owner (a VIN or a combined set) includes ``vin``."""
        return owner == vin if isinstance(owner, str) else vin in owner

    def _invalidate_heat_cache(self, vin: str, months: Iterable[str]) -> None:
        """Drop cached grids for the given months, their years, and "all".

        Covers this VIN's own entries and every combined (multi-VIN) entry that
        includes it.
        """
        touched = [m for m in months if m]
        if not touched:
            return
        periods = {"all", *touched, *(m[:4] for m in touched)}
        with self._heat_cache_lock:
            self._heat_generation[vin] = self._heat_generation.get(vin, 0) + 1
            for cache_key in [
                k
                for k in self._heat_cache
                if k[1] in periods and self._cache_owner_has(k[0], vin)
            ]:
                del self._heat_cache[cache_key]

    def _invalidate_heat_cache_all(self, vin: str) -> None:
        """Drop every cached grid (any period) that includes this VIN."""
        with self._heat_cache_lock:
            self._heat_generation[vin] = self._heat_generation.get(vin, 0) + 1
            for cache_key in [
                k for k in self._heat_cache if self._cache_owner_has(k[0], vin)
            ]:
                del self._heat_cache[cache_key]

    def _get_heat_grid(
        self, vin: str | Sequence[str], period: str, key: str | None
    ) -> tuple[HeatGrid, tuple[float, float, float, float] | None, int, int]:
        """Return (grid, bbox, scale_max, drive_count) for a period/key.

        ``vin`` is one VIN, or a sequence of VINs whose grids are merged
        cell-wise (a one-element sequence behaves like the bare VIN).

        Cached (LRU, ``HEAT_CACHE_LIMIT`` entries) since bbox/scale_max are
        O(n) over the grid's cells. A combined entry is keyed by
        ``(frozenset(vins), period_key)`` and dropped when any member VIN's
        heat changes.
        """
        vins = [vin] if isinstance(vin, str) else sorted(set(vin))
        owner: str | frozenset[str] = vins[0] if len(vins) == 1 else frozenset(vins)
        period_key = self._heat_cache_key(vins[0], period, key)[1]
        cache_key = (owner, period_key)
        with self._heat_cache_lock:
            cached = self._heat_cache.get(cache_key)
            if cached is not None:
                self._heat_cache.move_to_end(cache_key)
                return cached
            generation = sum(self._heat_generation.get(v, 0) for v in vins)

        marks = ",".join("?" for _ in vins)
        with self._lock:
            if period == "month":
                rows = self._conn.execute(
                    "SELECT data, drive_count, vin FROM road_heat "
                    f"WHERE vin IN ({marks}) AND month = ?",
                    (*vins, period_key),
                ).fetchall()
            elif period == "year":
                rows = self._conn.execute(
                    "SELECT data, drive_count, vin FROM road_heat "
                    f"WHERE vin IN ({marks}) AND month LIKE ?",
                    (*vins, f"{period_key}-%"),
                ).fetchall()
            else:  # "all"
                rows = self._conn.execute(
                    "SELECT data, drive_count, vin FROM road_heat "
                    f"WHERE vin IN ({marks}) AND month != ''",
                    tuple(vins),
                ).fetchall()

        heats: list[RoadHeat] = []
        drive_total = 0
        for row in rows:
            try:
                heats.append(RoadHeat.decode(row["data"]))
            except ValueError as err:
                _LOGGER.warning(
                    "road_heat: corrupt grid for VIN %s, skipping: %s", row["vin"], err
                )
                continue
            drive_total += row["drive_count"] or 0

        # Drawn cells are the ones a drive passed through, each counting the
        # passes within CORRIDOR_RADIUS of it (so a road's two directions and GPS
        # drift don't split its count); tiles and the color scale use this grid.
        grid = RoadHeat.merge(heats).display()
        result = (grid, grid.bbox(), grid.scale_max(), drive_total)

        with self._heat_cache_lock:
            # A heat update committed while this grid was being read: return it,
            # but don't cache what may already be stale.
            if sum(self._heat_generation.get(v, 0) for v in vins) == generation:
                self._heat_cache[cache_key] = result
                self._heat_cache.move_to_end(cache_key)
                while len(self._heat_cache) > HEAT_CACHE_LIMIT:
                    self._heat_cache.popitem(last=False)

        return result

    def heat_info(
        self, vin: str | Sequence[str], period: str, key: str | None = None
    ) -> dict[str, Any]:
        """Return summary info for a road-heat period: bbox, scale_max, cells, drives.

        ``vin`` may be a sequence of VINs for a combined (merged) grid.
        """
        self._assert_executor_thread()
        grid, bbox, scale_max, drive_total = self._get_heat_grid(vin, period, key)
        return {
            "period": period,
            "key": "all" if period == "all" else key,
            "bbox": list(bbox) if bbox else None,
            "scale_max": scale_max,
            "cells": len(grid),
            "drives": drive_total,
        }

    def heat_tile(
        self,
        vin: str | Sequence[str],
        period: str,
        key: str | None,
        z: int,
        x: int,
        y: int,
        margin: int = 0,
    ) -> dict[str, Any]:
        """Return one XYZ tile's coarsened cells for a road-heat period, plus scale_max."""
        self._assert_executor_thread()
        grid, _bbox, scale_max, _drive_total = self._get_heat_grid(vin, period, key)
        tile = grid.tile(z, x, y, margin=margin)
        tile["scale_max"] = scale_max
        return tile

    def _update_heat_locked(self, vin: str, tz: tzinfo, batch_size: int) -> int:
        """Count uncounted stored tracks into road_heat/road_heat_drives.

        Caller must hold ``self._heat_run_lock``. Runs until no uncounted
        rows remain, decoding tracks and computing cells outside ``self._lock``
        (CPU-heavy), then committing one batch per transaction.
        """
        total_counted = 0
        while True:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT t.drive_id AS drive_id, t.sort_ts AS sort_ts,
                           t.track_json AS track_json
                      FROM drive_tracks t
                      LEFT JOIN road_heat_drives h
                        ON h.vin = t.vin AND h.drive_id = t.drive_id
                     WHERE t.vin = ? AND h.drive_id IS NULL
                     ORDER BY t.sort_ts, t.id
                     LIMIT ?
                    """,
                    (vin, batch_size),
                ).fetchall()
            if not rows:
                break

            by_month: dict[str, list[tuple[dict[int, int], dict[int, int]]]] = {}
            drive_months: list[tuple[str, str]] = []
            for row in rows:
                drive_id = row["drive_id"]
                month = ""
                try:
                    track = DriveTrack.decode(row["track_json"])
                except ValueError as err:
                    _LOGGER.debug(
                        "road_heat: undecodable track for drive %s (VIN %s): %s",
                        drive_id,
                        vin,
                        err,
                    )
                    track = None
                if track is not None:
                    # Merge in any stored gap fills so a filled dropout
                    # contributes cells too, instead of a straight-line gap.
                    # Only matters here (drives not yet counted); a drive
                    # already counted that later gets a new fill is instead
                    # caught by DriveStore scheduling a rebuild_heat (see
                    # drive_storage.async_snap_gaps).
                    track = self._track_with_fills(vin, drive_id, track)
                    ts = row["sort_ts"]
                    if ts is None and track.points:
                        ts = track.points[0].t
                    if ts is not None:
                        local_dt = datetime.fromtimestamp(ts, tz)
                        month = f"{local_dt.year:04d}-{local_dt.month:02d}"
                        by_month.setdefault(month, []).append(track_passes(track))
                    else:
                        _LOGGER.debug(
                            "road_heat: no timestamp for drive %s (VIN %s); "
                            "recording without heat",
                            drive_id,
                            vin,
                        )
                drive_months.append((drive_id, month))

            now_ts = time.time()
            with self._lock, self._transaction():
                for month, drive_passes in by_month.items():
                    existing = self._conn.execute(
                        "SELECT data, drive_count FROM road_heat "
                        "WHERE vin = ? AND month = ?",
                        (vin, month),
                    ).fetchone()
                    if existing is None:
                        heat = RoadHeat.empty()
                        prior_drive_count = 0
                    else:
                        try:
                            heat = RoadHeat.decode(existing["data"])
                        except ValueError as err:
                            _LOGGER.warning(
                                "road_heat: corrupt grid for VIN %s month %s, "
                                "resetting: %s",
                                vin,
                                month,
                                err,
                            )
                            heat = RoadHeat.empty()
                        prior_drive_count = existing["drive_count"] or 0

                    new_heat = heat.add_drives(drive_passes)
                    self._conn.execute(
                        "INSERT INTO road_heat (vin, month, level, version, "
                        "cell_count, drive_count, data, updated_ts) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(vin, month) DO UPDATE SET "
                        "level=excluded.level, version=excluded.version, "
                        "cell_count=excluded.cell_count, "
                        "drive_count=excluded.drive_count, data=excluded.data, "
                        "updated_ts=excluded.updated_ts",
                        (
                            vin,
                            month,
                            BASE_LEVEL,
                            HEAT_FORMAT_VERSION,
                            new_heat.drawn_cells,
                            prior_drive_count + len(drive_passes),
                            new_heat.encode(),
                            now_ts,
                        ),
                    )

                for drive_id, month in drive_months:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO road_heat_drives "
                        "(vin, drive_id, month) VALUES (?, ?, ?)",
                        (vin, drive_id, month),
                    )

            self._invalidate_heat_cache(vin, by_month.keys())
            total_counted += len(rows)

        return total_counted

    def update_heat(
        self, vin: str, tz: tzinfo, batch_size: int = HEAT_UPDATE_BATCH_SIZE
    ) -> int:
        """Count every stored route not yet counted into the road-heat map.

        Idempotent: a route already recorded in ``road_heat_drives`` is
        skipped. Returns the number of drives newly counted. Safe to call
        after every finalize/backfill and once after setup.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; update_heat skipped")
            return 0
        with self._heat_run_lock:
            if self.get_meta(self._heat_format_meta_key(vin)) != str(
                HEAT_FORMAT_VERSION
            ):
                # Heat counted by an older version (e.g. once per drive, before
                # corridors): recount every month that still has routes, once.
                result = self._rebuild_heat_locked(vin, tz)
                _LOGGER.info(
                    "Upgraded the road heat map for VIN %s to format %s: %s",
                    vin,
                    HEAT_FORMAT_VERSION,
                    result,
                )
                return result["drives_counted"]
            return self._update_heat_locked(vin, tz, batch_size)

    @staticmethod
    def _heat_format_meta_key(vin: str) -> str:
        """Meta key recording the heat format a VIN's rows were counted with."""
        return f"road_heat_format:{vin}"

    def rebuild_heat(self, vin: str, tz: tzinfo) -> dict[str, Any]:
        """Recount road heat from scratch for every month with a stored route.

        For recovery after a time-zone or rasterizer change. Rebuilds the
        local months (in ``tz``) of every stored track for the VIN, plus any
        month recorded in ``road_heat_drives`` for a drive that still has a
        stored track. A month with no stored routes left is never touched,
        so its heat survives even if every drive in it was later pruned.
        """
        self._assert_executor_thread()
        if self.read_only:
            _LOGGER.warning("Analytics database is read-only; rebuild_heat skipped")
            return {"months_rebuilt": 0, "drives_counted": 0}

        with self._heat_run_lock:
            return self._rebuild_heat_locked(vin, tz)

    def _rebuild_heat_locked(self, vin: str, tz: tzinfo) -> dict[str, Any]:
        """Rebuild heat for months with stored routes; caller holds the run lock."""
        with self._lock:
            track_rows = self._conn.execute(
                "SELECT sort_ts, CASE WHEN sort_ts IS NULL THEN track_json END "
                "AS track_json FROM drive_tracks WHERE vin = ?",
                (vin,),
            ).fetchall()
            recorded_rows = self._conn.execute(
                "SELECT DISTINCT h.month AS month FROM road_heat_drives h "
                "JOIN drive_tracks t ON t.vin = h.vin AND t.drive_id = h.drive_id "
                "WHERE h.vin = ?",
                (vin,),
            ).fetchall()

        months: set[str] = {r["month"] for r in recorded_rows if r["month"]}
        for row in track_rows:
            ts = row["sort_ts"]
            if ts is None:
                # Only a track without a sort_ts needs decoding for its month.
                try:
                    track = DriveTrack.decode(row["track_json"])
                except ValueError:
                    continue
                ts = track.points[0].t if track.points else None
            if ts is not None:
                local_dt = datetime.fromtimestamp(ts, tz)
                months.add(f"{local_dt.year:04d}-{local_dt.month:02d}")

        if not months:
            self._record_heat_format(vin)
            return {"months_rebuilt": 0, "drives_counted": 0}

        result = self._rebuild_heat_months_locked(vin, tz, months)
        self._record_heat_format(vin)
        self._invalidate_heat_cache_all(vin)
        return result

    def _rebuild_heat_months_locked(
        self, vin: str, tz: tzinfo, months: set[str]
    ) -> dict[str, Any]:
        """Recount road heat for a specific set of months only.

        Caller must hold ``self._heat_run_lock``. Drops a month's
        ``road_heat`` row entirely when no route remains in it afterward --
        unlike ``rebuild_heat()``/``_rebuild_heat_locked()``'s full-VIN sweep,
        which never touches a month with no stored routes. Used by
        ``delete_drives()``, the one place a month's heat should be dropped
        rather than kept.
        """
        if not months:
            return {"months_rebuilt": 0, "drives_counted": 0}
        placeholders = ",".join("?" for _ in months)
        with self._lock, self._transaction():
            self._conn.execute(
                f"DELETE FROM road_heat WHERE vin = ? AND month IN ({placeholders})",
                [vin, *months],
            )
            self._conn.execute(
                "DELETE FROM road_heat_drives WHERE vin = ? "
                f"AND (month IN ({placeholders}) OR month = '')",
                [vin, *months],
            )

        drives_counted = self._update_heat_locked(vin, tz, HEAT_UPDATE_BATCH_SIZE)
        self._invalidate_heat_cache(vin, months)
        return {"months_rebuilt": len(months), "drives_counted": drives_counted}

    def _record_heat_format(self, vin: str) -> None:
        """Note that this VIN's heat rows use the current HEAT_FORMAT_VERSION."""
        with self._lock, self._transaction():
            self._set_meta_locked(
                self._heat_format_meta_key(vin), str(HEAT_FORMAT_VERSION)
            )
