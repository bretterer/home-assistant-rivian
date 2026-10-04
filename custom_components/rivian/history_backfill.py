"""Rivian Historical Recorder Backfill Engine."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import sqlite3
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers import entity_registry as er

from .charging import coarse_soc_samples, dedupe_soc_points, estimate_charge_curve
from .const import DOMAIN, DRIVE_MODE_MAP, RIVIAN_ANALYTICS_UPDATED_EVENT
from .drive_models import (
    AC_SESSION_MAX_SOC_POINTS,
    AC_SESSION_MIN_DURATION_S,
    AC_SESSION_MIN_SOC_GAIN_PCT,
    DCFC_MIN_POWER_KW,
    MICRO_DRIVE_THRESHOLD_MILES,
    MPGE_FACTOR,
    SESSION_KIND_AC,
    SESSION_KIND_DC,
    STANDARD_SPEED_BINS,
    ChargingSessionRecord,
    DriveChunk,
    DriveRecord,
    SpeedBinData,
    VampireDrainRecord,
    trailer_attached_any,
)
from .drive_storage import DriveStore
from .drive_track import DriveTrack, TrackPoint
from .statistics import async_update_statistics
from .weather import OpenMeteoWeatherClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DEFAULT_BATTERY_CAPACITY_KWH: Final[float] = 135.0
PARK_DEBOUNCE_SECONDS: Final[float] = 60.0
METERS_PER_MILE: Final[float] = 1609.344
METERS_TO_FEET: Final[float] = 3.28084
FEET_TO_METERS: Final[float] = 1.0 / METERS_TO_FEET
KM_TO_MILES: Final[float] = 0.621371
TRACK_WINDOW_PAD_SECONDS: Final[float] = 30.0
MIN_TRACK_POINTS: Final[int] = 2

# Recorder entity_id -> (entity registry domain, unique_id suffix) for the
# fields a GPS track needs, matching the unique_id scheme in entity.py
# (f"{vin}-{description.key}") and device_tracker.py's LOCATION_DESCRIPTION.
_TRACK_ENTITY_UNIQUE_ID_KEYS: Final[dict[str, tuple[str, str]]] = {
    "device_tracker": ("device_tracker", "location"),
    "speed": ("sensor", "speed"),
    "altitude": ("sensor", "altitude"),
    "battery_level": ("sensor", "battery_level"),
    "odometer": ("sensor", "vehicle_mileage"),
}
# Same idea, for the live vehicle-context fields (range/drive mode/trailer/
# driver) a backfilled drive can also be enriched with, when the entity is
# resolvable via the entity registry.
_CONTEXT_ENTITY_UNIQUE_ID_KEYS: Final[dict[str, tuple[str, str]]] = {
    "distance_to_empty": ("sensor", "distance_to_empty"),
    "drive_mode": ("sensor", "drive_mode"),
    "trailer_status": ("sensor", "trailer_status"),
    "driver": ("sensor", "active_driver"),
}
DRIVING_GEARS: Final[frozenset[str]] = frozenset({"drive", "reverse", "d", "r"})
PARK_GEAR: Final[frozenset[str]] = frozenset({"park", "p"})
NON_DRIVING_GEARS: Final[frozenset[str]] = frozenset(
    {"park", "p", "standby", "neutral", "n"}
)


def open_sqlite_readonly(db_path: str) -> sqlite3.Connection:
    """Open SQLite database connection in strict read-only mode."""
    abs_path = os.path.abspath(db_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"Database file does not exist: {abs_path}")

    # Enforce strict read-only mode via URI
    db_uri = f"file:{abs_path}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as err:
        _LOGGER.error(
            "Failed to open SQLite database in read-only mode (%s): %s", db_uri, err
        )
        raise


def _get_speed_bin_key(speed_mph: float) -> str:
    """Return the speed bin identifier for a given speed in mph."""
    if speed_mph < 0.0:
        return "0-9"
    if speed_mph >= 80.0:
        return "80+"
    bin_lower = int(speed_mph // 10) * 10
    return f"{bin_lower}-{bin_lower + 9}"


def _find_tracker_id(
    entity_map: dict[str, int], target_tokens: list[str]
) -> int | None:
    """Pick the best device_tracker candidate, preferring '_location' entity_ids.

    A recorder DB can hold several device_tracker entities (e.g. a phone),
    so a plain substring match on "device_tracker"/"location"/"tracker" can
    pick the wrong one. The vehicle's own tracker's entity_id always ends in
    "_location" (see device_tracker.py's LOCATION_DESCRIPTION), so that
    suffix outranks everything else; VIN/vehicle-id token matches break ties.
    """
    candidates = [eid for eid in entity_map if eid.startswith("device_tracker.")]
    if not candidates:
        candidates = [
            eid for eid in entity_map if "location" in eid or "tracker" in eid
        ]
    if not candidates:
        return None

    def _score(eid: str) -> tuple[int, int]:
        location_bonus = 1 if eid.endswith("_location") else 0
        token_score = sum(10 for token in target_tokens if token in eid)
        return (location_bonus, token_score)

    candidates.sort(key=_score, reverse=True)
    return entity_map[candidates[0]]


def resolve_recorder_entities(
    conn: sqlite3.Connection,
    vin: str | None = None,
    vehicle_id: str | None = None,
) -> dict[str, int]:
    """Resolve metadata IDs for vehicle entities in Home Assistant recorder schema."""
    cursor = conn.cursor()

    # Check if states_meta exists (HA schema >= 30)
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='states_meta'"
    )
    has_states_meta = cursor.fetchone() is not None

    entity_map: dict[str, int] = {}
    if has_states_meta:
        cursor.execute("SELECT metadata_id, entity_id FROM states_meta")
        for row in cursor.fetchall():
            entity_map[row["entity_id"].lower()] = row["metadata_id"]
    else:
        # Older schema fallback: check if states table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='states'"
        )
        has_states = cursor.fetchone() is not None
        if has_states:
            cursor.execute("SELECT DISTINCT entity_id FROM states")
            for idx, row in enumerate(cursor.fetchall(), start=1):
                entity_map[row["entity_id"].lower()] = idx

    resolved: dict[str, int] = {}
    target_tokens = []
    if vin:
        target_tokens.append(vin.lower())
    if vehicle_id:
        target_tokens.append(vehicle_id.lower())

    # Helper to find matching entity
    def find_entity_id(
        keywords: list[str], exclude: list[str] | None = None
    ) -> int | None:
        exclude_list = exclude or []
        candidates: list[tuple[int, str]] = []

        for entity_id in entity_map:
            if any(ex in entity_id for ex in exclude_list):
                continue
            if all(kw in entity_id for kw in keywords):
                # Score candidate by match specificity
                score = 0
                for token in target_tokens:
                    if token in entity_id:
                        score += 10
                candidates.append((score, entity_id))

        if not candidates:
            # Try matching any keyword
            for entity_id in entity_map:
                if any(ex in entity_id for ex in exclude_list):
                    continue
                if any(kw in entity_id for kw in keywords):
                    score = 0
                    for token in target_tokens:
                        if token in entity_id:
                            score += 10
                    candidates.append((score, entity_id))

        if candidates:
            candidates.sort(key=lambda c: c[0], reverse=True)
            best_entity = candidates[0][1]
            return entity_map[best_entity]
        return None

    # Resolve each sensor domain
    gear_id = (
        find_entity_id(["gear_selector"])
        or find_entity_id(["gear_status"])
        or find_entity_id(["gear"])
    )
    if gear_id is not None:
        resolved["gear_selector"] = gear_id

    odo_id = (
        find_entity_id(["odometer"])
        or find_entity_id(["vehicle_mileage"])
        or find_entity_id(["mileage"])
    )
    if odo_id is not None:
        resolved["odometer"] = odo_id

    soc_id = (
        find_entity_id(["battery_state_of_charge"])
        or find_entity_id(["state_of_charge"])
        or find_entity_id(["battery_level"])
        or find_entity_id(["battery_soc"])
        or find_entity_id(["soc"])
    )
    if soc_id is not None:
        resolved["battery_level"] = soc_id

    speed_id = find_entity_id(["speed"], exclude=["charging", "wind", "fan"])
    if speed_id is not None:
        resolved["speed"] = speed_id

    alt_id = find_entity_id(["altitude"]) or find_entity_id(["elevation"])
    if alt_id is not None:
        resolved["altitude"] = alt_id

    lat_id = find_entity_id(["latitude"])
    if lat_id is not None:
        resolved["latitude"] = lat_id

    lon_id = find_entity_id(["longitude"])
    if lon_id is not None:
        resolved["longitude"] = lon_id

    tracker_id = _find_tracker_id(entity_map, target_tokens)
    if tracker_id is not None:
        resolved["device_tracker"] = tracker_id

    cap_id = find_entity_id(["battery_capacity"])
    if cap_id is not None:
        resolved["battery_capacity"] = cap_id

    charging_id = (
        find_entity_id(["charging_status"])
        or find_entity_id(["charger_state"])
        or find_entity_id(["is_charging"])
    )
    if charging_id is not None:
        resolved["charging_status"] = charging_id

    range_id = (
        find_entity_id(["distance_to_empty"])
        or find_entity_id(["estimated_vehicle_range"])
        or find_entity_id(["vehicle_range"])
    )
    if range_id is not None:
        resolved["distance_to_empty"] = range_id

    drive_mode_id = find_entity_id(["drive_mode"])
    if drive_mode_id is not None:
        resolved["drive_mode"] = drive_mode_id

    trailer_id = find_entity_id(["trailer_status"]) or find_entity_id(["trailer"])
    if trailer_id is not None:
        resolved["trailer_status"] = trailer_id

    driver_id = find_entity_id(["active_driver"]) or find_entity_id(["driver"])
    if driver_id is not None:
        resolved["driver"] = driver_id

    _LOGGER.debug("Resolved recorder entities: %s", resolved)
    return resolved


def _extract_timeseries(
    conn: sqlite3.Connection,
    metadata_id: int,
    start_ts: float | None = None,
) -> list[tuple[float, str]]:
    """Extract ordered (timestamp, state) series for a metadata_id."""
    cursor = conn.cursor()

    # Determine timestamp column name
    cursor.execute("PRAGMA table_info(states)")
    columns = [row["name"] for row in cursor.fetchall()]
    ts_col = "last_updated_ts" if "last_updated_ts" in columns else "last_updated"

    query = f"SELECT state, {ts_col} FROM states WHERE metadata_id = ? "
    params: list[Any] = [metadata_id]

    if start_ts is not None:
        query += f"AND {ts_col} >= ? "
        params.append(start_ts)

    query += f"ORDER BY {ts_col} ASC"
    cursor.execute(query, params)

    records: list[tuple[float, str]] = []
    for row in cursor.fetchall():
        state_str = row["state"]
        raw_ts = row[ts_col]
        if state_str is None or state_str in (
            "unknown",
            "unavailable",
            "fault",
            "signal_not_available",
        ):
            continue
        try:
            if isinstance(raw_ts, (int, float)):
                ts_val = float(raw_ts)
            else:
                # Parse string timestamp
                dt = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                ts_val = dt.timestamp()
            records.append((ts_val, str(state_str)))
        except (ValueError, TypeError):
            continue

    return records


def _extract_coordinates_series(
    conn: sqlite3.Connection,
    tracker_id: int,
    start_ts: float | None = None,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Extract ordered (ts, lat) and (ts, lon) from device_tracker states."""
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(states)")
    columns = [row["name"] for row in cursor.fetchall()]
    ts_col = "last_updated_ts" if "last_updated_ts" in columns else "last_updated"

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='state_attributes'"
    )
    has_attributes_table = cursor.fetchone() is not None

    lat_series: list[tuple[float, float]] = []
    lon_series: list[tuple[float, float]] = []

    if has_attributes_table:
        query = (
            f"SELECT s.{ts_col}, a.shared_attrs FROM states s "
            f"LEFT JOIN state_attributes a ON s.attributes_id = a.attributes_id "
            f"WHERE s.metadata_id = ? "
        )
        params: list[Any] = [tracker_id]
        if start_ts is not None:
            query += f"AND s.{ts_col} >= ? "
            params.append(start_ts)
        query += f"ORDER BY s.{ts_col} ASC"
        cursor.execute(query, params)
        for row in cursor.fetchall():
            raw_ts = row[ts_col]
            attrs_str = row["shared_attrs"]
            if not attrs_str:
                continue
            try:
                if isinstance(raw_ts, (int, float)):
                    ts_val = float(raw_ts)
                else:
                    dt = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                    ts_val = dt.timestamp()
                attrs = json.loads(attrs_str)
                lat = attrs.get("latitude")
                lon = attrs.get("longitude")
                if lat is not None and lon is not None:
                    lat_series.append((ts_val, float(lat)))
                    lon_series.append((ts_val, float(lon)))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    elif "attributes" in columns:
        query = f"SELECT {ts_col}, attributes FROM states WHERE metadata_id = ? "
        params = [tracker_id]
        if start_ts is not None:
            query += f"AND {ts_col} >= ? "
            params.append(start_ts)
        query += f"ORDER BY {ts_col} ASC"
        cursor.execute(query, params)
        for row in cursor.fetchall():
            raw_ts = row[ts_col]
            attrs_str = row["attributes"]
            if not attrs_str:
                continue
            try:
                if isinstance(raw_ts, (int, float)):
                    ts_val = float(raw_ts)
                else:
                    dt = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                    ts_val = dt.timestamp()
                attrs = json.loads(attrs_str)
                lat = attrs.get("latitude")
                lon = attrs.get("longitude")
                if lat is not None and lon is not None:
                    lat_series.append((ts_val, float(lat)))
                    lon_series.append((ts_val, float(lon)))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue

    return lat_series, lon_series


