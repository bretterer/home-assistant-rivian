"""Tests for the Efficiency page backend: headwind, air density, expected kWh,
the schema v12 conditions columns, the weather backfill and the WS payload."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any, Self
from zoneinfo import ZoneInfo

import pytest

from custom_components.rivian import drive_storage as drive_storage_module
from custom_components.rivian.analytics_db import SCHEMA_VERSION, AnalyticsDatabase
from custom_components.rivian.const import ATTR_DRIVE_STORE, DOMAIN
from custom_components.rivian.drive_conditions import (
    air_density,
    archive_samples,
    compute_condition_columns,
    headwind_component,
    make_headwind_fn,
    summarize_conditions,
)
from custom_components.rivian.drive_models import DriveChunk, DriveRecord
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.drive_track import DriveTrack, TrackPoint
from custom_components.rivian.energy_model import (
    DEFAULT_PARAMS,
    J_PER_KWH,
    METERS_PER_MILE,
    expected_battery_kwh,
    interval_battery_j,
    interval_features,
)
from custom_components.rivian.weather import OpenMeteoWeatherClient
from custom_components.rivian.websocket_api import _websocket_analytics_efficiency

BASE_T = (
    1_760_000_000.0  # 2025-10-09, inside any "last 365 days" test window? see below
)
VIN = "7PDSGABA8NN000000"
OTHER_VIN = "7PDSGABA8NN111111"
STEP_DEG = 0.0009  # ~100 m of latitude, i.e. 20 m/s at 5 s per fix


def _leg(
    start: tuple[float, float],
    d_lat: float,
    d_lon: float,
    n: int,
    t0: float,
    speed: float = 20.0,
) -> list[TrackPoint]:
    return [
        TrackPoint(
            t=t0 + i * 5.0,
            lat=start[0] + i * d_lat,
            lon=start[1] + i * d_lon,
            speed_mps=speed,
            alt_m=800.0,
        )
        for i in range(n)
    ]


def _north_track(n: int = 40, t0: float = BASE_T) -> DriveTrack:
    return DriveTrack(_leg((44.0, -116.0), STEP_DEG, 0.0, n, t0))


def _wind(speed: float, from_deg: float, t: float = BASE_T, **extra: float) -> dict:
    return {"t": t, "wind_speed_mph": speed, "wind_dir_deg": from_deg, **extra}


# --- headwind ---------------------------------------------------------------


def test_north_drive_into_a_north_wind_is_a_positive_headwind() -> None:
    hw = headwind_component(_north_track(), [_wind(20.0, 0.0)])
    assert hw == pytest.approx(20.0 * 0.75, rel=0.01)


def test_tailwind_is_negative_and_crosswind_is_about_zero() -> None:
    track = _north_track()
    assert headwind_component(track, [_wind(20.0, 180.0)]) == pytest.approx(
        -15.0, rel=0.01
    )
    assert headwind_component(track, [_wind(20.0, 90.0)]) == pytest.approx(0.0, abs=0.1)


def test_out_and_back_cancels() -> None:
    out = _leg((44.0, -116.0), STEP_DEG, 0.0, 30, BASE_T)
    back = _leg((out[-1].lat, -116.0), -STEP_DEG, 0.0, 30, BASE_T + 150.0)
    hw = headwind_component(DriveTrack(out + back[1:]), [_wind(20.0, 0.0)])
    assert hw == pytest.approx(0.0, abs=0.3)


def test_headwind_is_weighted_by_distance_not_by_fix_count() -> None:
    # 3 km north (into the wind) then 1 km east (crosswind): 3/4 of the route.
    north = _leg((44.0, -116.0), STEP_DEG, 0.0, 31, BASE_T)
    east = _leg((north[-1].lat, -116.0), 0.0, STEP_DEG / 0.7193, 11, BASE_T + 150.0)
    track = DriveTrack(north + east[1:])
    hw = headwind_component(track, [_wind(20.0, 0.0)])
    assert hw == pytest.approx(15.0 * 0.75, rel=0.05)


def test_headwind_none_without_wind_samples() -> None:
    assert headwind_component(_north_track(), []) is None
    assert headwind_component(_north_track(), [{"t": BASE_T, "temp_f": 60.0}]) is None


def test_nearest_in_time_sample_wins() -> None:
    track = _north_track(40)  # 195 s long
    samples = [_wind(20.0, 0.0, t=BASE_T), _wind(20.0, 180.0, t=BASE_T + 195.0)]
    hw = headwind_component(track, samples)
    # First half headwind, second half tailwind -> about zero.
    assert hw == pytest.approx(0.0, abs=1.0)


# --- air density --------------------------------------------------------------


def test_air_density_matches_the_standard_atmosphere() -> None:
    assert air_density(15.0, 1013.25, 0.0) == pytest.approx(1.225, abs=0.001)


def test_moist_air_is_lighter_and_thin_air_is_lighter() -> None:
    dry = air_density(25.0, 1000.0, 0.0)
    assert air_density(25.0, 1000.0, 100.0) < dry
    assert air_density(25.0, 920.0, 0.0) < dry
    assert air_density(-5.0, 1000.0, 0.0) > dry


# --- summary -----------------------------------------------------------------


def test_summarize_conditions_reduces_samples() -> None:
    samples = [
        _wind(
            10.0,
            0.0,
            temp_f=59.0,
            pressure_hpa=1013.25,
            humidity_pct=0.0,
            precip_mm=2.0,
        ),
        _wind(
            10.0,
            0.0,
            t=BASE_T + 195.0,
            temp_f=59.0,
            pressure_hpa=1013.25,
            humidity_pct=0.0,
            precip_mm=0.0,
        ),
    ]
    out = summarize_conditions(_north_track(40), samples, duration_s=1800.0)
    assert out["wind_speed_mph"] == pytest.approx(10.0, abs=0.1)
    assert out["wind_dir_deg"] == pytest.approx(0.0, abs=1.0) or out[
        "wind_dir_deg"
    ] == pytest.approx(360.0, abs=1.0)
    assert out["headwind_mph"] == pytest.approx(7.5, abs=0.1)
    assert out["precip_mm"] == pytest.approx(0.5)  # 1 mm/h mean x 0.5 h
    assert out["pressure_hpa"] == 1013.2 or out["pressure_hpa"] == pytest.approx(
        1013.25, abs=0.1
    )
    assert out["air_density"] == pytest.approx(1.225, abs=0.002)


def test_summarize_conditions_all_none_without_samples() -> None:
    out = summarize_conditions(_north_track(), [], duration_s=600.0)
    assert set(out.values()) == {None}


def test_opposing_legs_cancel_in_the_mean_wind_vector() -> None:
    out = _leg((44.0, -116.0), STEP_DEG, 0.0, 30, BASE_T)
    back = _leg((out[-1].lat, -116.0), -STEP_DEG, 0.0, 30, BASE_T + 150.0)
    samples = [_wind(10.0, 0.0, t=BASE_T + 70.0), _wind(10.0, 180.0, t=BASE_T + 220.0)]
    s = summarize_conditions(DriveTrack(out + back[1:]), samples, duration_s=300.0)
    assert s["wind_speed_mph"] == pytest.approx(0.0, abs=1.0)


# --- expected energy -----------------------------------------------------------


def _distance_miles(track: DriveTrack) -> float:
    return sum(f.dist_m for f in interval_features(track)) / METERS_PER_MILE


def test_expected_kwh_is_unchanged_without_wind_or_density() -> None:
    track = _north_track()
    miles = _distance_miles(track)
    baseline = interval_battery_j(interval_features(track), DEFAULT_PARAMS) / J_PER_KWH
    assert expected_battery_kwh(track, DEFAULT_PARAMS, miles) == pytest.approx(baseline)
    # A zero headwind and the model's own density change nothing either.
    assert expected_battery_kwh(
        track,
        DEFAULT_PARAMS,
        miles,
        rho=DEFAULT_PARAMS.rho,
        headwind_fn=lambda a, b: 0.0,
    ) == pytest.approx(baseline)
    # And default interval_features are bit-identical to before.
    assert interval_features(track) == interval_features(track, headwind_fn=None)


def test_expected_kwh_rises_with_headwind_and_falls_with_tailwind() -> None:
    track = _north_track()
    miles = _distance_miles(track)
    calm = expected_battery_kwh(track, DEFAULT_PARAMS, miles)
    head = expected_battery_kwh(
        track,
        DEFAULT_PARAMS,
        miles,
        headwind_fn=make_headwind_fn(track, [_wind(20, 0)]),
    )
    tail = expected_battery_kwh(
        track,
        DEFAULT_PARAMS,
        miles,
        headwind_fn=make_headwind_fn(track, [_wind(20, 180)]),
    )
    assert head > calm > tail


def test_expected_kwh_rises_with_air_density() -> None:
    track = _north_track()
    miles = _distance_miles(track)
    thin = expected_battery_kwh(track, DEFAULT_PARAMS, miles, rho=0.95)
    thick = expected_battery_kwh(track, DEFAULT_PARAMS, miles, rho=1.30)
    assert thick > thin


def test_expected_kwh_none_when_gaps_leave_the_drive_uncovered() -> None:
    track = _north_track()
    assert (
        expected_battery_kwh(track, DEFAULT_PARAMS, _distance_miles(track) * 3) is None
    )


def test_compute_condition_columns_includes_expected_kwh() -> None:
    track = _north_track()
    cols = compute_condition_columns(
        track,
        [_wind(15.0, 0.0, temp_f=50.0, pressure_hpa=1000.0, humidity_pct=40.0)],
        DEFAULT_PARAMS,
        duration_s=195.0,
        distance_miles=_distance_miles(track),
    )
    assert cols["expected_kwh"] is not None and cols["expected_kwh"] > 0
    assert cols["headwind_mph"] == pytest.approx(11.2, abs=0.1)
    assert cols["air_density"] is not None


# --- weather parsing ----------------------------------------------------------------


class _Resp:
    def __init__(self, data: dict[str, Any], status: int = 200) -> None:
        self.status = status
        self._data = data

    async def json(self) -> dict[str, Any]:
        return self._data

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Session:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[dict[str, Any]] = []

    def get(
        self, url: str, params: dict[str, Any], timeout: Any, headers: Any = None
    ) -> _Resp:
        self.calls.append({"url": url, **params})
        return _Resp(self.data)


async def test_live_weather_parses_wind_pressure_precip_humidity() -> None:
    session = _Session(
        {
            "current": {
                "temperature_2m": 61.3,
                "wind_speed_10m": 12.4,
                "wind_direction_10m": 250,
                "surface_pressure": 921.5,
                "precipitation": 0.2,
                "relative_humidity_2m": 44,
            }
        }
    )
    client = OpenMeteoWeatherClient(session=session)  # type: ignore[arg-type]
    assert await client.async_get_current_temperature(39.8242, -105.0880) == 61.3
    # Same request serves the extras: no second HTTP call.
    cond = await client.async_get_current_conditions(39.8242, -105.0880)
    assert len(session.calls) == 1
    assert cond == {
        "wind_speed_mph": 12.4,
        "wind_dir_deg": 250.0,
        "pressure_hpa": 921.5,
        "precip_mm": 0.2,
        "humidity_pct": 44.0,
    }
    assert session.calls[0]["wind_speed_unit"] == "mph"
    assert "wind_speed_10m" in session.calls[0]["current"]


async def test_archive_weather_parses_hourly_conditions_and_tolerates_gaps() -> None:
    session = _Session(
        {
            "hourly": {
                "time": ["2026-08-20T14:00", "2026-08-20T15:00"],
                "temperature_2m": [70.0, 72.5],
                "wind_speed_10m": [5.0, None],
                "wind_direction_10m": [180, None],
                "surface_pressure": [920.0, 919.0],
                "precipitation": [0.0, 1.2],
                "relative_humidity_2m": [30, 35],
            }
        }
    )
    client = OpenMeteoWeatherClient(session=session)  # type: ignore[arg-type]
    hourly = await client.async_get_historical_conditions(
        39.8242, -105.0880, "2026-08-20", "2026-08-20"
    )
    assert hourly is not None
    assert hourly["2026-08-20T14:00"]["wind_speed_mph"] == 5.0
    assert "wind_speed_mph" not in hourly["2026-08-20T15:00"]
    assert hourly["2026-08-20T15:00"]["precip_mm"] == 1.2
    # The temperature-only API still works off the same cached request.
    temps = await client.async_get_historical_temperatures(
        39.8242, -105.0880, "2026-08-20", "2026-08-20"
    )
    assert temps == {"2026-08-20T14:00": 70.0, "2026-08-20T15:00": 72.5}
    assert len(session.calls) == 1


def test_archive_samples_bracket_the_drive() -> None:
    hourly = {
        "2026-08-20T12:00": {"wind_speed_mph": 1.0},
        "2026-08-20T13:00": {"wind_speed_mph": 2.0},
        "2026-08-20T14:00": {"wind_speed_mph": 3.0},
        "2026-08-20T15:00": {"wind_speed_mph": 4.0},
        "2026-08-20T17:00": {"wind_speed_mph": 5.0},
    }
    from datetime import datetime, timezone

    start = datetime(2026, 8, 20, 13, 30, tzinfo=timezone.utc).timestamp()
    out = archive_samples(hourly, start, start + 1800.0)
    assert [s["wind_speed_mph"] for s in out] == [2.0, 3.0, 4.0]


# --- schema v12 -------------------------------------------------------------------

_NEW_COLUMNS = {
    "wind_speed_mph",
    "wind_dir_deg",
    "headwind_mph",
    "precip_mm",
    "pressure_hpa",
    "humidity_pct",
    "air_density",
    "expected_kwh",
}


def _drive(
    drive_id: str,
    vin: str = VIN,
    start_ts: float = BASE_T,
    miles: float = 10.0,
    kwh: float = 4.0,
    **extra: Any,
) -> DriveRecord:
    from datetime import datetime, timezone

    return DriveRecord(
        vin=vin,
        drive_id=drive_id,
        start_time=datetime.fromtimestamp(start_ts, timezone.utc).isoformat(),
        end_time=datetime.fromtimestamp(start_ts + 1200, timezone.utc).isoformat(),
        distance_miles=miles,
        duration_seconds=1200.0,
        start_soc=80.0,
        end_soc=75.0,
        battery_capacity_kwh=135.0,
        energy_kwh=kwh,
        avg_speed_mph=30.0,
        integrated_temperature_f=55.0,
        start_lat=44.0,
        start_lon=-116.0,
        **extra,
    )


def test_v11_database_migrates_to_v12_keeping_rows(
    mock_hass: Any, analytics_db_path: str
) -> None:
    db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
    db.setup()
    db.upsert_drives(VIN, [_drive("old")])
    db.close()
    raw = sqlite3.connect(analytics_db_path)
    for column in _NEW_COLUMNS:
        raw.execute(f"ALTER TABLE drives DROP COLUMN {column}")
    raw.execute("PRAGMA user_version = 11")
    raw.commit()
    raw.close()

    db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
    db.setup()
    try:
        with db._lock:
            cols = {r[1] for r in db._conn.execute("PRAGMA table_info(drives)")}
            version = db._conn.execute("PRAGMA user_version").fetchone()[0]
            row = db._conn.execute(
                "SELECT drive_id, distance_miles, wind_speed_mph, expected_kwh "
                "FROM drives"
            ).fetchone()
        assert _NEW_COLUMNS <= cols
        assert version == SCHEMA_VERSION == 14
        assert (row["drive_id"], row["distance_miles"]) == ("old", 10.0)
        assert row["wind_speed_mph"] is None and row["expected_kwh"] is None
    finally:
        db.close()


def test_conditions_round_trip_and_are_add_only(analytics_db: Any) -> None:
    rec = _drive("a", wind_speed_mph=9.0, expected_kwh=3.5, air_density=1.1)
    analytics_db.upsert_drives(VIN, [rec])
    got = analytics_db.drives_since(VIN, 0, BASE_T + 10_000)[0]
    assert (got.wind_speed_mph, got.expected_kwh, got.air_density) == (9.0, 3.5, 1.1)
    # A re-upsert from a record that never computed conditions keeps them.
    analytics_db.upsert_drives(VIN, [_drive("a")])
    again = analytics_db.drives_since(VIN, 0, BASE_T + 10_000)[0]
    assert (again.wind_speed_mph, again.expected_kwh) == (9.0, 3.5)
    assert DriveRecord.from_dict(got.to_dict()).wind_speed_mph == 9.0


def test_finalize_computes_conditions_from_live_samples(analytics_db: Any) -> None:
    track = _north_track(60)
    rec = _drive(
        "live",
        miles=_distance_miles(track),
        weather_samples=[
            {
                "timestamp": "2025-10-09T08:53:20+00:00",
                "temp_f": 55.0,
                **_wind(
                    10.0, 0.0, pressure_hpa=920.0, humidity_pct=40.0, precip_mm=0.0
                ),
            }
        ],
    )
    analytics_db.finalize_drive(VIN, rec, track)
    got = analytics_db.drives_since(VIN, 0, BASE_T + 10_000)[0]
    assert got.headwind_mph == pytest.approx(7.5, abs=0.2)
    assert got.expected_kwh and got.expected_kwh > 0
    assert got.air_density and 1.0 < got.air_density < 1.2


# --- efficiency payload ---------------------------------------------------------------


def test_efficiency_data_rows_score_bands_and_trends(analytics_db: Any) -> None:
    chunk = DriveChunk(
        start_time="2025-10-09T08:00:00+00:00",
        duration_seconds=180.0,
        distance_miles=2.0,
        energy_kwh=0.8,
        efficiency_mi_kwh=2.5,
        avg_speed_mph=40.0,
        speed_bin="40-49",
    )
    good = _drive(
        "good",
        kwh=4.0,
        expected_kwh=3.0,
        headwind_mph=5.0,
        climb_ft=100.0,
        chunks=[chunk],
        drive_modes=["Sport"],
        trailer=False,
    )
    micro = _drive("micro", start_ts=BASE_T + 100, miles=0.2, kwh=0.1)
    bare = _drive("bare", start_ts=BASE_T + 200, kwh=5.0)
    analytics_db.upsert_drives(VIN, [good, micro, bare])

    tz = ZoneInfo("UTC")
    out = analytics_db.efficiency_data(VIN, None, tz)
    by_id = {d["drive_id"]: d for d in out["drives"]}
    assert set(by_id) == {"good", "bare"}  # micro excluded
    g = by_id["good"]
    assert g["efficiency_mi_kwh"] == pytest.approx(2.5)
    assert g["expected_eff_mi_kwh"] == pytest.approx(10.0 / 3.0, abs=0.001)
    assert g["score"] == pytest.approx(0.75)
    assert g["climb_ft_per_mi"] == pytest.approx(10.0)
    assert g["drive_mode"] == "Sport" and g["trailer"] is False
    assert g["headwind_mph"] == 5.0 and g["trip_length_mi"] == 10.0
    assert by_id["bare"]["score"] is None and by_id["bare"]["headwind_mph"] is None
    assert out["speed_bands"] == [
        {"band": "40-49", "miles": 2.0, "kwh": 0.8, "efficiency": 2.5}
    ]
    weekly = out["trend"]["weekly"]
    assert len(weekly) == 1 and weekly[0][3] == pytest.approx(20.0)
    assert weekly[0][1] == pytest.approx(20.0 / 9.0, abs=0.001)
    assert out["trend"]["monthly"][0][2] == pytest.approx(weekly[0][2])

    with_micro = analytics_db.efficiency_data(VIN, None, tz, include_micro=True)
    assert {d["drive_id"] for d in with_micro["drives"]} == {"good", "bare", "micro"}
    assert (
        analytics_db.efficiency_data(VIN, BASE_T + 150, tz)["drives"][0]["drive_id"]
        == "bare"
    )


class _FakeConnection:
    def __init__(self) -> None:
        self.results: dict[int, Any] = {}
        self.errors: dict[int, tuple[str, str]] = {}

    def send_result(self, msg_id: int, result: Any) -> None:
        self.results[msg_id] = result

    def send_error(self, msg_id: int, code: str, message: str) -> None:
        self.errors[msg_id] = (code, message)


class _FakeEffStore:
    def __init__(self, vin: str, drives: list[dict[str, Any]]) -> None:
        self.vin = vin
        self._drives = drives
        self.calls: list[tuple[Any, bool]] = []

    async def async_efficiency(
        self, days: Any, tz: Any, include_micro: bool = False
    ) -> dict[str, Any]:
        self.calls.append((days, include_micro))
        return {
            "drives": [dict(d) for d in self._drives],
            "speed_bands": [
                {"band": "40-49", "miles": 1.0, "kwh": 0.5, "efficiency": 2.0}
            ],
            "trend": {"weekly": [[1.0, 2.0, 100.0, 3.0]], "monthly": []},
        }


async def test_efficiency_command_merges_vehicles_and_tags_vin() -> None:
    a = _FakeEffStore(
        VIN, [{"drive_id": "a2", "date_ts": 20.0}, {"drive_id": "a1", "date_ts": 5.0}]
    )
    b = _FakeEffStore(OTHER_VIN, [{"drive_id": "b1", "date_ts": 10.0}])
    hass = SimpleNamespace(
        data={
            DOMAIN: {
                "entry": {ATTR_DRIVE_STORE: {a.vin: a, b.vin: b}},
            }
        }
    )
    conn = _FakeConnection()
    await _websocket_analytics_efficiency(
        hass, conn, {"id": 1, "vins": [VIN, OTHER_VIN], "days": 30}
    )
    res = conn.results[1]
    assert [(d["vin"], d["drive_id"]) for d in res["drives"]] == [
        (VIN, "a1"),
        (OTHER_VIN, "b1"),
        (VIN, "a2"),
    ]
    assert set(res["speed_bands"]) == {VIN, OTHER_VIN}
    assert res["trend"][VIN]["weekly"] == [[1.0, 2.0, 100.0, 3.0]]
    assert a.calls == [(30, False)]


async def test_efficiency_command_unknown_vin_is_not_found() -> None:
    hass = SimpleNamespace(data={DOMAIN: {"entry": {ATTR_DRIVE_STORE: {}}}})
    conn = _FakeConnection()
    await _websocket_analytics_efficiency(hass, conn, {"id": 1, "vins": [VIN]})
    assert conn.errors[1][0] == "not_found"


# --- weather backfill ------------------------------------------------------------------


class _FakeWeather:
    def __init__(self, hourly: dict[str, dict[str, float]] | None) -> None:
        self.hourly = hourly
        self.calls: list[tuple[float, float, str, str]] = []

    async def async_get_historical_conditions(
        self, lat: float, lon: float, start: str, end: str
    ) -> dict[str, dict[str, float]] | None:
        self.calls.append((lat, lon, start, end))
        return self.hourly


def _hourly_for(ts: float) -> dict[str, dict[str, float]]:
    from datetime import datetime, timezone

    out = {}
    for k in range(-2, 4):
        hour = datetime.fromtimestamp(ts + k * 3600, timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        out[hour.strftime("%Y-%m-%dT%H:%M")] = {
            "temp_f": 50.0,
            "wind_speed_mph": 10.0,
            "wind_dir_deg": 0.0,
            "pressure_hpa": 920.0,
            "precip_mm": 0.0,
            "humidity_pct": 40.0,
        }
    return out


async def _store_with_routed_drives(
    mock_hass: Any, analytics_db: Any, vin: str, *, demo: bool = False
) -> tuple[DriveStore, float]:
    import time

    now = time.time()
    track = _north_track(60, t0=now - 3 * 86400)
    miles = _distance_miles(track)
    rec = _drive("r1", vin=vin, start_ts=now - 3 * 86400, miles=miles)
    rec2 = _drive(
        "r2", vin=vin, start_ts=now - 2 * 86400, miles=miles, wind_speed_mph=99.0
    )
    analytics_db.upsert_drives(vin, [rec, rec2])
    analytics_db.upsert_tracks(
        vin, [("r1", track), ("r2", _north_track(60, t0=now - 2 * 86400))]
    )
    if demo:
        analytics_db.set_meta(
            "demo_vehicles", json.dumps([{"vin": vin, "name": "D", "model": "R2"}])
        )
    store = DriveStore(mock_hass, vin, analytics_db, is_demo=demo)
    store._weather_seeded = True  # the test drives the backfill itself
    await store.async_load()
    return store, now


async def test_backfill_weather_fills_null_columns_only(
    mock_hass: Any, analytics_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        drive_storage_module, "WEATHER_BACKFILL_REQUEST_INTERVAL_S", 0.0
    )
    store, now = await _store_with_routed_drives(mock_hass, analytics_db, VIN)
    fake = _FakeWeather(_hourly_for(now - 3 * 86400) | _hourly_for(now - 2 * 86400))
    store._weather_client = fake  # type: ignore[assignment]

    result = await store.async_backfill_weather(30)

    assert result["drives"] == 2 and result["updated"] == 2
    assert result["complete"] is True
    assert len(fake.calls) == 1  # both drives share a tile and a window
    got = {d.drive_id: d for d in analytics_db.drives_since(VIN, 0, now + 10_000)}
    assert got["r1"].wind_speed_mph == pytest.approx(10.0, abs=0.1)
    assert got["r1"].headwind_mph == pytest.approx(7.5, abs=0.2)
    assert got["r1"].expected_kwh is not None and got["r1"].air_density is not None
    # r2 already had a wind speed: it is left exactly as it was.
    assert got["r2"].wind_speed_mph == 99.0
    assert got["r2"].headwind_mph is not None  # its NULL columns did fill

    # Nothing left to fill: a second run makes no request.
    again = await store.async_backfill_weather(30)
    assert again["drives"] == 0 and len(fake.calls) == 1


async def test_backfill_weather_still_scores_drives_when_the_archive_fails(
    mock_hass: Any, analytics_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        drive_storage_module, "WEATHER_BACKFILL_REQUEST_INTERVAL_S", 0.0
    )
    store, _now = await _store_with_routed_drives(mock_hass, analytics_db, VIN)
    store._weather_client = _FakeWeather(None)  # type: ignore[assignment]
    result = await store.async_backfill_weather(30)
    assert result["failed_requests"] == 1 and result["complete"] is False
    assert result["updated"] == 0


async def test_backfill_weather_skips_demo_vehicles(
    mock_hass: Any, analytics_db: Any
) -> None:
    store, now = await _store_with_routed_drives(
        mock_hass, analytics_db, "DEMO0R2EAGLE00001", demo=True
    )
    fake = _FakeWeather(_hourly_for(now - 3 * 86400))
    store._weather_client = fake  # type: ignore[assignment]
    result = await store.async_backfill_weather(30)
    assert result["drives"] == 0 and result["updated"] == 0 and fake.calls == []
    # Even called directly at the database level, a demo VIN is never touched.
    assert analytics_db.drives_for_weather_backfill("DEMO0R2EAGLE00001", 0.0) == []
    assert analytics_db.apply_weather_backfill("DEMO0R2EAGLE00001", [("r1", [])]) == 0


def test_demo_fixture_drives_carry_conditions() -> None:
    from pathlib import Path

    fixture = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components"
            / "rivian"
            / "demo"
            / "demo_data.json"
        ).read_text(encoding="utf-8")
    )
    drives = [d for v in fixture["vehicles"] for d in v["drives"]]
    assert len(drives) == 16
    for drive in drives:
        for key in _NEW_COLUMNS:
            assert drive[key] is not None, (drive["drive_id"], key)
        assert 0.9 < drive["air_density"] < 1.3
