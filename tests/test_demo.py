"""Tests for the synthetic demo vehicles (demo.py, the registry and its privacy rules)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.rivian import _iter_entry_datas, demo
from custom_components.rivian.const import (
    ATTR_ANALYTICS_DB,
    ATTR_DEMO_STORES,
    ATTR_DEMO_VEHICLES,
    DOMAIN,
)
from custom_components.rivian.drive_models import DriveRecord
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.websocket_api import _find_store

R2_VIN = "DEMO0R2EAGLE00001"
R1T_VIN = "DEMO1R1TEAGLE0002"
REAL_VIN = "7PDSGABA8NN000000"
DENVER = ZoneInfo("America/Denver")


@pytest.fixture
def fixture_data() -> dict[str, Any]:
    return demo.load_fixture()


def _count(db: Any, table: str, vin: str) -> int:
    return db._conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE vin = ?", (vin,)
    ).fetchone()[0]


def _local_dt(iso: str, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(iso).astimezone(tz)


def _hass_with_db(mock_hass: Any, db: Any) -> Any:
    mock_hass.data[DOMAIN] = {ATTR_ANALYTICS_DB: db}
    mock_hass.bus = MagicMock()
    return mock_hass


def _patch_install_environment(tz: ZoneInfo, now: datetime) -> Any:
    """Pin HA's time zone/now and stub the dashboard refresh."""
    return (
        patch.object(
            demo,
            "dt_util",
            SimpleNamespace(
                get_default_time_zone=lambda: tz,
                now=lambda: now.astimezone(tz),
            ),
        ),
        patch.object(demo, "async_refresh_after_change", new=AsyncMock()),
    )


async def _install(hass: Any, tz: ZoneInfo, now: datetime) -> list[dict[str, str]]:
    p_dt, p_refresh = _patch_install_environment(tz, now)
    with p_dt, p_refresh:
        result = await demo.async_install_demo(hass)
    # Let the stores' background seed tasks finish.
    await asyncio.sleep(0.2)
    return result


# -- fixture + pure builder ----------------------------------------------------


def test_fixture_loads(fixture_data: dict[str, Any]) -> None:
    assert fixture_data["household"]["name"] == "Demo household"
    vehicles = {v["vin"]: v for v in fixture_data["vehicles"]}
    assert set(vehicles) == {R2_VIN, R1T_VIN}
    assert len(vehicles[R2_VIN]["drives"]) == 7
    assert len(vehicles[R1T_VIN]["drives"]) == 9
    r1t_kinds = [s.get("kind", "dc") for s in vehicles[R1T_VIN]["charging_sessions"]]
    assert r1t_kinds.count("dc") >= 5
    assert r1t_kinds.count("ac") >= 10
    r2_kinds = [s.get("kind", "dc") for s in vehicles[R2_VIN]["charging_sessions"]]
    assert r2_kinds.count("dc") >= 5
    assert r2_kinds.count("ac") >= 1
    assert {p["name"] for p in fixture_data["places"]} >= {"Home", "Office"}


def test_newest_drive_lands_yesterday_in_local_tz(fixture_data: dict[str, Any]) -> None:
    today = date(2026, 10, 2)
    builds = demo.build_demo_records(fixture_data, DENVER, today)
    last_days = {
        _local_dt(d.start_time, DENVER).date() for b in builds for d in b.drives
    }
    assert max(last_days) == today - timedelta(days=1)
    # Relative spacing is preserved: the fixture spans more than a week.
    assert (max(last_days) - min(last_days)).days >= 7


