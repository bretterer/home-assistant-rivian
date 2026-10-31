"""Tests for charging sessions (DC + AC), reference curves and battery analytics (Phase 3A)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.rivian import (
    battery_analytics,
    charge_curves,
    websocket_api as ws_api_module,
)
from custom_components.rivian.analytics_db import SCHEMA_VERSION, AnalyticsDatabase
from custom_components.rivian.const import ATTR_DRIVE_STORE, ATTR_VEHICLE, DOMAIN
from custom_components.rivian.drive_models import ChargingSample, ChargingSessionRecord
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.drive_tracker import DriveTracker
from custom_components.rivian.history_backfill import (
    _session_overlaps,
    reconstruct_dcfc_sessions_from_sqlite,
)

from tests.test_drive_tracker import TEST_VEHICLE_ID, TEST_VIN, MockVehicleCoordinator
from tests.test_history_backfill import _make_charging_states_conn

DENVER = ZoneInfo("America/Denver")

# -- charge_curves ----------------------------------------------------------------


def test_pack_for_maps_r2_and_r1_capacity_bands() -> None:
    assert charge_curves.pack_for("R2", 87.9) == "r2"
    assert charge_curves.pack_for("2027 R2", None) == "r2"
    assert charge_curves.pack_for("R1T", 105.0) == "standard"
    assert charge_curves.pack_for("R1S", 131.0) == "large"
    assert charge_curves.pack_for("R1T", 135.0) == "large"
    assert charge_curves.pack_for("R1T", 149.0) == "max"
    assert charge_curves.pack_for("R1T", None) == "large"
    # Gen 2 (model year 2025+, or a capacity only a Gen 2 pack has).
    assert charge_curves.pack_for("R1S", 92.0) == "gen2_standard"
    assert charge_curves.pack_for("R1S", 109.0) == "gen2_large"
    assert charge_curves.pack_for("R1T", 140.0, 2025) == "gen2_max"
    assert charge_curves.pack_for("R1T", 123.0, 2023) == "large"
    assert charge_curves.pack_for("R1T", None, 2026) == "gen2_large"
    assert charge_curves.pack_for("R1T", 105.0, 2024) == "standard"
    # R2 packs.
    assert charge_curves.pack_for("R2", 70.0) == "r2_standard"
    assert charge_curves.pack_for("R2", 87.5) == "r2"
    for key, ref in charge_curves.DCFC_REFERENCE_CURVES.items():
        assert ref["key"] == key and ref["label"] and ref["nominal_kwh"] > 0
        assert ref["generation"] in ("gen1", "gen2", "r2") and ref["models"]
        assert len(ref["x"]) == len(ref["y"])
    assert charge_curves.reference("gen2_max")["approximate"] is True
    assert charge_curves.reference("r2")["approximate"] is True
    assert charge_curves.reference("large")["approximate"] is False


def test_expected_minutes_integrates_energy_over_power() -> None:
    flat = {"x": [0, 100], "y": [100.0, 100.0], "capacity_kwh": 100.0}
    # 50 % of 100 kWh at 100 kW is 30 minutes; power scales time with capacity.
    assert charge_curves.expected_minutes(flat, 20, 70) == pytest.approx(30.0, rel=1e-3)
    assert charge_curves.expected_minutes(flat, 20, 70, 50.0) == pytest.approx(
        15.0, rel=1e-3
    )
    assert charge_curves.expected_avg_kw(flat, 20, 70) == pytest.approx(100.0, rel=1e-3)
    # A tapering curve is slower than its peak and faster than its tail.
    taper = {"x": [0, 100], "y": [200.0, 50.0], "capacity_kwh": 100.0}
    avg = charge_curves.expected_avg_kw(taper, 0, 100)
    assert 50.0 < avg < 200.0
    assert charge_curves.expected_minutes(taper, 70, 20) is None


def test_r2_reference_curve_matches_published_claim() -> None:
    r2 = charge_curves.reference("r2")
    minutes = charge_curves.expected_minutes(r2, 10, 80, r2["capacity_kwh"])
    assert minutes is not None and 27.0 <= minutes <= 31.0
    assert {"standard", "large", "max", "r2"} <= set(
        charge_curves.DCFC_REFERENCE_CURVES
    )


# -- battery_analytics ---------------------------------------------------------------


def test_session_counts_split_by_kind_and_band() -> None:
    sessions = [
        {"kind": "dc", "start_soc": 8.0, "end_soc": 80.0},
        {"kind": "dc", "start_soc": 25.0, "end_soc": 60.0},
        {"kind": "ac", "start_soc": 15.0, "end_soc": 95.0},
        {"kind": "ac", "start_soc": 50.0, "end_soc": 70.0},
    ]
    counts = battery_analytics.session_counts(sessions)
    assert counts["total"] == 4 and counts["dc"] == 2 and counts["ac"] == 2
    assert counts["ended_above_80"] == 1  # only 95; 80 is not above 80
    assert counts["ended_above_90"] == 1
    assert counts["started_below_20"] == 2
    assert counts["started_below_10"] == 1
    assert counts["dc_ended_above_80"] == 0
    assert counts["dc_ended_above_90"] == 0
    assert counts["dc_started_below_10"] == 1
    assert battery_analytics.session_counts([])["total"] == 0


def test_time_in_band_splits_a_ramp_at_the_edges() -> None:
    # 0 -> 100 % over 100 s ramp: 10 s below 10, 10 s in 10-20, 60 s in
    # 20-80, 10 s in 80-90, 10 s above 90.
    bands = battery_analytics.time_in_band([(0.0, 0.0), (100.0, 100.0)])
    assert bands == {
        "below_10": 0.1,
        "b10_20": 0.1,
        "b20_80": 0.6,
        "b80_90": 0.1,
        "above_90": 0.1,
    }
    flat = battery_analytics.time_in_band([(0.0, 50.0), (3600.0, 50.0)])
    assert flat["b20_80"] == 1.0
    assert battery_analytics.time_in_band([(0.0, 50.0)]) is None


def test_time_in_band_skips_gaps_longer_than_the_limit() -> None:
    points = [
        (0.0, 50.0),
        (600.0, 50.0),
        (600.0 + 10 * 3600, 5.0),
        (1200.0 + 10 * 3600, 5.0),
    ]
    bands = battery_analytics.time_in_band(points, max_gap_s=3600.0)
    # Only the two 10-minute flat stretches count; the 10 h jump is a gap.
    assert bands["b20_80"] == 0.5 and bands["below_10"] == 0.5


def test_synthesized_timeline_holds_flat_between_events() -> None:
    events = {
        "drives": [
            {
                "start_ts": 1000.0,
                "end_ts": 2000.0,
                "start_soc": 80.0,
                "end_soc": 70.0,
                "points": [],
            }
        ],
        "sessions": [
            {
                "start_ts": 5000.0,
                "end_ts": 6000.0,
                "start_soc": 70.0,
                "end_soc": 80.0,
                "kind": "ac",
                "points": [],
            }
        ],
    }
    points = battery_analytics.synthesize_timeline(events, 500.0, 8000.0)
    assert points[0] == (500.0, 80.0)  # held flat back to the window start
    assert (2000.0, 70.0) in points and (5000.0, 70.0) in points
    assert points[-1] == (8000.0, 80.0)  # held flat to the window end
    values = dict(points)
    assert values[6000.0] == 80.0
    assert battery_analytics.synthesize_timeline({}, 0.0, 10.0) == []


def test_synthesized_timeline_slopes_across_an_unrecorded_change() -> None:
    """A level that changed between events is a straight line, not a cliff."""
    events = {
        "drives": [],
        "sessions": [
            {
                "start_ts": 1000.0,
                "end_ts": 2000.0,
                "start_soc": 40.0,
                "end_soc": 80.0,
                "kind": "ac",
                "points": [],
            },
            {
                "start_ts": 10000.0,
                "end_ts": 11000.0,
                "start_soc": 20.0,
                "end_soc": 80.0,
                "kind": "dc",
                "points": [],
            },
        ],
    }
    points = battery_analytics.synthesize_timeline(events, 1000.0, 11000.0)
    # No point holding 80 % just before the second session's start.
    assert (9999.0, 80.0) not in points
    assert (2000.0, 80.0) in points and (10000.0, 20.0) in points
    ts = [t for t, _ in points]
    assert ts.index(10000.0) == ts.index(2000.0) + 1


def test_downsample_keeps_ends_and_extremes() -> None:
    points = [(float(i), 50.0) for i in range(5000)]
    points[2500] = (2500.0, 99.0)
    out = battery_analytics.downsample(points, 100)
    assert len(out) <= 100
    assert out[0] == points[0] and out[-1] == points[-1]
    assert (2500.0, 99.0) in out


def test_capacity_series_daily_max_percent_and_projected_range() -> None:
    day1 = datetime(2026, 9, 1, 12, tzinfo=DENVER).timestamp()
    day2 = datetime(2026, 9, 2, 12, tzinfo=DENVER).timestamp()
    rows = [
        {"ts": day1, "capacity_kwh": 134.0, "end_soc": 50.0, "end_range_mi": 130.0},
        {
            "ts": day1 + 600,
            "capacity_kwh": 135.0,
            "end_soc": 40.0,
            "end_range_mi": 104.0,
        },
        {"ts": day2, "capacity_kwh": 134.5, "end_soc": 5.0, "end_range_mi": 13.0},
    ]
    series = battery_analytics.capacity_series(rows, [], DENVER, 140.0)
    assert [p[1] for p in series["points"]] == [135.0, 134.5]
    assert series["original_kwh"] == 135.0
    assert series["pct_points"][0][1] == 100.0
    assert series["pct_points"][1][1] == pytest.approx(99.63, abs=0.01)
    # day 1: median of 260 and 260; day 2's 5 % is too low to project from.
    assert series["projected_range"] == [[series["points"][0][0], 260.0]]
    # The sensor's statistics extend the series; nominal stands in with no data.
    sensor = [(day1 - 86400.0, 136.0)]
    merged = battery_analytics.capacity_series(rows, sensor, DENVER, 140.0)
    assert merged["original_kwh"] == 136.0 and len(merged["points"]) == 3
    empty = battery_analytics.capacity_series([], [], DENVER, 87.9)
    assert empty["points"] == [] and empty["original_kwh"] == 87.9


# -- live AC sessions -------------------------------------------------------------------


def _tracker(mock_hass: Any, analytics_db: Any) -> tuple[Any, Any, Any, dict[str, Any]]:
    coordinator = MockVehicleCoordinator()
    store = DriveStore(mock_hass, TEST_VIN, analytics_db)
    tracker = DriveTracker(
        hass=mock_hass,
        entry=MagicMock(),
        coordinator=coordinator,  # type: ignore[arg-type]
        vehicle_info={
            "vin": TEST_VIN,
            "id": TEST_VEHICLE_ID,
            "battery_capacity": 135.0,
        },
        store=store,
    )
    base = datetime.now(UTC)
    clock = {"now": base}
    tracker._utcnow = lambda: clock["now"]  # type: ignore[method-assign]
    return coordinator, store, tracker, clock


async def _charge(
    coordinator: Any,
    clock: dict[str, Any],
    *,
    start_soc: float,
    end_soc: float,
    minutes: int,
    power: float | None,
    tick_s: int = 300,
) -> None:
    base = clock["now"]
    coordinator.set_telemetry(
        gear="park",
        battery_soc=start_soc,
        charger_state="charging_active",
        charger_power=power,
    )
    steps = max(1, minutes * 60 // tick_s)
    for i in range(1, steps + 1):
        clock["now"] = base + timedelta(seconds=i * tick_s)
        coordinator.set_telemetry(
            gear="park",
            battery_soc=start_soc + (end_soc - start_soc) * i / steps,
            charger_state="charging_active",
            charger_power=power,
        )
    clock["now"] = base + timedelta(minutes=minutes)
    coordinator.set_telemetry(
        gear="park", battery_soc=end_soc, charger_state="charging_complete"
    )
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_slow_session_is_kept_as_ac_with_location(
    mock_hass: Any, analytics_db: Any
) -> None:
    coordinator, store, tracker, clock = _tracker(mock_hass, analytics_db)
    await tracker.async_setup()

    await _charge(
        coordinator, clock, start_soc=40.0, end_soc=52.0, minutes=60, power=11.5
    )

    sessions = await store.async_list_charging_sessions()
    assert len(sessions) == 1
    session = sessions[0]
    assert session["kind"] == "ac" and session["is_dcfc"] is False
    assert session["source"] == "live"
    assert session["lat"] == pytest.approx(39.7392)
    assert session["lon"] == pytest.approx(-104.9903)
    assert session["start_soc"] == 40.0 and session["end_soc"] == 52.0
    assert session["max_power_kw"] < 22.0
    assert 2 <= len(session["samples"]) <= 20
    assert all(s["power_kw"] == 0.0 for s in session["samples"])
    # AC sessions never reach the DC series / hot cache.
    assert store.get_dcfc_sessions() == []


@pytest.mark.asyncio
async def test_estimated_ac_session_is_not_mistaken_for_dc(
    mock_hass: Any, analytics_db: Any
) -> None:
    """No power field: ~11 kW worth of SoC rise (0.1 % steps) stays AC."""
    coordinator, store, tracker, clock = _tracker(mock_hass, analytics_db)
    await tracker.async_setup()

    # 8 % of 135 kWh in 60 min = 10.8 kW, sampled every 30 s.
    await _charge(
        coordinator,
        clock,
        start_soc=40.0,
        end_soc=48.0,
        minutes=60,
        power=None,
        tick_s=30,
    )

    sessions = await store.async_list_charging_sessions()
    assert [s["kind"] for s in sessions] == ["ac"]
    assert sessions[0]["avg_power_kw"] == pytest.approx(10.8, abs=0.5)


@pytest.mark.asyncio
async def test_charging_blips_are_dropped(mock_hass: Any, analytics_db: Any) -> None:
    coordinator, store, tracker, clock = _tracker(mock_hass, analytics_db)
    await tracker.async_setup()

    # Under 1 % gained (even though it ran for ten minutes).
    await _charge(
        coordinator, clock, start_soc=50.0, end_soc=50.6, minutes=10, power=11.0
    )
    # Over 1 % but shorter than five minutes.
    await _charge(
        coordinator,
        clock,
        start_soc=50.0,
        end_soc=51.0,
        minutes=4,
        power=11.0,
        tick_s=60,
    )

    assert await store.async_list_charging_sessions() == []


@pytest.mark.asyncio
async def test_fast_charge_is_still_dc_and_in_the_hot_cache(
    mock_hass: Any, analytics_db: Any
) -> None:
    coordinator, store, tracker, clock = _tracker(mock_hass, analytics_db)
    await tracker.async_setup()

    await _charge(
        coordinator,
        clock,
        start_soc=20.0,
        end_soc=60.0,
        minutes=30,
        power=150.0,
        tick_s=60,
    )

    cached = store.get_dcfc_sessions()
    assert len(cached) == 1 and cached[0].kind == "dc" and cached[0].is_dcfc
    assert len(cached[0].samples) > 3  # a real curve


# -- backfill ------------------------------------------------------------------------------


def _ramp(
    t0: float, minutes: float, start: float, end: float
) -> tuple[list[Any], list[Any]]:
    duration_s = minutes * 60
    step = 30.0
    n = int(duration_s // step)
    soc_rows = [
        (round(start + (end - start) * i / n, 3), t0 + i * step) for i in range(n + 1)
    ]
    status_rows = [("on", t0 + i * 60.0) for i in range(int(duration_s // 60) + 1)] + [
        ("off", t0 + duration_s)
    ]
    return status_rows, soc_rows


def test_backfill_rebuilds_ac_sessions_with_location_and_classifies_dc() -> None:
    t0 = 1_700_000_000.0
    ac_status, ac_soc = _ramp(t0, 90, 40.0, 56.0)  # ~14 kW over 90 min on 135 kWh
    dc_t0 = t0 + 6 * 3600
    dc_status, dc_soc = _ramp(dc_t0, 30, 20.0, 70.0)  # ~135 kW
    conn = _make_charging_states_conn(ac_status + dc_status, ac_soc + dc_soc)
    try:
        sessions = reconstruct_dcfc_sessions_from_sqlite(
            conn=conn,
            entity_map={"charging_status": 1, "battery_level": 2},
            pack_capacity=135.0,
            vin=TEST_VIN,
            location=([(t0 - 600, 39.8242)], [(t0 - 600, -105.0880)]),
        )
    finally:
        conn.close()

    kinds = {s.kind for s in sessions}
    assert kinds == {"ac", "dc"}
    ac = next(s for s in sessions if s.kind == "ac")
    dc = next(s for s in sessions if s.kind == "dc")
    assert ac.source == "backfill" and dc.source == "backfill"
    assert ac.lat == 39.8242 and ac.lon == -105.0880
    assert ac.max_power_kw < 22.0 and len(ac.samples) <= 20
    assert dc.max_power_kw >= 22.0 and dc.samples


def test_backfill_drops_ac_blips() -> None:
    t0 = 1_700_000_000.0
    short_status, short_soc = _ramp(t0, 4, 40.0, 41.0)
    conn = _make_charging_states_conn(short_status, short_soc)
    try:
        # Too few SoC readings for a session at all, and under five minutes.
        assert (
            reconstruct_dcfc_sessions_from_sqlite(
                conn=conn,
                entity_map={"charging_status": 1, "battery_level": 2},
                pack_capacity=135.0,
                vin=TEST_VIN,
            )
            == []
        )
    finally:
        conn.close()


def test_backfill_session_overlap_dedupe_is_add_only() -> None:
    def rec(start: datetime, end: datetime) -> ChargingSessionRecord:
        return ChargingSessionRecord(
            session_id="x",
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            start_soc=40.0,
            end_soc=60.0,
            energy_added_kwh=27.0,
            max_power_kw=11.0,
            avg_power_kw=11.0,
            kind="ac",
        )

    base = datetime(2026, 9, 1, 18, tzinfo=UTC)
    stored = [(base.timestamp(), (base + timedelta(hours=2)).timestamp())]
    assert _session_overlaps(
        rec(base + timedelta(hours=1), base + timedelta(hours=3)), stored
    )
    assert not _session_overlaps(
        rec(base + timedelta(hours=5), base + timedelta(hours=6)), stored
    )
    assert not _session_overlaps(rec(base, base + timedelta(hours=1)), [])


# -- database: v11 migration, places, retention ------------------------------------------------------


def _insert_v10_session(
    raw: sqlite3.Connection, vin: str, sid: str, is_dcfc: int, ts: float
) -> None:
    raw.execute(
        "INSERT INTO dcfc_sessions (vin, session_id, start_time, end_time, start_ts, "
        "end_ts, created_ts, start_soc, end_soc, energy_added_kwh, max_power_kw, "
        "avg_power_kw, is_dcfc, sample_count, samples_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 20, 60, 50, 150, 100, ?, 0, '[]')",
        (
            vin,
            sid,
            "2026-09-01T00:00:00+00:00",
            "2026-09-01T00:30:00+00:00",
            ts,
            ts + 1800,
            ts,
            is_dcfc,
        ),
    )


def test_migration_v10_to_v11_preserves_rows_and_sets_kind_and_source(
    mock_hass: Any, analytics_db_path: str
) -> None:
    from tests.conftest import make_analytics_db  # type: ignore[import-not-found]

    make_analytics_db(mock_hass, analytics_db_path).close()
    raw = sqlite3.connect(analytics_db_path)
    raw.execute("DROP INDEX IF EXISTS ix_dcfc_kind")
    for column in ("lat", "lon", "place_id", "kind", "source"):
        raw.execute(f"ALTER TABLE dcfc_sessions DROP COLUMN {column}")
    _insert_v10_session(raw, "REALVIN", "r1", 1, 1000.0)
    _insert_v10_session(raw, "REALVIN", "r2", 0, 2000.0)
    _insert_v10_session(raw, "DEMOVIN", "d1", 1, 3000.0)
    raw.execute(
        "INSERT INTO meta(key, value) VALUES('demo_vehicles', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ('[{"vin": "DEMOVIN", "name": "Demo", "model": "R2"}]',),
    )
    raw.execute("PRAGMA user_version = 10")
    raw.commit()
    raw.close()

    db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
    db.setup()
    try:
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        rows = {
            r["session_id"]: r
            for r in db._conn.execute("SELECT * FROM dcfc_sessions").fetchall()
        }
        assert len(rows) == 3 and rows["r1"]["energy_added_kwh"] == 50
        assert (rows["r1"]["kind"], rows["r1"]["source"]) == ("dc", "live")
        assert (rows["r2"]["kind"], rows["r2"]["source"]) == ("ac", "live")
        assert (rows["d1"]["kind"], rows["d1"]["source"]) == ("dc", "demo")
        assert rows["r1"]["lat"] is None and rows["r1"]["place_id"] is None
    finally:
        db.close()


def test_upsert_assigns_place_in_the_vehicles_dataset(analytics_db: Any) -> None:
    analytics_db.create_place("real", 39.8242, -105.0880, "Home", 100.0, "home")
    analytics_db.create_place("demo", 39.6242, -104.8880, "Demo Home", 100.0, "home")
    analytics_db.set_meta(
        "demo_vehicles", '[{"vin": "DEMO", "name": "D", "model": "R2"}]'
    )

    def session(sid: str, lat: float, lon: float) -> ChargingSessionRecord:
        return ChargingSessionRecord(
            session_id=sid,
            start_time="2026-09-01T00:00:00+00:00",
            end_time="2026-09-01T01:00:00+00:00",
            start_soc=40.0,
            end_soc=60.0,
            energy_added_kwh=27.0,
            max_power_kw=11.0,
            avg_power_kw=10.0,
            kind="ac",
            lat=lat,
            lon=lon,
        )

    analytics_db.upsert_dcfc_sessions("REAL", [session("a", 39.8243, -105.0881)])
    analytics_db.upsert_dcfc_sessions("REAL", [session("b", 10.0, 10.0)])
    # The same spot in the demo dataset has no place there: the real one must not label it.
    analytics_db.upsert_dcfc_sessions("DEMO", [session("c", 39.8243, -105.0881)])
    analytics_db.upsert_dcfc_sessions("DEMO", [session("d", 39.6242, -104.8880)])

    by_id = {
        s["session_id"]: s
        for vin in ("REAL", "DEMO")
        for s in analytics_db.list_charging_sessions(vin)
    }
    assert by_id["a"]["place"]["label"] == "Home"
    assert by_id["b"]["place"] is None
    assert by_id["c"]["place"] is None
    assert by_id["d"]["place"]["label"] == "Demo Home"


def test_prune_blanks_only_dc_curves_beyond_the_newest_fifty(analytics_db: Any) -> None:
    sample = ChargingSample(
        timestamp="2026-09-01T00:00:00+00:00", soc=50.0, power_kw=100.0
    )
    sessions = []
    for i in range(52):
        sessions.append(
            ChargingSessionRecord(
                session_id=f"dc{i}",
                start_time=f"2026-01-{(i % 28) + 1:02d}T00:00:00+00:00",
                end_time=f"2026-01-{(i % 28) + 1:02d}T00:30:00+00:00",
                start_soc=20.0,
                end_soc=60.0,
                energy_added_kwh=50.0,
                max_power_kw=150.0,
                avg_power_kw=100.0,
                samples=[sample],
            )
        )
    sessions.append(
        ChargingSessionRecord(
            session_id="ac-old",
            start_time="2020-01-01T00:00:00+00:00",
            end_time="2020-01-01T01:00:00+00:00",
            start_soc=40.0,
            end_soc=60.0,
            energy_added_kwh=27.0,
            max_power_kw=11.0,
            avg_power_kw=10.0,
            kind="ac",
            samples=[sample, sample],
        )
    )
    analytics_db.upsert_dcfc_sessions("V", sessions)
    analytics_db.prune("V", 0.0)

    rows = {
        r["session_id"]: r["sample_count"]
        for r in analytics_db._conn.execute(
            "SELECT session_id, sample_count FROM dcfc_sessions WHERE vin = 'V'"
        ).fetchall()
    }
    assert sum(1 for k, v in rows.items() if k.startswith("dc") and v == 0) == 2
    assert rows["ac-old"] == 2  # an AC session's coarse points are kept


# -- WebSocket ----------------------------------------------------------------------------------------


class _Conn:
    def __init__(self) -> None:
        self.results: dict[int, Any] = {}
        self.errors: dict[int, tuple[str, str]] = {}

    def send_result(self, msg_id: int, result: Any = None) -> None:
        self.results[msg_id] = result

    def send_error(self, msg_id: int, code: str, message: str) -> None:
        self.errors[msg_id] = (code, message)


class _Store:
    def __init__(
        self,
        vin: str,
        *,
        sessions: list[dict[str, Any]] | None = None,
        events: dict[str, Any] | None = None,
        capacity_rows: list[dict[str, Any]] | None = None,
        capacity: float = 135.0,
        history: list[dict[str, Any]] | None = None,
    ) -> None:
        self._history = history or []
        self.vin = vin
        self._sessions = sessions or []
        self._events = events or {"drives": [], "sessions": []}
        self._capacity_rows = capacity_rows or []
        self.last_drive = SimpleNamespace(battery_capacity_kwh=capacity)

    async def async_list_charging_sessions(
        self, since_ts: float | None = None, until_ts: float | None = None
    ) -> list[dict[str, Any]]:
        self.last_range = (since_ts, until_ts)
        return [
            s
            for s in self._sessions
            if (since_ts is None or s["end_ts"] >= since_ts)
            and (until_ts is None or s["start_ts"] <= until_ts)
        ]

    async def async_capacity_history(self) -> list[dict[str, Any]]:
        return self._history

    async def async_charging_session_intervals(self) -> list[tuple[float, float]]:
        return [(s["start_ts"], s["end_ts"]) for s in self._sessions]

    async def async_drive_end_times(
        self, start_ts: float, end_ts: float
    ) -> list[float]:
        return [
            d["end_ts"]
            for d in self._events.get("drives", [])
            if start_ts <= d["end_ts"] <= end_ts
        ]

    async def async_soc_events(self, start_ts: float, end_ts: float) -> dict[str, Any]:
        return self._events

    async def async_capacity_rows(self) -> list[dict[str, Any]]:
        return self._capacity_rows


def _hass(*stores: Any, models: list[dict[str, str]] | None = None) -> Any:
    domain: dict[str, Any] = {"entry": {ATTR_DRIVE_STORE: {s.vin: s for s in stores}}}
    if models is not None:
        # Keyed by Rivian's vehicle id, like the real entry data.
        domain["entry"][ATTR_VEHICLE] = {
            f"vehicle-{i}": {"vin": m["vin"], "model": m["model"]}
            for i, m in enumerate(models)
        }
    return SimpleNamespace(data={DOMAIN: domain}, bus=None)


def _stored(
    sid: str, kind: str, start: float, soc: tuple[float, float], minutes: float
) -> dict[str, Any]:
    return {
        "session_id": sid,
        "kind": kind,
        "start_ts": start,
        "end_ts": start + minutes * 60,
        "start_soc": soc[0],
        "end_soc": soc[1],
        "energy_added_kwh": 50.0,
        "max_power_kw": 150.0 if kind == "dc" else 11.0,
        "avg_power_kw": 100.0 if kind == "dc" else 10.0,
        "place": {"id": 1, "label": "Home"} if kind == "ac" else None,
        "lat": 39.8242,
        "lon": -105.0880,
        "samples": [
            {"timestamp": "2026-09-01T00:00:00+00:00", "soc": soc[0], "power_kw": 100.0}
        ]
        * 100,
    }


@pytest.mark.asyncio
async def test_ws_sessions_expected_counts_and_dc_only_curves() -> None:
    sessions = [
        _stored("ac1", "ac", 1000.0, (40.0, 80.0), 240),
        _stored("dc1", "dc", 5000.0, (10.0, 80.0), 29),
    ]
    store = _Store("R2VIN", sessions=sessions, capacity=87.9)
    hass = _hass(store, models=[{"vin": "R2VIN", "model": "R2"}])
    conn = _Conn()

    await ws_api_module._websocket_charging_sessions(
        hass, conn, {"id": 1, "vins": ["R2VIN"]}
    )

    result = conn.results[1]
    ac, dc = result["sessions"]
    assert (ac["session_id"], dc["session_id"]) == ("ac1", "dc1")
    assert ac["samples"] == [] and ac["expected"] is None
    assert ac["place"] == {"id": 1, "label": "Home", "category": None}
    assert ac["duration_s"] == 240 * 60
    assert 0 < len(dc["samples"]) <= ws_api_module.MAX_DCFC_SAMPLES
    expected = dc["expected"]
    assert expected["pack"] == "r2" and expected["approximate"] is True
    assert expected["minutes"] == pytest.approx(29.0, abs=2.0)
    assert expected["pct_of_expected"] == pytest.approx(100.0, abs=8.0)
    counts = result["counts_by_vin"]["R2VIN"]
    assert counts["total"] == 2 and counts["dc"] == 1 and counts["ac"] == 1
    assert counts["ended_above_80"] == 0 and counts["started_below_10"] == 0
    assert counts["started_below_20"] == 1


@pytest.mark.asyncio
async def test_ws_reference_maps_pack_per_vehicle() -> None:
    r2 = _Store("R2VIN", capacity=87.9)
    r1 = _Store("R1VIN", capacity=135.0)
    hass = _hass(r2, r1, models=[{"vin": "R2VIN", "model": "R2"}])
    conn = _Conn()

    await ws_api_module._websocket_charging_reference(
        hass, conn, {"id": 1, "vins": ["R2VIN", "R1VIN"]}
    )

    ref = conn.results[1]
    assert ref["R2VIN"]["pack"] == "r2" and ref["R2VIN"]["approximate"] is True
    assert ref["R1VIN"]["pack"] == "large" and ref["R1VIN"]["approximate"] is False
    assert len(ref["R2VIN"]["curve"]["soc"]) == len(ref["R2VIN"]["curve"]["kw"]) > 5
    assert ref["R2VIN"]["capacity_kwh"] == 87.9


@pytest.mark.asyncio
async def test_ws_soc_timeline_uses_statistics_and_synthesizes_without(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = _Store("REAL")
    no_stats = _Store(
        "NOSTATS",
        events={
            "drives": [
                {
                    "start_ts": 1000.0,
                    "end_ts": 2000.0,
                    "start_soc": 80.0,
                    "end_soc": 60.0,
                    "points": [],
                }
            ],
            "sessions": [],
        },
    )
    hass = _hass(real, no_stats)

    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, unique_id: (
            "sensor.real_battery_level" if unique_id == "REAL-battery_level" else None
        )
    )
    monkeypatch.setattr(
        ws_api_module, "er", SimpleNamespace(async_get=lambda _h: registry)
    )
    calls: list[tuple[Any, ...]] = []

    async def fake_stats(
        _hass: Any, entity_id: str, start: float, end: float, period: str, stat: str
    ) -> list[tuple[float, float]]:
        calls.append((entity_id, period, stat, end - start))
        return [(start, 50.0), (start + 600.0, 50.0)]

    monkeypatch.setattr(ws_api_module, "async_entity_statistics", fake_stats)
    conn = _Conn()

    await ws_api_module._websocket_battery_soc_timeline(
        hass,
        conn,
        {"id": 1, "vins": ["REAL", "NOSTATS"], "start": 0.0, "end": 5 * 86400.0},
    )

    out = conn.results[1]
    assert out["series"]["REAL"]["source"] == "statistics"
    assert out["series"]["REAL"]["points"] == [[0, 50.0], [600, 50.0]]
    assert out["time_in_band"]["REAL"]["b20_80"] == 1.0
    assert calls == [("sensor.real_battery_level", "5minute", "mean", 5 * 86400.0)]
    assert out["series"]["NOSTATS"]["source"] == "synthesized"
    # Starts at the first recorded event, not the open-ended request start.
    assert out["series"]["NOSTATS"]["points"][0] == [1000, 80.0]
    assert out["time_in_band"]["NOSTATS"] is not None
    # Flat statistics hold no charge; a vehicle without statistics is never "detected".
    assert out["detected"] == {"REAL": [], "NOSTATS": []}

    # A window longer than ten days reads hourly statistics.
    calls.clear()
    await ws_api_module._websocket_battery_soc_timeline(
        hass, conn, {"id": 2, "vins": ["REAL"], "start": 0.0, "end": 30 * 86400.0}
    )
    assert calls[0][1] == "hour"


@pytest.mark.asyncio
async def test_ws_soc_timeline_fills_purged_5minute_statistics_with_hourly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zoom older than the recorder's 5-minute retention uses hourly
    statistics, not the drive/session estimate (which drew vertical jumps)."""
    real = _Store("REAL", events={"drives": [], "sessions": []})
    hass = _hass(real)
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, unique_id: (
            "sensor.real_battery_level"
        )
    )
    monkeypatch.setattr(
        ws_api_module, "er", SimpleNamespace(async_get=lambda _h: registry)
    )
    day = 86400.0
    calls: list[tuple[str, float, float]] = []

    async def fake_stats(
        _hass: Any, entity_id: str, start: float, end: float, period: str, stat: str
    ) -> list[tuple[float, float]]:
        calls.append((period, start, end))
        if period == "5minute":
            # Only the last day still has 5-minute statistics.
            return [(4 * day + 150.0, 60.0), (4 * day + 450.0, 61.0)]
        return [(t + 1800.0, 40.0 + t / day) for t in range(0, int(end), 3600)]

    monkeypatch.setattr(ws_api_module, "async_entity_statistics", fake_stats)
    conn = _Conn()
    await ws_api_module._websocket_battery_soc_timeline(
        hass, conn, {"id": 1, "vins": ["REAL"], "start": 0.0, "end": 5 * day}
    )
    out = conn.results[1]["series"]["REAL"]
    assert out["source"] == "statistics"
    assert calls == [("5minute", 0.0, 5 * day), ("hour", 0.0, 4 * day + 150.0)]
    ts = [p[0] for p in out["points"]]
    assert ts == sorted(ts) and ts[0] == 1800 and ts[-1] == int(4 * day + 450)

    # Nothing at 5-minute resolution at all: the whole window is hourly.
    calls.clear()

    async def no_fine(
        _hass: Any, entity_id: str, start: float, end: float, period: str, stat: str
    ) -> list[tuple[float, float]]:
        calls.append((period, start, end))
        return (
            []
            if period == "5minute"
            else [(start + 1800.0, 50.0), (start + 5400.0, 55.0)]
        )

    monkeypatch.setattr(ws_api_module, "async_entity_statistics", no_fine)
    await ws_api_module._websocket_battery_soc_timeline(
        hass, conn, {"id": 2, "vins": ["REAL"], "start": 0.0, "end": 2 * day}
    )
    out = conn.results[2]["series"]["REAL"]
    assert out["source"] == "statistics"
    assert out["points"] == [[1800, 50.0], [5400, 55.0]]
    assert calls == [("5minute", 0.0, 2 * day), ("hour", 0.0, 2 * day)]


