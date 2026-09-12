"""Rivian Historical Recorder Backfill Engine."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import sqlite3
from typing import TYPE_CHECKING, Any, Final

from .drive_models import (
    DCFC_MIN_POWER_KW,
    MAX_DCFC_HISTORY_SESSIONS,
    MICRO_DRIVE_THRESHOLD_MILES,
    MPGE_FACTOR,
    STANDARD_SPEED_BINS,
    ChargingSample,
    ChargingSessionRecord,
    DriveRecord,
    DriveSegment,
    SpeedBinData,
    VampireDrainRecord,
)
from .drive_storage import DriveStore
from .weather import OpenMeteoWeatherClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DEFAULT_BATTERY_CAPACITY_KWH: Final[float] = 135.0
PARK_DEBOUNCE_SECONDS: Final[float] = 60.0
METERS_PER_MILE: Final[float] = 1609.344
METERS_TO_FEET: Final[float] = 3.28084
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
        # Older schema fallback
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

    tracker_id = (
        find_entity_id(["device_tracker"])
        or find_entity_id(["location"])
        or find_entity_id(["tracker"])
    )
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
) -> tuple[list[DriveRecord], dict[str, Any]]:
    """Synchronously reconstruct historical drive records from SQLite database."""
    conn = open_sqlite_readonly(db_path)
    try:
        entities = resolve_recorder_entities(conn, vin=vin, vehicle_id=vehicle_id)
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

        # Determine battery capacity
        pack_capacity = battery_capacity or DEFAULT_BATTERY_CAPACITY_KWH
        if "battery_capacity" in entities:
            cap_series = load_numeric_series("battery_capacity")
            if cap_series:
                pack_capacity = cap_series[-1][1]

        # 3. Reconstruct raw drive segments (shifts out of Park into Drive/Reverse until Park)
        raw_segments: list[dict[str, Any]] = []
        current_segment: dict[str, Any] | None = None

        for ts, state in raw_gear_series:
            gear = state.strip().lower()
            if gear in DRIVING_GEARS:
                if current_segment is None:
                    current_segment = {
                        "start_ts": ts,
                        "end_ts": ts,
                    }
                else:
                    current_segment["end_ts"] = ts
            elif gear in PARK_GEAR:
                if current_segment is not None:
                    current_segment["end_ts"] = ts
                    if current_segment["end_ts"] > current_segment["start_ts"]:
                        raw_segments.append(current_segment)
                    current_segment = None
            else:
                # Neutral / standby: keep drive segment alive if active
                if current_segment is not None:
                    current_segment["end_ts"] = ts

        if (
            current_segment is not None
            and current_segment["end_ts"] > current_segment["start_ts"]
        ):
            raw_segments.append(current_segment)

        if not raw_segments:
            return [], {}

        # 4. Apply 60-second Park debounce
        merged_segments: list[dict[str, Any]] = []
        for seg in raw_segments:
            if not merged_segments:
                merged_segments.append(dict(seg))
                continue

            last_seg = merged_segments[-1]
            gap = seg["start_ts"] - last_seg["end_ts"]

            if 0.0 <= gap <= PARK_DEBOUNCE_SECONDS:
                # Merge segments
                last_seg["end_ts"] = seg["end_ts"]
            else:
                merged_segments.append(dict(seg))

        # 5. Build DriveRecords from merged segments
        effective_vin = vin or "UNKNOWN_VIN"
        reconstructed_drives: list[DriveRecord] = []

        for seg in merged_segments:
            start_ts = seg["start_ts"]
            end_ts = seg["end_ts"]
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

            # Generate 3-minute drive segments for speed-bin efficiency analysis
            drive_segments: list[DriveSegment] = []
            segment_duration_s = 180.0  # 3 minutes
            t_curr = start_ts
            while t_curr < end_ts:
                t_next = min(t_curr + segment_duration_s, end_ts)
                dt_win = t_next - t_curr
                if dt_win < (segment_duration_s * 0.5):
                    break

                s_odo = _get_value_at_ts(odometer_series, t_curr, prefer="nearest")
                e_odo = _get_value_at_ts(odometer_series, t_next, prefer="nearest")
                if s_odo is not None and e_odo is not None:
                    d_odo = e_odo - s_odo
                    if s_odo > 50000.0 and d_odo > 50.0:
                        seg_dist = round(d_odo / METERS_PER_MILE, 2)
                    else:
                        seg_dist = round(max(0.0, d_odo), 2)
                else:
                    seg_dist = 0.0

                s_soc = _get_value_at_ts(soc_series, t_curr, prefer="nearest") or 0.0
                e_soc = _get_value_at_ts(soc_series, t_next, prefer="nearest") or s_soc
                seg_dsoc = max(0.0, s_soc - e_soc)
                seg_kwh = round((seg_dsoc * pack_capacity) / 100.0, 2)

                win_speeds = [s for t, s in speed_series if t_curr <= t <= t_next]
                if win_speeds:
                    seg_avg_spd = round(sum(win_speeds) / len(win_speeds), 1)
                elif seg_dist > 0 and dt_win > 0:
                    seg_avg_spd = round(seg_dist / (dt_win / 3600.0), 1)
                else:
                    seg_avg_spd = 0.0

                s_alt = _get_value_at_ts(alt_series, t_curr, prefer="nearest") or 0.0
                e_alt = _get_value_at_ts(alt_series, t_next, prefer="nearest") or s_alt
                seg_elev = round(e_alt - s_alt, 1)

                seg_bin = _get_speed_bin_key(seg_avg_spd)

                if seg_kwh > 0.0 and seg_dist >= 0.05:
                    seg_eff = round(seg_dist / seg_kwh, 2)
                    drive_segments.append(
                        DriveSegment(
                            start_time=datetime.fromtimestamp(
                                t_curr, tz=timezone.utc
                            ).isoformat(),
                            duration_seconds=round(dt_win, 1),
                            distance_miles=seg_dist,
                            energy_kwh=seg_kwh,
                            efficiency_mi_kwh=seg_eff,
                            avg_speed_mph=seg_avg_spd,
                            speed_bin=seg_bin,
                            elevation_change_ft=seg_elev,
                        )
                    )

                t_curr = t_next

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
                segments=drive_segments,
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
        )
        return reconstructed_drives, {
            "total_segments": len(raw_segments),
            "merged_drives": len(merged_segments),
            "vampire_events": vampire_events,
            "dcfc_sessions": dcfc_sessions,
        }
    finally:
        conn.close()


def reconstruct_dcfc_sessions_from_sqlite(
    conn: sqlite3.Connection,
    entity_map: dict[str, int],
    pack_capacity: float = DEFAULT_BATTERY_CAPACITY_KWH,
    vin: str | None = None,
    start_ts: float | None = None,
) -> list[ChargingSessionRecord]:
    """Extract historical DC Fast Charging sessions and power curves from recorder states."""
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

        # Deduplicate
        dedup: list[tuple[float, float]] = []
        for ts, soc in raw_soc:
            if not dedup:
                dedup.append((ts, soc))
            elif abs(soc - dedup[-1][1]) >= 0.05 or (ts - dedup[-1][0]) >= 20.0:
                dedup.append((ts, soc))

        start_soc = dedup[0][1]
        end_soc = dedup[-1][1]
        delta_soc = end_soc - start_soc
        if delta_soc <= 1.0:
            continue

        energy_added = (delta_soc / 100.0) * pack_capacity
        avg_power = energy_added / (dur_sec / 3600.0)
        if avg_power < DCFC_MIN_POWER_KW:
            continue

        window_sec = 60.0
        samples: list[ChargingSample] = []
        max_power = 0.0
        for i in range(len(dedup)):
            ts_i, soc_i = dedup[i]
            past = [p for p in dedup if 0.0 < (ts_i - p[0]) <= window_sec]
            future = [f for f in dedup if 0.0 < (f[0] - ts_i) <= window_sec]

            t_start = past[0][0] if past else ts_i
            s_start = past[0][1] if past else soc_i
            t_end = future[-1][0] if future else ts_i
            s_end = future[-1][1] if future else soc_i

            dt = t_end - t_start
            dsoc = s_end - s_start
            if dt >= 30.0 and dsoc > 0.0:
                p_kw = (dsoc / 100.0 * pack_capacity) / (dt / 3600.0)
                p_kw = min(225.0, p_kw)
                if p_kw > max_power:
                    max_power = p_kw

                if not samples or (
                    abs(samples[-1].soc - soc_i) >= 0.2
                    and abs(samples[-1].power_kw - p_kw) >= 1.0
                ):
                    samples.append(
                        ChargingSample(
                            timestamp=datetime.fromtimestamp(
                                ts_i, tz=timezone.utc
                            ).isoformat(),
                            soc=round(soc_i, 1),
                            power_kw=round(p_kw, 1),
                            battery_temp_f=None,
                        )
                    )

        if max_power < DCFC_MIN_POWER_KW or not samples:
            continue

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
                is_dcfc=True,
                samples=samples,
            )
        )

    return dcfc_records


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
        )

    vampire_events: list[VampireDrainRecord] = _meta.get("vampire_events", [])
    dcfc_sessions: list[ChargingSessionRecord] = _meta.get("dcfc_sessions", [])

    # Weather Enrichment (Open-Meteo Historical Archive API - batched by grid and date range)
    default_lat = getattr(getattr(hass, "config", None), "latitude", None)
    default_lon = getattr(getattr(hass, "config", None), "longitude", None)

    for d in drives:
        if d.start_lat is None and default_lat is not None:
            d.start_lat = float(default_lat)
        if d.start_lon is None and default_lon is not None:
            d.start_lon = float(default_lon)

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
        if v.latitude is not None
        and v.longitude is not None
        and v.avg_temp_f is None
    ]

    if drives_needing_weather or vampire_needing_weather:
        # Group by grid coordinate (0.1 degree resolution ~11km grid)
        grid_map: dict[tuple[float, float], dict[str, list[Any]]] = {}
        for d in drives_needing_weather:
            if d.start_lat is not None and d.start_lon is not None:
                grid_key = (round(d.start_lat, 1), round(d.start_lon, 1))
                grid_map.setdefault(grid_key, {"drives": [], "vampire": []})["drives"].append(d)

        for v in vampire_needing_weather:
            if v.latitude is not None and v.longitude is not None:
                grid_key = (round(v.latitude, 1), round(v.longitude, 1))
                grid_map.setdefault(grid_key, {"drives": [], "vampire": []})["vampire"].append(v)

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
    if not dry_run and (drives or vampire_events or dcfc_sessions):
        target_store = store
        if target_store is None and hass is not None and vin:
            target_store = DriveStore(hass=hass, vin=vin)

        if target_store is not None:
            if drives:
                new_added = await target_store.async_save_drives_batch(drives)
                duplicates_skipped = len(drives) - new_added
            if vampire_events:
                await target_store.async_save_vampire_events(vampire_events)
            if dcfc_sessions:
                await target_store.async_save_dcfc_sessions(dcfc_sessions)

    result = {
        "drives_found": len(drives),
        "valid_drives": len(valid_drives),
        "micro_drives": len(micro_drives),
        "vampire_events_found": len(vampire_events),
        "dcfc_sessions_found": len(dcfc_sessions),
        "total_miles": total_miles,
        "total_kwh": total_kwh,
        "efficiency_mi_kwh": efficiency,
        "mpge": mpge,
        "duplicates_skipped": duplicates_skipped,
        "drives": drives,
        "vampire_events": vampire_events,
        "dcfc_sessions": dcfc_sessions,
    }

    _LOGGER.info(
        "Historical backfill complete for VIN %s: %d drives found (%d valid, %d micro), %d vampire events, %d DCFC sessions, %.2f mi, %.2f kWh, %.2f mi/kWh",
        vin,
        len(drives),
        len(valid_drives),
        len(micro_drives),
        len(vampire_events),
        len(dcfc_sessions),
        total_miles,
        total_kwh,
        efficiency,
    )
    return result
