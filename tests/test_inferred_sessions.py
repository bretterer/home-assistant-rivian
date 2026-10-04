"""Tests for charging sessions inferred from the battery-level history."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.rivian import (
    drive_storage as drive_storage_module,
    websocket_api as ws_api_module,
)
from custom_components.rivian.drive_models import ChargingSessionRecord, DriveRecord
from custom_components.rivian.drive_storage import DriveStore

VIN = "INFER0000000000001"
H = 3600.0
NOW = datetime.now(timezone.utc).timestamp()
# The unrecorded charge: three hours starting 47 h ago, 60 -> 90 %.
CHARGE_START = NOW - 47 * H


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _drive(end_ts: float, lat: float = 39.8242, lon: float = -105.0880) -> DriveRecord:
    return DriveRecord(
        vin=VIN,
        drive_id="d1",
        start_time=_iso(end_ts - 1200),
        end_time=_iso(end_ts),
        distance_miles=10.0,
        duration_seconds=1200.0,
        start_soc=65.0,
        end_soc=60.0,
        battery_capacity_kwh=100.0,
        energy_kwh=4.0,
        avg_speed_mph=30.0,
        start_lat=44.0,
        start_lon=-104.7880,
        end_lat=lat,
        end_lon=lon,
    )


def _points() -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    t = NOW - 4 * 86400.0
    while t <= NOW:
        if t < CHARGE_START:
            soc = 60.0
        elif t < CHARGE_START + 3 * H:
            soc = 60.0 + 10.0 * (t - CHARGE_START) / H
        else:
            soc = 90.0
        pts.append((t, soc))
        t += H
    return pts


def _session(
    sid: str, start: float, source: str = "live", hours: float = 1.0
) -> ChargingSessionRecord:
    return ChargingSessionRecord(
        session_id=sid,
        start_time=_iso(start),
        end_time=_iso(start + hours * 3600),
        start_soc=40.0,
        end_soc=60.0,
        energy_added_kwh=20.0,
        max_power_kw=7.0,
        avg_power_kw=7.0,
        kind="ac",
        source=source,
    )


@pytest.fixture
def reader() -> Any:
    pts = _points()

    async def fake(
        _h: Any, _entity: str, start: float, end: float, period: str, _stat: str
    ) -> list[tuple[float, float]]:
        if period != "hour":
            return []
        return [p for p in pts if start <= p[0] <= end]

    return fake


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = SimpleNamespace(async_get_entity_id=lambda *_a: "sensor.battery_level")
    monkeypatch.setattr(
        drive_storage_module, "er", SimpleNamespace(async_get=lambda _h: registry)
    )


async def _store(mock_hass: Any, db: Any, demo: bool = False) -> DriveStore:
    store = DriveStore(mock_hass, VIN, db, is_demo=demo)
    await store.async_load()
    return store


def _inferred(db: Any) -> list[dict[str, Any]]:
    return [s for s in db.list_charging_sessions(VIN) if s["source"] == "inferred"]


async def test_infers_a_charge_with_deterministic_id_and_location(
    mock_hass: Any, analytics_db: Any, reader: Any, registered: None
) -> None:
    analytics_db.create_place("real", 39.8242, -105.0880, "Home", 100.0, "home")
    analytics_db.upsert_drives(VIN, [_drive(NOW - 50 * H)])
    store = await _store(mock_hass, analytics_db)

    result = await store.async_infer_charging_sessions(reader=reader)

    # The first run scans the whole history.
    assert result == {"detected": 1, "changed": 1, "full": 1}
    (session,) = _inferred(analytics_db)
    assert session["session_id"] == f"inferred:{VIN}:{int(session['start_ts'])}"
    assert session["kind"] == "ac"
    assert (session["start_soc"], session["end_soc"]) == (60.0, 90.0)
    assert session["energy_added_kwh"] == pytest.approx(30.0, abs=0.5)
    # Location from the latest drive that ended before it, so the place resolves.
    assert (session["lat"], session["lon"]) == (39.8242, -105.0880)
    assert session["place"]["label"] == "Home"
    assert session["place"]["category"] == "home"
    mock_hass.bus.async_fire.assert_called()


async def test_rerun_replaces_instead_of_duplicating(
    mock_hass: Any, analytics_db: Any, reader: Any, registered: None
) -> None:
    analytics_db.upsert_drives(VIN, [_drive(NOW - 50 * H)])
    store = await _store(mock_hass, analytics_db)
    await store.async_infer_charging_sessions(reader=reader)
    first = _inferred(analytics_db)
    # Later runs only re-check the last few days, and change nothing here.
    again = await store.async_infer_charging_sessions(reader=reader)
    assert again == {"detected": 1, "changed": 0, "full": 0}
    assert [s["session_id"] for s in _inferred(analytics_db)] == [
        s["session_id"] for s in first
    ]
    # An explicit full re-scan (the service) also lands on the same set.
    full = await store.async_infer_charging_sessions(reader=reader, full=True)
    assert full == {"detected": 1, "changed": 0, "full": 1}
    assert len(_inferred(analytics_db)) == 1


async def test_incremental_run_keeps_older_inferred_rows(
    mock_hass: Any, analytics_db: Any, reader: Any, registered: None
) -> None:
    """After the one-time back-scan, a run only replaces rows inside its window."""
    analytics_db.upsert_drives(VIN, [_drive(NOW - 50 * H)])
    store = await _store(mock_hass, analytics_db)
    await store.async_infer_charging_sessions(reader=reader)
    (found,) = _inferred(analytics_db)
    # An incremental run inside the window re-finds the same charge.
    result = await store.async_infer_charging_sessions(reader=reader)
    assert result is not None and result["full"] == 0
    assert [s["session_id"] for s in _inferred(analytics_db)] == [found["session_id"]]
    # A stamp from an older detection version forces the full back-scan again.
    analytics_db.set_inferred_scan(VIN, {"version": 0, "through": NOW})
    result = await store.async_infer_charging_sessions(reader=reader)
    assert result is not None and result["full"] == 1


async def test_no_location_when_the_last_drive_is_too_old(
    mock_hass: Any, analytics_db: Any, reader: Any, registered: None
) -> None:
    analytics_db.upsert_drives(VIN, [_drive(CHARGE_START - 10 * 86400.0)])
    store = await _store(mock_hass, analytics_db)
    await store.async_infer_charging_sessions(reader=reader)
    (session,) = _inferred(analytics_db)
    assert session["lat"] is None and session["place"] is None


async def test_deleted_inferred_session_is_not_re_added(
    mock_hass: Any, analytics_db: Any, reader: Any, registered: None
) -> None:
    analytics_db.upsert_drives(VIN, [_drive(NOW - 50 * H)])
    store = await _store(mock_hass, analytics_db)
    await store.async_infer_charging_sessions(reader=reader)
    (session,) = _inferred(analytics_db)

    assert await store.async_delete_dcfc_session(session["session_id"]) == 1
    assert json.loads(analytics_db.get_meta(f"inferred_deleted:{VIN}")) == [
        session["session_id"]
    ]
    await store.async_infer_charging_sessions(reader=reader)
    assert _inferred(analytics_db) == []


async def test_real_sessions_are_never_touched_or_overlapped(
    mock_hass: Any, analytics_db: Any, reader: Any, registered: None
) -> None:
    analytics_db.upsert_drives(VIN, [_drive(NOW - 50 * H)])
    # A live session that covers the whole unrecorded charge, plus a Rivian one.
    analytics_db.upsert_dcfc_sessions(
        VIN,
        [
            _session("live1", CHARGE_START - 600, hours=5.0),
            _session("riv1", NOW - 10 * 86400.0, source="rivian"),
        ],
    )
    store = await _store(mock_hass, analytics_db)
    await store.async_infer_charging_sessions(reader=reader)
    ids = {
        s["session_id"]: s["source"] for s in analytics_db.list_charging_sessions(VIN)
    }
    assert ids == {"live1": "live", "riv1": "rivian"}


def test_replace_skips_overlap_with_non_inferred_row(analytics_db: Any) -> None:
    analytics_db.upsert_dcfc_sessions(VIN, [_session("live1", 1_000_000.0)])
    changed = analytics_db.replace_inferred_sessions(
        VIN,
        [
            _session(f"inferred:{VIN}:1000300", 1_000_300.0, source="inferred"),
            _session(f"inferred:{VIN}:2000000", 2_000_000.0, source="inferred"),
        ],
    )
    assert changed is True
    sources = {
        s["session_id"]: s["source"] for s in analytics_db.list_charging_sessions(VIN)
    }
    assert sources == {"live1": "live", f"inferred:{VIN}:2000000": "inferred"}
    # intervals can leave inferred rows out.
    assert len(analytics_db.charging_session_intervals(VIN)) == 2
    assert len(analytics_db.charging_session_intervals(VIN, exclude_inferred=True)) == 1


def test_replace_since_keeps_older_inferred_rows(analytics_db: Any) -> None:
    """An incremental replace only touches inferred rows from `since_ts` on."""
    old = _session(f"inferred:{VIN}:1000000", 1_000_000.0, source="inferred")
    mid = _session(f"inferred:{VIN}:2000000", 2_000_000.0, source="inferred")
    analytics_db.replace_inferred_sessions(VIN, [old, mid])
    new = _session(f"inferred:{VIN}:2100000", 2_100_000.0, source="inferred")
    assert analytics_db.replace_inferred_sessions(VIN, [new], 1_500_000.0) is True
    ids = sorted(
        s["session_id"]
        for s in analytics_db.list_charging_sessions(VIN)
        if s["source"] == "inferred"
    )
    assert ids == [f"inferred:{VIN}:1000000", f"inferred:{VIN}:2100000"]


async def test_demo_store_is_skipped(
    mock_hass: Any, analytics_db: Any, reader: Any, registered: None
) -> None:
    store = await _store(mock_hass, analytics_db, demo=True)
    assert await store.async_infer_charging_sessions(reader=reader) is None
    assert _inferred(analytics_db) == []


# -- WebSocket payload ------------------------------------------------------------------


def _stored(sid: str, source: str, place: dict[str, Any] | None, **over: Any) -> Any:
    base = {
        "session_id": sid,
        "kind": "ac",
        "start_ts": 1000.0,
        "end_ts": 5000.0,
        "start_soc": 40.0,
        "end_soc": 80.0,
        "energy_added_kwh": 0.0,
        "max_power_kw": 0.0,
        "avg_power_kw": 0.0,
        "place": place,
        "source": source,
        "samples": [],
    }
    base.update(over)
    return base


def test_session_payload_inferred_and_home_flags() -> None:
    ref = {"pack": "large", "approximate": False, "x": [0, 100], "y": [100, 100]}
    ref = ws_api_module.charge_curves.reference("large")

    def payload(session: dict[str, Any]) -> dict[str, Any]:
        return ws_api_module._session_payload(session, VIN, ref, 135.0)

    inferred = payload(_stored("i", "inferred", None))
    assert inferred["inferred"] is True and inferred["is_home_charge"] is False
    assert inferred["energy_added_kwh"] is None and inferred["max_power_kw"] is None
    assert inferred["avg_power_kw"] is None
    assert payload(_stored("l", "live", None, avg_power_kw=7.0))["inferred"] is False

    cat = {"id": 1, "label": "House", "category": "home", "zone_entity_id": None}
    out = payload(_stored("a", "live", cat))
    assert out["is_home_charge"] is True
    assert out["place"] == {"id": 1, "label": "House", "category": "home"}
    zone = {"id": 2, "label": "Home", "category": None, "zone_entity_id": "zone.home"}
    assert payload(_stored("b", "live", zone))["is_home_charge"] is True
    work = {"id": 3, "label": "Work", "category": "work", "zone_entity_id": None}
    assert payload(_stored("c", "live", work))["is_home_charge"] is False
    assert payload(_stored("d", "live", None, is_home=True))["is_home_charge"] is True
    assert payload(
        _stored("e", "live", None, vendor="Rivian Wall Charger", is_home=True)
    )["is_home_charge"]
    assert payload(_stored("f", "live", None))["is_home_charge"] is False


async def test_soc_timeline_does_not_redetect_a_stored_inferred_span(
    mock_hass: Any, analytics_db: Any, reader: Any, registered: None, monkeypatch: Any
) -> None:
    analytics_db.upsert_drives(VIN, [_drive(NOW - 50 * H)])
    store = await _store(mock_hass, analytics_db)
    registry = SimpleNamespace(async_get_entity_id=lambda *_a: "sensor.battery_level")
    monkeypatch.setattr(
        ws_api_module, "er", SimpleNamespace(async_get=lambda _h: registry)
    )

    async def stats(_h: Any, *a: Any) -> list[tuple[float, float]]:
        return await reader(_h, *a)

    monkeypatch.setattr(ws_api_module, "async_entity_statistics", stats)
    hass = SimpleNamespace(data={"rivian": {"e": {"drive_store": {VIN: store}}}})

    class Conn:
        results: dict[int, Any] = {}

        def send_result(self, msg_id: int, result: Any = None) -> None:
            self.results[msg_id] = result

        def send_error(self, *_a: Any) -> None:
            raise AssertionError(_a)

    monkeypatch.setattr(ws_api_module, "_resolve_stores", lambda *_a: ([store], False))
    msg = {"id": 1, "vins": [VIN], "start": NOW - 4 * 86400.0, "end": NOW}
    conn = Conn()
    await ws_api_module._websocket_battery_soc_timeline(hass, conn, msg)
    assert len(conn.results[1]["detected"][VIN]) == 1  # not stored yet: detected

    await store.async_infer_charging_sessions(reader=reader)
    conn = Conn()
    await ws_api_module._websocket_battery_soc_timeline(hass, conn, msg)
    assert conn.results[1]["detected"][VIN] == []  # now stored: not twice
