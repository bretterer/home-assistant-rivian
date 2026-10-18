"""Schema v10: places and routes belong to no vehicle (only to a dataset)."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from custom_components.rivian import analytics_db as analytics_db_module, places
from custom_components.rivian.analytics_db import (
    DEMO_VEHICLES_META_KEY,
    SCHEMA_VERSION,
    AnalyticsDatabase,
)
from custom_components.rivian.drive_models import DriveRecord

VIN_A = "7PDSGABA8NN000001"
VIN_B = "7PDSGABA8NN000002"
DEMO_1 = "DEMO0R2EAGLE00001"
DEMO_2 = "DEMO1R1TEAGLE0002"

HOME = (37.0, -122.0)
WORK = (37.5, -122.5)
GYM = (37.2, -122.3)
FAR = (38.5, -121.0)


def _hhmm(day: int, hour: int, minute: int = 0) -> str:
    return f"2026-02-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def _drive(
    vin: str,
    drive_id: str,
    day: int,
    start: tuple[float, float],
    end: tuple[float, float],
    hour: int = 8,
    minutes: int = 30,
) -> DriveRecord:
    return DriveRecord(
        vin=vin,
        drive_id=drive_id,
        start_time=_hhmm(day, hour),
        end_time=_hhmm(day, hour, minutes),
        distance_miles=20.0,
        duration_seconds=minutes * 60.0,
        start_soc=80.0,
        end_soc=75.0,
        battery_capacity_kwh=135.0,
        energy_kwh=6.0,
        start_lat=start[0],
        start_lon=start[1],
        end_lat=end[0],
        end_lon=end[1],
    )


def _register_demo(db: AnalyticsDatabase, *vins: str) -> None:
    db.set_meta(
        DEMO_VEHICLES_META_KEY, json.dumps([{"vin": v, "name": v} for v in vins])
    )


# -- pooled clustering ---------------------------------------------------------


class TestPooledEndpoints:
    def test_chaining_stays_within_each_car(self) -> None:
        # Car A ends a drive at WORK; car B's first drive starts ~2 km away
        # (inside the parked-position chaining distance). B must NOT snap to
        # A's end, but A's own next drive does snap to A's previous end.
        near_work = (WORK[0] + 0.018, WORK[1])  # ~2 km north
        rows = {
            VIN_A: [
                places.DriveEndpointInput("a1", *HOME, 0.0, *WORK, 100.0, vin=VIN_A),
                places.DriveEndpointInput(
                    "a2", WORK[0] + 0.01, WORK[1], 500.0, *HOME, 600.0, vin=VIN_A
                ),
            ],
            VIN_B: [
                places.DriveEndpointInput(
                    "b1", *near_work, 200.0, *HOME, 300.0, vin=VIN_B
                ),
            ],
        }
        pooled = places.pooled_endpoints(rows)
        by_key = {(e.vin, e.drive_id, e.kind): e for e in pooled}
        assert (
            by_key[(VIN_B, "b1", "start")].lat,
            by_key[(VIN_B, "b1", "start")].lon,
        ) == near_work
        a2 = by_key[(VIN_A, "a2", "start")]
        assert (a2.lat, a2.lon) == WORK  # snapped to A's own previous end
        times = [e.t for e in pooled]
        assert times == sorted(times)

    def test_single_vin_matches_drive_endpoints(self) -> None:
        rows = [
            places.DriveEndpointInput("a1", *HOME, 0.0, *WORK, 100.0, vin=VIN_A),
            places.DriveEndpointInput("a2", *WORK, 200.0, *HOME, 300.0, vin=VIN_A),
        ]
        assert places.pooled_endpoints({VIN_A: rows}) == places.drive_endpoints(rows)

    def test_pooled_visits_across_cars_form_a_place_and_one_route(
        self, analytics_db: Any
    ) -> None:
        # Neither car alone has the 3 visits a place needs; together they do.
        analytics_db.upsert_drives(
            VIN_A,
            [
                _drive(VIN_A, "a1", 1, HOME, WORK),
                _drive(VIN_A, "a2", 2, HOME, WORK, minutes=40),
            ],
        )
        analytics_db.upsert_drives(
            VIN_B, [_drive(VIN_B, "b1", 3, HOME, WORK, minutes=20)]
        )
        result = analytics_db.rebuild_places("real")
        assert result["places"] == 2  # Home + Work
        placed = analytics_db.list_places("real")
        assert {p["visits"] for p in placed} == {3}
        for p in placed:
            assert p["visits_by_vin"] == {VIN_A: 2, VIN_B: 1}

        analytics_db.rebuild_routes("real")
        routes = analytics_db.list_routes("real")
        assert len(routes) == 1  # one route spans both vehicles
        stats = routes[0]["stats"]
        assert stats["overall"]["count"] == 3
        assert stats["by_vin"][VIN_A]["count"] == 2
        assert stats["by_vin"][VIN_B]["count"] == 1
        assert stats["overall"]["fastest_seconds"] == 20 * 60.0
        assert stats["by_vin"][VIN_A]["fastest_seconds"] == 30 * 60.0
        # Ranks: overall across both cars, `vin_rank` within the car.
        per_drive = stats["per_drive"]
        assert per_drive[f"{VIN_B}|b1"]["rank"] == 1
        assert per_drive[f"{VIN_A}|a1"]["rank"] == 2
        assert per_drive[f"{VIN_A}|a1"]["vin_rank"] == 1
        assert per_drive[f"{VIN_A}|a2"]["vin_rank"] == 2
        assert per_drive[f"{VIN_B}|b1"]["vin_vs_avg_pct"] == 0.0  # its own average

        detail = analytics_db.route_detail("real", routes[0]["id"])
        assert {(d["vin"], d["drive_id"]) for d in detail["drives"]} == {
            (VIN_A, "a1"),
            (VIN_A, "a2"),
            (VIN_B, "b1"),
        }
        only_b = analytics_db.route_detail("real", routes[0]["id"], [VIN_B])
        assert [d["key"] for d in only_b["drives"]] == [f"{VIN_B}|b1"]

    def test_day_route_uses_the_cars_own_standing(self, analytics_db: Any) -> None:
        from datetime import date
        from zoneinfo import ZoneInfo

        analytics_db.upsert_drives(
            VIN_A,
            [
                _drive(VIN_A, "a1", 1, HOME, WORK, minutes=30),
                _drive(VIN_A, "a2", 2, HOME, WORK, minutes=40),
            ],
        )
        analytics_db.upsert_drives(
            VIN_B, [_drive(VIN_B, "b1", 1, HOME, WORK, hour=9, minutes=10)]
        )
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        day_a = analytics_db.day(VIN_A, ZoneInfo("UTC"), date(2026, 2, 1))
        route = day_a["segments"][0]["route"]
        assert route["count"] == 2  # car A's own drives on the route
        assert route["rank"] == 1  # fastest of A's two, though B is faster still
        day_b = analytics_db.day(VIN_B, ZoneInfo("UTC"), date(2026, 2, 1))
        assert day_b["segments"][0]["route"]["count"] == 1
        assert day_b["segments"][0]["route"]["rank"] == 1


# -- favorites: routes ordered by the selected vehicles' counts -----------------


class TestRouteFavorites:
    def _seed(self, db: AnalyticsDatabase) -> None:
        a = [_drive(VIN_A, f"aw{i}", i, HOME, WORK) for i in range(1, 5)]
        a.append(_drive(VIN_A, "ag1", 10, HOME, GYM))
        b = [_drive(VIN_B, "bw1", 11, HOME, WORK)]
        b += [_drive(VIN_B, f"bg{i}", 20 + i, HOME, GYM) for i in range(1, 4)]
        db.upsert_drives(VIN_A, a)
        db.upsert_drives(VIN_B, b)
        db.rebuild_places("real")
        db.rebuild_routes("real")

    def test_ordering_and_hiding_by_selected_vehicles(self, analytics_db: Any) -> None:
        self._seed(analytics_db)
        analytics_db.rebuild_places("real")
        labels = {
            r["id"]: (r["start_place"]["label"], r["end_place"]["label"])
            for r in analytics_db.list_routes("real")
        }
        assert len(labels) == 2
        by_total = analytics_db.list_routes("real")
        # Total counts: Work 5 (4 + 1), Gym 4 (1 + 3).
        assert [r["drive_count"] for r in by_total] == [5, 4]
        # Car A's favorites: Work (4) first, Gym (1) second.
        a_routes = analytics_db.list_routes("real", [VIN_A])
        assert [r["selected_count"] for r in a_routes] == [4, 1]
        # Car B's favorites: Gym (3) before Work (1).
        b_routes = analytics_db.list_routes("real", [VIN_B])
        assert [r["selected_count"] for r in b_routes] == [3, 1]
        assert a_routes[0]["id"] == b_routes[1]["id"]
        assert a_routes[1]["id"] == b_routes[0]["id"]
        both = analytics_db.list_routes("real", [VIN_A, VIN_B])
        assert [r["selected_count"] for r in both] == [5, 4]

    def test_a_route_a_car_never_drove_is_hidden(self, analytics_db: Any) -> None:
        analytics_db.upsert_drives(
            VIN_A, [_drive(VIN_A, f"a{i}", i, HOME, WORK) for i in range(1, 4)]
        )
        analytics_db.upsert_drives(VIN_B, [_drive(VIN_B, "b1", 9, FAR, FAR)])
        analytics_db.rebuild_places("real")
        analytics_db.rebuild_routes("real")
        assert len(analytics_db.list_routes("real", [VIN_A])) == 1
        assert analytics_db.list_routes("real", [VIN_B]) == []

    def test_rename_is_shared(self, analytics_db: Any) -> None:
        self._seed(analytics_db)
        route = analytics_db.list_routes("real", [VIN_A])[0]
        analytics_db.rename_route("real", route["id"], "The commute")
        assert analytics_db.list_routes("real", [VIN_B])[1]["label"] == "The commute"


# -- delete_vin keeps places ----------------------------------------------------


def test_delete_vin_keeps_places_and_routes_until_rebuild(analytics_db: Any) -> None:
    analytics_db.upsert_drives(
        VIN_A, [_drive(VIN_A, f"a{i}", i, HOME, WORK) for i in range(1, 4)]
    )
    analytics_db.upsert_drives(
        VIN_B, [_drive(VIN_B, f"b{i}", i + 5, HOME, WORK) for i in range(1, 4)]
    )
    analytics_db.rebuild_places("real")
    analytics_db.rebuild_routes("real")
    named = analytics_db.create_place("real", FAR[0], FAR[1], "Cabin")
    assert len(analytics_db.list_places("real")) == 3

    analytics_db.delete_vin(VIN_A)
    assert len(analytics_db.list_places("real")) == 3  # untouched
    assert len(analytics_db.list_routes("real")) == 1

    # B alone still supports Home/Work; the cabin is named so it stays too.
    analytics_db.rebuild_places("real")
    analytics_db.rebuild_routes("real")
    assert named in {p["id"] for p in analytics_db.list_places("real")}
    route = analytics_db.list_routes("real")[0]
    assert route["stats"]["by_vin"] == {VIN_B: {**route["stats"]["by_vin"][VIN_B]}}
    assert route["drive_count"] == 3

    analytics_db.delete_vin(VIN_B)
    analytics_db.rebuild_places("real")
    analytics_db.rebuild_routes("real")
    # Nothing visits Home/Work any more (unnamed auto places vanish)...
    assert [p["name"] for p in analytics_db.list_places("real")] == ["Cabin"]
    assert analytics_db.list_routes("real") == []


# -- demo isolation --------------------------------------------------------------


class TestDemoIsolation:
    def test_demo_endpoint_near_a_real_place_is_not_labelled_by_it(
        self, analytics_db: Any
    ) -> None:
        _register_demo(analytics_db, DEMO_1, DEMO_2)
        analytics_db.sync_zones(
            "real",
            [
                {
                    "entity_id": "zone.home",
                    "name": "Real Home",
                    "latitude": HOME[0],
                    "longitude": HOME[1],
                    "radius": 100,
                }
            ],
        )
        # Demo drives that start exactly at the real home.
        analytics_db.upsert_drives(
            DEMO_1, [_drive(DEMO_1, f"d{i}", i, HOME, WORK) for i in range(1, 4)]
        )
        analytics_db.upsert_drives(
            VIN_A, [_drive(VIN_A, f"a{i}", i, HOME, WORK) for i in range(1, 4)]
        )
        analytics_db.rebuild_places("demo")
        analytics_db.rebuild_places("real")

        real_home = next(p for p in analytics_db.list_places("real") if p["name"])
        demo_places = analytics_db.list_places("demo")
        assert all(p["source"] == "auto" for p in demo_places)
        assert real_home["id"] not in {p["id"] for p in demo_places}
        assert real_home["visits_by_vin"] == {VIN_A: 3}

        # The demo drives are labelled only by demo places.
        with analytics_db._lock:
            demo_start = analytics_db._conn.execute(
                "SELECT start_place_id FROM drives WHERE vin = ?", (DEMO_1,)
            ).fetchall()
        demo_ids = {p["id"] for p in demo_places}
        assert {r[0] for r in demo_start} <= demo_ids

        # And the real dataset has no demo places or routes mixed in.
        analytics_db.rebuild_routes("demo")
        analytics_db.rebuild_routes("real")
        assert {
            r["stats"]["by_vin"].popitem()[0] for r in analytics_db.list_routes("real")
        } == {VIN_A}
        assert {
            vin
            for r in analytics_db.list_routes("demo")
            for vin in r["stats"]["by_vin"]
        } == {DEMO_1}

    def test_zones_never_sync_into_the_demo_dataset(self, analytics_db: Any) -> None:
        analytics_db.sync_zones(
            "demo",
            [{"entity_id": "zone.home", "latitude": 1.0, "longitude": 2.0}],
        )
        assert analytics_db.list_places("demo") == []

    def test_assign_drive_places_uses_the_drives_dataset(
        self, analytics_db: Any
    ) -> None:
        _register_demo(analytics_db, DEMO_1)
        analytics_db.create_place("real", HOME[0], HOME[1], "Real Home")
        analytics_db.create_place("demo", HOME[0], HOME[1], "Demo Home")
        analytics_db.upsert_drives(DEMO_1, [_drive(DEMO_1, "d1", 1, HOME, WORK)])
        analytics_db.upsert_drives(VIN_A, [_drive(VIN_A, "a1", 1, HOME, WORK)])
        analytics_db.assign_drive_places(DEMO_1, "d1")
        analytics_db.assign_drive_places(VIN_A, "a1")
        labels = {
            p["id"]: p["name"]
            for ds in ("real", "demo")
            for p in analytics_db.list_places(ds)
        }
        with analytics_db._lock:
            rows = {
                r["vin"]: r["start_place_id"]
                for r in analytics_db._conn.execute(
                    "SELECT vin, start_place_id FROM drives"
                ).fetchall()
            }
        assert labels[rows[DEMO_1]] == "Demo Home"
        assert labels[rows[VIN_A]] == "Real Home"


# -- categories ------------------------------------------------------------------


def test_categories_one_source_of_truth() -> None:
    options = places.category_options()
    assert [o["key"] for o in options] == list(places.PLACE_CATEGORIES)
    assert places.PLACE_CATEGORIES == (
        "home",
        "work",
        "school",
        "shop",
        "dining",
        "charging",
        "friends",
        "family",
        "gym",
        "swim",
        "mountain_biking",
        "park",
        "medical",
        "other",
    )
    by_key = {o["key"]: o for o in options}
    assert by_key["home"]["icon"] == "mdi:home"
    assert by_key["mountain_biking"]["icon"] == "mdi:bike"
    assert all(o["label"] and o["icon"].startswith("mdi:") for o in options)


# -- the v9 -> v10 migration -------------------------------------------------------

_V9_PLACES = """
CREATE TABLE places (
  place_id INTEGER PRIMARY KEY, vin TEXT NOT NULL,
  name TEXT, category TEXT,
  lat REAL NOT NULL, lon REAL NOT NULL,
  radius_m REAL NOT NULL DEFAULT 150,
  source TEXT NOT NULL DEFAULT 'auto',
  zone_entity_id TEXT,
  hidden INTEGER NOT NULL DEFAULT 0,
  geocode_name TEXT, geocoded_ts REAL,
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL
);
CREATE INDEX ix_places_vin ON places(vin);
CREATE UNIQUE INDEX ux_places_zone ON places(vin, zone_entity_id)
  WHERE zone_entity_id IS NOT NULL;
