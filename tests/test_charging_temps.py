"""Charging-session type, rates and temperatures (schema v14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3
from typing import Any

import pytest

from custom_components.rivian import battery_analytics, websocket_api as ws_api_module
from custom_components.rivian.analytics_db import _migrate_to_v14
from custom_components.rivian.drive_models import (
    AC_L1_MAX_KW,
    ChargingSample,
    ChargingSessionRecord,
)
from custom_components.rivian.drive_storage import DriveStore

from tests.test_charging_sessions import (
    _charge,
    _Conn,
    _hass,
    _Store,
    _stored,
    _tracker,
)
from tests.test_drive_tracker import TEST_VIN

H = 3600.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _session(
    sid: str, start_ts: float, hours: float, **kw: Any
) -> ChargingSessionRecord:
    return ChargingSessionRecord(
        session_id=sid,
        start_time=_iso(start_ts),
        end_time=_iso(start_ts + hours * H),
        start_soc=40.0,
        end_soc=70.0,
        energy_added_kwh=40.0,
        max_power_kw=7.0,
        avg_power_kw=6.0,
        kind=kw.pop("kind", "ac"),
        **kw,
    )


def test_record_round_trips_temperatures() -> None:
    rec = _session("s", 0.0, 1, outside_temp_f=61.5, battery_temp_f=88.0)
    back = ChargingSessionRecord.from_dict(rec.to_dict())
    assert (back.outside_temp_f, back.battery_temp_f) == (61.5, 88.0)
    assert (
        ChargingSessionRecord.from_dict(_session("t", 0.0, 1).to_dict()).outside_temp_f
        is None
    )


def test_migration_v14_adds_columns_idempotently() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE dcfc_sessions (id INTEGER PRIMARY KEY, vin TEXT)")
    _migrate_to_v14(conn)
    _migrate_to_v14(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dcfc_sessions)")}
    assert {"outside_temp_f", "battery_temp_f"} <= cols


def test_upsert_keeps_temperatures_and_lists_sessions_needing_weather(
    analytics_db: Any,
) -> None:
    now = datetime.now(UTC).timestamp()
    located = _session("a", now - 30 * H, 4, lat=39.8242, lon=-105.0880)
    unlocated = _session("b", now - 20 * H, 4)
    running = _session(
        "c", now - 1 * H, 4, lat=39.8242, lon=-105.0880
    )  # ends in the future
    analytics_db.upsert_dcfc_sessions(TEST_VIN, [located, unlocated, running])

    rows = analytics_db.sessions_needing_outside_temp(TEST_VIN, now)
    assert [r["session_id"] for r in rows] == ["a"]

    analytics_db.update_session_fields(TEST_VIN, "a", {"outside_temp_f": 55.2})
    assert analytics_db.sessions_needing_outside_temp(TEST_VIN, now) == []
    # A re-upsert from a record that never had the temperature keeps it.
    analytics_db.upsert_dcfc_sessions(TEST_VIN, [located])
    stored = {s["session_id"]: s for s in analytics_db.list_charging_sessions(TEST_VIN)}
    assert stored["a"]["outside_temp_f"] == 55.2
    assert stored["b"]["outside_temp_f"] is None


def test_mean_hourly_temp_window_and_nearest_hour() -> None:
    base = datetime(2026, 9, 26, 20, tzinfo=UTC).timestamp()
    hourly = {
        "2026-09-26T20:00": 60.0,
        "2026-09-26T21:00": 58.0,
        "2026-09-26T22:00": 56.0,
        "2026-09-27T09:00": 70.0,
    }
    # 20:00-22:00 covers three hours.
    assert battery_analytics.mean_hourly_temp(hourly, base, base + 2 * H) == 58.0
    # A 10-minute charge at 21:40 takes the 22:00 hour (within 30 min).
    assert (
        battery_analytics.mean_hourly_temp(hourly, base + 1.66 * H, base + 1.83 * H)
        == 56.0
    )
    # Nothing within reach.
    assert (
        battery_analytics.mean_hourly_temp(hourly, base + 5 * H, base + 5.2 * H) is None
    )


def test_peak_from_soc_samples_and_charge_type() -> None:
    t0 = datetime(2026, 9, 26, 20, tzinfo=UTC)
    samples = [
        {"timestamp": (t0 + timedelta(minutes=m)).isoformat(), "soc": soc}
        for m, soc in ((0, 40.0), (30, 43.0), (60, 47.0), (120, 50.0))
    ]
    # Fastest 30 min: 4 % of 135 kWh in 0.5 h = 10.8 kW.
    assert battery_analytics.peak_from_soc_samples(samples, 135.0, 1800.0) == 10.8
    assert battery_analytics.peak_from_soc_samples(samples[:1], 135.0) is None
    assert battery_analytics.peak_from_soc_samples(samples, None) is None
    assert battery_analytics.charge_type("dc", 150.0, AC_L1_MAX_KW) == "dc"
    assert battery_analytics.charge_type("ac", 7.2, AC_L1_MAX_KW) == "ac_l2"
    assert battery_analytics.charge_type("ac", 1.4, AC_L1_MAX_KW) == "ac_l1"
    assert battery_analytics.charge_type("ac", None, AC_L1_MAX_KW) == "ac_l2"


@pytest.mark.asyncio
async def test_ws_session_payload_has_type_rates_and_temperatures() -> None:
    l1 = _stored("l1", "ac", 1000.0, (40.0, 50.0), 600)
    l1["avg_power_kw"] = l1["max_power_kw"] = 1.4
    l1["samples"] = []
    l2 = _stored("l2", "ac", 90_000.0, (40.0, 80.0), 300)
    l2["samples"] = [
        {"timestamp": _iso(90_000.0 + m * 60), "soc": soc, "power_kw": 0.0}
        for m, soc in ((0, 40.0), (30, 46.0), (60, 50.0))
    ]
    l2["outside_temp_f"] = 48.4
    dc = _stored("dc", "dc", 200_000.0, (10.0, 80.0), 30)
    dc["samples"] = [
        {
            "timestamp": _iso(200_000.0),
            "soc": 10.0,
            "power_kw": 150.0,
            "battery_temp_f": 90.0,
        },
        {
            "timestamp": _iso(200_600.0),
            "soc": 40.0,
            "power_kw": 140.0,
            "battery_temp_f": 96.0,
        },
    ]
    store = _Store("V", sessions=[l1, l2, dc], capacity=135.0)
    conn = _Conn()
    await ws_api_module._websocket_charging_sessions(
        _hass(store, models=[{"vin": "V", "model": "R1T"}]),
        conn,
        {"id": 1, "vins": ["V"]},
    )
    by_id = {s["session_id"]: s for s in conn.results[1]["sessions"]}
    assert [by_id[k]["charge_type_label"] for k in ("l1", "l2", "dc")] == [
        "AC L1",
        "AC L2",
        "DC Fast",
    ]
    # The AC peak comes from the fastest stretch of its SoC trace (6 % in 30 min).
    assert by_id["l2"]["max_power_kw"] == pytest.approx(16.2)
    assert by_id["l2"]["outside_temp_f"] == 48.4
    assert by_id["l1"]["max_power_kw"] == 1.4 and by_id["l1"]["outside_temp_f"] is None
    # Battery temperature falls back to the DC samples' mean.
    assert by_id["dc"]["battery_temp_f"] == 93.0


class _FakeWeather:
    def __init__(self, archive: dict[str, Any] | None, recent: dict[str, float] | None):
        self.archive = archive
        self.recent = recent
        self.calls: list[str] = []

    async def async_get_historical_temperatures(self, *_a: Any) -> Any:
        self.calls.append("archive")
        return self.archive

    async def async_get_recent_hourly_temperatures(self, *_a: Any) -> Any:
        self.calls.append("recent")
        return self.recent


def _hour_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:00")


@pytest.mark.asyncio
async def test_fill_session_temperatures_uses_archive_then_recent(
    mock_hass: Any, analytics_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "custom_components.rivian.drive_storage.WEATHER_BACKFILL_REQUEST_INTERVAL_S",
        0.0,
    )
    now = datetime.now(UTC).timestamp()
    old_start = (now - 20 * 86400.0) // H * H
    new_start = (now - 1 * 86400.0) // H * H
    analytics_db.upsert_dcfc_sessions(
        TEST_VIN,
        [
            _session("old", old_start, 2, lat=39.8242, lon=-105.0880),
            _session("new", new_start, 2, lat=39.8242, lon=-105.0880),
        ],
    )
    store = DriveStore(mock_hass, TEST_VIN, analytics_db)
    weather = _FakeWeather(
        archive={_hour_key(old_start + h * H): 50.0 + h for h in range(3)},
        recent={_hour_key(new_start + h * H): 70.0 for h in range(3)},
    )
    store._weather_client = weather  # type: ignore[assignment]

    result = await store.async_fill_session_temperatures()

    assert result["updated"] == 2
    stored = {s["session_id"]: s for s in await store.async_list_charging_sessions()}
    assert stored["old"]["outside_temp_f"] == 51.0
    assert stored["new"]["outside_temp_f"] == 70.0
    # Nothing left to do on the next run.
    weather.calls.clear()
    assert (await store.async_fill_session_temperatures())["requests"] == 0


@pytest.mark.asyncio
async def test_live_session_records_mean_battery_temperature(
    mock_hass: Any, analytics_db: Any
) -> None:
    coordinator, store, tracker, clock = _tracker(mock_hass, analytics_db)
    await tracker.async_setup()
    # The mock rebuilds its data on every update, so serve the battery
    # temperature through `get` (cycling 78-84 F) instead.
    original_get = coordinator.get
    temps = iter([78.0, 80.0, 82.0, 84.0] * 20)

    def get(key: str) -> Any:
        if key == "batteryTemperature":
            return next(temps)
        return original_get(key)

    coordinator.get = get  # type: ignore[method-assign]
    await _charge(
        coordinator, clock, start_soc=40.0, end_soc=52.0, minutes=60, power=11.5
    )

    (session,) = await store.async_list_charging_sessions()
    assert session["battery_temp_f"] == pytest.approx(81.0, abs=1.5)


def test_samples_without_battery_temperature_stay_unknown() -> None:
    rec = _session(
        "x", 0.0, 1, samples=[ChargingSample("2026-09-01T00:00:00+00:00", 40.0, 0.0)]
    )
    assert rec.battery_temp_f is None