def _get_value_at_ts(
    series: list[tuple[float, float]],
    target_ts: float,
    prefer: str = "nearest",
) -> float | None:
    """Find scalar value in sorted (ts, val) series closest to target_ts."""
    if not series:
        return None

    # Binary search for closest
    low = 0
    high = len(series) - 1

    if target_ts <= series[0][0]:
        return series[0][1]
    if target_ts >= series[-1][0]:
        return series[-1][1]

    while low <= high:
        mid = (low + high) // 2
        mid_ts = series[mid][0]
        if mid_ts == target_ts:
            return series[mid][1]
        if mid_ts < target_ts:
            low = mid + 1
        else:
            high = mid - 1

    # low is right of target, high is left of target
    left_item = series[max(0, high)]
    right_item = series[min(len(series) - 1, low)]

    if prefer == "before":
        return left_item[1]
    if prefer == "after":
        return right_item[1]

    # Nearest
    if abs(target_ts - left_item[0]) <= abs(target_ts - right_item[0]):
        return left_item[1]
    return right_item[1]


async def async_resolve_vehicle_entity_ids(
    hass: HomeAssistant, vin: str
) -> dict[str, str]:
    """Resolve this VIN's recorder-relevant entity_ids via the entity registry.

    Runs on the event loop (entity registry lookups are cheap, synchronous,
    in-memory operations - no executor needed). Looks up the exact
    (domain, platform="rivian", unique_id) triples that entity.py and
    device_tracker.py assign, so a phone's device_tracker or another
    integration's sensor can never be picked by accident. Falls back to the
    fuzzy `resolve_recorder_entities` matcher (used automatically by
    `reconstruct_tracks_for_windows`) for any key not found here, e.g. in
    tests that build a recorder DB without a matching entity registry.
    """
    try:
        registry = er.async_get(hass)
    except (AttributeError, TypeError, KeyError) as err:
        # No real entity registry available (e.g. a hand-rolled mock hass in
        # tests). reconstruct_tracks_for_windows falls back to fuzzy matching.
        _LOGGER.debug(
            "Entity registry unavailable for track entity resolution: %s", err
        )
        return {}

    resolved: dict[str, str] = {}
    for key, (domain, key_suffix) in _TRACK_ENTITY_UNIQUE_ID_KEYS.items():
        try:
            entity_id = registry.async_get_entity_id(
                domain, DOMAIN, f"{vin}-{key_suffix}"
            )
        except (AttributeError, TypeError, KeyError):
            continue
        if entity_id:
            resolved[key] = entity_id
    return resolved


