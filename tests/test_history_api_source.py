"""Tests for reading recorder data through the history API (HistoryApiRecorderSource)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import history_backfill
from custom_components.rivian.history_backfill import (
    HistoryApiRecorderSource,
    SqliteRecorderSource,
    async_backfill_from_recorder,
    reconstruct_tracks_for_windows,
)

from tests.test_history_backfill import (
    FIXTURE_DB_PATH,
    TEST_VEHICLE_ID,
    TEST_VIN,
    _build_track_recorder_db,
)


class _FakeState:
    """The parts of ``homeassistant.core.State`` the source reads."""

    def __init__(
        self, state: str | None, ts: float, attributes: dict[str, Any] | None
    ) -> None:
        self.state = state
        self.last_updated = datetime.fromtimestamp(ts, tz=UTC)
        self.attributes = attributes or {}


class _FakeRecorderHistory:
    """Stands in for ``recorder.history``, serving rows from a recorder SQLite file.

    Records every call so tests can check how often, and with what
    arguments, the source queries it.
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        cur = self._conn.cursor()
        cur.execute("SELECT metadata_id, entity_id FROM states_meta")
        self.meta = {r["entity_id"]: r["metadata_id"] for r in cur.fetchall()}
        cur.execute("SELECT name FROM sqlite_master WHERE name='state_attributes'")
        self._has_attrs_table = cur.fetchone() is not None
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _rows(self, entity_id: str, start_ts: float, with_attrs: bool) -> list[Any]:
        mid = self.meta.get(entity_id)
        if mid is None:
            return []
        if with_attrs and self._has_attrs_table:
            sql = (
                "SELECT s.state, s.last_updated_ts AS ts, a.shared_attrs AS attrs "
                "FROM states s LEFT JOIN state_attributes a "
                "ON s.attributes_id = a.attributes_id "
                "WHERE s.metadata_id = ? AND s.last_updated_ts > ? "
                "ORDER BY s.last_updated_ts"
            )
        elif with_attrs:
            sql = (
                "SELECT state, last_updated_ts AS ts, attributes AS attrs FROM states "
                "WHERE metadata_id = ? AND last_updated_ts > ? ORDER BY last_updated_ts"
            )
        else:
            sql = (
                "SELECT state, last_updated_ts AS ts, NULL AS attrs FROM states "
                "WHERE metadata_id = ? AND last_updated_ts > ? ORDER BY last_updated_ts"
            )
        return self._conn.execute(sql, (mid, start_ts)).fetchall()

    @staticmethod
    def _state(row: Any, with_attrs: bool) -> _FakeState:
        attrs = json.loads(row["attrs"]) if with_attrs and row["attrs"] else {}
        return _FakeState(row["state"], row["ts"], attrs)

    def get_significant_states(
        self,
        hass: Any,
        start_time: datetime,
        end_time: datetime | None = None,
        entity_ids: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, list[_FakeState]]:
        assert entity_ids and len(entity_ids) == 1
        assert kwargs["include_start_time_state"] is False
        assert kwargs["significant_changes_only"] is False
        self.calls.append(("significant", entity_ids[0], kwargs))
        with_attrs = not kwargs["no_attributes"]
        rows = self._rows(entity_ids[0], start_time.timestamp(), with_attrs)
        return {entity_ids[0]: [self._state(r, with_attrs) for r in rows]}

    def state_changes_during_period(
        self,
        hass: Any,
        start_time: datetime,
        end_time: datetime | None = None,
        entity_id: str | None = None,
        no_attributes: bool = False,
        descending: bool = False,
        limit: int | None = None,
        include_start_time_state: bool = True,
    ) -> dict[str, list[_FakeState]]:
        assert entity_id
        self.calls.append(("changes", entity_id, {"limit": limit}))
        rows = self._rows(entity_id, start_time.timestamp(), True)
        if descending:
            rows = rows[::-1]
        if limit:
            rows = rows[:limit]
        return {entity_id: [self._state(r, True) for r in rows]}


def _make_hass(history: _FakeRecorderHistory, loop: asyncio.AbstractEventLoop) -> Any:
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant()
    hass.loop = loop
    hass.config.components = ["recorder"]
    hass.states.async_entity_ids = MagicMock(return_value=list(history.meta))
    hass.data = {}
    return hass


