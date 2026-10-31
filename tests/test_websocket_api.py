"""Tests for the Rivian analytics WebSocket API commands and payload contracts."""

from __future__ import annotations

from datetime import datetime
import threading
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.rivian import websocket_api as ws_api_module
from custom_components.rivian.const import ATTR_DRIVE_STORE, ATTR_VEHICLE, DOMAIN
from custom_components.rivian.drive_models import (
    MPGE_FACTOR,
    STANDARD_SPEED_BINS,
    AggregatedDriveStats,
    ChargingSample,
    ChargingSessionRecord,
    DriveChunk,
    DriveRecord,
    SpeedBinData,
    VampireDrainRecord,
)
from custom_components.rivian.websocket_api import (
    RIVIAN_ANALYTICS_UPDATED_EVENT,
    SUMMARY_WINDOWS,
    VALID_SERIES_KEYS,
    VEHICLE_PALETTE,
    _build_series_payload,
    _multi_vin_schema,
    _websocket_analytics_calendar,
    _websocket_analytics_day,
    _websocket_analytics_drive,
    _websocket_analytics_drives,
    _websocket_analytics_heat,
    _websocket_analytics_heat_tile,
    _websocket_analytics_series,
    _websocket_analytics_subscribe,
    _websocket_analytics_summary,
    _websocket_vehicles_list,
    assign_vehicle_slots,
)

VIN = "7PDSGABA8NN000000"
OTHER_VIN = "7PDSGABA8NN999999"


def _chunk(temp_f: float | None) -> DriveChunk:
    return DriveChunk(
        start_time="2026-09-20T10:03:00+00:00",
        duration_seconds=180.0,
        distance_miles=2.0,
        energy_kwh=0.7,
        efficiency_mi_kwh=2.86,
        avg_speed_mph=40.0,
        speed_bin="40-49",
        elevation_change_ft=20.0,
        temp_f=temp_f,
    )


def _drive(temp: float | None = 68.5, chunk_temp: float | None = None) -> DriveRecord:
    return DriveRecord(
        vin=VIN,
        drive_id=f"{VIN}_1",
        start_time="2026-09-20T10:00:00+00:00",
        end_time="2026-09-20T10:30:00+00:00",
        distance_miles=12.3,
        duration_seconds=1800.0,
        start_soc=80.0,
        end_soc=74.0,
        battery_capacity_kwh=135.0,
        energy_kwh=8.1,
        elevation_change_ft=150.0,
        avg_speed_mph=40.0,
        max_speed_mph=62.0,
        integrated_temperature_f=temp,
        speed_bins={
            "30-39": SpeedBinData(miles=5.0, seconds=500.0),
            "40-49": SpeedBinData(miles=7.3, seconds=600.0),
        },
        chunks=[_chunk(chunk_temp)],
    )


class _FakeStore:
    """The slice of DriveStore's cache-only surface the WebSocket handler reads."""

    def __init__(self, drives: list[DriveRecord]) -> None:
        self.vin = VIN
        self.recent_drives = drives
        self.recent_vampire_events = [
            VampireDrainRecord(
                start_time="2026-09-20T11:00:00+00:00",
                end_time="2026-09-20T21:00:00+00:00",
                idle_hours=10.0,
                start_soc=74.0,
                end_soc=73.0,
                drain_soc=1.0,
                drain_kwh=1.35,
                rate_pct_per_day=2.4,
                avg_watts=135.0,
                avg_temp_f=60.0,
            )
        ]
        self._dcfc = [
            ChargingSessionRecord(
                session_id=f"{VIN}_2",
                start_time="2026-09-21T09:00:00+00:00",
                end_time="2026-09-21T09:30:00+00:00",
                start_soc=20.0,
                end_soc=80.0,
                energy_added_kwh=81.0,
                max_power_kw=210.0,
                avg_power_kw=150.0,
                samples=[
                    ChargingSample(
                        timestamp="2026-09-21T09:00:00+00:00",
                        soc=20.0 + i * 0.6,
                        power_kw=210.0 - i,
                        battery_temp_f=90.0,
                    )
                    for i in range(100)
                ],
            )
        ]
        self.speed_bin_totals = {
            b: {"miles": float(i), "seconds": float(i * 60)}
            for i, b in enumerate(STANDARD_SPEED_BINS)
        }

    def get_dcfc_sessions(self, limit: int = 50) -> list[ChargingSessionRecord]:
        return self._dcfc[-limit:]

    async def async_series_window(
        self, days: int
    ) -> tuple[list[DriveRecord], list[VampireDrainRecord]]:
        """The synchronous (days <= 90) path must never reach this."""
        raise AssertionError("async_series_window should not be called")


def test_speed_bins_total_the_storage_window() -> None:
    """The speed-bin series is the store's totals, not the last drive's bins."""
    store = _FakeStore([_drive()])
    payload = _build_series_payload(store, ["speed_bins"])
    assert payload == {"speed_bins": store.speed_bin_totals}


def test_drive_chart_shape_tolerates_a_missing_temperature() -> None:
    drive = _build_series_payload(_FakeStore([_drive(temp=None)]), ["drives"])[
        "drives"
    ][0]

    assert drive["temp_f"] is None
    assert drive["distance"] == 12.3
    assert drive["efficiency"] == round(12.3 / 8.1, 2)
    assert drive["speed_bins"]["40-49"] == {"miles": 7.3, "seconds": 600.0}


def test_chunk_borrows_drive_temperature_only_when_it_has_none() -> None:
    borrowed = _build_series_payload(_FakeStore([_drive(temp=68.5)]), ["chunks"])
    own = _build_series_payload(
        _FakeStore([_drive(temp=68.5, chunk_temp=41.0)]), ["chunks"]
    )

    assert borrowed["chunks"][0]["temp_f"] == 68.5
    assert own["chunks"][0]["temp_f"] == 41.0


def test_requesting_segments_returns_the_same_data_as_chunks() -> None:
    """The "segments" series key is a legacy alias for "chunks"."""
    store = _FakeStore([_drive(temp=68.5, chunk_temp=41.0)])
    payload = _build_series_payload(store, ["chunks", "segments"])

    assert payload["segments"] == payload["chunks"]


def test_drive_chart_includes_drive_id() -> None:
    payload = _build_series_payload(_FakeStore([_drive()]), ["drives"])
    assert payload["drives"][0]["drive_id"] == f"{VIN}_1"


# -- rivian/analytics/drives, /drive, /series (async) and /subscribe (sync) -------


class _FakeAsyncStore:
    """The slice of DriveStore's async surface the handlers call."""

    def __init__(
        self,
        vin: str = VIN,
        drives: list[dict[str, Any]] | None = None,
        previews: dict[str, dict[str, list]] | None = None,
        detail: dict[str, Any] | None = None,
        storage: dict[str, Any] | None = None,
        series_window: tuple[list[DriveRecord], list[VampireDrainRecord]] | None = None,
    ) -> None:
        self.vin = vin
        self._drives = drives or []
        self._previews = previews or {}
        self._detail = detail
        self._storage = storage or {"drive_count": 0}
        self._series_window = series_window
        self.list_drives_calls: list[tuple[float | None, int, bool]] = []
        self.preview_requests: list[list[str]] = []

    async def async_list_drives(
        self, before_ts: float | None, limit: int, include_micro: bool
    ) -> list[dict[str, Any]]:
        self.list_drives_calls.append((before_ts, limit, include_micro))
        return [dict(d) for d in self._drives]

    async def async_get_track_previews(
        self, drive_ids: list[str]
    ) -> dict[str, dict[str, list]]:
        self.preview_requests.append(drive_ids)
        return {k: v for k, v in self._previews.items() if k in drive_ids}

    async def async_get_drive_detail(self, drive_id: str) -> dict[str, Any] | None:
        return self._detail

    async def async_storage_stats(self) -> dict[str, Any]:
        return self._storage

    async def async_series_window(
        self, days: int
    ) -> tuple[list[DriveRecord], list[VampireDrainRecord]]:
        if self._series_window is None:
            raise AssertionError("async_series_window should not be called")
        return self._series_window

    # cache-only attributes _build_series_payload also reads
    recent_drives: list[DriveRecord] = []
    recent_vampire_events: list[VampireDrainRecord] = []
    speed_bin_totals: dict[str, dict[str, float]] = {}

    def get_dcfc_sessions(self, limit: int = 50) -> list[ChargingSessionRecord]:
        return []