@pytest.mark.asyncio
async def test_ws_soc_timeline_falls_back_when_statistics_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = _Store(
        "REAL",
        events={
            "drives": [
                {
                    "start_ts": 100.0,
                    "end_ts": 200.0,
                    "start_soc": 70.0,
                    "end_soc": 65.0,
                    "points": [],
                }
            ],
            "sessions": [],
        },
    )
    registry = SimpleNamespace(async_get_entity_id=lambda *_a: "sensor.x")
    monkeypatch.setattr(
        ws_api_module, "er", SimpleNamespace(async_get=lambda _h: registry)
    )

    async def no_stats(*_a: Any) -> list[tuple[float, float]]:
        return []

    monkeypatch.setattr(ws_api_module, "async_entity_statistics", no_stats)
    conn = _Conn()
    await ws_api_module._websocket_battery_soc_timeline(
        _hass(real), conn, {"id": 1, "vin": "REAL", "start": 0.0, "end": 1000.0}
    )
    assert conn.results[1]["series"]["REAL"]["source"] == "synthesized"


@pytest.mark.asyncio
async def test_ws_capacity_reads_history_with_temperatures_and_live_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day1 = datetime(2026, 9, 1, 12, tzinfo=DENVER).timestamp()
    day2 = day1 + 86400.0
    rows = [
        {"ts": day2, "capacity_kwh": 134.0, "end_soc": 50.0, "end_range_mi": 130.0},
    ]
    history = [
        {"day": "2026-09-01", "kwh": 135.0, "temp_f": 41.0, "temp_source": "battery"},
        {"day": "2026-09-02", "kwh": 134.0, "temp_f": 70.0, "temp_source": "outside"},
    ]
    store = _Store("REAL", capacity_rows=rows, capacity=134.0, history=history)
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, unique_id: (
            "sensor.cap" if unique_id == "REAL-battery_capacity" else None
        )
    )
    monkeypatch.setattr(
        ws_api_module, "er", SimpleNamespace(async_get=lambda _h: registry)
    )
    monkeypatch.setattr(
        ws_api_module.dt_util, "get_default_time_zone", lambda: DENVER, raising=False
    )
    hass = _hass(store)
    hass.states = SimpleNamespace(get=lambda _e: SimpleNamespace(state="133.5"))
    conn = _Conn()

    await ws_api_module._websocket_battery_capacity(
        hass, conn, {"id": 1, "vins": ["REAL"]}
    )

    out = conn.results[1]["REAL"]
    assert [p[1] for p in out["points"]][:2] == [135.0, 134.0]
    assert [p[2:] for p in out["points"]][:2] == [[41.0, "battery"], [70.0, "outside"]]
    assert out["points"][-1][1] == 133.5 and out["points"][-1][2] is None  # today
    assert out["original_kwh"] == 135.0
    assert out["pct_points"][1][1] == pytest.approx(99.26, abs=0.01)
    assert out["projected_range"][0][1] == 260.0
    assert out["pack"] == "large" and out["approximate"] is False