def _patched(history: _FakeRecorderHistory) -> Any:
    return patch.object(
        history_backfill,
        "_recorder_history_module",
        return_value=SimpleNamespace(
            get_significant_states=history.get_significant_states,
            state_changes_during_period=history.state_changes_during_period,
        ),
    )


class TestHistoryApiRecorderSource:
    """The source's own behaviour against a patched history API."""

    @pytest.mark.asyncio
    async def test_timeseries_coordinates_unit_and_caching(self, tmp_path: Any) -> None:
        t0 = 1790000000.0
        db_path = str(tmp_path / "tracks.db")
        _build_track_recorder_db(db_path, t0)
        history = _FakeRecorderHistory(db_path)
        loop = asyncio.get_running_loop()
        hass = _make_hass(history, loop)
        speed_id = f"sensor.{TEST_VEHICLE_ID}_speed"
        tracker_id = f"device_tracker.{TEST_VEHICLE_ID}_location"

        with (
            _patched(history),
            patch.object(
                history_backfill, "_known_entity_ids", return_value=set(history.meta)
            ),
        ):
            source = HistoryApiRecorderSource(hass)

            def work() -> tuple[Any, ...]:
                first = source.timeseries(speed_id, start_ts=t0 - 1)
                again = source.timeseries(speed_id, start_ts=t0 - 1)
                clipped = source.timeseries(speed_id, start_ts=t0 - 1, end_ts=t0 + 60)
                lat, lon = source.coordinates(tracker_id, start_ts=t0 - 1)
                unit = source.unit(speed_id)
                return first, again, clipped, lat, lon, unit

            first, again, clipped, lat, lon, unit = await loop.run_in_executor(
                None, work
            )

        assert source.entity_map()[speed_id] == speed_id
        assert len(first) == 21 and first == again
        assert all(t <= t0 + 60 for t, _ in clipped) and 0 < len(clipped) < len(first)
        assert len(lat) == len(lon) == 121
        assert lat[0] == (t0, pytest.approx(39.7242))
        assert unit == "mph"
        significant = [c for c in history.calls if c[0] == "significant"]
        # One fetch per entity and kind however often it is read; attributes
        # only for the tracker.
        assert [c[1] for c in significant] == [speed_id, tracker_id]
        assert significant[0][2]["no_attributes"] is True
        assert significant[1][2]["no_attributes"] is False

    @pytest.mark.asyncio
    async def test_drops_unusable_states(self) -> None:
        loop = asyncio.get_running_loop()
        states = [
            _FakeState("10", 100.0, None),
            _FakeState("unavailable", 110.0, None),
            _FakeState("unknown", 120.0, None),
            _FakeState(None, 125.0, None),
            _FakeState("12", 130.0, None),
        ]
        fake = SimpleNamespace(
            get_significant_states=lambda *a, **k: {"sensor.x": states},
        )
        hass = _make_hass(MagicMock(meta={}), loop)
        with (
            patch.object(
                history_backfill, "_recorder_history_module", return_value=fake
            ),
            patch.object(
                history_backfill, "_known_entity_ids", return_value={"sensor.x"}
            ),
        ):
            source = HistoryApiRecorderSource(hass)
            rows = await loop.run_in_executor(None, source.timeseries, "sensor.x", 0.0)
        assert rows == [(100.0, "10"), (130.0, "12")]

    @pytest.mark.asyncio
    async def test_refuses_to_run_on_the_event_loop(self) -> None:
        hass = _make_hass(MagicMock(meta={}), asyncio.get_running_loop())
        with (
            patch.object(history_backfill, "_known_entity_ids", return_value=set()),
            patch.object(
                history_backfill, "_recorder_history_module", return_value=MagicMock()
            ),
        ):
            source = HistoryApiRecorderSource(hass)
            with pytest.raises(RuntimeError, match="worker thread"):
                source.timeseries("sensor.x")