class _FakeConnection:
    """Records what a WebSocket command sent, in place of a real ActiveConnection."""

    def __init__(self) -> None:
        self.results: dict[int, Any] = {}
        self.errors: dict[int, tuple[str, str]] = {}
        self.messages: list[Any] = []
        self.subscriptions: dict[int, Any] = {}

    def send_result(self, msg_id: int, result: Any = None) -> None:
        self.results[msg_id] = result

    def send_error(self, msg_id: int, code: str, message: str) -> None:
        self.errors[msg_id] = (code, message)

    def send_message(self, message: Any) -> None:
        self.messages.append(message)


class _FakeBus:
    """A minimal event bus supporting async_listen + firing, for subscribe tests."""

    def __init__(self) -> None:
        self._listeners: list[tuple[str, Any]] = []

    def async_listen(self, event_type: str, callback: Any) -> Any:
        entry = (event_type, callback)
        self._listeners.append(entry)

        def _unsub() -> None:
            if entry in self._listeners:
                self._listeners.remove(entry)

        return _unsub

    def fire(self, event_type: str, data: dict[str, Any]) -> None:
        event = SimpleNamespace(data=data)
        for et, callback in list(self._listeners):
            if et == event_type:
                callback(event)

    def async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        """Alias for `fire`, matching HA's real `EventBus.async_fire` name."""
        self.fire(event_type, data)


def _hass_with_store(store: Any) -> Any:
    """A bare object shaped like hass, with just enough of hass.data for _find_store."""
    return SimpleNamespace(
        data={DOMAIN: {"entry": {ATTR_DRIVE_STORE: {store.vin: store}}}},
        bus=_FakeBus(),
    )


def _summary(drive_id: str, sort_ts: float, has_track: bool = False) -> dict[str, Any]:
    return {
        "drive_id": drive_id,
        "start_time": "2026-09-20T10:00:00+00:00",
        "end_time": "2026-09-20T10:30:00+00:00",
        "start_ts": sort_ts,
        "distance_miles": 12.3,
        "duration_seconds": 1800.0,
        "energy_kwh": 8.1,
        "efficiency_mi_kwh": 1.52,
        "mpge": 45.6,
        "avg_speed_mph": 40.0,
        "max_speed_mph": 62.0,
        "temp_f": 68.5,
        "elevation_change_ft": 150.0,
        "start_soc": 80.0,
        "end_soc": 74.0,
        "is_micro_drive": False,
        "start_lat": 40.0,
        "start_lon": -105.0,
        "end_lat": 40.1,
        "end_lon": -105.1,
        "has_track": has_track,
        "track_source": "live" if has_track else None,
        "track_detail": "full" if has_track else None,
        "point_count": 500 if has_track else 0,
        "sort_ts": sort_ts,
    }


async def test_drives_command_pages_and_strips_sort_ts() -> None:
    drives = [_summary(f"{VIN}_2", 200.0), _summary(f"{VIN}_1", 100.0)]
    store = _FakeAsyncStore(drives=drives, storage={"drive_count": 2})
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_drives(
        hass, connection, {"id": 1, "vin": VIN, "limit": 2}
    )

    result = connection.results[1]
    assert [d["drive_id"] for d in result["drives"]] == [f"{VIN}_2", f"{VIN}_1"]
    assert all("sort_ts" not in d for d in result["drives"])
    assert result["next_before_ts"] == 100.0
    assert result["storage"] == {"drive_count": 2}
    assert store.list_drives_calls == [(None, 2, False)]


async def test_drives_command_next_before_ts_none_when_page_not_full() -> None:
    drives = [_summary(f"{VIN}_1", 100.0)]
    store = _FakeAsyncStore(drives=drives)
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_drives(
        hass, connection, {"id": 1, "vin": VIN, "limit": 50}
    )

    assert connection.results[1]["next_before_ts"] is None


async def test_drives_command_attaches_previews_only_when_requested_and_has_track() -> (
    None
):
    drives = [
        _summary(f"{VIN}_1", 100.0, has_track=True),
        _summary(f"{VIN}_2", 90.0, has_track=False),
    ]
    previews = {f"{VIN}_1": {"lat": [40.0, 40.1], "lon": [-105.0, -105.1]}}
    store = _FakeAsyncStore(drives=drives, previews=previews)
    hass = _hass_with_store(store)

    connection = _FakeConnection()
    await _websocket_analytics_drives(
        hass, connection, {"id": 1, "vin": VIN, "previews": True}
    )
    result = connection.results[1]["drives"]
    by_id = {d["drive_id"]: d for d in result}
    assert by_id[f"{VIN}_1"]["preview"] == previews[f"{VIN}_1"]
    assert "preview" not in by_id[f"{VIN}_2"]
    assert store.preview_requests == [[f"{VIN}_1"]]

    connection2 = _FakeConnection()
    store2 = _FakeAsyncStore(drives=drives, previews=previews)
    hass2 = _hass_with_store(store2)
    await _websocket_analytics_drives(hass2, connection2, {"id": 1, "vin": VIN})
    assert all("preview" not in d for d in connection2.results[1]["drives"])
    assert store2.preview_requests == []


async def test_drives_command_unknown_vin_is_not_found() -> None:
    hass = _hass_with_store(_FakeAsyncStore())
    connection = _FakeConnection()

    await _websocket_analytics_drives(
        hass, connection, {"id": 1, "vin": OTHER_VIN, "limit": 50}
    )

    assert connection.errors[1][0] == "not_found"
    assert 1 not in connection.results


async def test_drive_command_returns_detail_without_sort_ts() -> None:
    detail = {"drive": _summary(f"{VIN}_1", 100.0), "track": None}
    store = _FakeAsyncStore(detail=detail)
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_drive(
        hass, connection, {"id": 1, "vin": VIN, "drive_id": f"{VIN}_1"}
    )

    result = connection.results[1]
    assert "sort_ts" not in result["drive"]
    assert result["drive"]["drive_id"] == f"{VIN}_1"


async def test_drive_command_not_found_for_missing_drive() -> None:
    store = _FakeAsyncStore(detail=None)
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_drive(
        hass, connection, {"id": 1, "vin": VIN, "drive_id": "missing"}
    )

    assert connection.errors[1][0] == "not_found"


async def test_drive_command_unknown_vin_is_not_found() -> None:
    hass = _hass_with_store(_FakeAsyncStore())
    connection = _FakeConnection()

    await _websocket_analytics_drive(
        hass, connection, {"id": 1, "vin": OTHER_VIN, "drive_id": "x"}
    )

    assert connection.errors[1][0] == "not_found"


async def test_series_days_le_90_never_touches_series_window() -> None:
    store = _FakeStore([_drive()])
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_series(
        hass, connection, {"id": 1, "vin": VIN, "series": ["drives"], "days": 90}
    )

    assert connection.results[1]["drives"][0]["drive_id"] == f"{VIN}_1"


async def test_series_days_gt_90_uses_series_window() -> None:
    old_drive = _drive()
    old_drive.drive_id = f"{VIN}_old"
    vampire = [
        VampireDrainRecord(
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T10:00:00+00:00",
            idle_hours=10.0,
            start_soc=74.0,
            end_soc=73.0,
            drain_soc=1.0,
            drain_kwh=1.35,
            rate_pct_per_day=2.4,
            avg_watts=135.0,
            avg_temp_f=60.0,
        )
    ]
    store = _FakeStore([_drive()])
    store.async_series_window = _AsyncReturns(([old_drive], vampire))
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_series(
        hass,
        connection,
        {"id": 1, "vin": VIN, "series": ["drives", "vampire"], "days": 365},
    )

    result = connection.results[1]
    assert [d["drive_id"] for d in result["drives"]] == [f"{VIN}_old"]
    assert len(result["vampire"]) == 1