@pytest.mark.asyncio
async def test_ws_charging_commands_report_unknown_vin_as_not_found() -> None:
    conn = _Conn()
    await ws_api_module._websocket_charging_sessions(
        _hass(), conn, {"id": 1, "vins": ["NOPE"]}
    )
    assert conn.errors[1][0] == "not_found"


def test_charging_session_record_round_trips_and_derives_kind() -> None:
    rec = ChargingSessionRecord(
        session_id="s",
        start_time="2026-09-01T00:00:00+00:00",
        end_time="2026-09-01T01:00:00+00:00",
        start_soc=1.0,
        end_soc=2.0,
        energy_added_kwh=1.0,
        max_power_kw=1.0,
        avg_power_kw=1.0,
        is_dcfc=False,
    )
    assert rec.kind == "ac"
    again = ChargingSessionRecord.from_dict(rec.to_dict())
    assert again.kind == "ac" and again.is_dcfc is False and again.source == "live"
    legacy = ChargingSessionRecord.from_dict({"session_id": "old", "is_dcfc": True})
    assert legacy.kind == "dc" and legacy.lat is None


H = 3600.0


def test_detect_charge_spans_finds_unrecorded_charges_and_classifies_them() -> None:
    # A slow overnight charge (40 -> 70 % over 8 h on 135 kWh = ~5 kW), then a
    # fast one (30 -> 70 % in 1 h = 54 kW), with flat stretches and a 0.1 % flicker.
    pts = [(0.0, 40.0), (H, 40.0)]
    pts += [(H + k * H, 40.0 + k * 3.75) for k in range(1, 9)]  # to 70 % at 9 h
    pts += [
        (10 * H, 69.9),
        (11 * H, 70.0),
        (12 * H, 30.0),
        (13 * H, 70.0),
        (14 * H, 70.0),
    ]
    spans = battery_analytics.detect_charge_spans(pts, [], 135.0)
    assert [(s["kind"], s["start_soc"], s["end_soc"]) for s in spans] == [
        ("ac", 40.0, 70.0),
        ("dc", 30.0, 70.0),
    ]
    assert spans[0]["start_ts"] == H and spans[0]["end_ts"] == 9 * H
    assert spans[0]["energy_added_kwh"] == 40.5
    assert 4.0 < spans[0]["avg_power_kw"] < 6.0
    assert spans[1]["avg_power_kw"] == 54.0