async def async_resolve_context_entity_ids(
    hass: HomeAssistant, vin: str
) -> dict[str, str]:
    """Resolve this VIN's vehicle-context entity_ids via the entity registry.

    Mirrors ``async_resolve_vehicle_entity_ids`` but for the live-context
    fields (range, drive mode, trailer, driver) rather than the ones a GPS
    track needs. Falls back to the fuzzy ``resolve_recorder_entities``
    matcher (used automatically by ``reconstruct_drives_from_sqlite``) for
    any key not found here.
    """
    try:
        registry = er.async_get(hass)
    except (AttributeError, TypeError, KeyError) as err:
        _LOGGER.debug(
            "Entity registry unavailable for context entity resolution: %s", err
        )
        return {}

    resolved: dict[str, str] = {}
    for key, (domain, key_suffix) in _CONTEXT_ENTITY_UNIQUE_ID_KEYS.items():
        try:
            entity_id = registry.async_get_entity_id(
                domain, DOMAIN, f"{vin}-{key_suffix}"
            )
        except (AttributeError, TypeError, KeyError):
            continue
        if entity_id:
            resolved[key] = entity_id
    return resolved


def _resolve_metadata_ids_from_entity_ids(
    conn: sqlite3.Connection, entity_ids: dict[str, str]
) -> dict[str, int]:
    """Map {key: entity_id} to {key: metadata_id} via the recorder's states_meta.

    Returns an empty dict if there is nothing to resolve or the recorder
    schema predates ``states_meta`` (an older schema has no stable id to look
    entity_ids up by here; the fuzzy matcher is the only option there).
    """
    if not entity_ids:
        return {}
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='states_meta'"
    )
    if cursor.fetchone() is None:
        return {}
    cursor.execute("SELECT metadata_id, entity_id FROM states_meta")
    meta_by_entity_id = {
        row["entity_id"]: row["metadata_id"] for row in cursor.fetchall()
    }
    return {
        key: meta_by_entity_id[entity_id]
        for key, entity_id in entity_ids.items()
        if entity_id in meta_by_entity_id
    }


