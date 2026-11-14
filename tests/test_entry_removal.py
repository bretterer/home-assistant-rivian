"""Removing a config entry removes its drive analytics."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from custom_components.rivian import _async_remove_analytics, _entry_vins_meta_key
from custom_components.rivian.analytics_db import (
    DEFAULT_DB_RELATIVE_PATH,
    AnalyticsDatabase,
)
from custom_components.rivian.const import ATTR_ANALYTICS_DB, DOMAIN
from custom_components.rivian.drive_models import DriveRecord

VIN_A = "7PDSGABA1NN00000A"
VIN_B = "7PDSGABA1NN00000B"
VIN_SHARED = "7PDSGABA1NN0000SH"


def _drive(vin: str) -> DriveRecord:
    return DriveRecord(
        vin=vin,
        drive_id=f"{vin}_1",
        start_time="2026-03-01T09:00:00+00:00",
        end_time="2026-03-01T09:30:00+00:00",
        distance_miles=10.0,
        duration_seconds=1800.0,
        start_soc=80.0,
        end_soc=75.0,
        battery_capacity_kwh=135.0,
        energy_kwh=4.0,
    )


def _hass(mock_hass: Any, tmp_path: Any, entries: list[Any]) -> Any:
    mock_hass.config.path = lambda *parts: os.path.join(str(tmp_path), *parts)
    mock_hass.config_entries = SimpleNamespace(async_entries=lambda _domain: entries)
    mock_hass.data[DOMAIN] = {}
    return mock_hass


def _seed(hass: Any) -> AnalyticsDatabase:
    db = AnalyticsDatabase(hass)
    db.setup()
    for vin in (VIN_A, VIN_B, VIN_SHARED):
        db.upsert_drives(vin, [_drive(vin)])
        db.set_meta(f"drive_stats_version:{vin}", "1")
    db.set_meta(_entry_vins_meta_key("one"), f'["{VIN_A}", "{VIN_SHARED}"]')
    db.set_meta(_entry_vins_meta_key("two"), f'["{VIN_B}", "{VIN_SHARED}"]')
    return db


def _drive_vins(db: AnalyticsDatabase) -> set[str]:
    with db._lock:
        return {r[0] for r in db._conn.execute("SELECT vin FROM drives").fetchall()}


def test_delete_vin_clears_every_vin_table_and_its_meta(
    mock_hass: Any, analytics_db_path: str
) -> None:
    db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
    db.setup()
    try:
        for vin in (VIN_A, VIN_B):
            db.upsert_drives(vin, [_drive(vin)])
            db.set_meta(f"drive_stats_version:{vin}", "1")
        db.delete_vin(VIN_A)
        assert _drive_vins(db) == {VIN_B}
        assert db.get_meta(f"drive_stats_version:{VIN_A}") is None
        assert db.get_meta(f"drive_stats_version:{VIN_B}") == "1"
    finally:
        db.close()


async def test_removing_an_entry_deletes_only_its_vehicles(
    mock_hass: Any, tmp_path: Any
) -> None:
    one = SimpleNamespace(entry_id="one")
    two = SimpleNamespace(entry_id="two")
    hass = _hass(mock_hass, tmp_path, [one, two])
    db = _seed(hass)
    hass.data[DOMAIN][ATTR_ANALYTICS_DB] = db
    try:
        await _async_remove_analytics(hass, one)
        # Its own vehicle goes; one the other entry also records stays
        assert _drive_vins(db) == {VIN_B, VIN_SHARED}
        assert db.get_meta(_entry_vins_meta_key("one")) is None
        assert db.get_meta(_entry_vins_meta_key("two")) is not None
    finally:
        db.close()


async def test_removing_the_last_entry_deletes_the_database(
    mock_hass: Any, tmp_path: Any
) -> None:
    one = SimpleNamespace(entry_id="one")
    hass = _hass(mock_hass, tmp_path, [one])
    db = _seed(hass)
    hass.data[DOMAIN][ATTR_ANALYTICS_DB] = db
    path = hass.config.path(DEFAULT_DB_RELATIVE_PATH)
    assert os.path.exists(path)

    await _async_remove_analytics(hass, one)

    for suffix in ("", "-wal", "-shm"):
        assert not os.path.exists(f"{path}{suffix}")
    assert ATTR_ANALYTICS_DB not in hass.data[DOMAIN]