def test_detect_charge_spans_skips_recorded_and_small_rises() -> None:
    pts = [(0.0, 40.0), (H, 50.0), (2 * H, 60.0), (3 * H, 60.0), (4 * H, 61.5)]
    # Fully recorded: nothing detected; the 1.5 % creep after it is too small.
    assert battery_analytics.detect_charge_spans(pts, [(0.0, 2 * H)], 135.0) == []
    # Recorded only for its first hour: the uncovered remainder is detected.
    pts = [(k * H, 40.0 + 5 * k) for k in range(6)]  # 40 -> 65 % over 5 h
    spans = battery_analytics.detect_charge_spans(pts, [(0.0, 0.5 * H)], 135.0)
    assert len(spans) == 1
    assert spans[0]["start_soc"] == 45.0 and spans[0]["end_soc"] == 65.0
    # No capacity: still detected, as AC with no energy figure.
    spans = battery_analytics.detect_charge_spans(pts, [], None)
    assert spans[0]["kind"] == "ac" and spans[0]["energy_added_kwh"] is None


@pytest.mark.asyncio
async def test_ws_soc_timeline_reports_detected_charges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = _Store(
        "REAL",
        sessions=[{"start_ts": 20 * H, "end_ts": 21 * H, "kind": "dc"}],
    )
    hass = _hass(real)
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, unique_id: (
            "sensor.real_battery_level"
        )
    )
    monkeypatch.setattr(
        ws_api_module, "er", SimpleNamespace(async_get=lambda _h: registry)
    )

    async def fake_stats(*_a: Any) -> list[tuple[float, float]]:
        # An unrecorded overnight charge, then the recorded fast charge.
        return [
            (0.0, 40.0),
            (4 * H, 60.0),
            (8 * H, 75.0),
            (20 * H, 30.0),
            (21 * H, 70.0),
        ]

    monkeypatch.setattr(ws_api_module, "async_entity_statistics", fake_stats)
    conn = _Conn()
    await ws_api_module._websocket_battery_soc_timeline(
        hass, conn, {"id": 1, "vins": ["REAL"], "start": 0.0, "end": 2 * 86400.0}
    )
    detected = conn.results[1]["detected"]["REAL"]
    assert [(d["kind"], d["start_soc"], d["end_soc"]) for d in detected] == [
        ("ac", 40.0, 75.0)
    ]


