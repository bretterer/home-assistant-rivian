"""Unit tests for the SQLite-backed AnalyticsDatabase storage layer.

Covers schema/versioning, legacy JSON -> SQLite import (lossless and
idempotent), SQL-vs-legacy aggregate parity against the empirical fixture
baseline, NULL-``sort_ts`` handling, prune boundary conditions, and -- most
importantly -- that DriveStore's synchronous cache surface never touches
SQLite (the executor-thread guard's whole reason to exist).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import sqlite3
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.rivian import analytics_db as analytics_db_module
from custom_components.rivian.analytics_db import (
    SCHEMA_VERSION,
    ActiveCheckpoint,
    AnalyticsDatabase,
)
from custom_components.rivian.const import DRIVE_SENSORS
from custom_components.rivian.drive_models import (
    STANDARD_SPEED_BINS,
    ChargingSample,
    ChargingSessionRecord,
    DriveRecord,
    SpeedBinData,
    VampireDrainRecord,
)
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.drive_track import DriveTrack, TrackPoint
from custom_components.rivian.history_backfill import reconstruct_drives_from_sqlite
from custom_components.rivian.sensor import RivianDriveSensorEntity

TEST_VIN = "7PDSGABA8NN000000"
FIXTURE_DB_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "r1s_10day_history.db"
)
EMPIRICAL_VIN = "7PDSGABA1NN000001"


def _make_track(
    n: int = 5,
    start_t: float = 1_700_000_000.0,
    lat0: float = 37.0,
    lon0: float = -122.0,
    step: float = 0.001,
) -> DriveTrack:
    """Build a simple monotonically-increasing GPS track for tests."""
    track = DriveTrack()
    for i in range(n):
        track.append(
            TrackPoint(t=start_t + i * 10, lat=lat0 + i * step, lon=lon0 + i * step)
        )
    return track


def _make_drive(
    drive_id: str,
    distance_miles: float,
    energy_kwh: float,
    start_time: str | None,
    end_time: str | None,
    is_micro_drive: bool = False,
    start_lat: float | None = None,
    start_lon: float | None = None,
    end_lat: float | None = None,
    end_lon: float | None = None,
) -> DriveRecord:
    """Build a minimal DriveRecord for storage-layer tests.

    The coordinate arguments default to None (unset), so every existing
    caller that doesn't pass them is unaffected.
    """
    return DriveRecord(
        vin=TEST_VIN,
        drive_id=drive_id,
        start_time=start_time or "",
        end_time=end_time or "",
        distance_miles=distance_miles,
        duration_seconds=600.0,
        start_soc=80.0,
        end_soc=75.0,
        battery_capacity_kwh=135.0,
        energy_kwh=energy_kwh,
        is_micro_drive=is_micro_drive,
        start_lat=start_lat,
        start_lon=start_lon,
        end_lat=end_lat,
        end_lon=end_lon,
    )


class TestSchemaAndMeta:
    """Schema creation, versioning, and meta key/value helpers."""

    def test_schema_creates_tables_and_user_version(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """setup() creates all expected tables and stamps PRAGMA user_version."""
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            with db._lock:
                tables = {
                    row[0]
                    for row in db._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                user_version = db._conn.execute("PRAGMA user_version").fetchone()[0]
            assert {"meta", "drives", "vampire_events", "dcfc_sessions"} <= tables
            assert user_version == SCHEMA_VERSION
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        finally:
            db.close()

    def test_setup_is_idempotent(self, mock_hass: Any, analytics_db_path: str) -> None:
        """Calling setup() twice on an already-open connection is a safe no-op."""
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        conn_before = db._conn
        db.setup()
        assert db._conn is conn_before
        db.close()

    def test_get_set_meta_roundtrip(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """set_meta/get_meta round-trip, and an unknown key returns None."""
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            assert db.get_meta("does_not_exist") is None
            db.set_meta("foo", "bar")
            assert db.get_meta("foo") == "bar"
            db.set_meta("foo", "baz")
            assert db.get_meta("foo") == "baz"
        finally:
            db.close()

    def test_executor_thread_guard_raises_on_loop_thread(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """Calling an executor-bound method from the (simulated) loop thread raises."""
        # Point loop_thread_id at *this* thread, the opposite of the usual
        # test setup, to specifically exercise the guard's failure path.
        mock_hass.loop_thread_id = threading.get_ident()
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        with pytest.raises(RuntimeError, match="executor"):
            db.setup()


class TestAggregateParitySQLvsLegacy:
    """SQL-computed aggregates must match a plain-Python aggregate over the same drives."""

    def test_empirical_fixture_aggregate_parity(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """window_stats() over imported fixture drives matches the empirical baseline
        (370.93 mi, 131.05 kWh, 2.83 mi/kWh, 4 micro-drives) and a Python-side sum."""
        drives, _meta = reconstruct_drives_from_sqlite(
            db_path=FIXTURE_DB_PATH, vin=EMPIRICAL_VIN, vehicle_id="r1s_test"
        )
        assert len(drives) == 62

        analytics_db.upsert_drives(EMPIRICAL_VIN, drives)
        now_ts = datetime.now(UTC).timestamp()
        sql_stats = analytics_db.window_stats(EMPIRICAL_VIN, None, now_ts)

        # Legacy-style plain-Python aggregate over the same in-memory records.
        valid = [d for d in drives if not d.is_micro_drive]
        micro = [d for d in drives if d.is_micro_drive]
        py_total_miles = round(sum(d.distance_miles for d in valid), 2)
        py_total_kwh = round(sum(d.energy_kwh for d in valid), 2)
        py_efficiency = round(py_total_miles / py_total_kwh, 2)
        py_mpge = round(py_efficiency * 33.705, 1)

        assert sql_stats.total_miles == py_total_miles == 370.93
        assert sql_stats.total_kwh == py_total_kwh == 131.05
        assert sql_stats.efficiency_mi_kwh == py_efficiency == 2.83
        assert round(sql_stats.mpge, 1) == py_mpge == 95.4
        assert sql_stats.drive_count == len(valid) == 58
        assert sql_stats.total_micro_drives == len(micro) == 4


class TestNullSortTsHandling:
    """Rows with unparseable timestamps (NULL sort_ts) are excluded from windows,
    included in all-time, and retained (not deleted) by prune()."""

    def test_null_sort_ts_excluded_from_window_included_in_all_time(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        now_dt = datetime.now(UTC)
        now_ts = now_dt.timestamp()
        recent_iso = (now_dt - timedelta(days=2)).isoformat()
        valid_drive = _make_drive("valid_1", 10.0, 4.0, recent_iso, recent_iso)
        bad_ts_drive = _make_drive(
            "bad_ts_1", 20.0, 5.0, "not-a-timestamp", "also-not-a-timestamp"
        )
        analytics_db.upsert_drives(TEST_VIN, [valid_drive, bad_ts_drive])

        window_stats = analytics_db.window_stats(TEST_VIN, 30, now_ts)
        assert window_stats.drive_count == 1
        assert window_stats.total_miles == 10.0

        all_time_stats = analytics_db.window_stats(TEST_VIN, None, now_ts)
        assert all_time_stats.drive_count == 2
        assert all_time_stats.total_miles == 30.0

    def test_null_sort_ts_retained_by_prune(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        bad_ts_drive = _make_drive("bad_ts_prune", 5.0, 2.0, "garbage", "garbage-end")
        analytics_db.upsert_drives(TEST_VIN, [bad_ts_drive])

        # A cutoff far in the future would delete everything with a real
        # sort_ts, but the NULL-sort_ts row is retained regardless: SQL's
        # `NULL < cutoff_ts` is never true.
        far_future_cutoff = datetime.now(UTC).timestamp() + 999_999_999
        removed = analytics_db.prune(TEST_VIN, far_future_cutoff)
        assert removed == 0

        stats = analytics_db.window_stats(TEST_VIN, None, datetime.now(UTC).timestamp())
        assert stats.drive_count == 1


class TestPruneBoundaryConditions:
    """Prune's cutoff comparison is a strict `<`, not `<=`."""

    def test_prune_boundary_retains_row_exactly_at_cutoff(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        cutoff_dt = datetime(2026, 1, 1, tzinfo=UTC)
        cutoff_ts = cutoff_dt.timestamp()

        at_cutoff = _make_drive(
            "at_cutoff",
            10.0,
            4.0,
            cutoff_dt.isoformat(),
            cutoff_dt.isoformat(),
        )
        just_before = _make_drive(
            "just_before",
            10.0,
            4.0,
            (cutoff_dt - timedelta(seconds=1)).isoformat(),
            (cutoff_dt - timedelta(seconds=1)).isoformat(),
        )
        just_after = _make_drive(
            "just_after",
            10.0,
            4.0,
            (cutoff_dt + timedelta(seconds=1)).isoformat(),
            (cutoff_dt + timedelta(seconds=1)).isoformat(),
        )
        analytics_db.upsert_drives(TEST_VIN, [at_cutoff, just_before, just_after])

        removed = analytics_db.prune(TEST_VIN, cutoff_ts)
        # Only the strictly-older-than-cutoff row is removed.
        assert removed == 1

        now_ts = datetime.now(UTC).timestamp()
        stats = analytics_db.window_stats(TEST_VIN, None, now_ts)
        assert stats.drive_count == 2

    def test_prune_removes_nothing_when_all_rows_newer_than_cutoff(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        recent = _make_drive(
            "recent_1",
            10.0,
            4.0,
            datetime.now(UTC).isoformat(),
            datetime.now(UTC).isoformat(),
        )
        analytics_db.upsert_drives(TEST_VIN, [recent])
        ancient_cutoff = datetime(2000, 1, 1, tzinfo=UTC).timestamp()
        removed = analytics_db.prune(TEST_VIN, ancient_cutoff)
        assert removed == 0


class _PoisonedAnalyticsDatabase(AnalyticsDatabase):
    """An AnalyticsDatabase stand-in whose every executor-bound method raises.

    Used to prove DriveStore's synchronous cache-only surface (and the
    sensor entities built on top of it) never falls through to SQLite.
    """

    def _boom(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "AnalyticsDatabase method was called from a supposedly cache-only path"
        )

    get_meta = _boom
    set_meta = _boom
    upsert_drives = _boom
    insert_vampire_event = _boom
    merge_vampire_events = _boom
    upsert_dcfc_sessions = _boom
    prune = _boom
    window_stats = _boom
    build_cache = _boom
    close = _boom


class _FakeTracker:
    """Minimal stand-in exposing only what RivianDriveSensorEntity reads from a tracker."""

    def __init__(self) -> None:
        from custom_components.rivian.drive_models import DriveState, DriveStatus

        self.drive_state = DriveState(is_driving=False, status=DriveStatus.PARKED.value)
        self.is_debouncing_park = False

    def async_add_listener(self, _callback: Any) -> Any:
        return lambda: None


class TestCachePurity:
    """Regression guard: DriveStore's sync surface and sensor entities must never touch SQLite."""

    @pytest.mark.asyncio
    async def test_all_sync_accessors_and_sensor_properties_survive_poisoned_db(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """With every AnalyticsDatabase method poisoned to raise, all DriveStore sync
        properties and the sensor entity's native_value/extra_state_attributes must
        still work without ever invoking the (poisoned) database."""
        # Populate real data first, using a *working* database...
        real_db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        real_db.setup()
        store = DriveStore(mock_hass, TEST_VIN, real_db)
        await store.async_load()

        drive = _make_drive(
            "purity_1", 20.0, 6.0, "2026-08-20T14:30:00Z", "2026-08-20T15:05:00Z"
        )
        await store.async_save_drive(drive)
        await store.async_save_vampire_events(
            [
                VampireDrainRecord(
                    start_time="2026-08-20T10:00:00Z",
                    end_time="2026-08-20T14:30:00Z",
                    idle_hours=4.5,
                    start_soc=83.0,
                    end_soc=82.5,
                    drain_soc=0.5,
                    drain_kwh=0.68,
                    rate_pct_per_day=2.67,
                    avg_watts=150.0,
                )
            ]
        )
        await store.async_save_dcfc_sessions(
            [
                ChargingSessionRecord(
                    session_id="purity_dcfc_1",
                    start_time="2026-08-20T13:00:00Z",
                    end_time="2026-08-20T13:30:00Z",
                    start_soc=20.0,
                    end_soc=80.0,
                    energy_added_kwh=81.0,
                    max_power_kw=180.0,
                    avg_power_kw=120.0,
                    samples=[
                        ChargingSample(
                            timestamp="2026-08-20T13:00:00Z", soc=20.0, power_kw=180.0
                        )
                    ],
                )
            ]
        )
        real_db.close()

        # ...then swap in a poisoned database that raises on every method, and
        # rebuild the DriveStore's cache manually (bypassing async_refresh_cache,
        # which itself would call the poisoned build_cache).
        poisoned_db = _PoisonedAnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        store._db = poisoned_db

        # Exercise every documented sync, cache-only accessor.
        assert store.last_drive is not None
        assert store.last_drive.drive_id == "purity_1"
        assert store.drive_count == 1
        assert isinstance(store.recent_drives, list) and len(store.recent_drives) == 1
        assert (
            isinstance(store.recent_vampire_events, list)
            and len(store.recent_vampire_events) == 1
        )
        assert store.revision > 0
        assert store.is_loaded is True
        dcfc_sessions = store.get_dcfc_sessions(limit=50)
        assert len(dcfc_sessions) == 1
        for stats_getter in (
            store.get_stats_30d,
            store.get_stats_90d,
            store.get_stats_365d,
            store.get_stats_all_time,
        ):
            stats = stats_getter()
            assert stats is not None

        # Exercise the sensor entity properties built on top of this store;
        # neither native_value nor extra_state_attributes may touch SQLite.
        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=None)
        vehicle = {
            "id": "veh-1",
            "vin": TEST_VIN,
            "name": "r1s_adventure",
            "model": "R1S",
        }
        tracker = _FakeTracker()
        for desc in DRIVE_SENSORS:
            entity = RivianDriveSensorEntity(
                coordinator=coordinator,
                config_entry=MagicMock(),
                description=desc,
                vehicle=vehicle,
                tracker=tracker,  # type: ignore[arg-type]
                store=store,
            )
            # Must not raise -- if any property fell through to sqlite, the
            # poisoned method's AssertionError would propagate here.
            _ = entity.native_value
            _ = entity.extra_state_attributes


class TestSpeedBinTotals:
    """Speed-bin totals cover the whole storage window, not the 90-day chart window."""

    def test_totals_sum_every_retained_non_micro_drive(self, analytics_db: Any) -> None:
        now = datetime(2026, 9, 23, tzinfo=UTC)

        def drive(
            n: int, days_ago: int, miles: float, micro: bool = False
        ) -> DriveRecord:
            start = (now - timedelta(days=days_ago)).isoformat()
            record = _make_drive(
                f"d{n}", miles, max(miles / 3, 0.1), start, start, is_micro_drive=micro
            )
            record.speed_bins = {"30-39": SpeedBinData(miles=miles, seconds=miles * 90)}
            return record

        analytics_db.upsert_drives(
            TEST_VIN,
            [drive(1, 5, 4.0), drive(2, 200, 6.0), drive(3, 1, 0.3, micro=True)],
        )
        cache = analytics_db.build_cache(TEST_VIN, now.timestamp())

        assert list(cache.speed_bin_totals) == list(STANDARD_SPEED_BINS)
        # The 200-day-old drive counts; the micro-drive doesn't.
        assert cache.speed_bin_totals["30-39"] == {"miles": 10.0, "seconds": 900.0}
        assert cache.speed_bin_totals["0-9"] == {"miles": 0.0, "seconds": 0.0}
        # The 90-day chart window is unaffected.
        assert [d.drive_id for d in cache.recent_drives] == ["d1"]


class TestSchemaAndIntegrity:
    """The schema ships as version 1; the integrity check runs only when due."""

    def test_fresh_db_creates_the_schema(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """A new database gets the whole schema at version 1."""
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            with db._lock:
                tables = {
                    row[0]
                    for row in db._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                drive_columns = {
                    row[1]
                    for row in db._conn.execute("PRAGMA table_info(drives)").fetchall()
                }
                user_version = db._conn.execute("PRAGMA user_version").fetchone()[0]
            assert {
                "drives",
                "drive_tracks",
                "active_drive",
                "active_track_chunks",
                "vehicle_pictures",
                "road_heat",
                "road_heat_drives",
                "places",
                "routes",
                "capacity_history",
            } <= tables
            assert "route_id" in drive_columns
            assert user_version == SCHEMA_VERSION == 1
            assert db.get_meta("schema_version") == "1"
            assert db.read_only is False
        finally:
            db.close()

    def test_newer_schema_opens_read_only(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """A database from a newer version (a downgrade) is opened read-only."""
        conn = sqlite3.connect(analytics_db_path)
        conn.execute("PRAGMA user_version = 2")
        conn.close()
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            assert db.read_only is True
        finally:
            db.close()

    @staticmethod
    def _open_counting_checks(
        mock_hass: Any, path: str, monkeypatch: Any
    ) -> tuple[AnalyticsDatabase, list[int]]:
        calls: list[int] = []
        real = AnalyticsDatabase._quick_check_ok

        def counting(conn: sqlite3.Connection) -> bool:
            calls.append(1)
            return real(conn)

        monkeypatch.setattr(
            AnalyticsDatabase, "_quick_check_ok", staticmethod(counting)
        )
        db = AnalyticsDatabase(mock_hass, db_path=path)
        db.setup()
        return db, calls

    def test_quick_check_only_after_unclean_shutdown(
        self, mock_hass: Any, analytics_db_path: str, monkeypatch: Any
    ) -> None:
        """Checked on first open and after a crash, not after a clean close."""
        db, calls = self._open_counting_checks(
            mock_hass, analytics_db_path, monkeypatch
        )
        assert len(calls) == 1
        db.close()

        db, calls = self._open_counting_checks(
            mock_hass, analytics_db_path, monkeypatch
        )
        assert calls == []
        # Simulate a crash: the connection goes away without close()
        with db._lock:
            db._conn.close()
            db._conn = None

        db, calls = self._open_counting_checks(
            mock_hass, analytics_db_path, monkeypatch
        )
        assert len(calls) == 1
        db.close()

    def test_quick_check_runs_again_after_the_interval(
        self, mock_hass: Any, analytics_db_path: str, monkeypatch: Any
    ) -> None:
        """A clean database is still checked once the last check is old enough."""
        db, _calls = self._open_counting_checks(
            mock_hass, analytics_db_path, monkeypatch
        )
        db.set_meta(
            analytics_db_module.LAST_QUICK_CHECK_META_KEY,
            str(time.time() - analytics_db_module.QUICK_CHECK_INTERVAL_S - 1),
        )
        db.close()
        db, calls = self._open_counting_checks(
            mock_hass, analytics_db_path, monkeypatch
        )
        assert len(calls) == 1
        db.close()

    def test_corrupt_file_is_quarantined(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """A file that fails the check is moved aside and a new one created."""
        with open(analytics_db_path, "wb") as fh:
            fh.write(b"not a database" * 100)
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        try:
            assert db.was_repaired is True
            assert db.read_only is False
            assert db.get_meta("schema_version") == "1"
        finally:
            db.close()


class TestUpsertTracks:
    """upsert_tracks: writes, short-track skip, and live-vs-backfill precedence."""

    def test_writes_and_skips_short_tracks(self, analytics_db: Any) -> None:
        analytics_db.upsert_drives(TEST_VIN, [_make_drive("d1", 5.0, 2.0, None, None)])
        good_track = _make_track(n=5)
        short_track = DriveTrack([TrackPoint(t=1.0, lat=1.0, lon=1.0)])

        written = analytics_db.upsert_tracks(
            TEST_VIN, [("d1", good_track), ("d2", short_track)]
        )
        assert written == 1
        assert analytics_db.get_track(TEST_VIN, "d1") is not None
        assert analytics_db.get_track(TEST_VIN, "d2") is None

    def test_backfill_does_not_replace_live(self, analytics_db: Any) -> None:
        live_track = _make_track(n=5, lat0=10.0)
        analytics_db.upsert_tracks(TEST_VIN, [("d1", live_track)], source="live")

        backfill_track = _make_track(n=8, lat0=99.0)
        written = analytics_db.upsert_tracks(
            TEST_VIN, [("d1", backfill_track)], source="backfill"
        )
        assert written == 0
        stored = analytics_db.get_track(TEST_VIN, "d1")
        assert len(stored) == 5
        assert stored.points[0].lat == pytest.approx(10.0)

    def test_live_replaces_backfill(self, analytics_db: Any) -> None:
        backfill_track = _make_track(n=4, lat0=20.0)
        analytics_db.upsert_tracks(
            TEST_VIN, [("d1", backfill_track)], source="backfill"
        )

        live_track = _make_track(n=7, lat0=50.0)
        written = analytics_db.upsert_tracks(
            TEST_VIN, [("d1", live_track)], source="live"
        )
        assert written == 1
        stored = analytics_db.get_track(TEST_VIN, "d1")
        assert len(stored) == 7
        assert stored.points[0].lat == pytest.approx(50.0)

    def test_sort_ts_uses_drive_row_when_present(self, analytics_db: Any) -> None:
        drive = _make_drive(
            "d1", 5.0, 2.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        track = _make_track(n=3, start_t=1_600_000_000.0)
        analytics_db.upsert_tracks(TEST_VIN, [("d1", track)])

        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT sort_ts FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "d1"),
            ).fetchone()
        expected = analytics_db._conn.execute(
            "SELECT sort_ts FROM drives WHERE vin = ? AND drive_id = ?",
            (TEST_VIN, "d1"),
        ).fetchone()["sort_ts"]
        assert row["sort_ts"] == expected
        assert row["sort_ts"] != track.points[0].t

    def test_sort_ts_uses_first_point_when_no_drive_row(
        self, analytics_db: Any
    ) -> None:
        track = _make_track(n=3, start_t=1_650_000_000.0)
        analytics_db.upsert_tracks(TEST_VIN, [("orphan_drive", track)])
        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT sort_ts FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "orphan_drive"),
            ).fetchone()
        assert row["sort_ts"] == track.points[0].t


class TestFinalizeDrive:
    """finalize_drive: one atomic transaction covering drive + track + checkpoint clear."""

    def test_finalize_writes_drive_and_track_and_clears_checkpoint(
        self, analytics_db: Any
    ) -> None:
        analytics_db.save_active_checkpoint(
            TEST_VIN, "d1", {"foo": "bar"}, _make_track(n=2), 0
        )
        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        track = _make_track(n=6)

        is_new = analytics_db.finalize_drive(TEST_VIN, record, track)
        assert is_new is True

        stored_track = analytics_db.get_track(TEST_VIN, "d1")
        assert stored_track is not None
        assert len(stored_track) == 6
        assert analytics_db.load_active_checkpoint(TEST_VIN) is None

        # A second finalize of the same drive_id is an update, not a new row.
        is_new_2 = analytics_db.finalize_drive(TEST_VIN, record, track)
        assert is_new_2 is False

    def test_finalize_atomic_on_track_encode_failure(
        self, analytics_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        analytics_db.save_active_checkpoint(
            TEST_VIN, "d1", {"foo": "bar"}, _make_track(n=2), 0
        )

        def _boom(self: DriveTrack) -> str:
            raise RuntimeError("encode boom")

        monkeypatch.setattr(DriveTrack, "encode", _boom)

        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        track = _make_track(n=6)
        with pytest.raises(RuntimeError, match="encode boom"):
            analytics_db.finalize_drive(TEST_VIN, record, track)

        with analytics_db._lock:
            drive_row = analytics_db._conn.execute(
                "SELECT 1 FROM drives WHERE vin = ? AND drive_id = ?", (TEST_VIN, "d1")
            ).fetchone()
        assert drive_row is None
        checkpoint = analytics_db.load_active_checkpoint(TEST_VIN)
        assert checkpoint is not None
        assert checkpoint.drive_id == "d1"


class TestActiveCheckpoint:
    """save/load/clear_active_checkpoint round-trip and drive-switch chunk cleanup."""

    def test_save_load_roundtrip_concatenates_chunks_in_order(
        self, analytics_db: Any
    ) -> None:
        chunk0 = _make_track(n=2, start_t=1_700_000_000.0)
        chunk1 = _make_track(n=2, start_t=1_700_000_100.0, lat0=40.0)
        chunk2 = _make_track(n=2, start_t=1_700_000_200.0, lat0=50.0)

        analytics_db.save_active_checkpoint(TEST_VIN, "d1", {"step": 0}, chunk0, 0)
        analytics_db.save_active_checkpoint(TEST_VIN, "d1", {"step": 1}, chunk1, 1)
        analytics_db.save_active_checkpoint(TEST_VIN, "d1", {"step": 2}, chunk2, 2)

        checkpoint = analytics_db.load_active_checkpoint(TEST_VIN)
        assert isinstance(checkpoint, ActiveCheckpoint)
        assert checkpoint.drive_id == "d1"
        assert checkpoint.state == {"step": 2}
        assert checkpoint.next_seq == 3
        assert len(checkpoint.track) == 6
        assert [p.lat for p in checkpoint.track.points[:2]] == [
            pytest.approx(chunk0.points[0].lat),
            pytest.approx(chunk0.points[1].lat),
        ]
        assert checkpoint.track.points[-1].lat == pytest.approx(chunk2.points[-1].lat)

        analytics_db.clear_active_checkpoint(TEST_VIN)
        assert analytics_db.load_active_checkpoint(TEST_VIN) is None

    def test_save_for_different_drive_wipes_old_chunks(self, analytics_db: Any) -> None:
        analytics_db.save_active_checkpoint(
            TEST_VIN, "d1", {"a": 1}, _make_track(n=2), 0
        )
        analytics_db.save_active_checkpoint(
            TEST_VIN, "d2", {"b": 2}, _make_track(n=3, lat0=60.0), 0
        )

        checkpoint = analytics_db.load_active_checkpoint(TEST_VIN)
        assert checkpoint.drive_id == "d2"
        assert len(checkpoint.track) == 3

        with analytics_db._lock:
            old_chunks = analytics_db._conn.execute(
                "SELECT COUNT(*) AS c FROM active_track_chunks WHERE vin = ? "
                "AND drive_id = ?",
                (TEST_VIN, "d1"),
            ).fetchone()["c"]
        assert old_chunks == 0

    def test_load_returns_none_when_absent(self, analytics_db: Any) -> None:
        assert analytics_db.load_active_checkpoint(TEST_VIN) is None


class TestListDrivesAndDetail:
    """list_drives ordering/paging and get_drive_detail payload shape."""

    def test_list_drives_ordering_paging_and_fields(self, analytics_db: Any) -> None:
        base = datetime(2026, 9, 1, tzinfo=UTC)
        drives = [
            _make_drive(
                f"d{i}",
                10.0,
                4.0,
                (base + timedelta(hours=i)).isoformat(),
                (base + timedelta(hours=i)).isoformat(),
            )
            for i in range(5)
        ]
        analytics_db.upsert_drives(TEST_VIN, drives)
        analytics_db.upsert_tracks(TEST_VIN, [("d3", _make_track(n=4))])

        page1 = analytics_db.list_drives(TEST_VIN, limit=3)
        assert [d["drive_id"] for d in page1] == ["d4", "d3", "d2"]
        assert page1[0]["has_track"] is False
        d3_summary = page1[1]
        assert d3_summary["has_track"] is True
        assert d3_summary["track_source"] == "live"
        assert d3_summary["track_detail"] == "full"
        assert d3_summary["point_count"] == 4

        expected_keys = {
            "drive_id",
            "start_time",
            "end_time",
            "start_ts",
            "end_ts",
            "distance_miles",
            "duration_seconds",
            "energy_kwh",
            "efficiency_mi_kwh",
            "mpge",
            "avg_speed_mph",
            "max_speed_mph",
            "temp_f",
            "elevation_change_ft",
            "start_soc",
            "end_soc",
            "is_micro_drive",
            "start_lat",
            "start_lon",
            "end_lat",
            "end_lon",
            "has_track",
            "track_source",
            "track_detail",
            "point_count",
            "sort_ts",
            "moving_seconds",
            "stopped_seconds",
            "stop_count",
            "climb_ft",
            "descent_ft",
            "track_max_speed_mph",
            "pct_distance_over_70mph",
            "start_range_mi",
            "end_range_mi",
            "range_used_mi",
            "drive_modes",
            "trailer",
            "driver",
        }
        assert set(page1[0]) == expected_keys

        page2 = analytics_db.list_drives(
            TEST_VIN, before_ts=page1[-1]["sort_ts"], limit=3
        )
        assert [d["drive_id"] for d in page2] == ["d1", "d0"]

    def test_list_drives_include_micro(self, analytics_db: Any) -> None:
        normal = _make_drive(
            "normal", 10.0, 4.0, "2026-08-20T10:00:00Z", "2026-08-20T10:20:00Z"
        )
        micro = _make_drive(
            "micro",
            0.2,
            0.1,
            "2026-08-20T11:00:00Z",
            "2026-08-20T11:05:00Z",
            is_micro_drive=True,
        )
        analytics_db.upsert_drives(TEST_VIN, [normal, micro])

        default_result = analytics_db.list_drives(TEST_VIN)
        assert [d["drive_id"] for d in default_result] == ["normal"]

        with_micro = analytics_db.list_drives(TEST_VIN, include_micro=True)
        assert {d["drive_id"] for d in with_micro} == {"normal", "micro"}

    def test_get_drive_detail_with_track(self, analytics_db: Any) -> None:
        record = _make_drive(
            "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
        )
        track = _make_track(n=4)
        analytics_db.finalize_drive(TEST_VIN, record, track)

        detail = analytics_db.get_drive_detail(TEST_VIN, "d1")
        assert detail is not None
        assert detail["drive"]["drive_id"] == "d1"
        assert "speed_bins" in detail["drive"]
        assert "chunks" in detail["drive"]
        assert detail["track"] is not None
        assert len(detail["track"]["lat"]) == 4

    def test_get_drive_detail_without_track(self, analytics_db: Any) -> None:
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive(
                    "d1", 8.0, 3.0, "2026-08-20T14:30:00Z", "2026-08-20T15:00:00Z"
                )
            ],
        )
        detail = analytics_db.get_drive_detail(TEST_VIN, "d1")
        assert detail is not None
        assert detail["track"] is None

    def test_get_drive_detail_unknown_returns_none(self, analytics_db: Any) -> None:
        assert analytics_db.get_drive_detail(TEST_VIN, "does-not-exist") is None


class TestTrackPreviewsAndMissing:
    """get_track_previews and drives_missing_tracks."""

    def test_get_track_previews(self, analytics_db: Any) -> None:
        analytics_db.upsert_tracks(
            TEST_VIN, [("d1", _make_track(n=4)), ("d2", _make_track(n=3, lat0=5.0))]
        )
        previews = analytics_db.get_track_previews(TEST_VIN, ["d1", "d2", "missing"])
        assert set(previews) == {"d1", "d2"}
        assert len(previews["d1"]["lat"]) == 4
        assert len(previews["d1"]["lon"]) == 4

    def test_drives_missing_tracks_filters(self, analytics_db: Any) -> None:
        now = datetime(2026, 9, 1, tzinfo=UTC)
        has_track = _make_drive("has_track", 5.0, 2.0, now.isoformat(), now.isoformat())
        missing_recent = _make_drive(
            "missing_recent",
            5.0,
            2.0,
            (now + timedelta(hours=1)).isoformat(),
            (now + timedelta(hours=1)).isoformat(),
        )
        missing_old = _make_drive(
            "missing_old",
            5.0,
            2.0,
            (now - timedelta(days=30)).isoformat(),
            (now - timedelta(days=30)).isoformat(),
        )
        null_ts = _make_drive("null_ts", 5.0, 2.0, "garbage", "garbage")
        analytics_db.upsert_drives(
            TEST_VIN, [has_track, missing_recent, missing_old, null_ts]
        )
        analytics_db.upsert_tracks(TEST_VIN, [("has_track", _make_track(n=3))])

        since_ts = (now - timedelta(days=1)).timestamp()
        missing = analytics_db.drives_missing_tracks(TEST_VIN, since_ts)
        drive_ids = [m[0] for m in missing]
        assert drive_ids == ["missing_recent"]


class TestPruneWithTracks:
    """prune() removes orphan tracks."""

    def test_prune_removes_orphan_tracks(self, analytics_db: Any) -> None:
        old = _make_drive(
            "old", 5.0, 2.0, "2020-01-01T00:00:00Z", "2020-01-01T00:10:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [old])
        analytics_db.upsert_tracks(TEST_VIN, [("old", _make_track(n=3))])

        cutoff = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
        analytics_db.prune(TEST_VIN, cutoff)

        with analytics_db._lock:
            row = analytics_db._conn.execute(
                "SELECT 1 FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "old"),
            ).fetchone()
        assert row is None


class TestPruneTracks:
    """prune_tracks: delete/thin cutoffs, idempotency, and storage_stats."""

    def test_delete_before_cutoff_keeps_drive_row(self, analytics_db: Any) -> None:
        drive = _make_drive(
            "d1", 5.0, 2.0, "2020-01-01T00:00:00Z", "2020-01-01T00:10:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=3))])

        cutoff = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
        result = analytics_db.prune_tracks(TEST_VIN, cutoff, None)
        assert result == {"deleted": 1, "thinned": 0}
        assert analytics_db.get_track(TEST_VIN, "d1") is None

        with analytics_db._lock:
            drive_row = analytics_db._conn.execute(
                "SELECT 1 FROM drives WHERE vin = ? AND drive_id = ?", (TEST_VIN, "d1")
            ).fetchone()
        assert drive_row is not None

    def test_thin_reduces_points_keeps_endpoints_and_is_idempotent(
        self, analytics_db: Any
    ) -> None:
        drive = _make_drive(
            "d1", 5.0, 2.0, "2020-01-01T00:00:00Z", "2020-01-01T00:10:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])

        # A track with a long straight run of points simplifies down heavily
        # under Douglas-Peucker, but the endpoints are always kept.
        track = DriveTrack()
        for i in range(40):
            track.append(
                TrackPoint(t=1_700_000_000.0 + i, lat=37.0 + i * 1e-6, lon=-122.0)
            )
        analytics_db.upsert_tracks(TEST_VIN, [("d1", track)])

        cutoff = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
        result = analytics_db.prune_tracks(
            TEST_VIN, None, cutoff, thin_tolerance_m=50.0
        )
        assert result["thinned"] == 1
        assert result["deleted"] == 0

        thinned_track = analytics_db.get_track(TEST_VIN, "d1")
        assert len(thinned_track) < 40
        assert thinned_track.points[0].t == track.points[0].t
        assert thinned_track.points[-1].t == track.points[-1].t

        with analytics_db._lock:
            detail = analytics_db._conn.execute(
                "SELECT detail FROM drive_tracks WHERE vin = ? AND drive_id = ?",
                (TEST_VIN, "d1"),
            ).fetchone()["detail"]
        assert detail == "thinned"

        # Second run: nothing left at detail='full' before the cutoff.
        result2 = analytics_db.prune_tracks(
            TEST_VIN, None, cutoff, thin_tolerance_m=50.0
        )
        assert result2 == {"deleted": 0, "thinned": 0}

    def test_unreadable_track_is_dropped_not_retried_forever(
        self, analytics_db: Any
    ) -> None:
        """A corrupt row must not abort thinning or be re-selected endlessly."""
        for drive_id in ("bad", "good"):
            analytics_db.upsert_drives(
                TEST_VIN,
                [
                    _make_drive(
                        drive_id,
                        5.0,
                        2.0,
                        "2020-01-01T00:00:00Z",
                        "2020-01-01T00:10:00Z",
                    )
                ],
            )
            analytics_db.upsert_tracks(TEST_VIN, [(drive_id, _make_track(n=5))])
        with analytics_db._lock:
            analytics_db._conn.execute(
                "UPDATE drive_tracks SET track_json = '{\"v\": 99}' WHERE drive_id = ?",
                ("bad",),
            )

        cutoff = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
        result = analytics_db.prune_tracks(TEST_VIN, None, cutoff)

        assert result["thinned"] == 1
        assert analytics_db.get_track(TEST_VIN, "bad") is None
        assert analytics_db.get_track(TEST_VIN, "good") is not None

    def test_none_cutoffs_skip_both_steps(self, analytics_db: Any) -> None:
        drive = _make_drive(
            "d1", 5.0, 2.0, "2020-01-01T00:00:00Z", "2020-01-01T00:10:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        analytics_db.upsert_tracks(TEST_VIN, [("d1", _make_track(n=3))])

        result = analytics_db.prune_tracks(TEST_VIN, None, None)
        assert result == {"deleted": 0, "thinned": 0}
        assert analytics_db.get_track(TEST_VIN, "d1") is not None

    def test_storage_stats_counts_full_and_thinned_and_bytes(
        self, analytics_db: Any
    ) -> None:
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive(
                    "d1", 5.0, 2.0, "2020-01-01T00:00:00Z", "2020-01-01T00:10:00Z"
                ),
                _make_drive(
                    "d2", 5.0, 2.0, "2026-08-01T00:00:00Z", "2026-08-01T00:10:00Z"
                ),
            ],
        )
        analytics_db.upsert_tracks(
            TEST_VIN, [("d1", _make_track(n=3)), ("d2", _make_track(n=3, lat0=20.0))]
        )
        cutoff = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
        analytics_db.prune_tracks(TEST_VIN, None, cutoff, thin_tolerance_m=50.0)

        stats = analytics_db.storage_stats(TEST_VIN)
        assert stats["drive_count"] == 2
        assert stats["track_count"] == 2
        assert stats["full_count"] == 1
        assert stats["thinned_count"] == 1
        assert stats["track_bytes"] > 0
        assert stats["db_bytes"] > 0


class TestRecomputeDriveStats:
    """recompute_drive_stats: batching, capacity fallback, context columns untouched."""

    @staticmethod
    def _track_with_telemetry(n: int = 6) -> DriveTrack:
        track = DriveTrack()
        for i in range(n):
            track.append(
                TrackPoint(
                    t=1_700_000_000.0 + i * 10,
                    lat=37.0 + i * 0.001,
                    lon=-122.0 + i * 0.001,
                    speed_mps=15.0,
                    alt_m=10.0 * i,
                    soc=80.0 - i,
                )
            )
        return track

    def test_recomputes_null_stats_from_stored_track(self, analytics_db: Any) -> None:
        drive = _make_drive(
            "d1", 5.0, 2.0, "2026-08-01T10:00:00Z", "2026-08-01T10:20:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        analytics_db.upsert_tracks(TEST_VIN, [("d1", self._track_with_telemetry())])

        assert analytics_db.has_unrecomputed_drive_stats(TEST_VIN) is True

        result = analytics_db.recompute_drive_stats(TEST_VIN)
        assert result == {"updated": 1}
        assert analytics_db.has_unrecomputed_drive_stats(TEST_VIN) is False

        detail = analytics_db.get_drive_detail(TEST_VIN, "d1")
        summary = detail["drive"]
        assert summary["moving_seconds"] == pytest.approx(50.0)
        assert summary["stopped_seconds"] == pytest.approx(0.0)

    def test_never_touches_live_only_context_columns(self, analytics_db: Any) -> None:
        drive = DriveRecord(
            vin=TEST_VIN,
            drive_id="d1",
            start_time="2026-08-01T10:00:00Z",
            end_time="2026-08-01T10:20:00Z",
            distance_miles=5.0,
            duration_seconds=600.0,
            start_soc=80.0,
            end_soc=75.0,
            battery_capacity_kwh=135.0,
            energy_kwh=2.0,
            start_range_mi=200.0,
            end_range_mi=190.0,
            drive_modes=["Sport"],
            trailer=True,
            driver="Kelly",
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        analytics_db.upsert_tracks(TEST_VIN, [("d1", self._track_with_telemetry())])

        analytics_db.recompute_drive_stats(TEST_VIN)

        detail = analytics_db.get_drive_detail(TEST_VIN, "d1")
        summary = detail["drive"]
        assert summary["start_range_mi"] == pytest.approx(200.0)
        assert summary["end_range_mi"] == pytest.approx(190.0)
        # A legacy display label reads back as the raw drive mode
        assert summary["drive_modes"] == ["sport"]
        assert summary["trailer"] is True
        assert summary["driver"] == "Kelly"

    def test_batches_across_many_drives(self, analytics_db: Any) -> None:
        for i in range(120):
            drive = _make_drive(
                f"d{i}",
                5.0,
                2.0,
                f"2026-08-{(i % 28) + 1:02d}T10:00:00Z",
                f"2026-08-{(i % 28) + 1:02d}T10:20:00Z",
            )
            analytics_db.upsert_drives(TEST_VIN, [drive])
            analytics_db.upsert_tracks(
                TEST_VIN, [(f"d{i}", self._track_with_telemetry())]
            )

        result = analytics_db.recompute_drive_stats(TEST_VIN)
        assert result == {"updated": 120}
        assert analytics_db.has_unrecomputed_drive_stats(TEST_VIN) is False

    def test_unreadable_track_is_skipped_not_fatal(self, analytics_db: Any) -> None:
        drive = _make_drive(
            "bad", 5.0, 2.0, "2026-08-01T10:00:00Z", "2026-08-01T10:20:00Z"
        )
        analytics_db.upsert_drives(TEST_VIN, [drive])
        analytics_db.upsert_tracks(TEST_VIN, [("bad", self._track_with_telemetry())])
        with analytics_db._lock:
            analytics_db._conn.execute(
                "UPDATE drive_tracks SET track_json = '{\"v\": 99}' WHERE drive_id = ?",
                ("bad",),
            )

        result = analytics_db.recompute_drive_stats(TEST_VIN)
        assert result == {"updated": 0}
        # Its stats stay NULL, but that mustn't retrigger a full recompute on
        # every restart.
        assert analytics_db.has_unrecomputed_drive_stats(TEST_VIN) is False

    def test_read_only_skips(self, analytics_db: Any) -> None:
        analytics_db.upsert_drives(
            TEST_VIN,
            [
                _make_drive(
                    "d1", 5.0, 2.0, "2026-08-01T10:00:00Z", "2026-08-01T10:20:00Z"
                )
            ],
        )
        analytics_db.upsert_tracks(TEST_VIN, [("d1", self._track_with_telemetry())])
        analytics_db.read_only = True
        result = analytics_db.recompute_drive_stats(TEST_VIN)
        assert result == {"updated": 0}


class TestSeriesWindow:
    """series_window: an arbitrary-length window beyond the 90-day hot cache."""

    def test_series_window_beyond_90_days_ascending_excludes_micro(
        self, analytics_db: Any
    ) -> None:
        now = datetime(2026, 9, 23, tzinfo=UTC)
        old = _make_drive(
            "old_200d",
            10.0,
            4.0,
            (now - timedelta(days=200)).isoformat(),
            (now - timedelta(days=200)).isoformat(),
        )
        recent = _make_drive(
            "recent_5d",
            10.0,
            4.0,
            (now - timedelta(days=5)).isoformat(),
            (now - timedelta(days=5)).isoformat(),
        )
        micro = _make_drive(
            "micro",
            0.2,
            0.1,
            (now - timedelta(days=10)).isoformat(),
            (now - timedelta(days=10)).isoformat(),
            is_micro_drive=True,
        )
        analytics_db.upsert_drives(TEST_VIN, [old, recent, micro])

        drives, _vampire = analytics_db.series_window(TEST_VIN, 365, now.timestamp())
        assert [d.drive_id for d in drives] == ["old_200d", "recent_5d"]


class TestLoopThreadGuardOnNewMethods:
    """Every new public method must refuse to run on the (simulated) loop thread."""

    def test_new_public_methods_raise_on_loop_thread(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
        db.setup()
        record = _make_drive(
            "d1", 5.0, 2.0, "2026-08-20T10:00:00Z", "2026-08-20T10:10:00Z"
        )
        track = _make_track(n=2)

        # _loop_thread_id is captured once at construction time, so flip it
        # directly (mirroring what a real loop-thread call would compare
        # against) rather than mutating mock_hass after the fact.
        db._loop_thread_id = threading.get_ident()
        calls = [
            (db.upsert_tracks, (TEST_VIN, [("d1", track)])),
            (db.finalize_drive, (TEST_VIN, record, track)),
            (db.get_track, (TEST_VIN, "d1")),
            (db.list_drives, (TEST_VIN,)),
            (db.get_track_previews, (TEST_VIN, ["d1"])),
            (db.get_drive_detail, (TEST_VIN, "d1")),
            (db.drives_missing_tracks, (TEST_VIN, 0.0)),
            (db.save_active_checkpoint, (TEST_VIN, "d1", {}, track, 0)),
            (db.load_active_checkpoint, (TEST_VIN,)),
            (db.clear_active_checkpoint, (TEST_VIN,)),
            (db.prune_tracks, (TEST_VIN, None, None)),
            (db.storage_stats, (TEST_VIN,)),
            (db.series_window, (TEST_VIN, 30, time.time())),
        ]
        for method, args in calls:
            with pytest.raises(RuntimeError, match="executor"):
                method(*args)

        db._loop_thread_id = -1
        db.close()