def _get_unit_of_measurement(
    conn: sqlite3.Connection, metadata_id: int, ts_col: str
) -> str | None:
    """Return the most recent recorded unit_of_measurement for a metadata_id."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='state_attributes'"
    )
    has_attributes_table = cursor.fetchone() is not None

    def _extract_unit(raw: str | None) -> str | None:
        if not raw:
            return None
        try:
            attrs = json.loads(raw)
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        unit = attrs.get("unit_of_measurement")
        return str(unit) if unit else None

    if has_attributes_table:
        cursor.execute(
            f"SELECT a.shared_attrs FROM states s "
            f"LEFT JOIN state_attributes a ON s.attributes_id = a.attributes_id "
            f"WHERE s.metadata_id = ? AND a.shared_attrs IS NOT NULL "
            f"ORDER BY s.{ts_col} DESC LIMIT 5",
            (metadata_id,),
        )
        for row in cursor.fetchall():
            unit = _extract_unit(row["shared_attrs"])
            if unit:
                return unit
        return None

    cursor.execute("PRAGMA table_info(states)")
    columns = [row["name"] for row in cursor.fetchall()]
    if "attributes" not in columns:
        return None
    cursor.execute(
        f"SELECT attributes FROM states WHERE metadata_id = ? "
        f"AND attributes IS NOT NULL ORDER BY {ts_col} DESC LIMIT 5",
        (metadata_id,),
    )
    for row in cursor.fetchall():
        unit = _extract_unit(row["attributes"])
        if unit:
            return unit
    return None


def _convert_speed_to_mps(value: float, unit: str | None) -> float:
    """Convert a recorded speed value to metres/second."""
    unit_norm = (unit or "").strip().lower()
    if unit_norm in ("mph", "mi/h"):
        return value * (METERS_PER_MILE / 3600.0)
    if unit_norm in ("km/h", "kph", "kmh"):
        return value / 3.6
    return value  # already m/s (or unknown - assume SI)


def _convert_length_to_meters(value: float, unit: str | None) -> float:
    """Convert a recorded length value (altitude/odometer) to metres."""
    unit_norm = (unit or "").strip().lower()
    if unit_norm in ("ft", "feet"):
        return value * FEET_TO_METERS
    if unit_norm in ("mi", "mile", "miles"):
        return value * METERS_PER_MILE
    if unit_norm in ("km",):
        return value * 1000.0
    return value  # already metres (or unknown - assume SI)


def reconstruct_tracks_for_windows(
    db_path: str,
    entity_ids: dict[str, str],
    windows: list[tuple[str, float, float]],
    vin: str | None = None,
    vehicle_id: str | None = None,
) -> dict[str, DriveTrack]:
    """Rebuild GPS DriveTracks for a batch of drive time windows.

    ``entity_ids`` maps the same keys as `resolve_recorder_entities`
    ("device_tracker", "speed", "altitude", "battery_level", "odometer") to
    recorder entity_id strings (normally from `async_resolve_vehicle_entity_ids`,
    the entity-registry lookup). Any key missing here is filled in with the
    fuzzy `resolve_recorder_entities` matcher, so this also works against a
    bare recorder DB with no matching entity registry (as in tests).

    Runs a single query per telemetry entity over the union of all windows,
    then slices per-window in memory, rather than one query per window.
    Windows that end up with fewer than 2 valid GPS points are omitted from
    the result.
    """
    if not windows:
        return {}

    try:
        conn = open_sqlite_readonly(db_path)
    except (sqlite3.Error, OSError) as err:
        _LOGGER.error(
            "Failed to open SQLite recorder database '%s' for track rebuild: %s",
            db_path,
            err,
        )
        return {}

    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='states_meta'"
        )
        has_states_meta = cursor.fetchone() is not None
        meta_by_entity_id: dict[str, int] = {}
        if has_states_meta:
            cursor.execute("SELECT metadata_id, entity_id FROM states_meta")
            for row in cursor.fetchall():
                meta_by_entity_id[row["entity_id"]] = row["metadata_id"]

        metadata_ids: dict[str, int] = {}
        for key, entity_id in (entity_ids or {}).items():
            if entity_id and entity_id in meta_by_entity_id:
                metadata_ids[key] = meta_by_entity_id[entity_id]

        if "device_tracker" not in metadata_ids:
            fallback = resolve_recorder_entities(conn, vin=vin, vehicle_id=vehicle_id)
            for key, mid in fallback.items():
                metadata_ids.setdefault(key, mid)

        if "device_tracker" not in metadata_ids:
            _LOGGER.warning(
                "No device_tracker entity resolved in recorder database for track rebuild"
            )
            return {}

        cursor.execute("PRAGMA table_info(states)")
        columns = [row["name"] for row in cursor.fetchall()]
        ts_col = "last_updated_ts" if "last_updated_ts" in columns else "last_updated"

        global_start = min(w[1] for w in windows) - TRACK_WINDOW_PAD_SECONDS
        global_end = max(w[2] for w in windows) + TRACK_WINDOW_PAD_SECONDS

        tracker_meta_id = metadata_ids["device_tracker"]
        lat_series, lon_series = _extract_coordinates_series(
            conn, tracker_meta_id, start_ts=global_start
        )
        lat_series = [(t, v) for t, v in lat_series if t <= global_end]
        lon_series = [(t, v) for t, v in lon_series if t <= global_end]

        def _load_numeric(key: str) -> tuple[list[tuple[float, float]], str | None]:
            if key not in metadata_ids:
                return [], None
            mid = metadata_ids[key]
            unit = _get_unit_of_measurement(conn, mid, ts_col)
            raw = _extract_timeseries(conn, mid, start_ts=global_start)
            series: list[tuple[float, float]] = []
            for ts, val in raw:
                if ts > global_end:
                    continue
                try:
                    series.append((ts, float(val)))
                except (ValueError, TypeError):
                    continue
            return series, unit

        speed_series, speed_unit = _load_numeric("speed")
        alt_series, alt_unit = _load_numeric("altitude")
        soc_series, _soc_unit = _load_numeric("battery_level")
        odo_series, odo_unit = _load_numeric("odometer")

        results: dict[str, DriveTrack] = {}
        for drive_id, start_ts, end_ts in windows:
            w_start = start_ts - TRACK_WINDOW_PAD_SECONDS
            w_end = end_ts + TRACK_WINDOW_PAD_SECONDS

            win_lat = [(t, v) for t, v in lat_series if w_start <= t <= w_end]
            win_lon = [(t, v) for t, v in lon_series if w_start <= t <= w_end]
            if len(win_lat) < MIN_TRACK_POINTS or len(win_lon) < MIN_TRACK_POINTS:
                continue

            win_speed = [(t, v) for t, v in speed_series if w_start <= t <= w_end]
            win_alt = [(t, v) for t, v in alt_series if w_start <= t <= w_end]
            win_soc = [(t, v) for t, v in soc_series if w_start <= t <= w_end]
            win_odo = [(t, v) for t, v in odo_series if w_start <= t <= w_end]

            track = DriveTrack()
            for (t, lat), (_lon_t, lon) in zip(win_lat, win_lon):
                spd = _get_value_at_ts(win_speed, t) if win_speed else None
                if spd is not None:
                    spd = _convert_speed_to_mps(spd, speed_unit)
                alt = _get_value_at_ts(win_alt, t) if win_alt else None
                if alt is not None:
                    alt = _convert_length_to_meters(alt, alt_unit)
                soc = _get_value_at_ts(win_soc, t) if win_soc else None
                odo = _get_value_at_ts(win_odo, t) if win_odo else None
                if odo is not None:
                    odo = _convert_length_to_meters(odo, odo_unit)
                track.append(
                    TrackPoint(
                        t=t,
                        lat=lat,
                        lon=lon,
                        speed_mps=spd,
                        alt_m=alt,
                        soc=soc,
                        odo_m=odo,
                    )
                )

            if len(track) >= MIN_TRACK_POINTS:
                results[drive_id] = track

        return results
    except (sqlite3.Error, OSError) as err:
        _LOGGER.error(
            "SQLite error while rebuilding tracks from '%s': %s", db_path, err
        )
        return {}
    finally:
        conn.close()


def reconstruct_vampire_events_from_drives(
    drives: list[DriveRecord],
    battery_capacity_kwh: float = DEFAULT_BATTERY_CAPACITY_KWH,
) -> list[VampireDrainRecord]:
    """Reconstruct non-charging parked intervals (>= 30 min) between consecutive drives."""
    vampire_events: list[VampireDrainRecord] = []
    if len(drives) < 2:
        return vampire_events

    for i in range(len(drives) - 1):
        prev_d = drives[i]
        next_d = drives[i + 1]

        start_iso = prev_d.end_time
        end_iso = next_d.start_time
        if not start_iso or not end_iso:
            continue

        try:
            dt_start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            dt_end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            idle_hours = (dt_end - dt_start).total_seconds() / 3600.0
        except (ValueError, TypeError):
            continue

        # Filter out short stops (< 30 min) to avoid cell voltage settling drift
        if idle_hours < 0.5:
            continue

        start_soc = prev_d.end_soc
        end_soc = next_d.start_soc

        # Exclude intervals where SOC did not decrease (charging, shore power, or below BMS resolution)
        if end_soc >= start_soc:
            continue

        drain_soc = round(start_soc - end_soc, 2)
        if drain_soc <= 0.0:
            continue

        pack_cap = prev_d.battery_capacity_kwh or battery_capacity_kwh
        drain_kwh = round((drain_soc * pack_cap) / 100.0, 2)
        if drain_kwh <= 0.0:
            continue

        rate_pct_day = (
            round((drain_soc / idle_hours) * 24.0, 2) if idle_hours > 0 else 0.0
        )
        avg_watts = (
            round((drain_kwh * 1000.0) / idle_hours, 1) if idle_hours > 0 else 0.0
        )

        lat = prev_d.end_lat if prev_d.end_lat is not None else next_d.start_lat
        lon = prev_d.end_lon if prev_d.end_lon is not None else next_d.start_lon

        vampire_events.append(
            VampireDrainRecord(
                start_time=start_iso,
                end_time=end_iso,
                idle_hours=round(idle_hours, 2),
                start_soc=round(start_soc, 2),
                end_soc=round(end_soc, 2),
                drain_soc=drain_soc,
                drain_kwh=drain_kwh,
                rate_pct_per_day=rate_pct_day,
                avg_watts=avg_watts,
                latitude=lat,
                longitude=lon,
            )
        )

    return vampire_events


def reconstruct_drives_from_sqlite(
    db_path: str,
    vin: str | None = None,
    vehicle_id: str | None = None,
    days: int | None = None,
    battery_capacity: float | None = None,
    context_entity_ids: dict[str, str] | None = None,
) -> tuple[list[DriveRecord], dict[str, Any]]:
    """Synchronously reconstruct historical drive records from SQLite database.

    ``context_entity_ids`` (from ``async_resolve_context_entity_ids``, the
    entity-registry lookup) resolves the live vehicle-context fields (range,
    drive mode, trailer, driver) precisely; any key it doesn't cover falls
    back to the fuzzy matcher already used for everything else here.
    """
    try:
        conn = open_sqlite_readonly(db_path)
    except (sqlite3.Error, OSError) as err:
        _LOGGER.error("Failed to open SQLite recorder database '%s': %s", db_path, err)
        return [], {}

    try:
        entities = resolve_recorder_entities(conn, vin=vin, vehicle_id=vehicle_id)
        if context_entity_ids:
            entities.update(
                _resolve_metadata_ids_from_entity_ids(conn, context_entity_ids)
            )
        if "gear_selector" not in entities:
            _LOGGER.warning(
                "No gear selector entity found in recorder database for backfill"
            )
            return [], {}

        cutoff_ts: float | None = None
        if days is not None and days > 0:
            now_ts = datetime.now(timezone.utc).timestamp()
            cutoff_ts = now_ts - (days * 86400.0)

        # 1. Extract gear transitions
        raw_gear_series = _extract_timeseries(
            conn, entities["gear_selector"], start_ts=cutoff_ts
        )
        if not raw_gear_series:
            _LOGGER.info("No gear transitions recorded in the specified timeframe")
            return [], {}

        # 2. Extract telemetry series
        def load_numeric_series(key: str) -> list[tuple[float, float]]:
            if key not in entities:
                return []
            raw = _extract_timeseries(conn, entities[key], start_ts=cutoff_ts)
            res = []
            for ts, val in raw:
                try:
                    res.append((ts, float(val)))
                except (ValueError, TypeError):
                    continue
            return res

        odometer_series = load_numeric_series("odometer")
        soc_series = load_numeric_series("battery_level")
        speed_series = load_numeric_series("speed")
        alt_series = load_numeric_series("altitude")
        lat_series = load_numeric_series("latitude")
        lon_series = load_numeric_series("longitude")

        if (not lat_series or not lon_series) and "device_tracker" in entities:
            trk_lat, trk_lon = _extract_coordinates_series(
                conn, entities["device_tracker"], start_ts=cutoff_ts
            )
            if trk_lat and trk_lon:
                lat_series = trk_lat
                lon_series = trk_lon

        # Vehicle-context series (range/drive mode/trailer/driver). Range is
        # numeric (km); the rest are raw string states, read like the gear
        # selector series above.
        range_series = load_numeric_series("distance_to_empty")
        drive_mode_raw_series = (
            _extract_timeseries(conn, entities["drive_mode"], start_ts=cutoff_ts)
            if "drive_mode" in entities
            else []
        )
        trailer_raw_series = (
            _extract_timeseries(conn, entities["trailer_status"], start_ts=cutoff_ts)
            if "trailer_status" in entities
            else []
        )
        driver_raw_series = (
            _extract_timeseries(conn, entities["driver"], start_ts=cutoff_ts)
            if "driver" in entities
            else []
        )

        # Determine battery capacity
        pack_capacity = battery_capacity or DEFAULT_BATTERY_CAPACITY_KWH
        if "battery_capacity" in entities:
            cap_series = load_numeric_series("battery_capacity")
            if cap_series:
                pack_capacity = cap_series[-1][1]

        # 3. Reconstruct raw gear spans (shifts out of Park into Drive/Reverse until Park)
        gear_spans: list[dict[str, Any]] = []
        current_span: dict[str, Any] | None = None

        for ts, state in raw_gear_series:
            gear = state.strip().lower()
            if gear in DRIVING_GEARS:
                if current_span is None:
                    current_span = {
                        "start_ts": ts,
                        "end_ts": ts,
                    }
                else:
                    current_span["end_ts"] = ts
            elif gear in PARK_GEAR:
                if current_span is not None:
                    current_span["end_ts"] = ts
                    if current_span["end_ts"] > current_span["start_ts"]:
                        gear_spans.append(current_span)
                    current_span = None
            else:
                # Neutral / standby: keep gear span alive if active
                if current_span is not None:
                    current_span["end_ts"] = ts

        if (
            current_span is not None
            and current_span["end_ts"] > current_span["start_ts"]
        ):
            gear_spans.append(current_span)

        if not gear_spans:
            return [], {}

        # 4. Apply 60-second Park debounce
        merged_spans: list[dict[str, Any]] = []
        for span in gear_spans:
            if not merged_spans:
                merged_spans.append(dict(span))
                continue

            last_span = merged_spans[-1]
            gap = span["start_ts"] - last_span["end_ts"]

            if 0.0 <= gap <= PARK_DEBOUNCE_SECONDS:
                # Merge gear spans
                last_span["end_ts"] = span["end_ts"]
            else:
                merged_spans.append(dict(span))

        # 5. Build DriveRecords from merged gear spans
        effective_vin = vin or "UNKNOWN_VIN"
        reconstructed_drives: list[DriveRecord] = []

        for span in merged_spans:
            start_ts = span["start_ts"]
            end_ts = span["end_ts"]
            duration_s = max(1.0, end_ts - start_ts)

            start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
            end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
            start_iso = start_dt.isoformat()
            end_iso = end_dt.isoformat()
            drive_id = f"{effective_vin}_{int(start_ts)}"

            # Odometer and distance
            start_odo = _get_value_at_ts(odometer_series, start_ts, prefer="nearest")
            end_odo = _get_value_at_ts(odometer_series, end_ts, prefer="nearest")

            distance_mi = 0.0
            if start_odo is not None and end_odo is not None:
                delta_odo = end_odo - start_odo
                # Check if odometer was in meters (> 10000 and delta > 100)
                if start_odo > 50000.0 and delta_odo > 50.0:
                    distance_mi = round(delta_odo / METERS_PER_MILE, 2)
                else:
                    distance_mi = round(max(0.0, delta_odo), 2)

            # Battery SOC and energy
            start_soc = _get_value_at_ts(soc_series, start_ts, prefer="nearest") or 0.0
            end_soc = (
                _get_value_at_ts(soc_series, end_ts, prefer="nearest") or start_soc
            )
            delta_soc = max(0.0, start_soc - end_soc)
            energy_kwh = round((delta_soc * pack_capacity) / 100.0, 2)

            # Altitude and elevation change
            start_alt = _get_value_at_ts(alt_series, start_ts, prefer="nearest") or 0.0
            end_alt = (
                _get_value_at_ts(alt_series, end_ts, prefer="nearest") or start_alt
            )
            elevation_delta_ft = round(end_alt - start_alt, 1)

            # Speed samples and bins
            seg_speeds = [(t, s) for t, s in speed_series if start_ts <= t <= end_ts]
            speed_bins = {b: SpeedBinData() for b in STANDARD_SPEED_BINS}
            max_speed = 0.0
            avg_speed = 0.0

            if seg_speeds:
                max_speed = round(max(s for _, s in seg_speeds), 1)
                for i in range(len(seg_speeds)):
                    t_curr, s_curr = seg_speeds[i]
                    if i + 1 < len(seg_speeds):
                        t_next = seg_speeds[i + 1][0]
                        dt_sample = max(0.0, t_next - t_curr)
                    else:
                        dt_sample = max(0.0, end_ts - t_curr)

                    s_mph = float(s_curr)
                    d_sample_mi = s_mph * (dt_sample / 3600.0)
                    bin_k = _get_speed_bin_key(s_mph)
                    speed_bins[bin_k].miles += d_sample_mi
                    speed_bins[bin_k].seconds += dt_sample

                if duration_s > 0:
                    avg_speed = round(distance_mi / (duration_s / 3600.0), 1)
            elif distance_mi > 0 and duration_s > 0:
                avg_speed = round(distance_mi / (duration_s / 3600.0), 1)
                max_speed = avg_speed
                bin_k = _get_speed_bin_key(avg_speed)
                speed_bins[bin_k].miles = distance_mi
                speed_bins[bin_k].seconds = duration_s

            # Lat / Lon coordinates
            start_lat = _get_value_at_ts(lat_series, start_ts, prefer="nearest")
            start_lon = _get_value_at_ts(lon_series, start_ts, prefer="nearest")
            end_lat = _get_value_at_ts(lat_series, end_ts, prefer="nearest")
            end_lon = _get_value_at_ts(lon_series, end_ts, prefer="nearest")

            is_micro = distance_mi < MICRO_DRIVE_THRESHOLD_MILES
            eff_mi_kwh = round(distance_mi / energy_kwh, 2) if energy_kwh > 0.0 else 0.0
            mpge = round(eff_mi_kwh * MPGE_FACTOR, 2) if eff_mi_kwh > 0.0 else 0.0

            # Generate 3-minute chunks for speed-bin efficiency analysis
            drive_chunks: list[DriveChunk] = []
            chunk_duration_s = 180.0  # 3 minutes
            t_curr = start_ts
            while t_curr < end_ts:
                t_next = min(t_curr + chunk_duration_s, end_ts)
                dt_win = t_next - t_curr
                if dt_win < (chunk_duration_s * 0.5):
                    break

                s_odo = _get_value_at_ts(odometer_series, t_curr, prefer="nearest")
                e_odo = _get_value_at_ts(odometer_series, t_next, prefer="nearest")
                if s_odo is not None and e_odo is not None:
                    d_odo = e_odo - s_odo
                    if s_odo > 50000.0 and d_odo > 50.0:
                        chunk_dist = round(d_odo / METERS_PER_MILE, 2)
                    else:
                        chunk_dist = round(max(0.0, d_odo), 2)
                else:
                    chunk_dist = 0.0

                s_soc = _get_value_at_ts(soc_series, t_curr, prefer="nearest") or 0.0
                e_soc = _get_value_at_ts(soc_series, t_next, prefer="nearest") or s_soc
                chunk_dsoc = max(0.0, s_soc - e_soc)
                chunk_kwh = round((chunk_dsoc * pack_capacity) / 100.0, 2)

                win_speeds = [s for t, s in speed_series if t_curr <= t <= t_next]
                if win_speeds:
                    chunk_avg_spd = round(sum(win_speeds) / len(win_speeds), 1)
                elif chunk_dist > 0 and dt_win > 0:
                    chunk_avg_spd = round(chunk_dist / (dt_win / 3600.0), 1)
                else:
                    chunk_avg_spd = 0.0

                s_alt = _get_value_at_ts(alt_series, t_curr, prefer="nearest") or 0.0
                e_alt = _get_value_at_ts(alt_series, t_next, prefer="nearest") or s_alt
                chunk_elev = round(e_alt - s_alt, 1)

                chunk_bin = _get_speed_bin_key(chunk_avg_spd)

                if chunk_kwh > 0.0 and chunk_dist >= 0.05:
                    chunk_eff = round(chunk_dist / chunk_kwh, 2)
                    drive_chunks.append(
                        DriveChunk(
                            start_time=datetime.fromtimestamp(
                                t_curr, tz=timezone.utc
                            ).isoformat(),
                            duration_seconds=round(dt_win, 1),
                            distance_miles=chunk_dist,
                            energy_kwh=chunk_kwh,
                            efficiency_mi_kwh=chunk_eff,
                            avg_speed_mph=chunk_avg_spd,
                            speed_bin=chunk_bin,
                            elevation_change_ft=chunk_elev,
                        )
                    )

                t_curr = t_next

            # Vehicle-context fields for this drive's window.
            start_range_km = _get_value_at_ts(range_series, start_ts, prefer="nearest")
            end_range_km = _get_value_at_ts(range_series, end_ts, prefer="nearest")
            start_range_mi = (
                round(start_range_km * KM_TO_MILES, 1)
                if start_range_km is not None
                else None
            )
            end_range_mi = (
                round(end_range_km * KM_TO_MILES, 1)
                if end_range_km is not None
                else None
            )

            modes_in_window = [
                s for t, s in drive_mode_raw_series if start_ts <= t <= end_ts
            ]
            drive_modes: list[str] = []
            for raw_mode in modes_in_window:
                display_mode = DRIVE_MODE_MAP.get(raw_mode, raw_mode)
                if display_mode not in drive_modes:
                    drive_modes.append(display_mode)

            trailer_states = [
                s for t, s in trailer_raw_series if start_ts <= t <= end_ts
            ]
            trailer = trailer_attached_any(trailer_states)

            driver_states = [s for t, s in driver_raw_series if start_ts <= t <= end_ts]
            driver = driver_states[-1] if driver_states else None

            drive = DriveRecord(
                vin=effective_vin,
                drive_id=drive_id,
                start_time=start_iso,
                end_time=end_iso,
                distance_miles=distance_mi,
                duration_seconds=round(duration_s, 1),
                start_soc=round(start_soc, 2),
                end_soc=round(end_soc, 2),
                battery_capacity_kwh=round(pack_capacity, 2),
                energy_kwh=energy_kwh,
                efficiency_mi_kwh=eff_mi_kwh,
                mpge=mpge,
                start_altitude_ft=round(start_alt, 1),
                end_altitude_ft=round(end_alt, 1),
                elevation_change_ft=elevation_delta_ft,
                avg_speed_mph=avg_speed,
                max_speed_mph=max_speed,
                speed_bins=speed_bins,
                is_micro_drive=is_micro,
                start_odometer_mi=round(start_odo, 2)
                if start_odo is not None
                else None,
                end_odometer_mi=round(end_odo, 2) if end_odo is not None else None,
                start_lat=start_lat,
                start_lon=start_lon,
                end_lat=end_lat,
                end_lon=end_lon,
                chunks=drive_chunks,
                start_range_mi=start_range_mi,
                end_range_mi=end_range_mi,
                drive_modes=drive_modes,
                trailer=trailer,
                driver=driver,
            )
            reconstructed_drives.append(drive)

        vampire_events = reconstruct_vampire_events_from_drives(
            reconstructed_drives, pack_capacity
        )
        dcfc_sessions = reconstruct_dcfc_sessions_from_sqlite(
            conn=conn,
            entity_map=entities,
            pack_capacity=pack_capacity,
            vin=effective_vin,
            start_ts=cutoff_ts,
            location=(lat_series, lon_series),
        )
        return reconstructed_drives, {
            "gear_spans": len(gear_spans),
            "merged_drives": len(merged_spans),
            "vampire_events": vampire_events,
            "dcfc_sessions": dcfc_sessions,
        }
    except (sqlite3.Error, OSError) as err:
        _LOGGER.error(
            "SQLite error while reconstructing drives from '%s': %s", db_path, err
        )
        return [], {}
    finally:
        conn.close()


def reconstruct_dcfc_sessions_from_sqlite(
    conn: sqlite3.Connection,
    entity_map: dict[str, int],
    pack_capacity: float = DEFAULT_BATTERY_CAPACITY_KWH,
    vin: str | None = None,
    start_ts: float | None = None,
    location: tuple[list[tuple[float, float]], list[tuple[float, float]]] | None = None,
) -> list[ChargingSessionRecord]:
    """Extract historical charging sessions from recorder states.

    A session whose average power reaches ``DCFC_MIN_POWER_KW`` is DC fast
    charging and gets an estimated power curve; a slower one is kept as a
    home/AC session (``AC_SESSION_MIN_SOC_GAIN_PCT`` / ``AC_SESSION_MIN_DURATION_S``
    apply) with a few coarse SoC points. ``location`` is the vehicle's
    ``(lat_series, lon_series)``; each session gets the position nearest
    before its start, or None when that isn't resolvable.
    """
    status_id = entity_map.get("charging_status")
    soc_id = entity_map.get("battery_level")
    if not soc_id:
        return []

    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(states)")
    columns = [row["name"] for row in cursor.fetchall()]
    ts_col = "last_updated_ts" if "last_updated_ts" in columns else "last_updated"

    charging_windows: list[tuple[float, float]] = []
    if status_id:
        query = f"SELECT state, {ts_col} FROM states WHERE metadata_id = ? "
        params: list[Any] = [status_id]
        if start_ts is not None:
            query += f"AND {ts_col} >= ? "
            params.append(start_ts)
        query += f"ORDER BY {ts_col} ASC"
        cursor.execute(query, params)
        cur_start = None
        cur_end = None
        for r in cursor.fetchall():
            st = str(r["state"]).lower()
            raw_ts = r[ts_col]
            ts = (
                float(raw_ts)
                if isinstance(raw_ts, (int, float))
                else datetime.fromisoformat(
                    str(raw_ts).replace("Z", "+00:00")
                ).timestamp()
            )
            if st in ("on", "true", "charging_active", "charging_connecting"):
                if cur_start is None:
                    cur_start = ts
                cur_end = ts
            elif (
                st in ("off", "false", "charging_complete", "charging_stopped")
                and cur_start is not None
            ):
                charging_windows.append(
                    (cur_start, cur_end if cur_end is not None else ts)
                )
                cur_start = None
                cur_end = None
        if cur_start is not None:
            charging_windows.append(
                (cur_start, cur_end if cur_end is not None else cur_start)
            )

    # Merge intervals closer than 5 minutes
    merged_windows: list[tuple[float, float]] = []
    for w_start, w_end in charging_windows:
        if not merged_windows:
            merged_windows.append((w_start, w_end))
        else:
            p_start, p_end = merged_windows[-1]
            if w_start - p_end <= 300.0:
                merged_windows[-1] = (p_start, max(p_end, w_end))
            else:
                merged_windows.append((w_start, w_end))

    effective_vin = vin or "vehicle"
    dcfc_records: list[ChargingSessionRecord] = []

    for win_start, win_end in merged_windows:
        dur_sec = win_end - win_start
        if dur_sec < 60.0:
            continue

        query = (
            f"SELECT state, {ts_col} FROM states WHERE metadata_id = ? "
            f"AND {ts_col} BETWEEN ? AND ? "
            f"ORDER BY {ts_col} ASC"
        )
        cursor.execute(query, (soc_id, win_start - 60.0, win_end + 60.0))
        raw_soc: list[tuple[float, float]] = []
        for sr in cursor.fetchall():
            val_str = sr["state"]
            if val_str in (None, "unknown", "unavailable", "fault"):
                continue
            try:
                val = float(val_str)
                if 0.0 < val < 99.5:
                    raw_ts = sr[ts_col]
                    ts = (
                        float(raw_ts)
                        if isinstance(raw_ts, (int, float))
                        else datetime.fromisoformat(
                            str(raw_ts).replace("Z", "+00:00")
                        ).timestamp()
                    )
                    raw_soc.append((ts, val))
            except (ValueError, TypeError):
                continue

        if len(raw_soc) < 5:
            continue

        dedup = dedupe_soc_points(raw_soc)

        start_soc = dedup[0][1]
        end_soc = dedup[-1][1]
        delta_soc = end_soc - start_soc
        if delta_soc < AC_SESSION_MIN_SOC_GAIN_PCT:
            continue

        energy_added = (delta_soc / 100.0) * pack_capacity
        avg_power = energy_added / (dur_sec / 3600.0)

        is_dc = avg_power >= DCFC_MIN_POWER_KW
        if is_dc:
            samples, max_power = estimate_charge_curve(dedup, pack_capacity)
            if max_power < DCFC_MIN_POWER_KW or not samples:
                continue
            kind = SESSION_KIND_DC
        else:
            if dur_sec < AC_SESSION_MIN_DURATION_S:
                continue
            samples = coarse_soc_samples(dedup, AC_SESSION_MAX_SOC_POINTS)
            max_power = avg_power
            kind = SESSION_KIND_AC

        lat: float | None = None
        lon: float | None = None
        if location and location[0] and location[1]:
            lat = _get_value_at_ts(location[0], win_start, prefer="before")
            lon = _get_value_at_ts(location[1], win_start, prefer="before")

        start_iso = datetime.fromtimestamp(win_start, tz=timezone.utc).isoformat()
        end_iso = datetime.fromtimestamp(win_end, tz=timezone.utc).isoformat()
        session_id = f"{effective_vin}_{int(win_start)}"

        dcfc_records.append(
            ChargingSessionRecord(
                session_id=session_id,
                start_time=start_iso,
                end_time=end_iso,
                start_soc=round(start_soc, 1),
                end_soc=round(end_soc, 1),
                energy_added_kwh=round(energy_added, 2),
                max_power_kw=round(max_power, 1),
                avg_power_kw=round(avg_power, 1),
                kind=kind,
                samples=samples,
                lat=lat,
                lon=lon,
                source="backfill",
            )
        )

    return dcfc_records


def _session_overlaps(
    session: ChargingSessionRecord, spans: list[tuple[float, float]]
) -> bool:
    """Return True if ``session``'s time range overlaps any stored ``(start, end)``."""
    start = _iso_to_ts(session.start_time)
    end = _iso_to_ts(session.end_time)
    if start is None or end is None:
        return False
    return any(
        start <= span_end and span_start <= end for span_start, span_end in spans
    )


