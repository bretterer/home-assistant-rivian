"""SQLite-backed analytics store for Rivian drive, vampire-drain and charging history.

One :class:`AnalyticsDatabase` instance owns a single ``sqlite3`` connection shared
by every vehicle (VIN) known to this Home Assistant instance. All public methods
perform blocking I/O and therefore must be invoked via
``hass.async_add_executor_job`` -- never from the event loop thread.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
import os
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Final

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
from .drive_track import DriveTrack, haversine_m

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION: Final[int] = 14
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
# Bump when a drive_stats definition changes: every VIN's stored drives are
# then recomputed once, in the background, after the next start.
DRIVE_STATS_VERSION: Final[int] = 1
# The ``meta`` row listing the synthetic demo vehicles, read by the v10/v11
# migrations of databases written by later versions.
DEMO_VEHICLES_META_KEY: Final[str] = "demo_vehicles"

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
  start_range_mi, end_range_mi, drive_modes_json, trailer, driver
) VALUES (
  :vin, :drive_id, :start_time, :end_time, :start_ts, :end_ts, :created_ts,
  :distance_miles, :duration_seconds, :start_soc, :end_soc, :battery_capacity_kwh,
  :energy_kwh, :efficiency_mi_kwh, :mpge, :start_altitude_ft, :end_altitude_ft,
  :elevation_change_ft, :avg_speed_mph, :max_speed_mph, :integrated_temperature_f,
  :is_micro_drive, :start_odometer_mi, :end_odometer_mi, :start_lat, :start_lon,
  :end_lat, :end_lon, :speed_bins_json, :weather_json, :chunks_json,
  :moving_seconds, :stopped_seconds, :stop_count, :climb_ft, :descent_ft,
  :track_max_speed_mph, :pct_distance_over_70mph,
  :start_range_mi, :end_range_mi, :drive_modes_json, :trailer, :driver
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
  driver=excluded.driver
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
  is_dcfc, sample_count, samples_json, lat, lon, kind, source,
  vendor, network, station_name, station_version, charger_max_kw, is_home,
  rivian_txn_id, outside_temp_f, battery_temp_f
) VALUES (
  :vin, :session_id, :start_time, :end_time, :start_ts, :end_ts, :created_ts,
  :start_soc, :end_soc, :energy_added_kwh, :max_power_kw, :avg_power_kw,
  :is_dcfc, :sample_count, :samples_json, :lat, :lon, :kind, :source,
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
  lat=excluded.lat, lon=excluded.lon,
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
        return "demo" if vin in demo_vins else "real"

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
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


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
            with self._transaction():
                for session in sessions:
                    self._conn.execute(
                        _UPSERT_DCFC_SQL,
                        self._dcfc_row_params(vin, session, created_ts),
                    )

    def charging_session_intervals(self, vin: str) -> list[tuple[float, float]]:
        """Return every stored session's ``(start_ts, end_ts)``, for backfill dedupe."""
        self._assert_executor_thread()
        with self._lock:
            rows = self._conn.execute(
                "SELECT start_ts, end_ts FROM dcfc_sessions WHERE vin = ? "
                "AND start_ts IS NOT NULL",
                (vin,),
            ).fetchall()
        return [(r["start_ts"], r["end_ts"] or r["start_ts"]) for r in rows]

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
        track_payload = (
            DriveTrack.decode(row["track_json"]).to_payload()
            if row["track_json"] is not None
            else None
        )
        return {"drive": summary, "track": track_payload}

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

        Used by the long-term-statistics rewrite after a delete or a backfill.
        Unlike ``series_window()``/``build_cache()`` this is uncapped (at most
        a backfill's year of unhydrated rows) and includes micro-drives, so
        bucketing exactly matches what ``async_update_statistics`` would have
        written.
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