def test_local_clock_times_survive_a_dst_change(fixture_data: dict[str, Any]) -> None:
    """Departure times stay at their local wall-clock hour across a DST change."""
    # Fall back (Nov 1, 2026): the fixture's days straddle it.
    today = date(2026, 11, 4)
    builds = demo.build_demo_records(fixture_data, DENVER, today)
    by_id = {d["drive_id"]: d for v in fixture_data["vehicles"] for d in v["drives"]}
    offsets = set()
    for build in builds:
        for record in build.drives:
            start_s = by_id[record.drive_id]["start_s"]
            local = _local_dt(record.start_time, DENVER)
            expected_minutes = round(start_s / 60)
            assert local.hour * 60 + local.minute == pytest.approx(
                expected_minutes, abs=1
            )
            offsets.add(local.utcoffset())
    # Both UTC offsets (MDT and MST) occur, so the check above was meaningful.
    assert len(offsets) == 2


def test_builder_is_pure_and_stable(fixture_data: dict[str, Any]) -> None:
    today = date(2026, 10, 2)
    a = demo.build_demo_records(fixture_data, DENVER, today)
    b = demo.build_demo_records(fixture_data, DENVER, today)
    assert [d.drive_id for x in a for d in x.drives] == [
        d.drive_id for x in b for d in x.drives
    ]
    # Tracks are SI epoch seconds inside the drive's own window.
    build = a[0]
    record = build.drives[0]
    track = dict(build.tracks)[record.drive_id]
    start = datetime.fromisoformat(record.start_time).timestamp()
    end = datetime.fromisoformat(record.end_time).timestamp()
    assert start <= track.points[0].t <= end + 1
    assert track.points[0].odo_m == pytest.approx(record.start_odometer_mi * 1609.344)


def test_each_vehicle_gets_only_the_places_it_visits(
    fixture_data: dict[str, Any],
) -> None:
    builds = {
        b.vin: b
        for b in demo.build_demo_records(fixture_data, DENVER, date(2026, 10, 2))
    }
    r2_places = {p.key for p in builds[R2_VIN].places}
    r1t_places = {p.key for p in builds[R1T_VIN].places}
    assert "library" in r2_places and "office" not in r2_places
    assert {"home", "office", "charger"} <= r1t_places


# -- install / remove ------------------------------------------------------------


async def test_install_writes_expected_data(mock_hass: Any, analytics_db: Any) -> None:
    hass = _hass_with_db(mock_hass, analytics_db)
    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

    installed = await _install(hass, DENVER, now)

    assert {v["vin"] for v in installed} == {R2_VIN, R1T_VIN}
    assert _count(analytics_db, "drives", R2_VIN) == 7
    assert _count(analytics_db, "drives", R1T_VIN) == 9
    assert _count(analytics_db, "dcfc_sessions", R1T_VIN) == 25
    assert _count(analytics_db, "dcfc_sessions", R2_VIN) == 24
    kinds = {
        (r["vin"], r["kind"], r["source"])
        for r in analytics_db._conn.execute(
            "SELECT vin, kind, source FROM dcfc_sessions"
        ).fetchall()
    }
    assert (R2_VIN, "dc", "demo") in kinds
    assert (R2_VIN, "ac", "demo") in kinds
    assert (R1T_VIN, "ac", "demo") in kinds
    # Every demo session is located at its place and labelled from the demo dataset.
    sessions = analytics_db.list_charging_sessions(R2_VIN)
    assert all(s["place"] and s["place"]["label"] for s in sessions)
    assert {s["place"]["label"] for s in sessions if s["kind"] == "ac"} == {"Home"}
    assert _count(analytics_db, "drive_tracks", R1T_VIN) == 9

    # Places belong to the demo dataset, shared by both demo cars (no dupes).
    places = analytics_db.list_places("demo")
    names = [p["name"] for p in places]
    assert {"Home", "Office"} <= set(names)
    assert len(names) == len(set(names))
    assert all(p["source"] == "user" for p in places)
    assert analytics_db.list_places("real") == []

    r1t_routes = analytics_db.list_routes("demo", [R1T_VIN])
    assert max(r["selected_count"] for r in r1t_routes) == 4  # Home -> Office x4
    r2_routes = analytics_db.list_routes("demo", [R2_VIN])
    assert max(r["selected_count"] for r in r2_routes) == 3  # Home -> Library x3
    assert analytics_db.list_routes("real") == []

    # Track-derived stats were computed for the stored routes.
    row = analytics_db._conn.execute(
        "SELECT moving_seconds FROM drives WHERE vin = ? LIMIT 1", (R2_VIN,)
    ).fetchone()
    assert row["moving_seconds"] is not None

    # Registry: persisted, in memory, and one detached demo store per vehicle.
    assert {v["vin"] for v in demo._read_registry(analytics_db)} == {R2_VIN, R1T_VIN}
    assert {v["name"] for v in demo.get_demo_vehicles(hass)} == {
        "Demo R2",
        "Demo R1T",
    }
    stores = hass.data[DOMAIN][ATTR_DEMO_STORES]
    assert set(stores) == {R2_VIN, R1T_VIN}
    assert all(s.is_demo for s in stores.values())
    # Every demo drive is over the 0.5 mi micro-drive threshold, so all count.
    assert stores[R2_VIN].drive_count == 7
    assert stores[R1T_VIN].drive_count == 9