class _AsyncReturns:
    """Callable returning a fixed value when awaited, for monkeypatching an async method."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __call__(self, days: int) -> Any:
        return self._value


async def test_series_unknown_vin_is_not_found() -> None:
    hass = _hass_with_store(_FakeStore([_drive()]))
    connection = _FakeConnection()

    await _websocket_analytics_series(
        hass, connection, {"id": 1, "vin": OTHER_VIN, "series": ["drives"]}
    )

    assert connection.errors[1][0] == "not_found"


def test_subscribe_forwards_matching_vin_events_and_unsubscribes() -> None:
    store = _FakeAsyncStore()
    hass = _hass_with_store(store)
    connection = _FakeConnection()
    ws_api_module.websocket_api.event_message = lambda msg_id, data: {
        "id": msg_id,
        "type": "event",
        "event": data,
    }

    _websocket_analytics_subscribe(hass, connection, {"id": 1, "vin": VIN})

    assert connection.results.get(1) is None or 1 in connection.results
    assert 1 in connection.subscriptions

    hass.bus.fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": OTHER_VIN})
    assert connection.messages == []

    hass.bus.fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": VIN})
    assert connection.messages == [{"id": 1, "type": "event", "event": {"vin": VIN}}]

    connection.subscriptions[1]()
    hass.bus.fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": VIN})
    assert len(connection.messages) == 1


def test_subscribe_unknown_vin_is_not_found() -> None:
    hass = _hass_with_store(_FakeAsyncStore())
    connection = _FakeConnection()

    _websocket_analytics_subscribe(hass, connection, {"id": 1, "vin": OTHER_VIN})

    assert connection.errors[1][0] == "not_found"
    assert 1 not in connection.subscriptions


# -- rivian/analytics/summary -------------------------------------------------


class _FakeSummaryStore:
    """A store whose `async_get_stats` and `last_drive` back the summary command."""

    def __init__(
        self,
        stats_by_days: dict[int | None, AggregatedDriveStats],
        last_drive: DriveRecord | None = None,
        vin: str = VIN,
    ) -> None:
        self.vin = vin
        self._stats_by_days = stats_by_days
        self.last_drive = last_drive
        self.calls: list[int | None] = []

    async def async_get_stats(self, days: int | None = None) -> AggregatedDriveStats:
        self.calls.append(days)
        return self._stats_by_days[days]


def _stats(
    miles: float,
    kwh: float,
    efficiency: float,
    mpge: float,
    drives: int,
    seconds: float,
) -> AggregatedDriveStats:
    return AggregatedDriveStats(
        total_miles=miles,
        total_kwh=kwh,
        efficiency_mi_kwh=efficiency,
        mpge=mpge,
        drive_count=drives,
        total_duration_seconds=seconds,
    )


async def test_summary_payload_shape_and_rounding() -> None:
    stats_by_days = {
        7: _stats(70.111, 30.0, 2.3703, 79.9083, 3, 3600.0 * 2),
        30: _stats(300.0, 120.0, 2.5, 84.2625, 12, 3600.0 * 8),
        365: _stats(3000.0, 1250.0, 2.4, 80.892, 120, 3600.0 * 90),
        None: _stats(9000.0, 3750.0, 2.4, 80.892, 400, 3600.0 * 300),
    }
    store = _FakeSummaryStore(stats_by_days, last_drive=_drive())
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_summary(hass, connection, {"id": 1, "vin": VIN})

    result = connection.results[1]
    assert set(result["windows"]) == {"7d", "30d", "365d", "all"}
    assert set(store.calls) == {days for _label, days in SUMMARY_WINDOWS}

    seven_day = result["windows"]["7d"]
    assert seven_day == {
        "miles": 70.11,
        "kwh": 30.0,
        "efficiency_mi_kwh": 2.37,
        "mpge": 79.9,
        "drives": 3,
        "hours": 2.0,
    }
    all_time = result["windows"]["all"]
    assert all_time["drives"] == 400
    assert all_time["hours"] == 300.0

    last_drive = result["last_drive"]
    assert last_drive["drive_id"] == f"{VIN}_1"
    assert last_drive["start_time"] == "2026-09-20T10:00:00+00:00"
    assert last_drive["end_time"] == "2026-09-20T10:30:00+00:00"
    assert last_drive["distance_miles"] == 12.3
    assert last_drive["duration_seconds"] == 1800.0
    assert last_drive["energy_kwh"] == 8.1
    assert last_drive["efficiency_mi_kwh"] == round(12.3 / 8.1, 2)
    assert last_drive["temp_f"] == 68.5
    assert last_drive["is_micro_drive"] is False
    assert (
        last_drive["start_ts"]
        == datetime.fromisoformat("2026-09-20T10:00:00+00:00").timestamp()
    )


async def test_summary_last_drive_none_when_no_drives_recorded() -> None:
    stats_by_days = {days: _stats(0, 0, 0, 0, 0, 0) for _label, days in SUMMARY_WINDOWS}
    store = _FakeSummaryStore(stats_by_days, last_drive=None)
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_summary(hass, connection, {"id": 1, "vin": VIN})

    assert connection.results[1]["last_drive"] is None


async def test_summary_last_drive_start_ts_none_when_unparseable() -> None:
    bad_drive = _drive()
    bad_drive.start_time = "not-a-timestamp"
    stats_by_days = {days: _stats(0, 0, 0, 0, 0, 0) for _label, days in SUMMARY_WINDOWS}
    store = _FakeSummaryStore(stats_by_days, last_drive=bad_drive)
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_summary(hass, connection, {"id": 1, "vin": VIN})

    assert connection.results[1]["last_drive"]["start_ts"] is None


async def test_summary_unknown_vin_is_not_found() -> None:
    stats_by_days = {days: _stats(0, 0, 0, 0, 0, 0) for _label, days in SUMMARY_WINDOWS}
    hass = _hass_with_store(_FakeSummaryStore(stats_by_days))
    connection = _FakeConnection()

    await _websocket_analytics_summary(hass, connection, {"id": 1, "vin": OTHER_VIN})

    assert connection.errors[1][0] == "not_found"


# -- rivian/analytics/calendar and /day ----------------------------------------


class _FakeCalendarDayStore:
    """The slice of DriveStore's async surface the calendar/day handlers call."""

    def __init__(
        self,
        vin: str = VIN,
        calendar_payload: dict[str, Any] | None = None,
        day_payload: dict[str, Any] | None = None,
    ) -> None:
        self.vin = vin
        self._calendar_payload = calendar_payload or {"totals": {}, "years": []}
        self._day_payload = day_payload or {
            "date": "2026-09-10",
            "totals": {},
            "segments": [],
            "stops": [],
            "start": None,
            "end": None,
        }
        self.calendar_calls: list[tuple[Any, int | None, int | None, bool]] = []
        self.day_calls: list[tuple[Any, Any, bool]] = []

    async def async_calendar(
        self,
        tz: Any,
        year: int | None = None,
        month: int | None = None,
        include_micro: bool = False,
    ) -> dict[str, Any]:
        self.calendar_calls.append((tz, year, month, include_micro))
        return self._calendar_payload

    async def async_day(
        self, tz: Any, day: Any, include_micro: bool = False
    ) -> dict[str, Any]:
        self.day_calls.append((tz, day, include_micro))
        return self._day_payload


