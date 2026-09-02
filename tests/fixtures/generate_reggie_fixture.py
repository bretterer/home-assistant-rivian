"""Deterministic Test Fixture Generator for R1S Reggie 10-Day Historical Recorder Database."""

from __future__ import annotations

import os
import random
import sqlite3
import sys
from typing import Any

TARGET_VIN = "7PDSGABA1NN000001"
TARGET_VEHICLE_NAME = "reggie"
TARGET_BATTERY_CAPACITY = 135.0

TARGET_VALID_DRIVES = 58
TARGET_TOTAL_MILES = 370.93
TARGET_TOTAL_KWH = 131.05


def generate_drives_distribution() -> tuple[
    list[dict[str, float]], list[dict[str, float]]
]:
    """Generate deterministic distance and kWh distribution for 58 valid drives and 4 micro-drives."""
    random.seed(20260902)

    # 1. Generate 58 valid drive distances
    weights = [random.uniform(1.0, 5.0) for _ in range(TARGET_VALID_DRIVES)]
    total_w = sum(weights)
    valid_dists = [round(w / total_w * TARGET_TOTAL_MILES, 2) for w in weights]
    diff_dist = round(TARGET_TOTAL_MILES - sum(valid_dists), 2)
    valid_dists[0] = round(valid_dists[0] + diff_dist, 2)

    # 2. Generate 58 valid drive energies
    eff_targets = [random.uniform(2.70, 2.95) for _ in range(TARGET_VALID_DRIVES)]
    raw_kwhs = [d / eff for d, eff in zip(valid_dists, eff_targets, strict=True)]
    total_raw_kwh = sum(raw_kwhs)
    valid_kwhs = [round(k / total_raw_kwh * TARGET_TOTAL_KWH, 2) for k in raw_kwhs]
    diff_kwh = round(TARGET_TOTAL_KWH - sum(valid_kwhs), 2)
    valid_kwhs[0] = round(valid_kwhs[0] + diff_kwh, 2)

    valid_drives = []
    for d, k in zip(valid_dists, valid_kwhs, strict=True):
        valid_drives.append({"distance_miles": d, "energy_kwh": k})

    # 4 micro-drives (< 0.5 miles)
    micro_drives = [
        {"distance_miles": 0.15, "energy_kwh": 0.05},
        {"distance_miles": 0.22, "energy_kwh": 0.08},
        {"distance_miles": 0.12, "energy_kwh": 0.04},
        {"distance_miles": 0.28, "energy_kwh": 0.10},
    ]

    return valid_drives, micro_drives


