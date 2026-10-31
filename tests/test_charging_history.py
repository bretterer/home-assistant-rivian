"""Tests for schema v13, the Rivian charging-history import, the OSM charger
lookup and the capacity history (charging_history.py, charger_lookup.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
import sqlite3
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.rivian import charger_lookup, charging_history
from custom_components.rivian.analytics_db import SCHEMA_VERSION, AnalyticsDatabase
from custom_components.rivian.drive_models import DriveRecord

VIN = "7PDSGABA8NN000000"
VEHICLE_ID = "vehicle-id-1"
DENVER = ZoneInfo("America/Denver")
T0 = datetime(2026, 9, 10, 18, 0, tzinfo=UTC).timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _drive(drive_id: str, end_ts: float, end_soc: float, **extra: Any) -> DriveRecord:
    return DriveRecord(
        vin=VIN,
        drive_id=drive_id,
        start_time=_iso(end_ts - 1200),
        end_time=_iso(end_ts),
        distance_miles=10.0,
        duration_seconds=1200.0,
        start_soc=end_soc + 5,
        end_soc=end_soc,
        battery_capacity_kwh=100.0,
        energy_kwh=4.0,
        avg_speed_mph=30.0,
        start_lat=44.0,
        start_lon=-104.7880,
        **extra,
    )


def _summary(**over: Any) -> dict[str, Any]:
    base = {
        "startInstant": _iso(T0 + 7200),
        "endInstant": _iso(T0 + 7200 + 3600),
        "totalEnergyKwh": 20.0,
        "rangeAddedKm": 80.0,
        "vendor": "Rivian Wall Charger",
        "chargerType": "AC",
        "isHomeCharger": True,
        "isPublic": False,
        "city": "Eagle",
        "transactionId": "txn-1",
        "vehicleId": VEHICLE_ID,
        "__typename": "CompletedSessionSummary",
    }
    return base | over


def _payload(*items: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"getCompletedSessionSummaries": list(items)}}


# -- schema v13 ---------------------------------------------------------------


def test_v12_database_migrates_to_v13_keeping_sessions(
    mock_hass: Any, analytics_db_path: str
) -> None:
    from custom_components.rivian.drive_models import ChargingSessionRecord

    db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
    db.setup()
    db.upsert_dcfc_sessions(
        VIN,
        [
            ChargingSessionRecord(
                session_id="old",
                start_time=_iso(T0),
                end_time=_iso(T0 + 600),
                start_soc=10,
                end_soc=40,
                energy_added_kwh=30,
                max_power_kw=150,
                avg_power_kw=100,
            )
        ],
    )
    db.close()
    raw = sqlite3.connect(analytics_db_path)
    raw.execute("DROP INDEX ux_dcfc_txn")
    for column in (
        "vendor",
        "network",
        "station_name",
        "station_version",
        "charger_max_kw",
        "is_home",
        "rivian_txn_id",
    ):
        raw.execute(f"ALTER TABLE dcfc_sessions DROP COLUMN {column}")
    raw.execute("DROP TABLE capacity_history")
    raw.execute("PRAGMA user_version = 12")
    raw.commit()
    raw.close()

    db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
    db.setup()
    try:
        with db._lock:
            cols = {r[1] for r in db._conn.execute("PRAGMA table_info(dcfc_sessions)")}
            version = db._conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master")}
        assert {"vendor", "network", "station_name", "station_version"} <= cols
        assert {"charger_max_kw", "is_home", "rivian_txn_id"} <= cols
        assert "capacity_history" in tables and version == SCHEMA_VERSION == 14
        rows = db.list_charging_sessions(VIN)
        assert [r["session_id"] for r in rows] == ["old"]
        assert rows[0]["vendor"] is None and rows[0]["is_home"] is None
    finally:
        db.close()


def test_rivian_txn_id_is_unique_per_vin(analytics_db: Any) -> None:
    from custom_components.rivian.drive_models import ChargingSessionRecord

    def rec(sid: str, txn: str | None) -> ChargingSessionRecord:
        return ChargingSessionRecord(
            session_id=sid,
            start_time=_iso(T0),
            end_time=_iso(T0 + 60),
            start_soc=1,
            end_soc=2,
            energy_added_kwh=1,
            max_power_kw=1,
            avg_power_kw=1,
            rivian_txn_id=txn,
        )

    analytics_db.upsert_dcfc_sessions(VIN, [rec("a", None), rec("b", None)])
    analytics_db.upsert_dcfc_sessions(VIN, [rec("c", "t1")])
    with pytest.raises(sqlite3.IntegrityError):
        analytics_db.upsert_dcfc_sessions(VIN, [rec("d", "t1")])
    # Another VIN may reuse it.
    analytics_db.upsert_dcfc_sessions("OTHER", [rec("e", "t1")])


# -- Rivian history import -------------------------------------------------------


def test_query_requests_only_the_allowed_fields() -> None:
    query = charging_history.SUMMARIES_QUERY
    assert "getCompletedSessionSummaries" in query
    for field in (
        "startInstant",
        "endInstant",
        "totalEnergyKwh",
        "rangeAddedKm",
        "vendor",
        "chargerType",
        "isHomeCharger",
        "isPublic",
        "city",
        "transactionId",
        "vehicleId",
    ):
        assert field in query
    low = query.lower()
    for forbidden in (
        "paid",
        "payment",
        "currency",
        "price",
        "cost",
        "account",
        "card",
    ):
        assert forbidden not in low
    assert charging_history.CHARGING_URL.endswith("/chrg/user/graphql")


def test_parse_summaries_is_defensive() -> None:
    items, fields, err = charging_history.parse_summaries(
        _payload(_summary(), {"startInstant": "garbage"}, "not a dict", {})
    )
    assert err is None and len(items) == 1
    assert "vendor" in fields and "__typename" not in fields
    assert items[0].energy_kwh == 20.0 and items[0].is_home is True
    _, _, err = charging_history.parse_summaries(
        {"errors": [{"message": "Cannot query field"}]}
    )
    assert err == "Cannot query field"
    assert charging_history.parse_summaries(None)[2] is not None
    assert charging_history.parse_summaries({"data": {}})[2] is not None


class _Client:
    pass


async def _run_import(
    mock_hass: Any, db: Any, payload: Any, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_graphql(_client: Any, url: str, body: dict[str, Any]) -> Any:
        calls.append((url, body))
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(charging_history, "_graphql", fake_graphql)
    result = await charging_history.async_import_rivian_history(
        mock_hass, _Client(), {VEHICLE_ID: VIN}, db
    )
    assert len(calls) == 1 and calls[0][0] == charging_history.CHARGING_URL
    assert calls[0][1]["query"] == charging_history.SUMMARIES_QUERY
    return result


async def test_import_enriches_matches_and_inserts_missing(
    mock_hass: Any,
    analytics_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from custom_components.rivian.drive_models import ChargingSessionRecord

    analytics_db.upsert_drives(VIN, [_drive("d1", T0, 40.0)])
    # A stored DC session 5 minutes off the Rivian summary's start.
    analytics_db.upsert_dcfc_sessions(
        VIN,
        [
            ChargingSessionRecord(
                session_id="live1",
                start_time=_iso(T0 + 300),
                end_time=_iso(T0 + 2400),
                start_soc=40,
                end_soc=70,
                energy_added_kwh=30.0,
                max_power_kw=150,
                avg_power_kw=90,
                kind="dc",
            )
        ],
    )
    matched = _summary(
        startInstant=_iso(T0),
        endInstant=_iso(T0 + 2100),
        totalEnergyKwh=33.3,
        vendor="Tesla",
        chargerType="DC",
        isHomeCharger=False,
        isPublic=True,
        transactionId="txn-dc",
    )
    missing = _summary(
        startInstant=_iso(T0 + 20000),
        endInstant=_iso(T0 + 20000 + 7200),
        totalEnergyKwh=25.0,
    )
    nosoc = _summary(
        startInstant=_iso(T0 - 90 * 86400),
        endInstant=_iso(T0 - 90 * 86400 + 3600),
        transactionId="txn-old",
    )
    caplog.set_level(logging.INFO)
    result = await _run_import(
        mock_hass, analytics_db, _payload(matched, missing, nosoc), monkeypatch
    )
    assert result["vehicles"][VIN] == {"matched": 1, "inserted": 1, "skipped": 1}
    probe = [r.message for r in caplog.records if "charging history:" in r.message]
    assert len(probe) == 1 and "3 summaries, 1 vehicles" in probe[0]
    assert "Eagle" not in probe[0] and "txn" not in probe[0]  # names only

    rows = {r["session_id"]: r for r in analytics_db.list_charging_sessions(VIN)}
    live = rows["live1"]
    assert live["vendor"] == "Tesla" and live["network"] == "Tesla Supercharger"
    assert live["station_version"] is None and live["is_home"] is False
    assert live["energy_added_kwh"] == 33.3 and live["rivian_txn_id"] == "txn-dc"
    new = rows["rivian-txn-1"]
    assert new["source"] == "rivian" and new["kind"] == "ac" and new["is_home"] is True
    assert new["vendor"] == "Rivian Wall Charger" and new["lat"] is None
    # SoC comes from the level the car had before (40 % after d1 -> session end 70).
    assert new["start_soc"] == 70.0 and new["end_soc"] == pytest.approx(95.0)
    assert new["energy_added_kwh"] == 25.0

    # Re-running is idempotent (matched by transaction id).
    again = await _run_import(
        mock_hass, analytics_db, _payload(matched, missing), monkeypatch
    )
    assert again["vehicles"][VIN]["inserted"] == 0
    assert len(analytics_db.list_charging_sessions(VIN)) == 2


async def test_graphql_error_logs_one_warning_and_stops(
    mock_hass: Any,
    analytics_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    result = await _run_import(
        mock_hass,
        analytics_db,
        {"errors": [{"message": "Cannot query field getCompletedSessionSummaries"}]},
        monkeypatch,
    )
    assert "error" in result and "vehicles" not in result
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Cannot query field" in warnings[0].message
    assert analytics_db.list_charging_sessions(VIN) == []

    caplog.clear()
    result = await _run_import(
        mock_hass, analytics_db, RuntimeError("boom"), monkeypatch
    )
    assert result == {"error": "boom"}
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_history_job_runs_at_most_once_a_day(
    mock_hass: Any, analytics_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def fake_graphql(*_a: Any) -> Any:
        nonlocal calls
        calls += 1
        return _payload()

    async def no_enrich(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(charging_history, "_graphql", fake_graphql)
    monkeypatch.setattr(charger_lookup, "async_enrich_sessions", no_enrich)
    args = (mock_hass, _Client(), {VEHICLE_ID: VIN}, analytics_db)
    await charging_history.async_run_history_job(*args)
    second = await charging_history.async_run_history_job(*args)
    assert second == {"skipped": True} and calls == 1
    await charging_history.async_run_history_job(*args, force=True)
    assert calls == 2


# -- OSM charger lookup ----------------------------------------------------------


def test_brand_normalization_and_labels() -> None:
    nb = charger_lookup.normalize_brand
    assert nb("Tesla, Inc.") == "tesla" and nb(None, "Tesla Supercharger") == "tesla"
    assert nb("Rivian Adventure Network") == "rivian" and nb("Rivian") == "rivian"
    assert nb("Electrify America") == "electrify_america"
    assert nb("ChargePoint Network") == "chargepoint"
    assert nb("EVgo") == "evgo" and nb("Blink Charging") == "blink"
    assert nb("Joe's Garage", None) == "other" and nb(None) == "other"
    assert charger_lookup.brand_label("tesla") == "Tesla Supercharger"
    assert charger_lookup.brand_for("Rivian Wall Charger", None, None, True) == (
        "home",
        "Home",
    )
    assert charger_lookup.brand_for(None, "Tesla Supercharger", None) == (
        "tesla",
        "Tesla Supercharger",
    )
    assert charger_lookup.brand_for("Acme Power", None, None) == ("other", "Acme Power")


def test_kw_parsing_and_tesla_versions() -> None:
    assert charger_lookup.parse_kw("250 kW") == 250.0
    assert charger_lookup.parse_kw("150000 W") == 150.0
    assert charger_lookup.parse_kw("1.2 MW") == 1200.0
    assert charger_lookup.parse_kw("62.5") == 62.5
    assert charger_lookup.parse_kw("150 kW;250 kW") == 250.0
    assert charger_lookup.parse_kw("fast") is None
    sv = charger_lookup.station_version
    assert sv("tesla", 150) == "V2" and sv("tesla", 250) == "V3"
    assert sv("tesla", 325) == "V4" and sv("tesla", None) is None
    assert sv("tesla", 150, "Supercharger V4 cabinet") == "V4"
    assert sv("rivian", None) is None and sv("evgo", 350) is None


_OVERPASS = {
    "elements": [
        {
            "type": "node",
            "id": 1,
            "lat": 39.7372,
            "lon": -105.1795,
            "tags": {
                "amenity": "charging_station",
                "operator": "Tesla, Inc.",
                "brand": "Tesla Supercharger",
                "name": "Meridian Supercharger",
                "socket:tesla_supercharger:output": "250 kW",
                "capacity": "12",
            },
        },
        {
            "type": "way",
            "id": 2,
            "center": {"lat": 39.8242, "lon": -105.2880},
            "tags": {"amenity": "charging_station", "operator": "Far Away"},
        },
    ]
}


def test_parse_stations_picks_nearest_and_normalizes() -> None:
    station = charger_lookup.parse_stations(_OVERPASS, 39.7369, -105.1792)
    assert station == {
        "brand": "tesla",
        "brand_label": "Tesla Supercharger",
        "operator": "Tesla, Inc.",
        "network": "Tesla Supercharger",
        "name": "Meridian Supercharger",
        "max_kw": 250.0,
        "version": "V3",
    }
    assert charger_lookup.parse_stations({"elements": []}, 1, 1) is None
    assert charger_lookup.parse_stations("junk", 1, 1) is None
    assert "around:150,39.73690,-105.17920" in charger_lookup.build_query(
        39.7369, -105.1792
    )
    assert charger_lookup.location_key(39.73694, -105.17921) == "chg:39.737,-105.179"


async def test_lookup_caches_results_and_enriches_sessions(
    mock_hass: Any, analytics_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.rivian import road_snap
    from custom_components.rivian.drive_models import ChargingSessionRecord

    def session(sid: str, start: float) -> ChargingSessionRecord:
        return ChargingSessionRecord(
            session_id=sid,
            start_time=_iso(start),
            end_time=_iso(start + 1800),
            start_soc=20,
            end_soc=60,
            energy_added_kwh=40,
            max_power_kw=200,
            avg_power_kw=100,
            kind="dc",
            lat=39.7369,
            lon=-105.1792,
        )

    analytics_db.upsert_dcfc_sessions(VIN, [session("a", T0), session("b", T0 + 9000)])
    queries: list[str] = []

    async def fake_overpass(_hass: Any, query: str) -> Any:
        queries.append(query)
        return _OVERPASS

    monkeypatch.setattr(road_snap, "async_overpass_json", fake_overpass)
    updated = await charger_lookup.async_enrich_sessions(mock_hass, analytics_db, VIN)
    assert updated == 2 and len(queries) == 1  # the second hit the cache
    rows = {r["session_id"]: r for r in analytics_db.list_charging_sessions(VIN)}
    assert rows["a"]["station_name"] == "Meridian Supercharger"
    assert rows["a"]["station_version"] == "V3" and rows["a"]["charger_max_kw"] == 250.0
    assert rows["a"]["network"] == "Tesla Supercharger"
    # Only the rounded location was sent.
    assert "39.73690" in queries[0] and VIN not in queries[0]

    # A failed fetch resolves nothing and is not cached; "nothing mapped" is.
    analytics_db.upsert_dcfc_sessions(VIN, [session("c", T0 + 20000)])
    analytics_db.update_session_fields(VIN, "c", {"lat": 40.0, "lon": -100.0})

    async def failing(_h: Any, _q: str) -> Any:
        return None

    monkeypatch.setattr(road_snap, "async_overpass_json", failing)
    assert await charger_lookup.async_enrich_sessions(mock_hass, analytics_db, VIN) == 0
    assert (
        analytics_db.get_cached_osm(charger_lookup.location_key(40.0, -100.0)) is None
    )

    async def empty(_h: Any, _q: str) -> Any:
        return {"elements": []}

    monkeypatch.setattr(road_snap, "async_overpass_json", empty)
    await charger_lookup.async_enrich_sessions(mock_hass, analytics_db, VIN)
    assert analytics_db.get_cached_osm(charger_lookup.location_key(40.0, -100.0)) == {
        "station": None
    }


# -- capacity history ------------------------------------------------------------


def test_merge_capacity_days_prefers_battery_temp_and_only_grows() -> None:
    d1 = datetime(2026, 9, 1, 12, tzinfo=DENVER).timestamp()
    inputs = {
        "2026-09-01": {"kwh": 134.0, "outside_f": 70.0},
        "2026-09-02": {"kwh": 133.9, "outside_f": 60.0, "battery_f": 80.0},
    }
    existing = [
        {
            "day": "2026-09-01",
            "kwh": 135.5,
            "temp_f": 50.0,
            "temp_source": "outside",
            "source": "statistics",
        },
    ]
    rows = charging_history.merge_capacity_days(
        existing, [(d1, 135.0), (d1 + 86400, 133.0)], inputs, DENVER
    )
    by_day = {r["day"]: r for r in rows}
    # Never shrinks a stored day; temp refreshed from the day's drives.
    assert by_day["2026-09-01"]["kwh"] == 135.5
    assert by_day["2026-09-01"]["temp_f"] == 70.0
    assert (
        by_day["2026-09-02"]["kwh"] == 133.9
        and by_day["2026-09-02"]["source"] == "drive"
    )
    assert by_day["2026-09-02"]["temp_source"] == "battery"
    assert by_day["2026-09-02"]["temp_f"] == 80.0
    # Unchanged rows are not rewritten.
    assert charging_history.merge_capacity_days(rows, [], inputs, DENVER) == []


async def test_capacity_job_seeds_from_all_statistics_and_drives(
    mock_hass: Any, analytics_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    day1 = datetime(2026, 8, 1, 12, tzinfo=DENVER).timestamp()
    analytics_db.upsert_drives(
        VIN,
        [
            _drive("d1", day1 + 86400 * 40, 50.0, integrated_temperature_f=75.0),
        ],
    )
    registry = SimpleNamespace(async_get_entity_id=lambda *_a: "sensor.cap")
    monkeypatch.setattr(
        charging_history, "er", SimpleNamespace(async_get=lambda _h: registry)
    )
    monkeypatch.setattr(
        charging_history.dt_util, "get_default_time_zone", lambda: DENVER, raising=False
    )
    seen: list[tuple[Any, ...]] = []

    async def fake_stats(
        _h: Any, entity_id: str, start: float, _end: float, period: str, stat: str
    ) -> list[tuple[float, float]]:
        seen.append((entity_id, start, period, stat))
        return [(day1, 135.0), (day1 + 86400, 134.9)]

    monkeypatch.setattr(charging_history, "async_entity_statistics", fake_stats)
    written = await charging_history.async_update_capacity_history(
        mock_hass, analytics_db, VIN
    )
    assert seen == [("sensor.cap", 0.0, "day", "max")]  # from the earliest available
    rows = analytics_db.capacity_history_rows(VIN)
    assert [r["day"] for r in rows][:2] == ["2026-08-01", "2026-08-02"]
    assert written == len(rows) == 3
    assert rows[-1]["source"] == "drive" and rows[-1]["temp_source"] == "outside"
    assert rows[-1]["temp_f"] == 75.0 and rows[-1]["kwh"] == 100.0
    # A second run changes nothing.
    assert (
        await charging_history.async_update_capacity_history(
            mock_hass, analytics_db, VIN
        )
        == 0
    )


def test_capacity_history_is_never_pruned_and_delete_vin_clears_it(
    analytics_db: Any,
) -> None:
    old = (datetime.now(UTC) - timedelta(days=2000)).strftime("%Y-%m-%d")
    analytics_db.upsert_capacity_history(
        VIN,
        [{"day": old, "kwh": 135.0, "temp_f": 40.0, "temp_source": "outside"}],
    )
    analytics_db.upsert_capacity_history(
        "OTHER", [{"day": old, "kwh": 80.0, "source": "demo"}]
    )
    analytics_db.prune(VIN, datetime.now(UTC).timestamp() - 30 * 86400)
    assert [r["kwh"] for r in analytics_db.capacity_history_rows(VIN)] == [135.0]
    analytics_db.delete_vin(VIN)
    assert analytics_db.capacity_history_rows(VIN) == []
    assert len(analytics_db.capacity_history_rows("OTHER")) == 1


def test_capacity_history_series_shape_and_live_value() -> None:
    from custom_components.rivian import battery_analytics

    history = [
        {"day": "2026-09-01", "kwh": 135.0, "temp_f": 41.0, "temp_source": "battery"},
        {"day": "2026-09-02", "kwh": 134.0, "temp_f": None, "temp_source": None},
    ]
    live_ts = datetime(2026, 9, 3, 9, tzinfo=DENVER).timestamp()
    out = battery_analytics.capacity_history_series(
        history, [], DENVER, 135.0, (live_ts, 133.5)
    )
    assert [p[1] for p in out["points"]] == [135.0, 134.0, 133.5]
    assert out["points"][0][2:] == [41.0, "battery"]
    assert out["points"][2][2:] == [None, None]
    assert out["original_kwh"] == 135.0 and out["pct_points"][2][1] == pytest.approx(
        98.89
    )
    empty = battery_analytics.capacity_history_series([], [], DENVER, 87.9)
    assert empty["points"] == [] and empty["original_kwh"] == 87.9


# -- WS payload ------------------------------------------------------------------


async def test_ws_sessions_payload_fields_and_filters() -> None:
    from custom_components.rivian import websocket_api as ws_api_module
    from custom_components.rivian.const import ATTR_DRIVE_STORE, DOMAIN

    def row(sid: str, kind: str, start: float, **extra: Any) -> dict[str, Any]:
        return {
            "session_id": sid,
            "kind": kind,
            "start_ts": start,
            "end_ts": start + 1800,
            "start_soc": 20.0,
            "end_soc": 60.0,
            "energy_added_kwh": 40.0,
            "max_power_kw": 150.0,
            "avg_power_kw": 80.0,
            "place": None,
            "lat": None,
            "lon": None,
            "samples": [],
            "source": "live",
        } | extra

    now = datetime.now(UTC).timestamp()
    sessions = [
        row(
            "old",
            "dc",
            now - 40 * 86400,
            network="Tesla Supercharger",
            vendor="Tesla",
            station_version="V3",
            charger_max_kw=250.0,
            station_name="Meridian",
        ),
        row("ea", "dc", now - 5 * 86400, vendor="Electrify America"),
        row("home", "ac", now - 2 * 86400, vendor="Rivian Wall Charger", is_home=True),
        row("anon", "dc", now - 1 * 86400),
    ]
    seen: list[tuple[Any, Any]] = []

    class Store:
        vin = "V"
        last_drive = SimpleNamespace(battery_capacity_kwh=135.0)

        async def async_list_charging_sessions(
            self, since: float | None = None, until: float | None = None
        ) -> list[dict[str, Any]]:
            seen.append((since, until))
            return [
                s
                for s in sessions
                if (since is None or s["end_ts"] >= since)
                and (until is None or s["start_ts"] <= until)
            ]

    class Conn:
        def __init__(self) -> None:
            self.results: dict[int, Any] = {}
            self.errors: dict[int, Any] = {}
            self.user = SimpleNamespace(is_admin=True)

        def send_result(self, i: int, r: Any = None) -> None:
            self.results[i] = r

        def send_error(self, i: int, c: str, m: str) -> None:
            self.errors[i] = (c, m)

    hass = SimpleNamespace(
        data={DOMAIN: {"entry": {ATTR_DRIVE_STORE: {"V": Store()}}}}, bus=None
    )
    conn = Conn()
    await ws_api_module._websocket_charging_sessions(
        hass, conn, {"id": 1, "vins": ["V"]}
    )
    out = {s["session_id"]: s for s in conn.results[1]["sessions"]}
    assert out["old"]["brand"] == "tesla" and out["old"]["station_version"] == "V3"
    assert out["old"]["charger_max_kw"] == 250.0
    assert out["old"]["station_name"] == "Meridian"
    assert out["ea"]["brand"] == "electrify_america"
    assert out["home"]["brand"] == "home" and out["home"]["brand_label"] == "Home"
    assert out["home"]["is_home"] is True
    assert out["anon"]["brand"] == "other" and out["anon"]["vendor"] is None

    conn = Conn()
    await ws_api_module._websocket_charging_sessions(
        hass,
        conn,
        {"id": 2, "vins": ["V"], "start": now - 10 * 86400, "end": now - 3 * 86400},
    )
    assert [s["session_id"] for s in conn.results[2]["sessions"]] == ["ea"]
    assert conn.results[2]["counts_by_vin"]["V"]["total"] == 1
    assert seen[-1] == (now - 10 * 86400, now - 3 * 86400)

    conn = Conn()
    await ws_api_module._websocket_charging_sessions(
        hass, conn, {"id": 3, "vins": ["V"], "brands": ["tesla", "home"]}
    )
    assert [s["session_id"] for s in conn.results[3]["sessions"]] == ["old", "home"]
    assert conn.results[3]["counts_by_vin"]["V"] == {
        **conn.results[3]["counts_by_vin"]["V"],
        "total": 2,
        "dc": 1,
        "ac": 1,
    }


def test_unlocated_sessions_take_the_end_of_the_drive_before_them(
    analytics_db: Any,
) -> None:
    """A session recorded without a position sits where the car last parked."""
    from custom_components.rivian.drive_models import ChargingSessionRecord

    analytics_db.upsert_drives(
        VIN,
        [
            _drive("near", T0 - 600, 40.0, end_lat=39.7342, end_lon=-105.1780),
            _drive(
                "far", T0 + 20000 - 3 * 3600, 40.0, end_lat=41.0242, end_lon=-104.8880
            ),
        ],
    )

    def session(sid: str, start: float) -> ChargingSessionRecord:
        return ChargingSessionRecord(
            session_id=sid,
            start_time=_iso(start),
            end_time=_iso(start + 1800),
            start_soc=40,
            end_soc=80,
            energy_added_kwh=50,
            max_power_kw=150,
            avg_power_kw=100,
            kind="dc",
        )

    analytics_db.upsert_dcfc_sessions(
        VIN, [session("s1", T0), session("s2", T0 + 20000)]
    )
    assert analytics_db.fill_session_locations_from_drives(VIN) == 1
    rows = {r["session_id"]: r for r in analytics_db.list_charging_sessions(VIN)}
    assert (rows["s1"]["lat"], rows["s1"]["lon"]) == (39.7342, -105.1780)
    # The only earlier drive ended 3 h before s2: too long ago to trust.
    assert rows["s2"]["lat"] is None


async def test_history_job_skips_station_lookups_when_place_naming_is_off(
    mock_hass: Any, analytics_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The place-naming option also turns off the OSM charging-station lookup."""
    calls: list[str] = []

    async def fake_enrich(_hass: Any, _db: Any, vin: str, limit: int = 20) -> int:
        calls.append(vin)
        return 0

    async def fake_import(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"vehicles": {}}

    monkeypatch.setattr(charger_lookup, "async_enrich_sessions", fake_enrich)
    monkeypatch.setattr(charging_history, "async_import_rivian_history", fake_import)
    off = await charging_history.async_run_history_job(
        mock_hass, object(), {"id1": VIN}, analytics_db, True, False
    )
    assert calls == [] and off["stations"] == {}
    await charging_history.async_run_history_job(
        mock_hass, object(), {"id1": VIN}, analytics_db, True, True
    )
    assert calls == [VIN]