async def test_calendar_command_returns_payload_and_resolves_default_tz() -> None:
    payload = {"totals": {"drives": 3}, "years": [{"key": "2026", "drives": 3}]}
    store = _FakeCalendarDayStore(calendar_payload=payload)
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_calendar(
        hass, connection, {"id": 1, "vin": VIN, "year": 2026, "month": 9}
    )

    assert connection.results[1] == payload
    assert store.calendar_calls == [(store.calendar_calls[0][0], 2026, 9, False)]


async def test_calendar_command_month_without_year_is_invalid_format() -> None:
    store = _FakeCalendarDayStore()
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_calendar(
        hass, connection, {"id": 1, "vin": VIN, "month": 9}
    )

    assert connection.errors[1][0] == "invalid_format"
    assert 1 not in connection.results
    assert store.calendar_calls == []


async def test_calendar_command_unknown_vin_is_not_found() -> None:
    store = _FakeCalendarDayStore()
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_calendar(hass, connection, {"id": 1, "vin": OTHER_VIN})

    assert connection.errors[1][0] == "not_found"


async def test_day_command_returns_payload_and_strips_sort_ts() -> None:
    day_payload = {
        "date": "2026-09-10",
        "totals": {"drives": 1},
        "segments": [{"index": 0, "drive_id": "d1", "sort_ts": 123.0}],
        "stops": [],
        "start": None,
        "end": None,
    }
    store = _FakeCalendarDayStore(day_payload=day_payload)
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_day(
        hass, connection, {"id": 1, "vin": VIN, "date": "2026-09-10"}
    )

    result = connection.results[1]
    assert result["date"] == "2026-09-10"
    assert "sort_ts" not in result["segments"][0]
    assert store.day_calls[0][2] is False


async def test_day_command_bad_date_is_invalid_format() -> None:
    store = _FakeCalendarDayStore()
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_day(
        hass, connection, {"id": 1, "vin": VIN, "date": "not-a-date"}
    )

    assert connection.errors[1][0] == "invalid_format"
    assert 1 not in connection.results
    assert store.day_calls == []


async def test_day_command_unknown_vin_is_not_found() -> None:
    store = _FakeCalendarDayStore()
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_day(
        hass, connection, {"id": 1, "vin": OTHER_VIN, "date": "2026-09-10"}
    )

    assert connection.errors[1][0] == "not_found"


async def test_calendar_and_day_commands_only_reach_sqlite_via_the_executor(
    mock_hass: Any, analytics_db_path: str
) -> None:
    """The handlers must go through DriveStore's executor wrappers, not the loop.

    Points ``AnalyticsDatabase``'s executor-thread guard at *this* (main test)
    thread, mirroring ``test_executor_thread_guard_raises_on_loop_thread`` in
    ``tests/test_analytics_db.py``: a direct, synchronous call into
    ``calendar()``/``day()`` now raises, while going through the real
    WebSocket handlers (which only ever call the ``async_`` wrappers, which
    run on ``hass.async_add_executor_job``'s thread pool) still succeeds.
    """
    from custom_components.rivian.analytics_db import AnalyticsDatabase
    from custom_components.rivian.drive_models import DriveRecord
    from custom_components.rivian.drive_storage import DriveStore

    db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
    db.setup()
    store = DriveStore(mock_hass, VIN, db)
    try:
        await store.async_save_drives_batch(
            [
                DriveRecord(
                    vin=VIN,
                    drive_id="d1",
                    start_time="2026-09-10T15:00:00Z",
                    end_time="2026-09-10T15:25:00Z",
                    distance_miles=5.0,
                    duration_seconds=600.0,
                    start_soc=80.0,
                    end_soc=75.0,
                    battery_capacity_kwh=135.0,
                    energy_kwh=2.0,
                )
            ]
        )

        # Force the guard: any direct, synchronous DB call from this thread
        # must now raise, proving the assertion actually protects us.
        db._loop_thread_id = threading.get_ident()
        with pytest.raises(RuntimeError, match="executor"):
            db.calendar(VIN, ZoneInfo("America/Chicago"))

        hass = _hass_with_store(store)
        connection = _FakeConnection()
        await _websocket_analytics_calendar(
            hass, connection, {"id": 1, "vin": VIN, "year": 2026, "month": 9}
        )
        assert connection.results[1]["totals"]["drives"] == 1

        connection2 = _FakeConnection()
        await _websocket_analytics_day(
            hass, connection2, {"id": 1, "vin": VIN, "date": "2026-09-10"}
        )
        assert [s["drive_id"] for s in connection2.results[1]["segments"]] == ["d1"]
    finally:
        db._loop_thread_id = -1
        db.close()


# -- rivian/analytics/heat and /heat_tile --------------------------------------


class _FakeHeatStore:
    """The slice of DriveStore's async surface the heat/heat_tile handlers call."""

    def __init__(
        self,
        vin: str = VIN,
        info_payload: dict[str, Any] | None = None,
        tile_payload: dict[str, Any] | None = None,
        raise_value_error: str | None = None,
    ) -> None:
        self.vin = vin
        self._info_payload = info_payload or {
            "period": "all",
            "key": "all",
            "bbox": None,
            "scale_max": 2,
            "cells": 0,
            "drives": 0,
        }
        self._tile_payload = tile_payload or {"level": 21, "size": 1, "cells": []}
        self._raise_value_error = raise_value_error
        self.info_calls: list[tuple[str, str | None]] = []
        self.tile_calls: list[tuple[str, str | None, int, int, int]] = []
        self.tile_margins: list[int] = []

    async def async_heat_info(
        self, period: str, key: str | None = None
    ) -> dict[str, Any]:
        if self._raise_value_error:
            raise ValueError(self._raise_value_error)
        self.info_calls.append((period, key))
        return self._info_payload

    async def async_heat_tile(
        self, period: str, key: str | None, z: int, x: int, y: int, margin: int = 0
    ) -> dict[str, Any]:
        if self._raise_value_error:
            raise ValueError(self._raise_value_error)
        self.tile_calls.append((period, key, z, x, y))
        self.tile_margins.append(margin)
        return self._tile_payload


async def test_heat_command_returns_payload() -> None:
    payload = {
        "period": "month",
        "key": "2026-09",
        "bbox": [40.0, -105.1, 40.1, -105.0],
        "scale_max": 5,
        "cells": 12,
        "drives": 3,
    }
    store = _FakeHeatStore(info_payload=payload)
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_heat(
        hass, connection, {"id": 1, "vin": VIN, "period": "month", "key": "2026-09"}
    )

    assert connection.results[1] == payload
    assert store.info_calls == [("month", "2026-09")]


async def test_heat_command_invalid_format_from_a_bad_key() -> None:
    store = _FakeHeatStore(raise_value_error="road_heat: invalid month key 'bogus'")
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_heat(
        hass, connection, {"id": 1, "vin": VIN, "period": "month", "key": "bogus"}
    )

    assert connection.errors[1][0] == "invalid_format"
    assert 1 not in connection.results


async def test_heat_command_unknown_vin_is_not_found() -> None:
    store = _FakeHeatStore()
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_heat(
        hass, connection, {"id": 1, "vin": OTHER_VIN, "period": "all"}
    )

    assert connection.errors[1][0] == "not_found"


async def test_heat_tile_command_returns_payload_with_scale_max() -> None:
    payload = {"level": 21, "size": 1, "cells": [[0, 0, 3]]}
    store = _FakeHeatStore(tile_payload=payload)
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_heat_tile(
        hass,
        connection,
        {"id": 1, "vin": VIN, "period": "all", "z": 10, "x": 163, "y": 396},
    )

    assert connection.results[1] == payload
    assert store.tile_calls == [("all", None, 10, 163, 396)]


async def test_heat_tile_command_passes_the_margin_through() -> None:
    store = _FakeHeatStore(tile_payload={"level": 21, "size": 1, "cells": []})
    hass = _hass_with_store(store)

    await _websocket_analytics_heat_tile(
        hass,
        _FakeConnection(),
        {"id": 1, "vin": VIN, "period": "all", "z": 10, "x": 1, "y": 2, "margin": 1},
    )

    assert store.tile_margins == [1]