CREATE TABLE routes (
  route_id INTEGER PRIMARY KEY, vin TEXT NOT NULL,
  start_place_id INTEGER NOT NULL, end_place_id INTEGER NOT NULL,
  variant INTEGER NOT NULL, name TEXT,
  drive_count INTEGER NOT NULL DEFAULT 0,
  stats_json TEXT NOT NULL DEFAULT '{}',
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL
);
CREATE UNIQUE INDEX ux_routes_variant
  ON routes(vin, start_place_id, end_place_id, variant);
CREATE INDEX ix_routes_vin ON routes(vin);
"""


def build_v9_database(
    mock_hass: Any, path: str, db_factory: Any
) -> dict[str, dict[str, int]]:
    """Make a schema-v9 database with per-vehicle duplicate places and routes.

    Two real vehicles (A, B) and two demo ones, each with its own copy of the
    household's zones, nearby user/auto places and routes. Returns the ids of
    the interesting rows by name.
    """
    seed = db_factory(mock_hass, path)
    seed.close()
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    raw.execute("DROP TABLE places")
    raw.execute("DROP TABLE routes")
    raw.executescript(_V9_PLACES)
    now = time.time()
    ids: dict[str, dict[str, int]] = {"p": {}, "r": {}}

    def place(
        key: str,
        vin: str,
        lat: float,
        lon: float,
        *,
        name: str | None = None,
        source: str = "auto",
        zone: str | None = None,
        category: str | None = None,
        geocode: str | None = None,
        radius: float = 150.0,
        hidden: int = 0,
        created: float = 0.0,
    ) -> None:
        cur = raw.execute(
            "INSERT INTO places (vin, name, category, lat, lon, radius_m, source, "
            "zone_entity_id, hidden, geocode_name, created_ts, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                vin,
                name,
                category,
                lat,
                lon,
                radius,
                source,
                zone,
                hidden,
                geocode,
                now + created,
                now,
            ),
        )
        ids["p"][key] = cur.lastrowid

    # Zone duplicates: zone.home / zone.work exist once per real VIN.
    place("home_a", VIN_A, *HOME, name="Home", source="zone", zone="zone.home")
    place("home_b", VIN_B, *HOME, name="Home", source="zone", zone="zone.home")
    place("work_a", VIN_A, *WORK, name="Work", source="zone", zone="zone.work")
    place("work_b", VIN_B, *WORK, name="Work", source="zone", zone="zone.work")
    # A's named user place and B's unnamed auto place at the same spot: merge,
    # the named one survives and the geocode name is kept.
    place("gym_a", VIN_A, *GYM, name="Gym", source="user", category="gym", created=1)
    place(
        "gym_b",
        VIN_B,
        GYM[0] + 0.0004,
        GYM[1],
        source="auto",
        geocode="Main St",
        created=2,
    )
    # Unnamed auto places near each other for both cars: collapse to one.
    place("auto_a", VIN_A, 37.8, -122.2, source="auto", created=3)
    place("auto_b", VIN_B, 37.8005, -122.2, source="auto", geocode="Elm St", created=4)
    # A user place no other car has: stays on its own.
    place("cabin_a", VIN_A, *FAR, name="Cabin", source="user", created=5)
    # Demo copies, deliberately at the real home's coordinates: they merge
    # with each other but never with the real ones.
    place("demo_home_1", DEMO_1, *HOME, name="Demo Home", source="user", created=6)
    place("demo_home_2", DEMO_2, *HOME, name="Demo Home", source="user", created=7)

    def route(
        key: str, vin: str, start: str, end: str, count: int, name: str | None = None
    ) -> None:
        cur = raw.execute(
            "INSERT INTO routes (vin, start_place_id, end_place_id, variant, name, "
            "drive_count, stats_json, created_ts, updated_ts) "
            "VALUES (?, ?, ?, 1, ?, ?, '{\"old\": true}', ?, ?)",
            (vin, ids["p"][start], ids["p"][end], name, count, now, now),
        )
        ids["r"][key] = cur.lastrowid

    route("commute_a", VIN_A, "home_a", "work_a", 5, name="Commute")
    route("commute_b", VIN_B, "home_b", "work_b", 3)
    route("gym_a", VIN_A, "home_a", "gym_a", 3)
    route("demo_1", DEMO_1, "demo_home_1", "demo_home_1", 3)
    route("demo_2", DEMO_2, "demo_home_2", "demo_home_2", 4)

    def drive(vin: str, drive_id: str, start: str, end: str, route_key: str) -> None:
        raw.execute(
            "INSERT INTO drives (vin, drive_id, start_time, end_time, distance_miles, "
            "duration_seconds, energy_kwh, created_ts, start_place_id, end_place_id, "
            "route_id) VALUES (?, ?, '2026-02-01T00:00:00Z', '2026-02-01T00:10:00Z', "
            "1, 600, 1, ?, ?, ?, ?)",
            (vin, drive_id, now, ids["p"][start], ids["p"][end], ids["r"][route_key]),
        )

    drive(VIN_A, "da", "home_a", "work_a", "commute_a")
    drive(VIN_B, "db", "home_b", "work_b", "commute_b")
    drive(VIN_B, "dg", "home_b", "gym_b", "commute_b")
    drive(DEMO_1, "dd1", "demo_home_1", "demo_home_1", "demo_1")
    drive(DEMO_2, "dd2", "demo_home_2", "demo_home_2", "demo_2")
    raw.execute("DELETE FROM meta WHERE key = ?", (DEMO_VEHICLES_META_KEY,))
    raw.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        (
            DEMO_VEHICLES_META_KEY,
            json.dumps([{"vin": DEMO_1}, {"vin": DEMO_2}]),
        ),
    )
    for key in (
        f"places_version:{VIN_A}",
        f"places_version:{VIN_B}",
        f"routes_version:{VIN_A}",
        f"routes_version:{VIN_B}",
        f"places_version:{DEMO_1}",
    ):
        raw.execute("INSERT INTO meta(key, value) VALUES (?, '1')", (key,))
    raw.execute("PRAGMA user_version = 9")
    raw.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()
    return ids


class TestMigrationV9ToV10:
    def test_migration_merges_duplicates_and_remaps(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        from tests.conftest import make_analytics_db  # type: ignore[import-not-found]

        ids = build_v9_database(mock_hass, analytics_db_path, make_analytics_db)
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            counts = dict(analytics_db_module._V10_MERGE_COUNTS)
            assert (
                db._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            )
            place_cols = {r[1] for r in db._conn.execute("PRAGMA table_info(places)")}
            route_cols = {r[1] for r in db._conn.execute("PRAGMA table_info(routes)")}
            assert "vin" not in place_cols and "dataset" in place_cols
            assert "vin" not in route_cols and "dataset" in route_cols

            real = {
                p["name"] or p["geocode_name"] or p["id"]: p
                for p in db.list_places("real")
            }
            # zones merged by entity id; gym merged by proximity (named wins,
            # category kept, geocode name adopted); autos collapsed to one.
            assert {"Home", "Work", "Gym", "Cabin"} <= set(real)
            gym = real["Gym"]
            assert gym["category"] == "gym" and gym["geocode_name"] == "Main St"
            assert gym["id"] == ids["p"]["gym_a"]
            assert real["Home"]["id"] == ids["p"]["home_a"]  # earliest zone survives
            real_autos = [p for p in db.list_places("real") if p["source"] == "auto"]
            assert len(real_autos) == 1 and real_autos[0]["geocode_name"] == "Elm St"
            assert len(db.list_places("real")) == 5  # Home, Work, Gym, Cabin, auto

            demo = db.list_places("demo")
            assert [p["name"] for p in demo] == ["Demo Home"]
            assert db.list_places("real") and all(
                p["name"] != "Demo Home" for p in db.list_places("real")
            )

            assert counts["places_before"] == 11
            assert counts["places_after"] == 6
            assert counts["zone_merged"] == 2
            assert counts["proximity_merged"] == 3
            assert counts["routes_before"] == 5

            # Drives remapped to the survivors.
            rows = {
                r["drive_id"]: r
                for r in db._conn.execute(
                    "SELECT drive_id, start_place_id, end_place_id, route_id FROM drives"
                )
            }
            assert rows["db"]["start_place_id"] == ids["p"]["home_a"]
            assert rows["db"]["end_place_id"] == ids["p"]["work_a"]
            assert rows["dg"]["end_place_id"] == ids["p"]["gym_a"]
            assert rows["dd2"]["start_place_id"] == ids["p"]["demo_home_1"]

            # Routes: the commute merged (name kept, fullest survivor), the
            # demo routes stay in the demo dataset.
            real_routes = db.list_routes("real")
            assert len(real_routes) == 2
            commute = next(r for r in real_routes if r["name"] == "Commute")
            assert commute["id"] == ids["r"]["commute_a"]
            assert commute["drive_count"] == 8
            assert rows["da"]["route_id"] == rows["db"]["route_id"] == commute["id"]
            assert commute["stats"] == {}  # the rebuild recomputes the new shape
            assert len(db.list_routes("demo")) == 1  # (demo Home->Home merged pair)

            # Stamps cleared so the background rebuild runs; other meta stays.
            assert db.has_unbuilt_places("real") and db.has_unbuilt_routes("demo")
            assert db.get_meta(f"places_version:{VIN_A}") is None
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)

            # The rebuild that follows produces the new-shaped stats.
            db.rebuild_places("real")
            db.rebuild_routes("real")
            assert not db.has_unbuilt_places("real")
        finally:
            db.close()

    def test_migration_is_noop_for_fresh_database(self, analytics_db: Any) -> None:
        cols = {r[1] for r in analytics_db._conn.execute("PRAGMA table_info(places)")}
        assert "dataset" in cols and "vin" not in cols
        index_sql = " ".join(
            r[0] or ""
            for r in analytics_db._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name IN "
                "('ux_places_zone', 'ux_routes_variant')"
            )
        )
        assert "dataset, zone_entity_id" in index_sql
        assert "dataset, start_place_id, end_place_id, variant" in index_sql


def test_vehicle_filter_keeps_named_and_zone_places_with_no_visits(
    analytics_db: Any,
) -> None:
    """A place someone made (named, or an HA zone) shows before any drive."""
    named = analytics_db.create_place("real", 39.8242, -105.1680, "Trailhead")
    analytics_db.sync_zones(
        "real",
        [
            {
                "entity_id": "zone.gym",
                "name": "Gym",
                "latitude": 39.7242,
                "longitude": -104.9880,
                "radius": 100,
            }
        ],
    )
    ids = {p["id"] for p in analytics_db.list_places("real", ["SOME_VIN"])}
    assert named in ids
    assert any(
        p["zone_entity_id"] == "zone.gym"
        for p in analytics_db.list_places("real", ["SOME_VIN"])
    )