class TestHistoryApiBackfill:
    """A backfill through the history API matches one through the SQLite file."""

    @pytest.mark.asyncio
    async def test_same_drives_and_sessions_as_sqlite(self) -> None:
        loop = asyncio.get_running_loop()
        history = _FakeRecorderHistory(FIXTURE_DB_PATH)
        hass = _make_hass(history, loop)
        weather = MagicMock()
        weather.async_get_historical_temperatures = AsyncMock(return_value={})

        with patch.object(
            history_backfill, "_known_entity_ids", return_value=set(history.meta)
        ):
            via_sqlite = await async_backfill_from_recorder(
                hass=hass,
                vin=TEST_VIN,
                db_path=FIXTURE_DB_PATH,
                dry_run=True,
                weather_client=weather,
                tracks=False,
            )
            with _patched(history):
                via_api = await async_backfill_from_recorder(
                    hass=hass,
                    vin=TEST_VIN,
                    dry_run=True,
                    weather_client=weather,
                    tracks=False,
                )

        assert via_api["drives_found"] == via_sqlite["drives_found"] > 0
        assert [d.to_dict() for d in via_api["drives"]] == [
            d.to_dict() for d in via_sqlite["drives"]
        ]
        assert via_api["total_miles"] == via_sqlite["total_miles"]
        assert via_api["total_kwh"] == via_sqlite["total_kwh"]
        assert [s.session_id for s in via_api["dcfc_sessions"]] == [
            s.session_id for s in via_sqlite["dcfc_sessions"]
        ]
        assert via_api["vampire_events_found"] == via_sqlite["vampire_events_found"]

    def test_tracks_match_sqlite(self, tmp_path: Any) -> None:
        t0 = 1790000000.0
        db_path = str(tmp_path / "tracks.db")
        _build_track_recorder_db(db_path, t0)
        history = _FakeRecorderHistory(db_path)
        loop = asyncio.new_event_loop()
        try:
            import threading

            thread = threading.Thread(target=loop.run_forever, daemon=True)
            thread.start()
            hass = _make_hass(history, loop)
            entity_ids = {
                "device_tracker": f"device_tracker.{TEST_VEHICLE_ID}_location",
                "speed": f"sensor.{TEST_VEHICLE_ID}_speed",
                "altitude": f"sensor.{TEST_VEHICLE_ID}_altitude",
                "battery_level": f"sensor.{TEST_VEHICLE_ID}_battery_state_of_charge",
                "odometer": f"sensor.{TEST_VEHICLE_ID}_odometer",
            }
            windows = [("d1", t0, t0 + 600.0), ("d2", t0 + 100000.0, t0 + 100600.0)]
            expected = reconstruct_tracks_for_windows(db_path, entity_ids, windows)
            with (
                _patched(history),
                patch.object(
                    history_backfill,
                    "_known_entity_ids",
                    return_value=set(history.meta),
                ),
            ):
                source = HistoryApiRecorderSource(hass)
                actual = reconstruct_tracks_for_windows(source, entity_ids, windows)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()

        assert set(actual) == set(expected) == {"d1"}

        def flat(track: Any) -> list[tuple[Any, ...]]:
            return [
                (p.t, p.lat, p.lon, p.speed_mps, p.alt_m, p.soc, p.odo_m)
                for p in track.points
            ]

        assert flat(actual["d1"]) == flat(expected["d1"])


class TestRecorderRequired:
    """Without the recorder, the backfill fails with a clear message."""

    @pytest.mark.asyncio
    async def test_clear_error_when_recorder_missing(self) -> None:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant()
        hass.config.components = []
        with pytest.raises(Exception, match="recorder integration is required"):
            await async_backfill_from_recorder(hass=hass, vin=TEST_VIN, dry_run=True)

    @pytest.mark.asyncio
    async def test_clear_error_when_recorder_instance_missing(self) -> None:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant()
        hass.config.components = ["recorder"]
        with (
            patch(
                "homeassistant.components.recorder.get_instance",
                side_effect=KeyError("recorder"),
            ),
            pytest.raises(Exception, match="recorder integration is required"),
        ):
            await async_backfill_from_recorder(hass=hass, vin=TEST_VIN, dry_run=True)

    def test_sqlite_source_unchanged_for_fixture(self) -> None:
        source = SqliteRecorderSource(FIXTURE_DB_PATH)
        try:
            assert source.entity_map()
        finally:
            source.close()
        assert os.path.exists(FIXTURE_DB_PATH)