def test_detect_charge_spans_ends_at_a_plateau_despite_a_late_flicker() -> None:
    # Hourly means: flat, a charge to 75 % that then rests at the limit for
    # ten hours, and a 0.1 % reading flicker afterwards.
    socs = [37.0, 37.2, 41.3, 45.5, 49.6, 53.6, 57.3, 60.9, 64.7, 68.7, 73.2, 75.0]
    socs += [75.0] * 9 + [75.1, 75.1]
    pts = [(k * H, v) for k, v in enumerate(socs)]
    (span,) = battery_analytics.detect_charge_spans(pts, [], 135.0)
    # The flat lead-in hour is trimmed and the end is where 75 % was reached.
    assert (span["start_ts"], span["start_soc"]) == (H, 37.2)
    assert (span["end_ts"], span["end_soc"]) == (11 * H, 75.0)


def test_detect_charge_spans_splits_two_charges_around_a_pause() -> None:
    # A charge to 83.4 %, four flat hours, then a second top-up to 91.2 %.
    socs = [65.1, 69.0, 73.0, 77.2, 81.1, 83.4, 83.4, 83.4, 83.4, 83.4, 89.1, 91.2]
    pts = [(k * H, v) for k, v in enumerate(socs)]
    spans = battery_analytics.detect_charge_spans(pts, [], 135.0)
    assert [(s["start_ts"] / H, s["end_ts"] / H) for s in spans] == [(0, 5), (9, 11)]
    assert [(s["start_soc"], s["end_soc"]) for s in spans] == [
        (65.1, 83.4),
        (83.4, 91.2),
    ]


