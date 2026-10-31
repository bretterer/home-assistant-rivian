"""Tests for the Rivian analytics WebSocket API commands and payload contracts."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.rivian import websocket_api as ws_api_module
from custom_components.rivian.const import ATTR_DRIVE_STORE, DOMAIN
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
    _build_series_payload,
    _multi_vin_schema,
    _websocket_analytics_drive,
    _websocket_analytics_drives,
    _websocket_analytics_series,
    _websocket_analytics_subscribe,
    _websocket_analytics_summary,
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


def test_speed_bins_series_totals_the_storage_window() -> None:
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


# -- multi-VIN reads (`vins`) --------------------------

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