async def test_heat_tile_command_invalid_format_from_an_out_of_range_tile() -> None:
    store = _FakeHeatStore(
        raise_value_error="road_heat: tile (10,9999,9999) out of range"
    )
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_heat_tile(
        hass,
        connection,
        {"id": 1, "vin": VIN, "period": "all", "z": 10, "x": 9999, "y": 9999},
    )

    assert connection.errors[1][0] == "invalid_format"
    assert 1 not in connection.results


async def test_heat_tile_command_unknown_vin_is_not_found() -> None:
    store = _FakeHeatStore()
    hass = _hass_with_store(store)
    connection = _FakeConnection()

    await _websocket_analytics_heat_tile(
        hass,
        connection,
        {"id": 1, "vin": OTHER_VIN, "period": "all", "z": 1, "x": 0, "y": 0},
    )

    assert connection.errors[1][0] == "not_found"


async def test_heat_and_heat_tile_commands_only_reach_sqlite_via_the_executor(
    mock_hass: Any, analytics_db_path: str
) -> None:
    """Mirrors the calendar/day executor-boundary test for the heat handlers."""
    from custom_components.rivian.analytics_db import AnalyticsDatabase
    from custom_components.rivian.drive_storage import DriveStore
    from custom_components.rivian.drive_track import DriveTrack, TrackPoint

    db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
    db.setup()
    store = DriveStore(mock_hass, VIN, db)
    try:
        track = DriveTrack()
        track.append(TrackPoint(t=1_700_000_000.0, lat=40.0, lon=-105.0))
        track.append(TrackPoint(t=1_700_000_010.0, lat=40.001, lon=-105.001))
        await store.async_upsert_tracks([("d1", track)])
        await store.async_update_heat()

        # Point the executor-thread guard at *this* (main test) thread: a
        # direct, synchronous call into heat_info()/heat_tile() now raises,
        # while going through the real WebSocket handlers still succeeds.
        db._loop_thread_id = threading.get_ident()
        with pytest.raises(RuntimeError, match="executor"):
            db.heat_info(VIN, "all")

        hass = _hass_with_store(store)
        connection = _FakeConnection()
        await _websocket_analytics_heat(
            hass, connection, {"id": 1, "vin": VIN, "period": "all"}
        )
        assert connection.results[1]["drives"] == 1

        connection2 = _FakeConnection()
        await _websocket_analytics_heat_tile(
            hass,
            connection2,
            {"id": 1, "vin": VIN, "period": "all", "z": 1, "x": 0, "y": 0},
        )
        assert "scale_max" in connection2.results[1]
    finally:
        db._loop_thread_id = -1
        db.close()


def _registered_schemas(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Register the commands against a real base schema; return {type: schema}.

    Skips where voluptuous is conftest's stand-in (it has no working
    Schema.extend); CI installs the real library with Home Assistant.
    """
    import voluptuous as vol

    if not hasattr(vol.Schema({}), "extend"):
        pytest.skip("real voluptuous is not installed")

    schemas: dict[str, Any] = {}

    def capture(_hass: Any, command_type: str, _handler: Any, schema: Any) -> None:
        schemas[command_type] = schema

    ws = ws_api_module.websocket_api
    monkeypatch.setattr(
        ws,
        "BASE_COMMAND_MESSAGE_SCHEMA",
        vol.Schema({vol.Required("id"): int, vol.Required("type"): str}),
    )
    monkeypatch.setattr(ws, "async_register_command", capture)
    ws_api_module.async_register_websocket_api(SimpleNamespace(data={}))
    return schemas


def test_schemas_accept_the_messages_the_drives_card_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explorer card's requests, including "All time" without (or with a null) key."""
    schemas = _registered_schemas(monkeypatch)
    vin = "VIN123"
    messages = [
        {"type": "rivian/analytics/calendar", "vin": vin},
        {"type": "rivian/analytics/calendar", "vin": vin, "year": 2026, "month": 9},
        {"type": "rivian/analytics/day", "vin": vin, "date": "2026-09-16"},
        {"type": "rivian/analytics/heat", "vin": vin, "period": "all"},
        {"type": "rivian/analytics/heat", "vin": vin, "period": "all", "key": None},
        {
            "type": "rivian/analytics/heat",
            "vin": vin,
            "period": "month",
            "key": "2026-09",
        },
        {
            "type": "rivian/analytics/heat_tile",
            "vin": vin,
            "period": "all",
            "z": 12,
            "x": 757,
            "y": 1478,
        },
        {
            "type": "rivian/analytics/heat_tile",
            "vin": vin,
            "period": "year",
            "key": "2026",
            "z": 3,
            "x": 1,
            "y": 2,
            "margin": 1,
        },
    ]
    for i, message in enumerate(messages, start=1):
        schemas[message["type"]]({"id": i, **message})


def test_schemas_reject_malformed_heat_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voluptuous as vol

    schemas = _registered_schemas(monkeypatch)
    bad = [
        {"type": "rivian/analytics/heat", "vin": "V", "period": "week"},
        {
            "type": "rivian/analytics/heat_tile",
            "vin": "V",
            "period": "all",
            "z": 23,
            "x": 0,
            "y": 0,
        },
        {"type": "rivian/analytics/calendar", "vin": "V", "year": 2026, "month": 13},
    ]
    for message in bad:
        with pytest.raises(vol.Invalid):
            schemas[message["type"]]({"id": 1, **message})


def _admin_connection(is_admin: bool = True) -> _FakeConnection:
    connection = _FakeConnection()
    connection.user = SimpleNamespace(is_admin=is_admin)
    return connection


# -- delete, with confirmation: /delete_drive, /delete_day, /delete_vehicle_history ----


class _FakeDeleteStore:
    """The slice of DriveStore's async surface the delete handlers call."""

    def __init__(self, vin: str = VIN) -> None:
        self.vin = vin
        self.deleted_drive_ids: list[str] = []
        self.deleted_days: list[Any] = []
        self.vehicle_history_deleted = False
        self.drive_result: dict[str, Any] = {"deleted": 1, "affected_hours": [0.0]}
        self.day_result: dict[str, Any] = {"deleted": 2, "affected_hours": [0.0]}

    async def async_delete_drive(self, drive_id: str) -> dict[str, Any]:
        self.deleted_drive_ids.append(drive_id)
        return self.drive_result

    async def async_delete_day(self, tz: Any, day: Any) -> dict[str, Any]:
        self.deleted_days.append(day)
        return self.day_result

    async def async_delete_vehicle_history(self) -> None:
        self.vehicle_history_deleted = True


async def test_delete_drive_admin_succeeds() -> None:
    store = _FakeDeleteStore()
    hass = _hass_with_store(store)
    connection = _admin_connection(is_admin=True)

    await ws_api_module._websocket_analytics_delete_drive(
        hass, connection, {"id": 1, "vin": VIN, "drive_id": "d1"}
    )

    assert store.deleted_drive_ids == ["d1"]
    assert connection.results[1] == store.drive_result


async def test_delete_drive_rejects_non_admin() -> None:
    store = _FakeDeleteStore()
    hass = _hass_with_store(store)
    connection = _admin_connection(is_admin=False)

    await ws_api_module._websocket_analytics_delete_drive(
        hass, connection, {"id": 1, "vin": VIN, "drive_id": "d1"}
    )

    assert store.deleted_drive_ids == []
    assert connection.errors[1][0] == "unauthorized"


async def test_delete_drive_unknown_vin_is_not_found() -> None:
    store = _FakeDeleteStore()
    hass = _hass_with_store(store)
    connection = _admin_connection(is_admin=True)

    await ws_api_module._websocket_analytics_delete_drive(
        hass, connection, {"id": 1, "vin": OTHER_VIN, "drive_id": "d1"}
    )

    assert connection.errors[1][0] == "not_found"