M5 = 300.0


def test_detect_charge_spans_five_minute_data_splits_a_short_pause() -> None:
    # L2 at ~0.3 %/5 min for an hour, a 20-minute pause, then another hour.
    socs = [50.0 + 0.3 * k for k in range(13)]  # to 53.6
    socs += [53.6] * 4
    socs += [53.6 + 0.3 * k for k in range(1, 13)]  # to 57.2
    pts = [(k * M5, round(v, 1)) for k, v in enumerate(socs)]
    spans = battery_analytics.detect_charge_spans(pts, [], 135.0)
    assert [(s["start_soc"], s["end_soc"]) for s in spans] == [
        (50.0, 53.6),
        (53.6, 57.2),
    ]


def test_detect_charge_spans_level_1_is_not_chopped_by_flat_readings() -> None:
    # Level 1 (~1 %/h): 0.1 % readings that repeat for a step or two at a time.
    socs = [round(40.0 + int(k * 0.0875 * 10) / 10, 1) for k in range(73)]  # 6 h
    pts = [(k * M5, v) for k, v in enumerate(socs)]
    (span,) = battery_analytics.detect_charge_spans(pts, [], 135.0)
    assert span["start_soc"] <= 40.1 and span["end_soc"] >= 46.2
    assert span["kind"] == "ac" and span["avg_power_kw"] < 2.0