async def test_rerun_replaces_instead_of_duplicating(
    mock_hass: Any, analytics_db: Any
) -> None:
    hass = _hass_with_db(mock_hass, analytics_db)
    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

    await _install(hass, DENVER, now)
    await _install(hass, DENVER, now + timedelta(days=3))  # shifted days, same ids

    assert _count(analytics_db, "drives", R2_VIN) == 7
    assert _count(analytics_db, "drives", R1T_VIN) == 9
    assert _count(analytics_db, "dcfc_sessions", R1T_VIN) == 25
    assert len(demo.get_demo_vehicles(hass)) == 2
    assert len(analytics_db.list_places(R1T_VIN)) == len(
        {p["name"] for p in analytics_db.list_places(R1T_VIN)}
    )


async def test_remove_deletes_only_demo_vins(mock_hass: Any, analytics_db: Any) -> None:
    hass = _hass_with_db(mock_hass, analytics_db)
    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    real = DriveRecord(
        vin=REAL_VIN,
        drive_id=f"{REAL_VIN}_1",
        start_time="2026-09-20T10:00:00+00:00",
        end_time="2026-09-20T10:30:00+00:00",
        distance_miles=12.3,
        duration_seconds=1800.0,
        start_soc=80.0,
        end_soc=74.0,
        battery_capacity_kwh=135.0,
        energy_kwh=8.1,
    )
    analytics_db.upsert_drives(REAL_VIN, [real])
    await _install(hass, DENVER, now)

    p_refresh = patch.object(demo, "async_refresh_after_change", new=AsyncMock())
    with p_refresh as refresh:
        removed = await demo.async_remove_demo(hass)
    refresh.assert_awaited_once()

    assert set(removed) == {R2_VIN, R1T_VIN}
    assert _count(analytics_db, "drives", R2_VIN) == 0
    assert _count(analytics_db, "drives", R1T_VIN) == 0
    # The last demo car takes the demo dataset's places/routes with it.
    assert analytics_db.list_places("demo") == []
    assert analytics_db.list_routes("demo") == []
    assert _count(analytics_db, "drives", REAL_VIN) == 1  # untouched
    assert demo.get_demo_vehicles(hass) == []
    assert hass.data[DOMAIN][ATTR_DEMO_STORES] == {}
    assert demo._read_registry(analytics_db) == []


async def test_remove_one_vin_keeps_the_other(
    mock_hass: Any, analytics_db: Any
) -> None:
    hass = _hass_with_db(mock_hass, analytics_db)
    await _install(hass, DENVER, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))

    with patch.object(demo, "async_refresh_after_change", new=AsyncMock()):
        removed = await demo.async_remove_demo(hass, R2_VIN)
        assert removed == [R2_VIN]
        # A real VIN is never removed by the demo path.
        assert await demo.async_remove_demo(hass, REAL_VIN) == []
        assert await demo.async_remove_demo_vehicle_history(hass, REAL_VIN) is False

    assert [v["vin"] for v in demo.get_demo_vehicles(hass)] == [R1T_VIN]
    assert _count(analytics_db, "drives", R1T_VIN) == 9