async def test_delete_day_admin_succeeds() -> None:
    store = _FakeDeleteStore()
    hass = _hass_with_store(store)
    connection = _admin_connection(is_admin=True)

    await ws_api_module._websocket_analytics_delete_day(
        hass, connection, {"id": 1, "vin": VIN, "date": "2026-09-20"}
    )

    assert len(store.deleted_days) == 1
    assert connection.results[1] == store.day_result


async def test_delete_day_rejects_non_admin() -> None:
    store = _FakeDeleteStore()
    hass = _hass_with_store(store)
    connection = _admin_connection(is_admin=False)

    await ws_api_module._websocket_analytics_delete_day(
        hass, connection, {"id": 1, "vin": VIN, "date": "2026-09-20"}
    )

    assert store.deleted_days == []
    assert connection.errors[1][0] == "unauthorized"


async def test_delete_day_invalid_date_is_invalid_format() -> None:
    store = _FakeDeleteStore()
    hass = _hass_with_store(store)
    connection = _admin_connection(is_admin=True)

    await ws_api_module._websocket_analytics_delete_day(
        hass, connection, {"id": 1, "vin": VIN, "date": "not-a-date"}
    )

    assert connection.errors[1][0] == "invalid_format"
    assert store.deleted_days == []


async def test_delete_vehicle_history_admin_succeeds() -> None:
    store = _FakeDeleteStore()
    hass = _hass_with_store(store)
    connection = _admin_connection(is_admin=True)

    await ws_api_module._websocket_analytics_delete_vehicle_history(
        hass, connection, {"id": 1, "vin": VIN}
    )

    assert store.vehicle_history_deleted is True
    assert 1 in connection.results


async def test_delete_vehicle_history_rejects_non_admin() -> None:
    store = _FakeDeleteStore()
    hass = _hass_with_store(store)
    connection = _admin_connection(is_admin=False)

    await ws_api_module._websocket_analytics_delete_vehicle_history(
        hass, connection, {"id": 1, "vin": VIN}
    )

    assert store.vehicle_history_deleted is False
    assert connection.errors[1][0] == "unauthorized"


def test_delete_schemas_accept_well_formed_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schemas = _registered_schemas(monkeypatch)
    messages = [
        {"type": "rivian/analytics/delete_drive", "vin": VIN, "drive_id": "d1"},
        {"type": "rivian/analytics/delete_day", "vin": VIN, "date": "2026-09-20"},
        {"type": "rivian/analytics/delete_vehicle_history", "vin": VIN},
    ]
    for i, message in enumerate(messages, start=1):
        schemas[message["type"]]({"id": i, **message})


def test_delete_schemas_reject_malformed_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voluptuous as vol

    schemas = _registered_schemas(monkeypatch)
    bad = [
        {"type": "rivian/analytics/delete_drive", "vin": VIN},
        {"type": "rivian/analytics/delete_day", "vin": VIN},
    ]
    for message in bad:
        with pytest.raises(vol.Invalid):
            schemas[message["type"]]({"id": 1, **message})


# -- multi-VIN reads (`vins`) and rivian/vehicles/list --------------------------

THIRD_VIN = "7PDSGABA8NN555555"
FOURTH_VIN = "7PDSGABA8NN444444"


def _hass_with_stores(*stores: Any) -> Any:
    """hass whose single entry holds several stores (for `vins` requests)."""
    return SimpleNamespace(
        data={DOMAIN: {"entry": {ATTR_DRIVE_STORE: {s.vin: s for s in stores}}}},
        bus=_FakeBus(),
    )


def test_schema_requires_exactly_one_of_vin_or_vins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voluptuous as vol

    if getattr(vol.Schema, "extend", None) is None:
        pytest.skip("voluptuous is mocked by conftest in this environment")

    # conftest mocks Home Assistant's websocket schema; use a real base here.
    monkeypatch.setattr(
        ws_api_module.websocket_api,
        "BASE_COMMAND_MESSAGE_SCHEMA",
        vol.Schema({vol.Required("id"): int}, extra=vol.ALLOW_EXTRA),
    )
    schema = _multi_vin_schema({vol.Required("type"): "x/y"})
    assert schema({"id": 1, "type": "x/y", "vin": VIN})["vin"] == VIN
    assert schema({"id": 1, "type": "x/y", "vins": [VIN, OTHER_VIN]})["vins"] == [
        VIN,
        OTHER_VIN,
    ]
    with pytest.raises(vol.Invalid):
        schema({"id": 1, "type": "x/y"})
    with pytest.raises(vol.Invalid):
        schema({"id": 1, "type": "x/y", "vin": VIN, "vins": [VIN]})
    with pytest.raises(vol.Invalid):
        schema({"id": 1, "type": "x/y", "vins": []})


async def test_multi_vin_unknown_vin_is_not_found_naming_it() -> None:
    hass = _hass_with_stores(_FakeAsyncStore(vin=VIN))
    connection = _FakeConnection()

    await _websocket_analytics_drives(
        hass, connection, {"id": 1, "vins": [VIN, OTHER_VIN]}
    )

    code, message = connection.errors[1]
    assert code == "not_found"
    assert OTHER_VIN in message


async def test_summary_vins_combines_from_sums_and_keeps_by_vin() -> None:
    def windows(miles: float, kwh: float, drives: int) -> dict[int | None, Any]:
        return {
            days: _stats(miles, kwh, miles / kwh, miles / kwh * 33.7, drives, 7200.0)
            for _label, days in SUMMARY_WINDOWS
        }

    older = _drive()
    newer = _drive()
    newer.drive_id = "newer"
    newer.start_time = "2026-09-25T10:00:00+00:00"
    a = _FakeSummaryStore(windows(100.0, 25.0, 4), last_drive=older, vin=VIN)  # 4.0
    b = _FakeSummaryStore(
        windows(60.0, 40.0, 2), last_drive=newer, vin=OTHER_VIN
    )  # 1.5
    hass = _hass_with_stores(a, b)
    connection = _FakeConnection()

    await _websocket_analytics_summary(
        hass, connection, {"id": 1, "vins": [VIN, OTHER_VIN]}
    )

    result = connection.results[1]
    seven = result["combined"]["7d"]
    assert seven["miles"] == 160.0
    assert seven["kwh"] == 65.0
    assert seven["drives"] == 6
    assert seven["hours"] == 4.0
    # recomputed from the sums (160 / 65), not the mean of 4.0 and 1.5
    assert seven["efficiency_mi_kwh"] == round(160 / 65, 2)
    assert seven["mpge"] == round(160 / 65 * MPGE_FACTOR, 1)
    assert set(result["combined"]) == {"7d", "30d", "365d", "all"}
    assert set(result["by_vin"]) == {VIN, OTHER_VIN}
    assert result["by_vin"][VIN]["windows"]["7d"]["miles"] == 100.0
    assert result["last_drive"]["drive_id"] == "newer"
    assert result["last_drive"]["vin"] == OTHER_VIN


async def test_summary_vin_payload_is_unchanged() -> None:
    stats = {days: _stats(1, 1, 1, 1, 1, 1) for _label, days in SUMMARY_WINDOWS}
    hass = _hass_with_stores(_FakeSummaryStore(stats, vin=VIN))
    connection = _FakeConnection()
    await _websocket_analytics_summary(hass, connection, {"id": 1, "vin": VIN})
    assert set(connection.results[1]) == {"windows", "last_drive"}