_OVERLAP_MAX_PAGES: Final[int] = 25
_OVERLAP_PAGE_SIZE: Final[int] = 200


def _iso_to_ts(value: str | None) -> float | None:
    """Parse a DriveRecord ISO timestamp to POSIX epoch seconds."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


async def _async_existing_drive_intervals(
    store: DriveStore,
) -> list[tuple[float, float, str]]:
    """Return (start_ts, end_ts, drive_id) for this VIN's already-stored drives.

    Paged via `async_list_drives`, which already exists for the drive
    explorer; capped at `_OVERLAP_MAX_PAGES` pages as a safety net against an
    unbounded history.
    """
    intervals: list[tuple[float, float, str]] = []
    before_ts: float | None = None
    for _ in range(_OVERLAP_MAX_PAGES):
        page = await store.async_list_drives(
            before_ts=before_ts, limit=_OVERLAP_PAGE_SIZE, include_micro=True
        )
        if not page:
            break
        for row in page:
            start_ts = row.get("start_ts")
            drive_id = row.get("drive_id")
            if start_ts is None or not drive_id:
                continue
            duration = row.get("duration_seconds") or 0.0
            intervals.append((start_ts, start_ts + duration, drive_id))
        if len(page) < _OVERLAP_PAGE_SIZE:
            break
        before_ts = page[-1].get("start_ts")
        if before_ts is None:
            break
    return intervals


def _overlaps_existing_drive(
    start_ts: float,
    end_ts: float,
    drive_id: str,
    existing: list[tuple[float, float, str]],
) -> bool:
    """Return True if [start_ts, end_ts] overlaps a *different-id* stored drive.

    The overlap must exceed 50% of the shorter of the two intervals. A match
    on the same drive_id is never treated as an overlap here - that is a
    legitimate re-run and is handled by upsert_drives' own (vin, drive_id)
    dedup, not by this check.
    """
    duration = max(1e-6, end_ts - start_ts)
    for e_start, e_end, e_id in existing:
        if e_id == drive_id:
            continue
        e_duration = max(1e-6, e_end - e_start)
        overlap = min(end_ts, e_end) - max(start_ts, e_start)
        if overlap <= 0.0:
            continue
        if overlap > 0.5 * min(duration, e_duration):
            return True
    return False


async def async_backfill_from_recorder(
    hass: HomeAssistant | None = None,
    vehicle_id: str | None = None,
    vin: str | None = None,
    days: int | None = None,
    dry_run: bool = False,
    db_path: str | None = None,
    battery_capacity: float | None = None,
    weather_client: OpenMeteoWeatherClient | None = None,
    store: DriveStore | None = None,
    tracks: bool = True,
) -> dict[str, Any]:
    """Asynchronously backfill historical drives from Home Assistant recorder SQLite database."""
    # Resolve SQLite database path
    sqlite_path = db_path
    if not sqlite_path and hass is not None:
        try:
            sqlite_path = hass.config.path("home-assistant_v2.db")
        except AttributeError:
            sqlite_path = "home-assistant_v2.db"

    if not sqlite_path:
        raise ValueError(
            "No database path provided and could not resolve default recorder path"
        )

    # Resolve vehicle-context entities (range/drive mode/trailer/driver) up
    # front, on the event loop, the same way track entities are resolved
    # further down -- reconstruct_drives_from_sqlite itself runs in the
    # executor and has no registry access.
    context_entity_ids: dict[str, str] = {}
    if hass is not None and vin:
        context_entity_ids = await async_resolve_context_entity_ids(hass, vin)

    # Run heavy extraction logic in executor thread
    if hass is not None:
        loop = hass.loop
        drives, _meta = await hass.async_add_executor_job(
            reconstruct_drives_from_sqlite,
            sqlite_path,
            vin,
            vehicle_id,
            days,
            battery_capacity,
            context_entity_ids,
        )
    else:
        loop = asyncio.get_running_loop()
        drives, _meta = await loop.run_in_executor(
            None,
            reconstruct_drives_from_sqlite,
            sqlite_path,
            vin,
            vehicle_id,
            days,
            battery_capacity,
            context_entity_ids,
        )

    vampire_events: list[VampireDrainRecord] = _meta.get("vampire_events", [])
    dcfc_sessions: list[ChargingSessionRecord] = _meta.get("dcfc_sessions", [])
    # Weather Enrichment (Open-Meteo Historical Archive API - batched by grid and date range)
    # NOTE: drives with missing GPS no longer get HA's home coordinates filled in
    # here (they used to). That fill corrupts future GPS route/track matching,
    # since a drive would then carry the *home* location instead of "unknown",
    # and it also skewed weather lookups for drives that never actually visited
    # home. Vampire (idle-drain) events are left as-is below: they only ever
    # borrow a location for a temperature lookup between two known drives, not
    # for route reconstruction, so the home-location fallback is harmless there.
    default_lat = getattr(getattr(hass, "config", None), "latitude", None)
    default_lon = getattr(getattr(hass, "config", None), "longitude", None)

    for v in vampire_events:
        if v.latitude is None and default_lat is not None:
            v.latitude = float(default_lat)
        if v.longitude is None and default_lon is not None:
            v.longitude = float(default_lon)

    client = weather_client or OpenMeteoWeatherClient(hass=hass)
    drives_needing_weather = [
        d
        for d in drives
        if d.start_lat is not None
        and d.start_lon is not None
        and d.integrated_temperature_f is None
    ]
    vampire_needing_weather = [
        v
        for v in vampire_events
        if v.latitude is not None and v.longitude is not None and v.avg_temp_f is None
    ]

    if drives_needing_weather or vampire_needing_weather:
        # Group by grid coordinate (0.1 degree resolution ~11km grid)
        grid_map: dict[tuple[float, float], dict[str, list[Any]]] = {}
        for d in drives_needing_weather:
            if d.start_lat is not None and d.start_lon is not None:
                grid_key = (round(d.start_lat, 1), round(d.start_lon, 1))
                grid_map.setdefault(grid_key, {"drives": [], "vampire": []})[
                    "drives"
                ].append(d)

        for v in vampire_needing_weather:
            if v.latitude is not None and v.longitude is not None:
                grid_key = (round(v.latitude, 1), round(v.longitude, 1))
                grid_map.setdefault(grid_key, {"drives": [], "vampire": []})[
                    "vampire"
                ].append(v)

        for (grid_lat, grid_lon), items in grid_map.items():
            grid_drives: list[DriveRecord] = items["drives"]
            grid_vampire: list[VampireDrainRecord] = items["vampire"]

            # Determine bounding date range
            date_strings = (
                [d.start_time[:10] for d in grid_drives if len(d.start_time) >= 10]
                + [v.start_time[:10] for v in grid_vampire if len(v.start_time) >= 10]
                + [v.end_time[:10] for v in grid_vampire if len(v.end_time) >= 10]
            )
            if not date_strings:
                continue
            start_date_str = min(date_strings)
            end_date_str = max(date_strings)

            try:
                hourly = await client.async_get_historical_temperatures(
                    latitude=grid_lat,
                    longitude=grid_lon,
                    start_date=start_date_str,
                    end_date=end_date_str,
                )
                if hourly:
                    from .weather import (
                        calculate_window_average_temperature,
                        get_interpolated_temperature,
                    )

                    for d in grid_drives:
                        temp = get_interpolated_temperature(hourly, d.start_time)
                        if temp is not None:
                            d.integrated_temperature_f = temp

                    for v in grid_vampire:
                        avg_temp = calculate_window_average_temperature(
                            hourly, v.start_time, v.end_time
                        )
                        if avg_temp is not None:
                            v.avg_temp_f = avg_temp
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Could not fetch historical temperatures for grid (%s, %s): %s",
                    grid_lat,
                    grid_lon,
                    err,
                )

    # Resolve the target store up front (not only when persisting) so both the
    # overlap check below and the track rebuild further down can use it, even
    # in dry_run mode, for an accurate preview. A dry run must not write, and
    # a store's first load can run the one-time legacy-JSON import, so a dry
    # run only previews against a store that is already loaded.
    target_store = store
    if target_store is None and not dry_run and hass is not None and vin:
        analytics_db = hass.data.get(DOMAIN, {}).get("_analytics_db")
        target_store = DriveStore(hass=hass, vin=vin, db=analytics_db)
    if (
        dry_run
        and target_store is not None
        and not getattr(target_store, "is_loaded", True)
    ):
        target_store = None

    if dcfc_sessions and target_store is not None:
        # Add-only: skip a reconstructed session that overlaps one already
        # stored (a live-recorded one, or an earlier backfill).
        existing_spans = await target_store.async_charging_session_intervals()
        dcfc_sessions = [
            sess
            for sess in dcfc_sessions
            if not _session_overlaps(sess, existing_spans)
        ]

    # Skip reconstructed drives that substantially overlap an already-stored
    # drive under a *different* drive_id. Backfill's drive_id is derived from
    # the recorder's gear-state timestamp, while a live-recorded drive's
    # drive_id comes from the vehicle's own drive-start event, so the same
    # physical drive can otherwise be double-counted under two IDs. A drive
    # whose drive_id is already stored is skipped too: live capture usually
    # starts a drive on the same gear-change second the recorder saw, so the
    # ids match, and upserting the reconstruction would overwrite the richer
    # live record (vehicle context, stats). Backfill only ever adds drives.
    overlap_skipped = 0
    already_stored = 0
    if target_store is not None and drives:
        existing_intervals = await _async_existing_drive_intervals(target_store)
        if existing_intervals:
            existing_ids = {e_id for _s, _e, e_id in existing_intervals}
            kept_drives: list[DriveRecord] = []
            for d in drives:
                if d.drive_id in existing_ids:
                    already_stored += 1
                    continue
                d_start_ts = _iso_to_ts(d.start_time)
                d_end_ts = _iso_to_ts(d.end_time)
                if (
                    d_start_ts is not None
                    and d_end_ts is not None
                    and _overlaps_existing_drive(
                        d_start_ts, d_end_ts, d.drive_id, existing_intervals
                    )
                ):
                    overlap_skipped += 1
                    continue
                kept_drives.append(d)
            drives = kept_drives

    # Calculate statistics
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

    total_miles = round(sum(d.distance_miles for d in valid_drives), 2)
    total_kwh = round(sum(d.energy_kwh for d in valid_drives), 2)
    efficiency = round(total_miles / total_kwh, 2) if total_kwh > 0.0 else 0.0
    mpge = round(efficiency * MPGE_FACTOR, 2) if efficiency > 0.0 else 0.0

    duplicates_skipped = 0

    # Persist if not dry run
    if (
        not dry_run
        and (drives or vampire_events or dcfc_sessions)
        and target_store is not None
    ):
        if drives:
            new_added = await target_store.async_save_drives_batch(drives)
            duplicates_skipped = len(drives) - new_added
        if vampire_events:
            await target_store.async_save_vampire_events(vampire_events)
        if dcfc_sessions:
            await target_store.async_save_dcfc_sessions(dcfc_sessions)

        if hass is not None and vin:
            await async_update_statistics(hass, vin, drives)
            hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": vin})

    # GPS route rebuild for previously-stored drives that have no track yet.
    # This is independent of the drive reconstruction above: it targets drives
    # already sitting in storage (whether saved by backfill or live), not the
    # `drives` list just computed.
    drives_missing_tracks_count = 0
    tracks_rebuilt_count = 0
    tracks_written_count = 0
    if tracks and target_store is not None:
        now_ts = datetime.now(timezone.utc).timestamp()
        since_ts = now_ts - (days * 86400.0) if days and days > 0 else 0.0
        missing = await target_store.async_drives_missing_tracks(since_ts=since_ts)
        drives_missing_tracks_count = len(missing)

        if missing:
            entity_ids: dict[str, str] = {}
            if hass is not None and vin:
                entity_ids = await async_resolve_vehicle_entity_ids(hass, vin)

            if hass is not None:
                reconstructed_tracks = await hass.async_add_executor_job(
                    reconstruct_tracks_for_windows,
                    sqlite_path,
                    entity_ids,
                    missing,
                    vin,
                    vehicle_id,
                )
            else:
                reconstructed_tracks = await loop.run_in_executor(
                    None,
                    reconstruct_tracks_for_windows,
                    sqlite_path,
                    entity_ids,
                    missing,
                    vin,
                    vehicle_id,
                )
            tracks_rebuilt_count = len(reconstructed_tracks)

            if not dry_run and reconstructed_tracks:
                tracks_written_count = await target_store.async_upsert_tracks(
                    list(reconstructed_tracks.items()), source="backfill"
                )
                # Fill the track-derived summary stats (moving/stopped time,
                # stop count, climb/descent, robust max speed, highway share)
                # for every drive that now has a track but none yet --
                # covers both the drives just backfilled above and any
                # older stored drives whose route was just rebuilt.
                try:
                    await target_store.async_recompute_stats()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning(
                        "Post-backfill drive stats recompute failed for VIN %s: %s",
                        vin,
                        err,
                    )

    result = {
        "drives_found": len(drives) + already_stored,
        "valid_drives": len(valid_drives),
        "micro_drives": len(micro_drives),
        "vampire_events_found": len(vampire_events),
        "dcfc_sessions_found": len(dcfc_sessions),
        "total_miles": total_miles,
        "total_kwh": total_kwh,
        "efficiency_mi_kwh": efficiency,
        "mpge": mpge,
        # Already-stored drives are skipped before the save, so count them here.
        "duplicates_skipped": duplicates_skipped + already_stored,
        "overlap_skipped_existing": overlap_skipped,
        "already_stored": already_stored,
        "drives_missing_tracks": drives_missing_tracks_count,
        "tracks_rebuilt": tracks_rebuilt_count,
        "tracks_written": tracks_written_count,
        "drives": drives,
        "vampire_events": vampire_events,
        "dcfc_sessions": dcfc_sessions,
    }

    _LOGGER.info(
        "Historical backfill complete for VIN %s: %d drives found (%d valid, %d micro, "
        "%d overlap-skipped, %d already stored), %d vampire events, %d DCFC sessions, %.2f mi, %.2f kWh, "
        "%.2f mi/kWh; tracks: %d missing, %d rebuilt, %d written",
        vin,
        len(drives),
        len(valid_drives),
        len(micro_drives),
        overlap_skipped,
        already_stored,
        len(vampire_events),
        len(dcfc_sessions),
        total_miles,
        total_kwh,
        efficiency,
        drives_missing_tracks_count,
        tracks_rebuilt_count,
        tracks_written_count,
    )
    return result