def create_reggie_sqlite_db(output_path: str) -> None:
    """Create the SQLite database populated with 10 days of historical recorder states."""
    if os.path.exists(output_path):
        os.remove(output_path)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    conn = sqlite3.connect(output_path)
    cur = conn.cursor()

    # Create HA schema tables
    cur.execute("""
        CREATE TABLE states_meta (
            metadata_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id VARCHAR(255) UNIQUE
        );
    """)

    cur.execute("""
        CREATE TABLE states (
            state_id INTEGER PRIMARY KEY AUTOINCREMENT,
            metadata_id INTEGER,
            state VARCHAR(255),
            attributes TEXT,
            event_id INTEGER,
            last_updated_ts REAL,
            last_changed_ts REAL,
            last_reported_ts REAL,
            FOREIGN KEY(metadata_id) REFERENCES states_meta(metadata_id)
        );
    """)

    cur.execute(
        "CREATE INDEX ix_states_metadata_id_last_updated_ts ON states (metadata_id, last_updated_ts);"
    )

    cur.execute("""
        CREATE TABLE schema_version (
            schema_version INTEGER PRIMARY KEY
        );
    """)
    cur.execute("INSERT INTO schema_version VALUES (35);")

    # Insert metadata
    entities = [
        (1, f"sensor.{TARGET_VEHICLE_NAME}_gear_selector"),
        (2, f"sensor.{TARGET_VEHICLE_NAME}_odometer"),
        (3, f"sensor.{TARGET_VEHICLE_NAME}_battery_level"),
        (4, f"sensor.{TARGET_VEHICLE_NAME}_speed"),
        (5, f"sensor.{TARGET_VEHICLE_NAME}_altitude"),
        (6, f"sensor.{TARGET_VEHICLE_NAME}_latitude"),
        (7, f"sensor.{TARGET_VEHICLE_NAME}_longitude"),
        (8, f"sensor.{TARGET_VEHICLE_NAME}_battery_capacity"),
    ]
    cur.executemany(
        "INSERT INTO states_meta (metadata_id, entity_id) VALUES (?, ?)", entities
    )

    # Telemetry tracking variables
    current_ts = 1786435200.0  # 2026-08-11 08:00:00 UTC
    current_odometer = 12450.0  # miles
    current_soc = 85.0  # %
    current_alt = 5280.0  # ft (Denver)
    base_lat = 39.7392
    base_lon = -104.9903

    # Initial battery capacity state
    cur.execute(
        "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
        (8, str(TARGET_BATTERY_CAPACITY), current_ts, current_ts),
    )

    valid_drives, micro_drives = generate_drives_distribution()

    # Interleave valid drives and micro drives across 10 days
    all_drives_plan: list[dict[str, Any]] = []
    micro_idx = 0
    for i, v_drive in enumerate(valid_drives):
        all_drives_plan.append({"type": "valid", "index": i, "data": v_drive})
        # Insert micro drives at intervals (e.g. after drives 5, 18, 32, 45)
        if i in (5, 18, 32, 45) and micro_idx < len(micro_drives):
            all_drives_plan.append(
                {"type": "micro", "index": micro_idx, "data": micro_drives[micro_idx]}
            )
            micro_idx += 1

    # Record initial parked state
    cur.execute(
        "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
        (1, "park", current_ts, current_ts),
    )
    cur.execute(
        "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
        (2, str(current_odometer), current_ts, current_ts),
    )
    cur.execute(
        "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
        (3, str(current_soc), current_ts, current_ts),
    )
    cur.execute(
        "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
        (4, "0.0", current_ts, current_ts),
    )
    cur.execute(
        "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
        (5, str(current_alt), current_ts, current_ts),
    )
    cur.execute(
        "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
        (6, str(base_lat), current_ts, current_ts),
    )
    cur.execute(
        "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
        (7, str(base_lon), current_ts, current_ts),
    )

    for item in all_drives_plan:
        d_info = item["data"]
        dist = d_info["distance_miles"]
        kwh = d_info["energy_kwh"]
        delta_soc = (kwh / TARGET_BATTERY_CAPACITY) * 100.0

        # Rest interval before drive (e.g. 1.5 - 4 hours)
        rest_seconds = random.uniform(5400, 14400)
        current_ts += rest_seconds

        # Charging simulation if SOC gets low (< 35%)
        if current_soc - delta_soc < 20.0:
            current_soc = min(88.0, current_soc + 55.0)
            cur.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
                (3, str(round(current_soc, 2)), current_ts - 1800, current_ts - 1800),
            )

        # Drive duration: average speed 25 - 45 mph
        avg_speed = random.uniform(28.0, 42.0)
        duration_s = max(45.0, (dist / avg_speed) * 3600.0)

        drive_start_ts = current_ts
        drive_end_ts = current_ts + duration_s

        # Special debounce test for Drive #12 (one of the valid drives)
        is_debounce_test = item["type"] == "valid" and item["index"] == 11

        if is_debounce_test:
            # Split Drive #12 into 2 segments with 35s Park in between
            seg1_dur = duration_s * 0.4
            seg2_dur = duration_s * 0.6
            park_gap = 35.0  # <= 60s debounce

            # Shift to Drive
            cur.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
                (1, "drive", drive_start_ts, drive_start_ts),
            )
            # Mid-point shift to Park
            t_park = drive_start_ts + seg1_dur
            cur.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
                (1, "park", t_park, t_park),
            )
            # Shift back to Drive within 35s
            t_resume = t_park + park_gap
            cur.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
                (1, "drive", t_resume, t_resume),
            )
            # Final shift to Park
            final_end_ts = t_resume + seg2_dur
            cur.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
                (1, "park", final_end_ts, final_end_ts),
            )
            drive_end_ts = final_end_ts
        else:
            # Standard drive start
            cur.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
                (1, "drive", drive_start_ts, drive_start_ts),
            )

        # Record start states
        cur.execute(
            "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
            (2, str(round(current_odometer, 2)), drive_start_ts, drive_start_ts),
        )
        cur.execute(
            "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
            (3, str(round(current_soc, 2)), drive_start_ts, drive_start_ts),
        )
        cur.execute(
            "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
            (5, str(round(current_alt, 1)), drive_start_ts, drive_start_ts),
        )

        # Speed samples during drive
        num_speed_samples = max(2, int(duration_s / 60.0))
        delta_t_sample = duration_s / num_speed_samples
        for s_idx in range(num_speed_samples):
            t_sample = drive_start_ts + (s_idx * delta_t_sample)
            speed_val = random.uniform(avg_speed * 0.7, min(75.0, avg_speed * 1.3))
            cur.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
                (4, str(round(speed_val, 1)), t_sample, t_sample),
            )

        # Update telemetry at end of drive
        current_odometer += dist
        current_soc = max(5.0, current_soc - delta_soc)
        current_alt += random.uniform(-60.0, 60.0)

        # Record end states
        if not is_debounce_test:
            cur.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
                (1, "park", drive_end_ts, drive_end_ts),
            )

        cur.execute(
            "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
            (2, str(round(current_odometer, 2)), drive_end_ts, drive_end_ts),
        )
        cur.execute(
            "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
            (3, str(round(current_soc, 2)), drive_end_ts, drive_end_ts),
        )
        cur.execute(
            "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
            (4, "0.0", drive_end_ts, drive_end_ts),
        )
        cur.execute(
            "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
            (5, str(round(current_alt, 1)), drive_end_ts, drive_end_ts),
        )
        cur.execute(
            "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
            (
                6,
                str(round(base_lat + random.uniform(-0.02, 0.02), 4)),
                drive_end_ts,
                drive_end_ts,
            ),
        )
        cur.execute(
            "INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts) VALUES (?, ?, ?, ?)",
            (
                7,
                str(round(base_lon + random.uniform(-0.02, 0.02), 4)),
                drive_end_ts,
                drive_end_ts,
            ),
        )

        current_ts = drive_end_ts

    conn.commit()
    conn.close()
    print(
        f"Generated {output_path} successfully ({len(valid_drives)} valid drives, {len(micro_drives)} micro-drives)."
    )


if __name__ == "__main__":
    target_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(os.path.dirname(__file__), "reggie_10day_history.db")
    )
    create_reggie_sqlite_db(target_path)