async def test_registry_is_restored_at_setup(mock_hass: Any, analytics_db: Any) -> None:
    hass = _hass_with_db(mock_hass, analytics_db)
    await _install(hass, DENVER, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))

    # A fresh Home Assistant run: no in-memory registry, only the database.
    fresh = _hass_with_db(type(mock_hass)(), analytics_db)
    await demo.async_setup_demo_registry(fresh, analytics_db)
    await asyncio.sleep(0.2)

    assert {v["name"] for v in demo.get_demo_vehicles(fresh)} == {
        "Demo R2",
        "Demo R1T",
    }
    assert set(fresh.data[DOMAIN][ATTR_DEMO_STORES]) == {R2_VIN, R1T_VIN}


# -- privacy: demo stores never see the user's zones ------------------------------


async def test_demo_store_ignores_zone_sync(mock_hass: Any, analytics_db: Any) -> None:
    hass = _hass_with_db(mock_hass, analytics_db)
    await _install(hass, DENVER, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    store = hass.data[DOMAIN][ATTR_DEMO_STORES][R1T_VIN]

    real_home_zone = {
        "entity_id": "zone.home",
        "name": "Home",
        "latitude": 40.0,
        "longitude": -100.0,
        "radius": 100.0,
        "icon": None,
    }
    await store.async_sync_zones([real_home_zone])

    sources = {p["source"] for p in analytics_db.list_places(R1T_VIN)}
    assert "zone" not in sources
    assert all(abs(p["lat"] - 40.0) > 1 for p in analytics_db.list_places(R1T_VIN))


async def test_demo_store_never_geocodes_or_snaps(
    mock_hass: Any, analytics_db: Any
) -> None:
    store = DriveStore(
        mock_hass, R2_VIN, analytics_db, place_geocoding=True, is_demo=True
    )
    assert store._place_geocoding is False
    assert (await store.async_geocode_places()) == {"geocoded": 0}


async def test_zone_resync_and_entry_iteration_skip_demo_stores(
    mock_hass: Any, analytics_db: Any
) -> None:
    hass = _hass_with_db(mock_hass, analytics_db)
    await _install(hass, DENVER, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    hass.data[DOMAIN]["entry1"] = {"drive_store": {}}

    entry_datas = _iter_entry_datas(hass)

    assert entry_datas == [{"drive_store": {}}]
    assert ATTR_DEMO_STORES.startswith("_")
    assert ATTR_DEMO_VEHICLES.startswith("_")


async def test_find_store_resolves_demo_stores(
    mock_hass: Any, analytics_db: Any
) -> None:
    hass = _hass_with_db(mock_hass, analytics_db)
    await _install(hass, DENVER, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    real_store = SimpleNamespace(vin=REAL_VIN)
    hass.data[DOMAIN]["entry1"] = {"drive_store": {"v1": real_store}}

    assert _find_store(hass, R2_VIN) is hass.data[DOMAIN][ATTR_DEMO_STORES][R2_VIN]
    assert _find_store(hass, REAL_VIN) is real_store
    assert _find_store(hass, "NOPE") is None


# -- the retired "Dashboard vehicle" select ---------------------------------------


def test_setup_removes_the_retired_dashboard_vehicle_select(mock_hass: Any) -> None:
    """Beta builds created the select; setup drops it from the entity registry."""
    from custom_components import rivian

    registry = MagicMock()
    registry.async_get_entity_id.return_value = "select.dashboard_vehicle"
    with patch.object(
        rivian, "er", MagicMock(async_get=MagicMock(return_value=registry))
    ):
        rivian._async_remove_retired_entities(
            mock_hass, SimpleNamespace(entry_id="entry1")
        )

    registry.async_get_entity_id.assert_called_once_with(
        "select", DOMAIN, "entry1-dashboard_vehicle"
    )
    registry.async_remove.assert_called_once_with("select.dashboard_vehicle")


# -- WebSocket: summary + delete -----------------------------------------------------


class _Connection:
    def __init__(self, admin: bool = True) -> None:
        self.user = SimpleNamespace(is_admin=admin)
        self.results: dict[int, Any] = {}
        self.errors: dict[int, tuple[str, str]] = {}

    def send_result(self, msg_id: int, result: Any = None) -> None:
        self.results[msg_id] = result

    def send_error(self, msg_id: int, code: str, message: str) -> None:
        self.errors[msg_id] = (code, message)


async def test_summary_for_demo_vin_carries_demo_block(
    mock_hass: Any, analytics_db: Any
) -> None:
    from custom_components.rivian import websocket_api as ws

    hass = _hass_with_db(mock_hass, analytics_db)
    await _install(hass, DENVER, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    connection = _Connection()

    await ws._websocket_analytics_summary(hass, connection, {"id": 1, "vin": R1T_VIN})

    result = connection.results[1]
    assert result["demo"] is True
    block = result["vehicle"]
    last = hass.data[DOMAIN][ATTR_DEMO_STORES][R1T_VIN].last_drive
    assert block["battery_pct"] == round(last.end_soc, 1)
    assert block["range_mi"] == round(last.end_range_mi, 1)
    assert block["odometer_mi"] == round(last.end_odometer_mi, 1)
    assert block["location"] == "Home"  # the R1T's last drive ends at Home
    assert result["last_drive"]["drive_id"] == last.drive_id


async def test_summary_for_a_real_vin_has_no_demo_block(mock_hass: Any) -> None:
    from custom_components.rivian import websocket_api as ws
    from custom_components.rivian.drive_models import AggregatedDriveStats

    class _Store:
        vin = REAL_VIN
        last_drive = None

        async def async_get_stats(self, days: Any) -> Any:
            return AggregatedDriveStats()

    mock_hass.data[DOMAIN] = {"entry1": {"drive_store": {"v1": _Store()}}}
    connection = _Connection()

    await ws._websocket_analytics_summary(
        mock_hass, connection, {"id": 1, "vin": REAL_VIN}
    )

    assert "demo" not in connection.results[1]
    assert "vehicle" not in connection.results[1]


async def test_delete_vehicle_history_on_a_demo_vin_removes_it_completely(
    mock_hass: Any, analytics_db: Any
) -> None:
    from custom_components.rivian import websocket_api as ws

    hass = _hass_with_db(mock_hass, analytics_db)
    await _install(hass, DENVER, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    connection = _Connection(admin=True)

    with patch.object(demo, "async_refresh_after_change", new=AsyncMock()) as refresh:
        await ws._websocket_analytics_delete_vehicle_history(
            hass, connection, {"id": 1, "vin": R2_VIN}
        )

    refresh.assert_awaited_once()
    assert 1 in connection.results
    assert [v["vin"] for v in demo.get_demo_vehicles(hass)] == [R1T_VIN]
    assert R2_VIN not in hass.data[DOMAIN][ATTR_DEMO_STORES]
    assert _count(analytics_db, "drives", R2_VIN) == 0
    assert _count(analytics_db, "drives", R1T_VIN) == 9
    # The vehicle is gone: further requests for it are "not found".
    await ws._websocket_analytics_summary(hass, connection, {"id": 2, "vin": R2_VIN})
    assert connection.errors[2][0] == "not_found"


async def test_delete_vehicle_history_rejects_non_admin_for_demo(
    mock_hass: Any, analytics_db: Any
) -> None:
    from custom_components.rivian import websocket_api as ws

    hass = _hass_with_db(mock_hass, analytics_db)
    await _install(hass, DENVER, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    connection = _Connection(admin=False)

    await ws._websocket_analytics_delete_vehicle_history(
        hass, connection, {"id": 1, "vin": R2_VIN}
    )

    assert connection.errors[1][0] == "unauthorized"
    assert _count(analytics_db, "drives", R2_VIN) == 7


# -- services ----------------------------------------------------------------------------


def test_services_are_documented_in_yaml_and_strings() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "rivian"
    yaml_text = (root / "services.yaml").read_text(encoding="utf-8")
    for name in ("create_demo_data", "delete_demo_data"):
        assert f"\n{name}:" in "\n" + yaml_text
        for path in ("strings.json", "translations/en.json"):
            data = json.loads((root / path).read_text(encoding="utf-8"))
            assert name in data["services"]


def test_demo_picture_urls_cover_both_vehicles() -> None:
    """Each demo vehicle has an Overview-card picture (no image entity exists)."""
    from pathlib import Path

    from custom_components.rivian.demo import demo_picture_url

    r1t = demo_picture_url(R1T_VIN)
    r2 = demo_picture_url(R2_VIN)
    assert r1t.startswith("/rivian_static/demo-r1t.svg")
    assert r2.startswith("/rivian_static/demo-r2.svg")
    # Neutral bundled illustrations: nothing hotlinked from Rivian.
    assert "rivian.com" not in r1t + r2
    frontend = Path(__file__).parents[1] / "custom_components" / "rivian" / "frontend"
    for name in ("demo-r1t.svg", "demo-r2.svg"):
        assert (frontend / name).is_file()
    # The URL changes with the file's contents, so a redrawn picture isn't
    # hidden behind the static path's month-long browser cache.
    import zlib

    crc = f"{zlib.crc32((frontend / 'demo-r1t.svg').read_bytes()):08x}"
    assert r1t.endswith(f"-{crc}")
    assert demo_picture_url("7PDSGABA8NN000000") is None


async def test_install_writes_a_year_of_capacity_history(
    mock_hass: Any, analytics_db: Any
) -> None:
    hass = _hass_with_db(mock_hass, analytics_db)
    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

    await _install(hass, DENVER, now)

    for vin, (first, last) in ((R1T_VIN, (135.0, 134.2)), (R2_VIN, (87.9, 87.5))):
        rows = analytics_db.capacity_history_rows(vin)
        assert len(rows) == 365 and {r["source"] for r in rows} == {"demo"}
        assert rows[0]["kwh"] == first
        assert rows[-1]["kwh"] == pytest.approx(last, abs=0.05)
        assert rows[-1]["day"] == "2026-10-01"  # yesterday
        assert {r["temp_source"] for r in rows} == {"battery", "outside"}
        by_day = {r["day"]: r["temp_f"] for r in rows}
        # Cold winter, hot summer, from the real calendar.
        assert by_day["2026-01-20"] < 45 and by_day["2026-07-20"] > 70
    # Removing a demo vehicle clears its history; the other keeps its own.
    await asyncio.get_running_loop().run_in_executor(
        None, analytics_db.delete_vin, R1T_VIN
    )
    assert analytics_db.capacity_history_rows(R1T_VIN) == []
    assert len(analytics_db.capacity_history_rows(R2_VIN)) == 365


async def test_demo_sessions_carry_station_details(
    mock_hass: Any, analytics_db: Any
) -> None:
    hass = _hass_with_db(mock_hass, analytics_db)
    await _install(hass, DENVER, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    r1t = analytics_db.list_charging_sessions(R1T_VIN)
    r2 = analytics_db.list_charging_sessions(R2_VIN)
    home = [s for s in r1t + r2 if s["kind"] == "ac"]
    assert home and all(
        s["is_home"] is True and s["vendor"] == "Rivian Wall Charger" for s in home
    )
    assert {"Rivian Adventure Network", "Tesla Supercharger"} <= {
        s["network"] for s in r1t if s["kind"] == "dc"
    }
    assert {"Electrify America", "Tesla Supercharger"} <= {
        s["network"] for s in r2 if s["kind"] == "dc"
    }
    assert {s["station_version"] for s in r1t + r2} >= {"V3", "V4"}