class _FakeMultiStore(_FakeCalendarDayStore):
    """Calendar/day surface for several vehicles at once."""

    def __init__(self, vin: str, **kwargs: Any) -> None:
        super().__init__(vin=vin, **kwargs)
        self.calendar_vins: list[Any] = []

    async def async_calendar(
        self,
        tz: Any,
        year: int | None = None,
        month: int | None = None,
        include_micro: bool = False,
        vins: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calendar_vins.append(vins)
        return await super().async_calendar(tz, year, month, include_micro)


async def test_calendar_vins_uses_the_first_store_with_the_vin_list() -> None:
    payload = {"totals": {"drives": 3, "by_vin": {}}, "years": []}
    a = _FakeMultiStore(VIN)
    b = _FakeMultiStore(OTHER_VIN, calendar_payload=payload)
    hass = _hass_with_stores(a, b)
    connection = _FakeConnection()

    await _websocket_analytics_calendar(
        hass, connection, {"id": 1, "vins": [OTHER_VIN, VIN], "year": 2026}
    )

    assert b.calendar_vins == [[OTHER_VIN, VIN]]
    assert a.calendar_vins == []
    assert connection.results[1] == payload


def _day_payload(
    vin: str, segs: list[tuple[str, float]], stops: list[Any]
) -> dict[str, Any]:
    return {
        "date": "2026-09-10",
        "totals": {
            "drives": len(segs),
            "miles": 10.0 * len(segs),
            "hours": 1.0,
            "energy_kwh": 4.0 * len(segs),
            "efficiency_mi_kwh": 2.5,
            "with_route": len(segs),
            "first_ts": segs[0][1],
            "last_ts": segs[-1][1],
        },
        "segments": [
            {"index": i, "drive_id": d, "sort_ts": ts} for i, (d, ts) in enumerate(segs)
        ],
        "prior_tail": {"tail": vin},
        "stops": stops,
        "gaps": [],
        "start": {"vin": vin, "s": 1},
        "end": {"vin": vin, "e": 1},
    }


async def test_day_vins_merges_segments_and_keeps_per_vehicle_stops() -> None:
    a = _FakeMultiStore(
        VIN,
        day_payload=_day_payload(VIN, [("a1", 100.0), ("a2", 300.0)], [{"stop": "A"}]),
    )
    b = _FakeMultiStore(
        OTHER_VIN,
        day_payload=_day_payload(OTHER_VIN, [("b1", 200.0)], [{"stop": "B"}]),
    )
    hass = _hass_with_stores(a, b)
    connection = _FakeConnection()

    await _websocket_analytics_day(
        hass, connection, {"id": 1, "vins": [VIN, OTHER_VIN], "date": "2026-09-10"}
    )

    result = connection.results[1]
    assert [(s["drive_id"], s["vin"]) for s in result["segments"]] == [
        ("a1", VIN),
        ("b1", OTHER_VIN),
        ("a2", VIN),
    ]
    assert all("sort_ts" not in s for s in result["segments"])
    assert result["vehicles"][VIN]["stops"] == [{"stop": "A"}]
    assert result["vehicles"][OTHER_VIN]["stops"] == [{"stop": "B"}]
    assert result["vehicles"][OTHER_VIN]["prior_tail"] == {"tail": OTHER_VIN}
    assert set(result["vehicles"][VIN]) == {
        "start",
        "end",
        "stops",
        "gaps",
        "prior_tail",
        "totals",
    }
    assert "stops" not in result
    assert result["totals"]["drives"] == 3
    assert result["totals"]["miles"] == 30.0
    assert result["totals"]["efficiency_mi_kwh"] == round(30.0 / 12.0, 2)
    assert result["totals"]["first_ts"] == 100.0
    assert result["totals"]["last_ts"] == 300.0


class _VinsHeatStore(_FakeHeatStore):
    def __init__(self, vin: str) -> None:
        super().__init__(vin=vin)
        self.vins_seen: list[Any] = []

    async def async_heat_info(self, period, key=None, vins=None):
        self.vins_seen.append(vins)
        return await super().async_heat_info(period, key)

    async def async_heat_tile(self, period, key, z, x, y, margin=0, vins=None):
        self.vins_seen.append(vins)
        return await super().async_heat_tile(period, key, z, x, y, margin)


async def test_heat_vins_passes_the_vin_list_and_single_vin_does_not() -> None:
    a, b = _VinsHeatStore(VIN), _VinsHeatStore(OTHER_VIN)
    hass = _hass_with_stores(a, b)
    connection = _FakeConnection()

    await _websocket_analytics_heat(
        hass, connection, {"id": 1, "vins": [VIN, OTHER_VIN], "period": "all"}
    )
    await _websocket_analytics_heat_tile(
        hass,
        connection,
        {"id": 2, "vins": [VIN, OTHER_VIN], "period": "all", "z": 5, "x": 1, "y": 2},
    )
    await _websocket_analytics_heat(
        hass, connection, {"id": 3, "vin": OTHER_VIN, "period": "all"}
    )

    assert a.vins_seen == [[VIN, OTHER_VIN], [VIN, OTHER_VIN]]
    assert b.vins_seen == [None]
    assert {1, 2, 3} <= set(connection.results)


async def test_series_vins_merges_with_vin_tags_sorted_and_summed() -> None:
    d1 = _drive()
    d1.drive_id = "late"
    d1.start_time = "2026-09-22T10:00:00+00:00"
    d2 = _drive()
    d2.drive_id = "early"
    d2.start_time = "2026-09-18T10:00:00+00:00"
    a = _FakeStore([d1])
    b = _FakeStore([d2])
    a.vin, b.vin = VIN, OTHER_VIN
    hass = _hass_with_stores(a, b)
    connection = _FakeConnection()

    await _websocket_analytics_series(
        hass,
        connection,
        {
            "id": 1,
            "vins": [VIN, OTHER_VIN],
            "series": list(VALID_SERIES_KEYS),
            "days": 30,
        },
    )

    result = connection.results[1]
    assert [(d["drive_id"], d["vin"]) for d in result["drives"]] == [
        ("early", OTHER_VIN),
        ("late", VIN),
    ]
    assert {c["vin"] for c in result["chunks"]} == {VIN, OTHER_VIN}
    assert result["segments"] == result["chunks"]
    assert [v["vin"] for v in result["vampire"]] == [VIN, OTHER_VIN]
    assert {s["vin"] for s in result["dcfc"]} == {VIN, OTHER_VIN}
    # speed_bins are summed per bin across the vehicles
    for bin_key, totals in a.speed_bin_totals.items():
        assert result["speed_bins"][bin_key]["miles"] == round(totals["miles"] * 2, 2)


async def test_series_vins_applies_max_chunks_after_the_merge() -> None:
    def many(vin: str, day: str) -> _FakeStore:
        drive = _drive()
        drive.chunks = [
            DriveChunk(
                start_time=f"2026-09-{day}T10:{i // 60:02d}:{i % 60:02d}+00:00",
                duration_seconds=180.0,
                distance_miles=1.0,
                energy_kwh=0.5,
                efficiency_mi_kwh=2.0,
                avg_speed_mph=30.0,
                speed_bin="30-39",
            )
            for i in range(ws_api_module.MAX_CHUNKS)
        ]
        store = _FakeStore([drive])
        store.vin = vin
        return store

    hass = _hass_with_stores(many(VIN, "10"), many(OTHER_VIN, "11"))
    connection = _FakeConnection()
    await _websocket_analytics_series(
        hass,
        connection,
        {"id": 1, "vins": [VIN, OTHER_VIN], "series": ["chunks"], "days": 30},
    )
    chunks = connection.results[1]["chunks"]
    assert len(chunks) == ws_api_module.MAX_CHUNKS
    assert {c["vin"] for c in chunks} == {OTHER_VIN}  # the newest day wins


async def test_drives_vins_pages_newest_first_across_vehicles() -> None:
    a = _FakeAsyncStore(
        vin=VIN,
        drives=[_summary("a2", 400.0), _summary("a1", 100.0)],
        storage={"drive_count": 2, "db_bytes": 10},
    )
    b = _FakeAsyncStore(
        vin=OTHER_VIN,
        drives=[_summary("b2", 300.0), _summary("b1", 200.0)],
        storage={"drive_count": 2, "db_bytes": 10},
    )
    hass = _hass_with_stores(a, b)
    connection = _FakeConnection()

    await _websocket_analytics_drives(
        hass, connection, {"id": 1, "vins": [VIN, OTHER_VIN], "limit": 3}
    )

    result = connection.results[1]
    assert [(d["drive_id"], d["vin"]) for d in result["drives"]] == [
        ("a2", VIN),
        ("b2", OTHER_VIN),
        ("b1", OTHER_VIN),
    ]
    assert result["next_before_ts"] == 200.0
    assert all("sort_ts" not in d for d in result["drives"])
    assert result["storage"]["drive_count"] == 4
    assert a.list_drives_calls == [(None, 3, False)]
    assert b.list_drives_calls == [(None, 3, False)]


async def test_drives_vins_previews_are_requested_per_vehicle() -> None:
    a = _FakeAsyncStore(
        vin=VIN,
        drives=[_summary("a1", 400.0, has_track=True)],
        previews={"a1": {"lat": [1], "lon": [2]}},
    )
    b = _FakeAsyncStore(vin=OTHER_VIN, drives=[_summary("b1", 300.0, has_track=True)])
    hass = _hass_with_stores(a, b)
    connection = _FakeConnection()

    await _websocket_analytics_drives(
        hass,
        connection,
        {"id": 1, "vins": [VIN, OTHER_VIN], "limit": 10, "previews": True},
    )

    drives = {d["drive_id"]: d for d in connection.results[1]["drives"]}
    assert drives["a1"]["preview"] == {"lat": [1], "lon": [2]}
    assert "preview" not in drives["b1"]
    assert a.preview_requests == [["a1"]]
    assert b.preview_requests == [["b1"]]


def test_subscribe_vins_forwards_events_for_any_listed_vin() -> None:
    hass = _hass_with_stores(
        _FakeAsyncStore(vin=VIN),
        _FakeAsyncStore(vin=OTHER_VIN),
        _FakeAsyncStore(vin=THIRD_VIN),
    )
    connection = _FakeConnection()
    ws_api_module.websocket_api.event_message = lambda msg_id, data: {
        "id": msg_id,
        "event": data,
    }

    _websocket_analytics_subscribe(
        hass, connection, {"id": 1, "vins": [VIN, OTHER_VIN]}
    )

    hass.bus.fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": THIRD_VIN})
    assert connection.messages == []
    hass.bus.fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": OTHER_VIN})
    hass.bus.fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": VIN})
    assert connection.messages == [
        {"id": 1, "event": {"vin": OTHER_VIN}},
        {"id": 1, "event": {"vin": VIN}},
    ]


def test_subscribe_vins_unknown_vin_is_not_found() -> None:
    hass = _hass_with_stores(_FakeAsyncStore(vin=VIN))
    connection = _FakeConnection()
    _websocket_analytics_subscribe(
        hass, connection, {"id": 1, "vins": [VIN, OTHER_VIN]}
    )
    assert connection.errors[1][0] == "not_found"
    assert 1 not in connection.subscriptions


# -- rivian/vehicles/list ---------------------------------------------------


class _MetaStore:
    """A store holding the shared meta table in a dict."""

    def __init__(self, vin: str, meta: dict[str, str]) -> None:
        self.vin = vin
        self.meta = meta

    async def async_get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    async def async_set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


def _vehicles_hass(
    real: list[tuple[str, str, str]],
    meta: dict[str, str],
) -> Any:
    stores = {vin: _MetaStore(vin, meta) for vin, _n, _m in real}
    return SimpleNamespace(
        data={
            DOMAIN: {
                "entry": {
                    ATTR_VEHICLE: {
                        f"v{i}": {"vin": vin, "name": name, "model": model}
                        for i, (vin, name, model) in enumerate(real)
                    },
                    ATTR_DRIVE_STORE: stores,
                },
            }
        },
        bus=_FakeBus(),
    )


@pytest.fixture
def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Registry:
        def async_get_entity_id(self, domain: str, platform: str, unique_id: str):
            return {f"{VIN}-picture": "image.rivi_picture"}.get(unique_id)

    monkeypatch.setattr(
        ws_api_module, "er", SimpleNamespace(async_get=lambda hass: _Registry())
    )


async def test_vehicles_list_orders_vehicles_with_letters_and_colors(
    _registry: None,
) -> None:
    meta: dict[str, str] = {}
    hass = _vehicles_hass(
        [(VIN, "Rivi", "R1S"), (OTHER_VIN, "Otto", "R1T"), (THIRD_VIN, "New", "R2")],
        meta,
    )
    connection = _FakeConnection()

    await _websocket_vehicles_list(hass, connection, {"id": 1})

    result = connection.results[1]
    assert [v["vin"] for v in result] == [VIN, OTHER_VIN, THIRD_VIN]
    assert [v["letter"] for v in result] == ["A", "B", "C"]
    assert [v["name"] for v in result] == ["Rivi", "Otto", "New"]
    assert [(v["color"], v["color_dark"]) for v in result] == list(VEHICLE_PALETTE[:3])
    assert result[0]["picture_entity"] == "image.rivi_picture"
    assert result[1]["picture_entity"] is None
    assert set(result[0]) == {
        "vin",
        "name",
        "model",
        "letter",
        "color",
        "color_dark",
        "picture_entity",
    }


async def test_vehicle_colors_are_stable_and_slots_are_reused(_registry: None) -> None:
    import json

    meta: dict[str, str] = {}
    real = [(VIN, "Rivi", "R1S"), (OTHER_VIN, "Otto", "R1T")]

    real.append((THIRD_VIN, "New", "R2"))
    hass = _vehicles_hass(real, meta)
    await _websocket_vehicles_list(hass, _FakeConnection(), {"id": 1})
    assert json.loads(meta["vehicle_colors"]) == {VIN: 0, OTHER_VIN: 1, THIRD_VIN: 2}

    # Removing Otto never repaints the others.
    hass = _vehicles_hass([real[0], real[2]], meta)
    connection = _FakeConnection()
    await _websocket_vehicles_list(hass, connection, {"id": 2})
    colors = {v["vin"]: v["color"] for v in connection.results[2]}
    assert colors[THIRD_VIN] == VEHICLE_PALETTE[2][0]
    # Letters follow display order; colors do not.
    assert [v["letter"] for v in connection.results[2]] == ["A", "B"]

    # A newly added vehicle takes the lowest slot no known VIN holds: Otto
    # keeps slot 1 while absent, so it comes back in the same color.
    hass = _vehicles_hass([real[0], (FOURTH_VIN, "Newer", "R2")], meta)
    connection = _FakeConnection()
    await _websocket_vehicles_list(hass, connection, {"id": 3})
    colors = {v["vin"]: v["color"] for v in connection.results[3]}
    assert colors[FOURTH_VIN] == VEHICLE_PALETTE[3][0]
    assert colors[VIN] == VEHICLE_PALETTE[0][0]


def test_assign_vehicle_slots_shares_the_least_used_slot_when_exhausted() -> None:
    vins = [f"V{i}" for i in range(len(VEHICLE_PALETTE) + 1)]
    slots = assign_vehicle_slots({}, vins)
    assert [slots[v] for v in vins[:-1]] == list(range(len(VEHICLE_PALETTE)))
    assert slots[vins[-1]] == 0
    # Garbage in storage is ignored; valid entries are kept.
    assert assign_vehicle_slots({"A": 99, "B": 4}, ["A", "B"]) == {"A": 0, "B": 4}


def test_assign_vehicle_slots_keeps_an_absent_vehicles_slot() -> None:
    # B is briefly missing (entry reloading); it keeps slot 1 and gets it back.
    stored = {"A": 0, "B": 1}
    slots = assign_vehicle_slots(stored, ["A"])
    assert slots == {"A": 0, "B": 1}
    assert assign_vehicle_slots(slots, ["A", "B"]) == {"A": 0, "B": 1}
    # Once every slot is held, a new VIN reuses one no present vehicle uses.
    full = {f"old{i}": i for i in range(len(VEHICLE_PALETTE))}
    assert assign_vehicle_slots(full, ["old0", "new"])["new"] == 1