def test_detect_charge_spans_fast_rise_needs_a_drive_before_it() -> None:
    # Parked and flat, then the level jumps 11 % in 15 minutes when the car
    # wakes: it charged while not reporting, not at a fast charger.
    socs = [83.4] * 6 + [94.6, 94.6]
    pts = [(k * M5 * 3, v) for k, v in enumerate(socs)]  # 15-minute steps
    (span,) = battery_analytics.detect_charge_spans(pts, [], 135.0, drive_ends=[])
    assert span["kind"] == "ac" and span["unreported"] is True
    assert span["avg_power_kw"] is None
    # With a drive ending just before, the same rise is a fast charge.
    (fast,) = battery_analytics.detect_charge_spans(
        pts, [], 135.0, drive_ends=[pts[5][0] - 600.0]
    )
    assert fast["kind"] == "dc" and fast["unreported"] is False
    # Without drive information the rate alone decides (older callers).
    (rate_only,) = battery_analytics.detect_charge_spans(pts, [], 135.0)
    assert rate_only["kind"] == "dc"


def test_detect_charge_spans_joins_a_charge_the_car_slept_through() -> None:
    # 5-minute data: charging to 83.3 %, the reading freezes for five hours
    # (Home Assistant gets no updates; the car keeps charging), then 94.6 %.
    socs = [64.0 + 0.4 * k for k in range(49)]  # to 83.2 over 4 h
    socs += [83.3] * 60
    socs += [94.6, 94.6]
    pts = [(k * M5, round(v, 1)) for k, v in enumerate(socs)]
    (span,) = battery_analytics.detect_charge_spans(pts, [], 135.0, drive_ends=[])
    assert (span["start_soc"], span["end_soc"]) == (64.0, 94.6)
    assert span["kind"] == "ac" and span["unreported"] is True
    assert span["avg_power_kw"] is not None and span["avg_power_kw"] < 11.5
    # It ends when the rate before the gap (19.2 % in 4 h) reaches 94.6 %,
    # not at the next reading 9 h in.
    assert span["end_ts"] == pytest.approx(4 * H + 11.4 / 4.8 * H, abs=60)
    # A drive between the two rises means they are separate charges.
    two = battery_analytics.detect_charge_spans(pts, [], 135.0, drive_ends=[pts[80][0]])
    assert len(two) == 2
