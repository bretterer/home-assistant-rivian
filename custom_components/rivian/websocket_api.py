"""WebSocket API for Rivian trip efficiency & analytics data.

The ``rivian/analytics/drives``, ``rivian/analytics/drive``,
``rivian/analytics/summary`` and ``rivian/analytics/subscribe`` handlers read
SQLite (via :class:`~custom_components.rivian.drive_storage.DriveStore`'s
``async_`` methods), but only ever through the executor round-trip those
wrappers already do internally; nothing here calls into ``AnalyticsDatabase``
directly or blocks the event loop.

``rivian/analytics/series`` serves bulk chart data. For ``days <= 90`` it is
built entirely, and synchronously, from each vehicle's in-memory
:class:`~custom_components.rivian.drive_storage.DriveStore` hot cache and
must never touch SQLite. For ``days > 90`` it goes through
``DriveStore.async_series_window`` (executor-bound) to reach further back
than the cache holds.

``rivian/analytics/summary`` serves rolling/all-time aggregate stats (7d,
30d, 365d, all-time) plus the most recent drive, for the Overview tab's
per-vehicle card.

None of this bulk data is ever attached to entity state attributes; the
recorder's 16 KiB attribute limit and the state DB are irrelevant here.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
import json
import logging
from typing import Any, Final

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import battery_analytics, charge_curves, charger_lookup
from .const import (
    ATTR_DRIVE_STORE,
    ATTR_VEHICLE,
    DOMAIN,
    RIVIAN_ANALYTICS_UPDATED_EVENT,
)
from .drive_models import (
    AC_L1_MAX_KW,
    MPGE_FACTOR,
    AggregatedDriveStats,
    DriveChunk,
    DriveRecord,
    VampireDrainRecord,
)
from .drive_storage import DriveStore
from .places import DATASET_REAL, DATASETS, PLACE_CATEGORIES, category_options
from .statistics import async_entity_statistics, async_soc_points

_LOGGER = logging.getLogger(__name__)

WS_TYPE_VEHICLES_LIST: Final[str] = "rivian/vehicles/list"
WS_TYPE_ANALYTICS_SERIES: Final[str] = "rivian/analytics/series"
WS_TYPE_ANALYTICS_DRIVES: Final[str] = "rivian/analytics/drives"
WS_TYPE_ANALYTICS_DRIVE: Final[str] = "rivian/analytics/drive"
WS_TYPE_ANALYTICS_SUMMARY: Final[str] = "rivian/analytics/summary"
WS_TYPE_ANALYTICS_CALENDAR: Final[str] = "rivian/analytics/calendar"
WS_TYPE_ANALYTICS_DAY: Final[str] = "rivian/analytics/day"
WS_TYPE_ANALYTICS_HEAT: Final[str] = "rivian/analytics/heat"
WS_TYPE_ANALYTICS_HEAT_TILE: Final[str] = "rivian/analytics/heat_tile"
WS_TYPE_ANALYTICS_SUBSCRIBE: Final[str] = "rivian/analytics/subscribe"
WS_TYPE_ANALYTICS_DELETE_DRIVE: Final[str] = "rivian/analytics/delete_drive"
WS_TYPE_ANALYTICS_DELETE_DAY: Final[str] = "rivian/analytics/delete_day"
WS_TYPE_ANALYTICS_DELETE_VEHICLE_HISTORY: Final[str] = (
    "rivian/analytics/delete_vehicle_history"
)
WS_TYPE_PLACES_LIST: Final[str] = "rivian/places/list"
WS_TYPE_PLACES_UPDATE: Final[str] = "rivian/places/update"
WS_TYPE_PLACES_CREATE: Final[str] = "rivian/places/create"
WS_TYPE_PLACES_MERGE: Final[str] = "rivian/places/merge"
WS_TYPE_PLACES_REBUILD: Final[str] = "rivian/places/rebuild"
WS_TYPE_PLACES_DELETE: Final[str] = "rivian/places/delete"
WS_TYPE_ROUTES_LIST: Final[str] = "rivian/routes/list"
WS_TYPE_ROUTES_ROUTE: Final[str] = "rivian/routes/route"
WS_TYPE_ROUTES_RENAME: Final[str] = "rivian/routes/rename"
WS_TYPE_CHARGING_DELETE_SESSION: Final[str] = "rivian/charging/delete_session"
WS_TYPE_CHARGING_SESSIONS: Final[str] = "rivian/charging/sessions"
WS_TYPE_CHARGING_REFERENCE: Final[str] = "rivian/charging/reference"
WS_TYPE_BATTERY_SOC_TIMELINE: Final[str] = "rivian/battery/soc_timeline"
WS_TYPE_BATTERY_CAPACITY: Final[str] = "rivian/battery/capacity"
ROUTE_NAME_MAX_LEN: Final[int] = 80
VALID_HEAT_PERIODS: Final[tuple[str, ...]] = ("all", "year", "month")
# Places and routes belong to no vehicle: ``dataset`` ('real' by default) picks
# the data, and ``vin`` stays accepted as an alias that implies its dataset.
_DATASET_FIELDS: Final[dict[Any, Any]] = {
    vol.Optional("vin"): str,
    vol.Optional("dataset"): vol.In(DATASETS),
}
PLACE_RADIUS_MIN: Final[float] = 25.0
PLACE_RADIUS_MAX: Final[float] = 1000.0
PLACE_NAME_MAX_LEN: Final[int] = 80
SUMMARY_WINDOWS: Final[tuple[tuple[str, int | None], ...]] = (
    ("7d", 7),
    ("30d", 30),
    ("365d", 365),
    ("all", None),
)
VALID_SERIES_KEYS: Final[tuple[str, ...]] = (
    "drives",
    "chunks",
    # "segments" is a legacy alias for "chunks": dashboards generated before
    # the segment->chunk rename request ``series: ["segments"]`` and read
    # ``window.__rivianAnalytics[vin].segments``, so it must keep working
    # until those dashboards are regenerated.
    "segments",
    "vampire",
    "dcfc",
    "speed_bins",
)
MAX_CHUNKS: Final[int] = 600

# Per-vehicle categorical colors, (light, dark) per slot, in the dataviz
# skill's fixed categorical order (references/palette.md). A vehicle's slot is
# persisted in the analytics DB's ``meta`` row below so its color never moves
# when other vehicles are added or removed.
VEHICLE_PALETTE: Final[tuple[tuple[str, str], ...]] = (
    ("#2a78d6", "#3987e5"),  # blue
    ("#eb6834", "#d95926"),  # orange
    ("#1baf7a", "#199e70"),  # aqua
    ("#eda100", "#c98500"),  # yellow
    ("#e87ba4", "#d55181"),  # magenta
    ("#008300", "#008300"),  # green
    ("#4a3aa7", "#9085e9"),  # violet
    ("#e34948", "#e66767"),  # red
)
VEHICLE_COLORS_META_KEY: Final[str] = "vehicle_colors"
MAX_DCFC_SAMPLES: Final[int] = 60
SERIES_CACHE_MAX_DAYS: Final[int] = 90
SECONDS_PER_DAY: Final[float] = 86400.0
# A battery-% timeline window up to this long uses 5-minute statistics.
SOC_TIMELINE_FINE_MAX_DAYS: Final[int] = 10

WS_API_REGISTERED_KEY: Final[str] = "_ws_api_registered"


def _find_store(hass: HomeAssistant, vin: str) -> DriveStore | None:
    """Find the DriveStore for a given VIN across every config entry's data."""
    domain_data = hass.data.get(DOMAIN, {})
    for entry_data in domain_data.values():
        if not isinstance(entry_data, dict):
            continue
        stores: dict[str, DriveStore] | None = entry_data.get(ATTR_DRIVE_STORE)
        if not stores:
            continue
        for store in stores.values():
            if store.vin == vin:
                return store
    return None


def _find_stores(
    hass: HomeAssistant, vins: list[str]
) -> tuple[list[DriveStore], str | None]:
    """Resolve every VIN to its DriveStore, in the requested order.

    Returns ``(stores, missing_vin)``: ``missing_vin`` is the first unknown VIN
    (the caller replies ``not_found`` naming it) and ``stores`` is then empty.
    Duplicate VINs are collapsed.
    """
    stores: list[DriveStore] = []
    for vin in dict.fromkeys(vins):
        store = _find_store(hass, vin)
        if store is None:
            return [], vin
        stores.append(store)
    return stores, None


def _require_vin_or_vins(msg: dict[str, Any]) -> dict[str, Any]:
    """Voluptuous validator: one of ``vin`` / ``vins`` must be present."""
    if "vin" not in msg and "vins" not in msg:
        raise vol.Invalid("required key not provided: 'vin' or 'vins'")
    return msg


def _multi_vin_schema(fields: dict[Any, Any]) -> vol.Schema:
    """Command schema accepting exactly one of ``vin`` or ``vins`` (non-empty)."""
    return vol.All(
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Exclusive("vin", "vin_or_vins"): str,
                vol.Exclusive("vins", "vin_or_vins"): vol.All([str], vol.Length(min=1)),
                **fields,
            }
        ),
        _require_vin_or_vins,
    )


def _resolve_stores(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> tuple[list[DriveStore], bool] | None:
    """Resolve a ``vin`` / ``vins`` request to ``(stores, multi)``.

    ``multi`` is True when the caller used ``vins`` (even with one entry), which
    selects the combined payload shape. On an unknown VIN this sends the
    ``not_found`` error itself and returns None.
    """
    multi = "vins" in msg
    vins: list[str] = list(msg["vins"]) if multi else [msg["vin"]]
    stores, missing = _find_stores(hass, vins)
    if missing is not None:
        _not_found(connection, msg["id"], missing)
        return None
    return stores, multi


def _vehicle_letter(index: int) -> str:
    """A, B, ... Z, AA, AB, ... for the vehicle at ``index``."""
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def assign_vehicle_slots(stored: dict[str, int], vins: list[str]) -> dict[str, int]:
    """Stable palette slots: a VIN keeps its slot for good, even while absent.

    A vehicle can be missing for a while (its config entry reloading,
    car removed and re-added), and it should come back in the same color, so
    absent VINs keep their stored slot. A new VIN takes the lowest slot no
    known VIN holds; once every slot is held, the lowest one no *present*
    vehicle uses; and with more present vehicles than slots, the least-used
    one is shared.
    """
    size = len(VEHICLE_PALETTE)
    slots = {vin: slot for vin, slot in stored.items() if slot in range(size)}
    for vin in vins:
        if vin in slots:
            continue
        held = set(slots.values())
        present = [slots[v] for v in vins if v in slots]
        unheld = [i for i in range(size) if i not in held]
        unused_now = [i for i in range(size) if i not in present]
        if unheld:
            slots[vin] = unheld[0]
        elif unused_now:
            slots[vin] = unused_now[0]
        else:
            slots[vin] = min(range(size), key=lambda i: (present.count(i), i))
    return slots


def _parse_slots(raw: str | None) -> dict[str, int]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, int)}


def _decimate(samples: list[Any], limit: int) -> list[Any]:
    """Evenly downsample a sequence to at most ``limit`` items, keeping first/last."""
    count = len(samples)
    if count <= limit or limit <= 0:
        return list(samples)
    if limit == 1:
        return [samples[0]]

    step = (count - 1) / (limit - 1)
    indices = sorted({round(i * step) for i in range(limit)})
    return [samples[i] for i in indices]


def _bin_value(value: Any, attr: str) -> float:
    """Read miles or seconds from a speed bin held as SpeedBinData, a dict, or bare miles."""
    if hasattr(value, attr):
        return float(getattr(value, attr))
    if isinstance(value, dict):
        return float(value.get(attr, 0.0))
    if attr == "miles" and isinstance(value, (int, float)):
        return float(value)
    return 0.0


# The drive and chunk shapes below are a contract with the chart expressions
# in dashboard_generator.py (they read e.g. d.distance, d.efficiency, d.temp_f);
# tests/test_websocket_api.py fails if a chart reads a field this doesn't send.
def _drive_chart_dict(drive: DriveRecord) -> dict[str, Any]:
    """Shape a drive the way the dashboard's chart expressions read it."""
    temp = drive.integrated_temperature_f
    return {
        "drive_id": drive.drive_id,
        "start_time": drive.start_time,
        "distance": round(drive.distance_miles, 2),
        "energy_kwh": round(drive.energy_kwh, 2),
        "efficiency": round(drive.efficiency_mi_kwh, 2),
        "mpge": round(drive.mpge, 1),
        "elevation_change_ft": round(drive.elevation_change_ft, 0),
        "avg_speed_mph": round(drive.avg_speed_mph, 1),
        "temp_f": round(temp, 1) if temp is not None else None,
        "speed_bins": {
            key: {
                "miles": round(_bin_value(value, "miles"), 2),
                "seconds": round(_bin_value(value, "seconds"), 0),
            }
            for key, value in (drive.speed_bins or {}).items()
        },
    }


def _chunk_chart_dict(chunk: DriveChunk, drive: DriveRecord) -> dict[str, Any]:
    """Shape a chunk for the charts, borrowing its drive's temperature if it has none."""
    data = chunk.to_dict()
    if data.get("temp_f") is None and drive.integrated_temperature_f is not None:
        data["temp_f"] = round(drive.integrated_temperature_f, 1)
    return data


def _build_series_payload(
    store: DriveStore,
    series: list[str],
    *,
    drives: list[DriveRecord] | None = None,
    vampire: list[VampireDrainRecord] | None = None,
    limit_chunks: bool = True,
) -> dict[str, Any]:
    """Build the requested subset of series.

    By default everything comes from the DriveStore hot cache (synchronous,
    never touches SQLite). Passing explicit ``drives``/``vampire`` lists
    (e.g. from ``DriveStore.async_series_window``) substitutes them for the
    cache's ``recent_drives``/``recent_vampire_events`` when building the
    "drives"/"chunks" and "vampire" series respectively; ``dcfc`` and
    "speed_bins" always come from the cache regardless.
    """
    payload: dict[str, Any] = {}
    drive_list = store.recent_drives if drives is None else drives
    vampire_list = store.recent_vampire_events if vampire is None else vampire

    if "drives" in series:
        payload["drives"] = [_drive_chart_dict(d) for d in drive_list]

    if "chunks" in series or "segments" in series:
        chunks = [
            _chunk_chart_dict(chunk, drive)
            for drive in drive_list
            for chunk in drive.chunks
        ]
        if limit_chunks:
            chunks = chunks[-MAX_CHUNKS:]
        if "chunks" in series:
            payload["chunks"] = chunks
        if "segments" in series:
            # Legacy alias: see the comment on VALID_SERIES_KEYS above.
            payload["segments"] = chunks

    if "speed_bins" in series:
        payload["speed_bins"] = store.speed_bin_totals

    if "vampire" in series:
        payload["vampire"] = [event.to_dict() for event in vampire_list]

    if "dcfc" in series:
        dcfc_payload: list[dict[str, Any]] = []
        for session in store.get_dcfc_sessions():
            session_dict = session.to_dict()
            session_dict["samples"] = _decimate(
                session_dict.get("samples", []), MAX_DCFC_SAMPLES
            )
            dcfc_payload.append(session_dict)
        payload["dcfc"] = dcfc_payload

    return payload


def _not_found(
    connection: websocket_api.ActiveConnection, msg_id: int, vin: str
) -> None:
    """Send the standard "no such vehicle" error for an unknown VIN."""
    connection.send_error(msg_id, "not_found", f"No Rivian vehicle found for VIN {vin}")


def _ts_key(item: dict[str, Any]) -> float:
    """Sort key for a series item by its ``start_time`` (unparseable sorts first)."""
    ts = _parse_start_ts(item.get("start_time") or "")
    return ts if ts is not None else 0.0


def _merge_series_payloads(
    parts: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Merge per-VIN series payloads into one, tagging every item with ``vin``.

    List series (drives/chunks/segments/vampire/dcfc) are concatenated and
    re-sorted by time (``MAX_CHUNKS`` applies to the merged chunks, keeping
    the newest); ``speed_bins`` are summed per bin.
    """
    merged: dict[str, Any] = {}
    for vin, payload in parts:
        for key, value in payload.items():
            if key == "speed_bins":
                bins = merged.setdefault("speed_bins", {})
                for bin_key, bin_val in (value or {}).items():
                    slot = bins.setdefault(bin_key, {"miles": 0.0, "seconds": 0.0})
                    slot["miles"] += _bin_value(bin_val, "miles")
                    slot["seconds"] += _bin_value(bin_val, "seconds")
                continue
            items = merged.setdefault(key, [])
            items.extend({**item, "vin": vin} for item in value)
    for key, value in merged.items():
        if key == "speed_bins":
            for slot in value.values():
                slot["miles"] = round(slot["miles"], 2)
                slot["seconds"] = round(slot["seconds"], 0)
            continue
        value.sort(key=_ts_key)
        if key in ("chunks", "segments"):
            merged[key] = value[-MAX_CHUNKS:]
    return merged


@websocket_api.async_response
async def _websocket_analytics_series(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/series`` WebSocket command.

    ``days <= 90`` builds synchronously from the hot cache (never touches
    SQLite); ``days > 90`` reaches further back via
    ``DriveStore.async_series_window`` (executor-bound). With ``vins`` the
    per-VIN payloads are merged (see ``_merge_series_payloads``).
    """
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    stores, multi = resolved

    requested = [key for key in msg["series"] if key in VALID_SERIES_KEYS]
    days = msg.get("days", 90)
    long_window = days is not None and days > SERIES_CACHE_MAX_DAYS
    parts: list[tuple[str, dict[str, Any]]] = []
    for store in stores:
        if long_window:
            drives, vampire = await store.async_series_window(days)
            payload = _build_series_payload(
                store, requested, drives=drives, vampire=vampire, limit_chunks=not multi
            )
        else:
            payload = _build_series_payload(store, requested, limit_chunks=not multi)
        parts.append((store.vin, payload))
    connection.send_result(
        msg["id"], _merge_series_payloads(parts) if multi else parts[0][1]
    )


@websocket_api.async_response
async def _websocket_analytics_drives(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/drives`` WebSocket command (a paged listing).

    With ``vins`` the page is the newest ``limit`` drives across all of them
    (each store contributes its own newest ``limit`` before ``before_ts``, so
    the merged top ``limit`` is exact); every item is tagged with ``vin`` and
    ``storage`` sums the per-VIN counts.
    """
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    stores, multi = resolved

    before_ts = msg.get("before_ts")
    limit = msg.get("limit", 50)
    include_micro = msg.get("include_micro", False)
    want_previews = msg.get("previews", False)

    pages = await asyncio.gather(
        *(store.async_list_drives(before_ts, limit, include_micro) for store in stores)
    )
    drives: list[dict[str, Any]] = []
    for store, page in zip(stores, pages, strict=True):
        for drive in page:
            if multi:
                drive["vin"] = store.vin
            drives.append(drive)
    if multi:
        drives.sort(key=lambda d: d.get("sort_ts") or 0.0, reverse=True)
        drives = drives[:limit]

    if want_previews:
        for store in stores:
            track_ids = [
                d["drive_id"]
                for d in drives
                if d.get("has_track") and (not multi or d.get("vin") == store.vin)
            ]
            if not track_ids:
                continue
            previews = await store.async_get_track_previews(track_ids)
            for drive in drives:
                if multi and drive.get("vin") != store.vin:
                    continue
                preview = previews.get(drive["drive_id"])
                if preview is not None:
                    drive["preview"] = preview

    next_before_ts: float | None = None
    if drives and len(drives) == limit:
        last_sort_ts = drives[-1].get("sort_ts")
        if last_sort_ts is not None:
            next_before_ts = last_sort_ts

    for drive in drives:
        drive.pop("sort_ts", None)

    if multi:
        all_storage = await asyncio.gather(
            *(store.async_storage_stats() for store in stores)
        )
        storage: dict[str, Any] = {
            key: sum(st.get(key, 0) for st in all_storage)
            for key in (
                "drive_count",
                "track_count",
                "full_count",
                "thinned_count",
                "track_bytes",
            )
        }
        storage["db_bytes"] = max(
            (st.get("db_bytes", 0) for st in all_storage), default=0
        )
    else:
        storage = await stores[0].async_storage_stats()
    connection.send_result(
        msg["id"],
        {"drives": drives, "next_before_ts": next_before_ts, "storage": storage},
    )


@websocket_api.async_response
async def _websocket_analytics_drive(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/drive`` WebSocket command (single-drive detail)."""
    vin: str = msg["vin"]
    store = _find_store(hass, vin)
    if store is None:
        _not_found(connection, msg["id"], vin)
        return

    drive_id: str = msg["drive_id"]
    detail = await store.async_get_drive_detail(drive_id)
    if detail is None:
        connection.send_error(
            msg["id"], "not_found", f"No drive {drive_id} for VIN {vin}"
        )
        return

    detail["drive"].pop("sort_ts", None)
    connection.send_result(msg["id"], detail)


def _parse_start_ts(start_time: str) -> float | None:
    """Parse an ISO 8601 start_time to a POSIX epoch float, or None if unparseable."""
    try:
        return datetime.fromisoformat(start_time).timestamp()
    except (ValueError, TypeError):
        return None


def _window_stats_dict(stats: AggregatedDriveStats) -> dict[str, Any]:
    """Shape one aggregation window (7d/30d/365d/all) for the summary payload."""
    return {
        "miles": round(stats.total_miles, 2),
        "kwh": round(stats.total_kwh, 2),
        "efficiency_mi_kwh": round(stats.efficiency_mi_kwh, 2),
        "mpge": round(stats.mpge, 1),
        "drives": stats.drive_count,
        "hours": round(stats.total_duration_seconds / 3600.0, 1),
    }


def _last_drive_dict(drive: DriveRecord | None) -> dict[str, Any] | None:
    """Shape the most recent drive (cache-only, no SQLite) for the summary payload."""
    if drive is None:
        return None
    temp = drive.integrated_temperature_f
    return {
        "drive_id": drive.drive_id,
        "start_time": drive.start_time,
        "end_time": drive.end_time,
        "start_ts": _parse_start_ts(drive.start_time),
        "distance_miles": round(drive.distance_miles, 2),
        "duration_seconds": round(drive.duration_seconds, 1),
        "energy_kwh": round(drive.energy_kwh, 2),
        "efficiency_mi_kwh": round(drive.efficiency_mi_kwh, 2),
        "mpge": round(drive.mpge, 1),
        "temp_f": round(temp, 1) if temp is not None else None,
        "is_micro_drive": drive.is_micro_drive,
    }


async def _summary_for_store(
    store: DriveStore,
) -> tuple[dict[str, Any], list[AggregatedDriveStats]]:
    """One vehicle's summary payload plus its raw per-window stats (SUMMARY_WINDOWS order)."""
    results = await asyncio.gather(
        *(store.async_get_stats(days) for _label, days in SUMMARY_WINDOWS)
    )
    windows = {
        label: _window_stats_dict(stats)
        for (label, _days), stats in zip(SUMMARY_WINDOWS, results, strict=True)
    }
    payload: dict[str, Any] = {
        "windows": windows,
        "last_drive": _last_drive_dict(store.last_drive),
    }
    return payload, list(results)


def _combine_stats(parts: list[AggregatedDriveStats]) -> AggregatedDriveStats:
    """Sum windows across vehicles; efficiency and MPGe come from the sums, never averaged."""
    miles = sum(p.total_miles for p in parts)
    kwh = sum(p.total_kwh for p in parts)
    count = sum(p.drive_count for p in parts)
    efficiency = miles / kwh if kwh > 0.0 else 0.0
    return AggregatedDriveStats(
        total_miles=miles,
        total_kwh=kwh,
        efficiency_mi_kwh=efficiency,
        mpge=efficiency * MPGE_FACTOR,
        drive_count=count,
        total_duration_seconds=sum(p.total_duration_seconds for p in parts),
        avg_distance_miles=miles / count if count else 0.0,
        total_micro_drives=sum(p.total_micro_drives for p in parts),
    )


@websocket_api.async_response
async def _websocket_analytics_summary(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/summary`` WebSocket command.

    Computes rolling (7d/30d/365d) and all-time stats directly from SQLite
    (each an executor round-trip via ``DriveStore.async_get_stats``, run
    concurrently) plus the most recent drive from the cache-only surface.
    With ``vins``: ``{combined: {window: ...}, by_vin: {vin: <single payload>},
    last_drive: <newest across vins, with "vin"> | None}``.
    """
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    stores, multi = resolved

    per_store = [await _summary_for_store(store) for store in stores]
    if not multi:
        connection.send_result(msg["id"], per_store[0][0])
        return

    combined = {
        label: _window_stats_dict(
            _combine_stats([raw[idx] for _payload, raw in per_store])
        )
        for idx, (label, _days) in enumerate(SUMMARY_WINDOWS)
    }
    by_vin = {
        store.vin: payload
        for store, (payload, _raw) in zip(stores, per_store, strict=True)
    }
    newest: dict[str, Any] | None = None
    for store, (payload, _raw) in zip(stores, per_store, strict=True):
        last = payload["last_drive"]
        if last is None:
            continue
        if newest is None or (last.get("start_ts") or 0.0) > (
            newest.get("start_ts") or 0.0
        ):
            newest = {**last, "vin": store.vin}
    connection.send_result(
        msg["id"], {"combined": combined, "by_vin": by_vin, "last_drive": newest}
    )


@websocket_api.async_response
async def _websocket_analytics_calendar(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/calendar`` WebSocket command.

    Groups the drives into an All time -> years -> months -> days tree,
    by local calendar day in Home Assistant's configured time zone. With
    ``vins`` the tree is combined and every node (``totals`` and each
    year/month/day) also has ``by_vin: {vin: {drives, miles}}``.
    """
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    stores, multi = resolved

    year = msg.get("year")
    month = msg.get("month")
    if month is not None and year is None:
        connection.send_error(msg["id"], "invalid_format", "'month' requires 'year'")
        return

    tz = dt_util.get_default_time_zone()
    extra: dict[str, Any] = {"vins": [s.vin for s in stores]} if multi else {}
    payload = await stores[0].async_calendar(
        tz, year, month, msg.get("include_micro", False), **extra
    )
    connection.send_result(msg["id"], payload)


def _combine_day_totals(totals: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine per-vehicle day totals (miles/kWh summed, efficiency recomputed)."""
    miles = sum(float(t.get("miles") or 0.0) for t in totals)
    energy = sum(float(t.get("energy_kwh") or 0.0) for t in totals)
    firsts = [t["first_ts"] for t in totals if t.get("first_ts") is not None]
    lasts = [t["last_ts"] for t in totals if t.get("last_ts") is not None]
    return {
        "drives": sum(int(t.get("drives") or 0) for t in totals),
        "miles": round(miles, 1),
        "hours": round(sum(float(t.get("hours") or 0.0) for t in totals), 2),
        "energy_kwh": round(energy, 2),
        "efficiency_mi_kwh": round(miles / energy, 2) if energy > 0 else None,
        "with_route": sum(int(t.get("with_route") or 0) for t in totals),
        "first_ts": min(firsts) if firsts else None,
        "last_ts": max(lasts) if lasts else None,
    }


@websocket_api.async_response
async def _websocket_analytics_day(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/day`` WebSocket command (one calendar day).

    With ``vins``: ``{date, totals (combined), segments (all vehicles merged,
    time-sorted, each tagged "vin"), vehicles: {vin: {start, end, stops, gaps,
    prior_tail, totals}}}``. Stops, gaps and prior_tail stay per vehicle.
    """
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    stores, multi = resolved

    try:
        day = date.fromisoformat(msg["date"])
    except ValueError:
        connection.send_error(
            msg["id"], "invalid_format", f"Invalid date {msg['date']!r}"
        )
        return

    tz = dt_util.get_default_time_zone()
    include_micro = msg.get("include_micro", False)
    payloads = [await store.async_day(tz, day, include_micro) for store in stores]
    if not multi:
        payload = payloads[0]
        for segment in payload["segments"]:
            segment.pop("sort_ts", None)
        connection.send_result(msg["id"], payload)
        return

    segments: list[dict[str, Any]] = []
    vehicles: dict[str, Any] = {}
    for store, payload in zip(stores, payloads, strict=True):
        for segment in payload["segments"]:
            segment["vin"] = store.vin
            segments.append(segment)
        vehicles[store.vin] = {
            key: payload.get(key)
            for key in ("start", "end", "stops", "gaps", "prior_tail", "totals")
        }
    segments.sort(key=lambda seg: seg.get("sort_ts") or 0.0)
    for segment in segments:
        segment.pop("sort_ts", None)
    connection.send_result(
        msg["id"],
        {
            "date": day.isoformat(),
            "totals": _combine_day_totals([p["totals"] for p in payloads]),
            "segments": segments,
            "vehicles": vehicles,
        },
    )


@websocket_api.async_response
async def _websocket_analytics_heat(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/heat`` WebSocket command (road-heat summary).

    With ``vins`` the result describes the cell-wise merged grid (bbox union,
    summed drive count); the payload shape is unchanged.
    """
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    stores, multi = resolved

    try:
        extra: dict[str, Any] = {"vins": [s.vin for s in stores]} if multi else {}
        payload = await stores[0].async_heat_info(
            msg["period"], msg.get("key"), **extra
        )
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], payload)


@websocket_api.async_response
async def _websocket_analytics_heat_tile(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/heat_tile`` WebSocket command (one XYZ tile)."""
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    stores, multi = resolved

    extra: dict[str, Any] = {"vins": [s.vin for s in stores]} if multi else {}
    try:
        payload = await stores[0].async_heat_tile(
            msg["period"],
            msg.get("key"),
            msg["z"],
            msg["x"],
            msg["y"],
            msg.get("margin", 0),
            **extra,
        )
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], payload)


@callback
def _websocket_analytics_subscribe(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/subscribe`` WebSocket command.

    Non-admin users cannot ``subscribe_events`` to an arbitrary custom event,
    so this re-exposes ``rivian_analytics_updated`` as a filtered
    subscription command instead, the standard HA pattern for subscriptions.
    ``vin`` or ``vins`` selects which vehicles' events are forwarded; each
    event message is ``{"vin": <the vehicle that updated>}``.
    """
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    wanted = {store.vin for store in resolved[0]}

    @callback
    def _forward_if_matching(event: Event) -> None:
        event_vin = event.data.get("vin")
        if event_vin in wanted:
            connection.send_message(
                websocket_api.event_message(msg["id"], {"vin": event_vin})
            )

    unsub = hass.bus.async_listen(RIVIAN_ANALYTICS_UPDATED_EVENT, _forward_if_matching)
    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])


@websocket_api.async_response
async def _websocket_vehicles_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle ``rivian/vehicles/list``: the vehicles in display order.

    Vehicles in config order. Letters A, B, C... follow
    that order; colors come from a persisted, stable palette slot per VIN.
    """
    from .dashboard_generator import _model_str

    registry = er.async_get(hass)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(entry_data, dict):
            continue
        for v_info in (entry_data.get(ATTR_VEHICLE) or {}).values():
            vin = str(v_info.get("vin") or "")
            if not vin or vin in seen:
                continue
            seen.add(vin)
            picture = registry.async_get_entity_id("image", DOMAIN, f"{vin}-picture")
            entries.append(
                {
                    "vin": vin,
                    "name": str(v_info.get("name") or v_info.get("model") or "Rivian"),
                    "model": _model_str(v_info),
                    "picture_entity": picture if isinstance(picture, str) else None,
                }
            )

    vins = [e["vin"] for e in entries]
    meta_store = next((s for s in (_find_store(hass, v) for v in vins) if s), None)
    stored: dict[str, int] = {}
    if meta_store is not None:
        stored = _parse_slots(await meta_store.async_get_meta(VEHICLE_COLORS_META_KEY))
    slots = assign_vehicle_slots(stored, vins)
    if meta_store is not None and slots != stored:
        await meta_store.async_set_meta(VEHICLE_COLORS_META_KEY, json.dumps(slots))

    for index, entry in enumerate(entries):
        light, dark = VEHICLE_PALETTE[slots[entry["vin"]]]
        entry["letter"] = _vehicle_letter(index)
        entry["color"] = light
        entry["color_dark"] = dark
    connection.send_result(msg["id"], entries)


@websocket_api.require_admin
@websocket_api.async_response
async def _websocket_analytics_delete_drive(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/delete_drive`` WebSocket command. Admin only."""
    vin: str = msg["vin"]
    store = _find_store(hass, vin)
    if store is None:
        _not_found(connection, msg["id"], vin)
        return
    result = await store.async_delete_drive(msg["drive_id"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.async_response
async def _websocket_analytics_delete_day(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/delete_day`` WebSocket command. Admin only."""
    vin: str = msg["vin"]
    store = _find_store(hass, vin)
    if store is None:
        _not_found(connection, msg["id"], vin)
        return
    try:
        day = date.fromisoformat(msg["date"])
    except ValueError:
        connection.send_error(
            msg["id"], "invalid_format", f"Invalid date {msg['date']!r}"
        )
        return
    tz = dt_util.get_default_time_zone()
    result = await store.async_delete_day(tz, day)
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.async_response
async def _websocket_analytics_delete_vehicle_history(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/analytics/delete_vehicle_history`` WebSocket command. Admin only."""
    vin: str = msg["vin"]
    store = _find_store(hass, vin)
    if store is None:
        _not_found(connection, msg["id"], vin)
        return
    await store.async_delete_vehicle_history()
    connection.send_result(msg["id"])


def _dataset_stores(hass: HomeAssistant, dataset: str) -> list[DriveStore]:
    """Return every DriveStore whose vehicle belongs to ``dataset``."""
    domain_data = hass.data.get(DOMAIN, {})
    if dataset != DATASET_REAL:
        return []
    stores: list[DriveStore] = []
    for entry_data in domain_data.values():
        if not isinstance(entry_data, dict):
            continue
        stores.extend((entry_data.get(ATTR_DRIVE_STORE) or {}).values())
    return stores


def _dataset_target(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> tuple[DriveStore, str, list[str] | None] | None:
    """Resolve a places/routes command to ``(store, dataset, vins)``.

    Places and routes belong to no vehicle, so ``vin`` is no longer required:
    ``dataset`` ('real' by default) picks the data. ``vin`` is still accepted as an alias that implies its vehicle's
    dataset, and ``vins`` (reads only) names the vehicles whose visits/drives
    to count -- vehicles of the other dataset are ignored. ``store`` is any
    store of that dataset, used to run the (dataset-wide) operation.
    On an unknown vehicle or an empty dataset this sends the error itself and
    returns None.
    """
    vins: list[str] | None = list(msg["vins"]) if "vins" in msg else None
    vin = msg.get("vin")
    store: DriveStore | None = None
    if vin is not None:
        store = _find_store(hass, vin)
        if store is None:
            _not_found(connection, msg["id"], vin)
            return None
        dataset = DATASET_REAL
    elif "dataset" in msg:
        dataset = msg["dataset"]
    elif vins:
        if _find_store(hass, vins[0]) is None:
            _not_found(connection, msg["id"], vins[0])
            return None
        dataset = DATASET_REAL
    else:
        dataset = DATASET_REAL
    if vins is not None:
        resolved, missing = _find_stores(hass, vins)
        if missing is not None:
            _not_found(connection, msg["id"], missing)
            return None
        vins = [s.vin for s in resolved] if dataset == DATASET_REAL else []
    if store is None:
        candidates = _dataset_stores(hass, dataset)
        if not candidates:
            connection.send_error(
                msg["id"], "not_found", f"No vehicles in the {dataset} dataset"
            )
            return None
        store = candidates[0]
    return store, dataset, vins


@websocket_api.async_response
async def _websocket_places_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/places/list`` WebSocket command. Open to all users.

    Places are shared by the household. With ``vins`` the visit counts cover
    only those vehicles and the list is filtered to the places they visit;
    each place carries ``visits_by_vin``. The reply also carries the
    category list (``categories``) the cards use for their pickers.
    """
    target = _dataset_target(hass, connection, msg)
    if target is None:
        return
    store, dataset, vins = target
    places = await store.async_list_places(vins)
    connection.send_result(
        msg["id"],
        {
            "places": places,
            "categories": category_options(),
            "dataset": dataset,
        },
    )


_PLACE_UPDATE_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "category",
    "radius_m",
    "hidden",
    "lat",
    "lon",
)


@websocket_api.require_admin
@websocket_api.async_response
async def _websocket_places_update(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/places/update`` WebSocket command. Admin only."""
    target = _dataset_target(hass, connection, msg)
    if target is None:
        return
    store = target[0]
    fields = {key: msg[key] for key in _PLACE_UPDATE_FIELDS if key in msg}
    try:
        await store.async_update_place(msg["place_id"], **fields)
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.async_response
async def _websocket_places_create(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/places/create`` WebSocket command. Admin only."""
    target = _dataset_target(hass, connection, msg)
    if target is None:
        return
    store = target[0]
    place_id = await store.async_create_place(
        msg["lat"],
        msg["lon"],
        msg["name"],
        msg.get("radius_m"),
        msg.get("category"),
    )
    connection.send_result(msg["id"], {"place_id": place_id})


@websocket_api.require_admin
@websocket_api.async_response
async def _websocket_places_merge(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/places/merge`` WebSocket command. Admin only."""
    target = _dataset_target(hass, connection, msg)
    if target is None:
        return
    store = target[0]
    await store.async_merge_places(msg["into"], msg["place_ids"])
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.async_response
async def _websocket_places_rebuild(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/places/rebuild`` WebSocket command. Admin only."""
    target = _dataset_target(hass, connection, msg)
    if target is None:
        return
    store = target[0]
    result = await store.async_rebuild_places()
    connection.send_result(msg["id"], result)
    store._fire_dataset_updated()


@websocket_api.require_admin
@websocket_api.async_response
async def _websocket_places_delete(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/places/delete`` WebSocket command. Admin only.

    Deletes a ``user`` place outright, hides an ``auto`` suggestion (so it
    won't be suggested again), or errors for a ``zone`` place -- those are
    managed in Home Assistant zones.
    """
    target = _dataset_target(hass, connection, msg)
    if target is None:
        return
    store = target[0]
    try:
        result = await store.async_delete_place(msg["place_id"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.async_response
async def _websocket_routes_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/routes/list`` WebSocket command. Open to all users.

    Routes are shared; with ``vins`` only routes those vehicles drove come
    back, ordered by *their* drive count (each car's favorites are its
    most-driven routes).
    """
    target = _dataset_target(hass, connection, msg)
    if target is None:
        return
    store, dataset, vins = target
    routes = await store.async_list_routes(vins)
    connection.send_result(msg["id"], {"routes": routes, "dataset": dataset})


@websocket_api.async_response
async def _websocket_routes_route(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/routes/route`` WebSocket command. Open to all users."""
    target = _dataset_target(hass, connection, msg)
    if target is None:
        return
    store, _dataset, vins = target
    route = await store.async_route_detail(msg["route_id"], vins)
    if route is None:
        connection.send_error(msg["id"], "not_found", f"No route {msg['route_id']}")
        return
    connection.send_result(msg["id"], {"route": route})


@websocket_api.require_admin
@websocket_api.async_response
async def _websocket_routes_rename(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/routes/rename`` WebSocket command. Admin only."""
    target = _dataset_target(hass, connection, msg)
    if target is None:
        return
    store = target[0]
    try:
        await store.async_rename_route(msg["route_id"], msg.get("name"))
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.async_response
async def _websocket_charging_delete_session(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle the ``rivian/charging/delete_session`` WebSocket command. Admin only."""
    vin: str = msg["vin"]
    store = _find_store(hass, vin)
    if store is None:
        _not_found(connection, msg["id"], vin)
        return
    removed = await store.async_delete_dcfc_session(msg["session_id"])
    connection.send_result(msg["id"], {"removed": removed})


def _vehicle_info(entry_data: dict[str, Any], vin: str) -> dict[str, Any] | None:
    """Return the account's info for ``vin`` from one entry's vehicles.

    ``ATTR_VEHICLE`` is keyed by Rivian's vehicle id, not the VIN.
    """
    for info in (entry_data.get(ATTR_VEHICLE) or {}).values():
        if isinstance(info, dict) and info.get("vin") == vin:
            return info
    return None


def _vehicle_model_and_capacity(
    hass: HomeAssistant, store: DriveStore
) -> tuple[str | None, float | None]:
    """Return ``(model, capacity_kwh)`` for a vehicle, as far as they are known.

    The model and capacity come from the discovered vehicle info; the capacity is the newest drive's reported battery capacity when
    there is one (it tracks degradation), else the vehicle info's.
    """
    model: str | None = None
    capacity: Any = None
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(entry_data, dict):
            continue
        info = _vehicle_info(entry_data, store.vin)
        if info is not None:
            model = info.get("model") or model
            capacity = info.get("battery_capacity") or capacity
    last = store.last_drive
    if last is not None and last.battery_capacity_kwh:
        capacity = last.battery_capacity_kwh
    try:
        capacity = float(capacity) if capacity else None
    except (TypeError, ValueError):
        capacity = None
    return (str(model) if model else None), capacity


def _vehicle_model_year(hass: HomeAssistant, store: DriveStore) -> int | None:
    """Return the vehicle's model year when the account data has one."""
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(entry_data, dict):
            continue
        info = _vehicle_info(entry_data, store.vin)
        if info is not None:
            year = info.get("model_year") or info.get("modelYear")
            try:
                return int(year) if year else None
            except (TypeError, ValueError):
                return None
    return None


def _vehicle_reference(
    hass: HomeAssistant, store: DriveStore
) -> tuple[dict[str, Any], float]:
    """Return ``(reference curve entry, capacity_kwh)`` for a vehicle."""
    model, capacity = _vehicle_model_and_capacity(hass, store)
    ref = charge_curves.reference(
        charge_curves.pack_for(model, capacity, _vehicle_model_year(hass, store))
    )
    return ref, capacity or float(ref["capacity_kwh"])


CHARGE_TYPE_LABELS: Final[dict[str, str]] = {
    "dc": "DC Fast",
    "ac_l2": "AC L2",
    "ac_l1": "AC L1",
    # An inferred slow charge whose rate is unknown (Home Assistant got no
    # updates during it): not guessed as L1 or L2.
    "ac": "AC",
}


def _session_payload(
    session: dict[str, Any], vin: str, ref: dict[str, Any], capacity: float
) -> dict[str, Any]:
    """Shape one stored session for ``rivian/charging/sessions``."""
    start_ts, end_ts = session.get("start_ts"), session.get("end_ts")
    duration_s = (
        round(end_ts - start_ts)
        if start_ts is not None and end_ts is not None
        else None
    )
    is_dc = session.get("kind") == "dc"
    peak_kw = session["max_power_kw"]
    if not is_dc:
        # AC sessions store only a coarse SoC trace: its fastest stretch is
        # the peak (backfilled ones have no trace, so peak = average).
        estimated = battery_analytics.peak_from_soc_samples(
            session.get("samples") or [], capacity
        )
        if estimated is not None and estimated > (peak_kw or 0.0):
            peak_kw = estimated
    temps = [
        s["battery_temp_f"]
        for s in session.get("samples") or []
        if s.get("battery_temp_f") is not None
    ]
    battery_temp = session.get("battery_temp_f")
    if battery_temp is None and temps:
        battery_temp = round(sum(temps) / len(temps), 1)
    kind_type = battery_analytics.charge_type(
        session.get("kind"), session.get("avg_power_kw"), AC_L1_MAX_KW
    )
    brand, label = charger_lookup.brand_for(
        session.get("vendor") or None,
        session.get("network") or None,
        session.get("station_name") or None,
        session.get("is_home"),
    )
    inferred = session.get("source") == "inferred"
    raw_place = session.get("place")
    place = (
        {k: raw_place.get(k) for k in ("id", "label", "category")}
        if raw_place
        else None
    )
    is_home_charge = bool(
        session.get("is_home") is True
        or brand == "home"
        or (raw_place or {}).get("category") == "home"
        or (raw_place or {}).get("zone_entity_id") == "zone.home"
    )
    if (
        session.get("source") == "inferred"
        and kind_type != "dc"
        and not session.get("avg_power_kw")
    ):
        kind_type = "ac"
    payload: dict[str, Any] = {
        "vin": vin,
        "session_id": session["session_id"],
        "kind": session.get("kind"),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "start_soc": session["start_soc"],
        "end_soc": session["end_soc"],
        "energy_added_kwh": session["energy_added_kwh"],
        "max_power_kw": peak_kw,
        "avg_power_kw": session["avg_power_kw"],
        "charge_type": kind_type,
        "charge_type_label": CHARGE_TYPE_LABELS[kind_type],
        "outside_temp_f": session.get("outside_temp_f"),
        "battery_temp_f": battery_temp,
        "duration_s": duration_s,
        "place": place,
        "inferred": inferred,
        "is_home_charge": is_home_charge,
        "lat": session.get("lat"),
        "lon": session.get("lon"),
        "source": session.get("source"),
        "vendor": session.get("vendor") or None,
        "network": session.get("network") or None,
        "station_name": session.get("station_name") or None,
        "station_version": session.get("station_version"),
        "charger_max_kw": session.get("charger_max_kw"),
        "is_home": session.get("is_home"),
        "brand": brand,
        "brand_label": label,
        "samples": (
            _decimate(session.get("samples", []), MAX_DCFC_SAMPLES) if is_dc else []
        ),
        "expected": None,
    }
    if inferred:
        # Derived from the battery level alone: no measured power or energy
        # unless the span could estimate it.
        for key in ("max_power_kw", "avg_power_kw", "energy_added_kwh"):
            if not payload[key]:
                payload[key] = None
    if is_dc:
        minutes = charge_curves.expected_minutes(
            ref, session["start_soc"], session["end_soc"], capacity
        )
        avg_kw = charge_curves.expected_avg_kw(
            ref, session["start_soc"], session["end_soc"], capacity
        )
        if minutes is not None and avg_kw is not None:
            pct = (
                round(minutes / (duration_s / 60.0) * 100.0, 1) if duration_s else None
            )
            payload["expected"] = {
                "pack": ref["pack"],
                "approximate": ref["approximate"],
                "minutes": round(minutes, 1),
                "avg_kw": round(avg_kw, 1),
                "pct_of_expected": pct,
            }
    return payload


@websocket_api.async_response
async def _websocket_charging_sessions(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle ``rivian/charging/sessions``. Open to all users.

    Every stored charging session (DC fast and AC/home) of the requested
    vehicles, oldest first, plus per-vehicle counts. DC sessions carry their
    (downsampled) curve and ``expected`` -- what the vehicle's reference pack
    needs for the same SoC range (``pct_of_expected`` is expected minutes over
    actual minutes, so 100 means as fast as the reference).
    """
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    stores, _multi = resolved
    days = msg.get("days")
    since_ts = dt_util.utcnow().timestamp() - days * SECONDS_PER_DAY if days else None
    if msg.get("start") is not None:
        since_ts = max(since_ts, msg["start"]) if since_ts is not None else msg["start"]
    until_ts = msg.get("end")
    brands = set(msg.get("brands") or [])
    sessions: list[dict[str, Any]] = []
    counts: dict[str, dict[str, int]] = {}
    for store in stores:
        rows = await store.async_list_charging_sessions(since_ts, until_ts)
        ref, capacity = _vehicle_reference(hass, store)
        payloads = [_session_payload(r, store.vin, ref, capacity) for r in rows]
        if brands:
            keep = [i for i, p in enumerate(payloads) if p["brand"] in brands]
            rows = [rows[i] for i in keep]
            payloads = [payloads[i] for i in keep]
        sessions.extend(payloads)
        counts[store.vin] = battery_analytics.session_counts(rows)
    sessions.sort(key=lambda s: (s["start_ts"] is None, s["start_ts"] or 0.0))
    connection.send_result(msg["id"], {"sessions": sessions, "counts_by_vin": counts})


@websocket_api.async_response
async def _websocket_charging_reference(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle ``rivian/charging/reference``: each vehicle's expected DC curve."""
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    stores, _multi = resolved
    result: dict[str, Any] = {}
    for store in stores:
        ref, capacity = _vehicle_reference(hass, store)
        result[store.vin] = {
            "pack": ref["pack"],
            "label": ref["name"],
            "approximate": ref["approximate"],
            "capacity_kwh": round(capacity, 1),
            "curve": {"soc": list(ref["x"]), "kw": list(ref["y"])},
        }
    connection.send_result(msg["id"], result)


async def _soc_timeline_for_store(
    hass: HomeAssistant, store: DriveStore, start_ts: float, end_ts: float
) -> tuple[list[battery_analytics.Point], str]:
    """Return ``(points, source)`` for one vehicle's battery-% timeline.

    A real vehicle uses its battery-level sensor's recorder statistics (5-minute
    for a window of 10 days or less, hourly beyond). Where 5-minute statistics
    have already been purged (older than ~10 days), hourly statistics fill the
    uncovered start of the window. A vehicle with no statistics at
    all is synthesized from its drives and charging sessions.
    """
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{store.vin}-battery_level"
    )
    if entity_id:
        fine = end_ts - start_ts <= SOC_TIMELINE_FINE_MAX_DAYS * SECONDS_PER_DAY
        points = await async_soc_points(
            hass,
            entity_id,
            start_ts,
            end_ts,
            fine=fine,
            reader=async_entity_statistics,
        )
        if points:
            return points, "statistics"
    events = await store.async_soc_events(start_ts, end_ts)
    # An open-ended request ("All", start 0) would otherwise hold the first
    # level flat back to 1970: start the synthesized series at the vehicle's
    # first recorded drive or session instead.
    first = min(
        (
            e["start_ts"]
            for kind in ("drives", "sessions")
            for e in events.get(kind, [])
        ),
        default=None,
    )
    if first is not None and first > start_ts:
        start_ts = first
    return (
        battery_analytics.synthesize_timeline(events, start_ts, end_ts),
        "synthesized",
    )


@websocket_api.async_response
async def _websocket_battery_soc_timeline(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle ``rivian/battery/soc_timeline``: battery % over time per vehicle.

    ``time_in_band`` is the fraction of covered time per SoC band (below 10,
    10-20, 20-80, 80-90, above 90 %), or None when nothing is covered.
    """
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    stores, _multi = resolved
    start_ts, end_ts = battery_analytics.window_bounds(
        msg.get("start"), msg.get("end"), dt_util.utcnow().timestamp()
    )
    series: dict[str, Any] = {}
    bands: dict[str, Any] = {}
    detected: dict[str, list[dict[str, Any]]] = {}
    for store in stores:
        points, source = await _soc_timeline_for_store(hass, store, start_ts, end_ts)
        detected[store.vin] = []
        if source == "statistics" and points:
            # Charges the battery level shows but no stored session covers.
            recorded = await store.async_charging_session_intervals()
            _ref, capacity = _vehicle_reference(hass, store)
            # A fast charge needs a drive just before it (see detect_charge_spans).
            drive_ends = await store.async_drive_end_times(
                start_ts - battery_analytics.DETECT_DC_DRIVE_GAP_S, end_ts
            )
            detected[store.vin] = battery_analytics.detect_charge_spans(
                points, recorded, capacity, drive_ends=drive_ends
            )
        bands[store.vin] = battery_analytics.time_in_band(
            points,
            battery_analytics.STATISTICS_MAX_GAP_S if source == "statistics" else None,
        )
        series[store.vin] = {
            "points": [
                [int(ts), round(soc, 1)]
                for ts, soc in battery_analytics.downsample(
                    points, battery_analytics.TIMELINE_MAX_POINTS
                )
            ],
            "source": source,
        }
    connection.send_result(
        msg["id"], {"series": series, "time_in_band": bands, "detected": detected}
    )


@websocket_api.async_response
async def _websocket_battery_capacity(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle ``rivian/battery/capacity``: battery health per vehicle.

    Reads the stored ``capacity_history`` (one row per local day, kept forever;
    seeded from the capacity sensor's long-term statistics and the drives, and
    refreshed daily) plus today's live sensor value, and returns
    ``points: [[day_ts, kwh, temp_f|null, temp_source 'battery'|'outside'|null]]``,
    ``pct_points`` (% of the original), ``original_kwh`` (the first day's, else
    the pack's nominal), ``projected_range`` (median of
    ``end_range_mi / end_soc * 100`` per day), ``pack`` and ``approximate``.
    """
    resolved = _resolve_stores(hass, connection, msg)
    if resolved is None:
        return
    stores, _multi = resolved
    tz = dt_util.get_default_time_zone()
    now_ts = dt_util.utcnow().timestamp()
    registry = er.async_get(hass)
    result: dict[str, Any] = {}
    for store in stores:
        rows = await store.async_capacity_rows()
        history = await store.async_capacity_history()
        live: tuple[float, float] | None = None
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{store.vin}-battery_capacity"
        )
        state = hass.states.get(entity_id) if entity_id else None
        try:
            value = float(state.state) if state is not None else 0.0
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            live = (now_ts, value)
        model, capacity = _vehicle_model_and_capacity(hass, store)
        pack = charge_curves.pack_for(model, capacity, _vehicle_model_year(hass, store))
        ref = charge_curves.reference(pack)
        series = battery_analytics.capacity_history_series(
            history, rows, tz, float(capacity or ref["capacity_kwh"]), live
        )
        series["pack"] = pack
        series["approximate"] = ref["approximate"]
        result[store.vin] = series
    connection.send_result(msg["id"], result)


@callback
def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register the Rivian analytics WebSocket API commands.

    Safe to call once per config entry; registering the command more than
    once is a no-op.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(WS_API_REGISTERED_KEY):
        return

    websocket_api.async_register_command(
        hass,
        WS_TYPE_VEHICLES_LIST,
        _websocket_vehicles_list,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {vol.Required("type"): WS_TYPE_VEHICLES_LIST}
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_SERIES,
        _websocket_analytics_series,
        _multi_vin_schema(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_SERIES,
                vol.Required("series"): [str],
                vol.Optional("days", default=90): int,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_DRIVES,
        _websocket_analytics_drives,
        _multi_vin_schema(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_DRIVES,
                vol.Optional("before_ts"): vol.Coerce(float),
                vol.Optional("limit", default=50): vol.All(
                    vol.Coerce(int), vol.Range(1, 200)
                ),
                vol.Optional("include_micro", default=False): bool,
                vol.Optional("previews", default=False): bool,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_DRIVE,
        _websocket_analytics_drive,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_DRIVE,
                vol.Required("vin"): str,
                vol.Required("drive_id"): str,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_SUMMARY,
        _websocket_analytics_summary,
        _multi_vin_schema(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_SUMMARY,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_CALENDAR,
        _websocket_analytics_calendar,
        _multi_vin_schema(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_CALENDAR,
                vol.Optional("year"): int,
                vol.Optional("month"): vol.All(vol.Coerce(int), vol.Range(1, 12)),
                vol.Optional("include_micro", default=False): bool,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_DAY,
        _websocket_analytics_day,
        _multi_vin_schema(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_DAY,
                vol.Required("date"): str,
                vol.Optional("include_micro", default=False): bool,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_HEAT,
        _websocket_analytics_heat,
        _multi_vin_schema(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_HEAT,
                vol.Required("period"): vol.In(VALID_HEAT_PERIODS),
                vol.Optional("key"): vol.Any(str, None),
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_HEAT_TILE,
        _websocket_analytics_heat_tile,
        _multi_vin_schema(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_HEAT_TILE,
                vol.Required("period"): vol.In(VALID_HEAT_PERIODS),
                vol.Optional("key"): vol.Any(str, None),
                vol.Required("z"): vol.All(vol.Coerce(int), vol.Range(0, 22)),
                vol.Required("x"): vol.All(vol.Coerce(int), vol.Range(min=0)),
                vol.Required("y"): vol.All(vol.Coerce(int), vol.Range(min=0)),
                # Extra display cells around the tile, so drawn lines can join
                # up across tile edges.
                vol.Optional("margin", default=0): vol.All(
                    vol.Coerce(int), vol.Range(0, 2)
                ),
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_SUBSCRIBE,
        _websocket_analytics_subscribe,
        _multi_vin_schema(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_SUBSCRIBE,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_DELETE_DRIVE,
        _websocket_analytics_delete_drive,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_DELETE_DRIVE,
                vol.Required("vin"): str,
                vol.Required("drive_id"): str,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_DELETE_DAY,
        _websocket_analytics_delete_day,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_DELETE_DAY,
                vol.Required("vin"): str,
                vol.Required("date"): str,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ANALYTICS_DELETE_VEHICLE_HISTORY,
        _websocket_analytics_delete_vehicle_history,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_DELETE_VEHICLE_HISTORY,
                vol.Required("vin"): str,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_PLACES_LIST,
        _websocket_places_list,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_PLACES_LIST,
                **_DATASET_FIELDS,
                vol.Optional("vins"): vol.All([str], vol.Length(min=1)),
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_PLACES_UPDATE,
        _websocket_places_update,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_PLACES_UPDATE,
                **_DATASET_FIELDS,
                vol.Required("place_id"): vol.Coerce(int),
                vol.Optional("name"): vol.All(str, vol.Length(max=PLACE_NAME_MAX_LEN)),
                vol.Optional("category"): vol.Any(vol.In(PLACE_CATEGORIES), None),
                vol.Optional("radius_m"): vol.All(
                    vol.Coerce(float), vol.Range(PLACE_RADIUS_MIN, PLACE_RADIUS_MAX)
                ),
                vol.Optional("hidden"): bool,
                vol.Optional("lat"): vol.All(vol.Coerce(float), vol.Range(-90, 90)),
                vol.Optional("lon"): vol.All(vol.Coerce(float), vol.Range(-180, 180)),
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_PLACES_CREATE,
        _websocket_places_create,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_PLACES_CREATE,
                **_DATASET_FIELDS,
                vol.Required("lat"): vol.All(vol.Coerce(float), vol.Range(-90, 90)),
                vol.Required("lon"): vol.All(vol.Coerce(float), vol.Range(-180, 180)),
                vol.Required("name"): vol.All(str, vol.Length(max=PLACE_NAME_MAX_LEN)),
                vol.Optional("radius_m"): vol.All(
                    vol.Coerce(float), vol.Range(PLACE_RADIUS_MIN, PLACE_RADIUS_MAX)
                ),
                vol.Optional("category"): vol.Any(vol.In(PLACE_CATEGORIES), None),
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_PLACES_MERGE,
        _websocket_places_merge,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_PLACES_MERGE,
                **_DATASET_FIELDS,
                vol.Required("into"): vol.Coerce(int),
                vol.Required("place_ids"): [vol.Coerce(int)],
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_PLACES_REBUILD,
        _websocket_places_rebuild,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_PLACES_REBUILD,
                **_DATASET_FIELDS,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_PLACES_DELETE,
        _websocket_places_delete,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_PLACES_DELETE,
                **_DATASET_FIELDS,
                vol.Required("place_id"): vol.Coerce(int),
            }
        ),
    )

    websocket_api.async_register_command(
        hass,
        WS_TYPE_ROUTES_LIST,
        _websocket_routes_list,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_ROUTES_LIST,
                **_DATASET_FIELDS,
                vol.Optional("vins"): vol.All([str], vol.Length(min=1)),
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ROUTES_ROUTE,
        _websocket_routes_route,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_ROUTES_ROUTE,
                **_DATASET_FIELDS,
                vol.Optional("vins"): vol.All([str], vol.Length(min=1)),
                vol.Required("route_id"): vol.Coerce(int),
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_ROUTES_RENAME,
        _websocket_routes_rename,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_ROUTES_RENAME,
                **_DATASET_FIELDS,
                vol.Required("route_id"): vol.Coerce(int),
                vol.Optional("name"): vol.Any(
                    vol.All(str, vol.Length(max=ROUTE_NAME_MAX_LEN)), None
                ),
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_CHARGING_DELETE_SESSION,
        _websocket_charging_delete_session,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_TYPE_CHARGING_DELETE_SESSION,
                vol.Required("vin"): str,
                vol.Required("session_id"): str,
            }
        ),
    )

    websocket_api.async_register_command(
        hass,
        WS_TYPE_CHARGING_SESSIONS,
        _websocket_charging_sessions,
        _multi_vin_schema(
            {
                vol.Required("type"): WS_TYPE_CHARGING_SESSIONS,
                vol.Optional("days"): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional("start"): vol.Coerce(float),
                vol.Optional("end"): vol.Coerce(float),
                vol.Optional("brands"): [str],
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_CHARGING_REFERENCE,
        _websocket_charging_reference,
        _multi_vin_schema({vol.Required("type"): WS_TYPE_CHARGING_REFERENCE}),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_BATTERY_SOC_TIMELINE,
        _websocket_battery_soc_timeline,
        _multi_vin_schema(
            {
                vol.Required("type"): WS_TYPE_BATTERY_SOC_TIMELINE,
                vol.Optional("start"): vol.Coerce(float),
                vol.Optional("end"): vol.Coerce(float),
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_TYPE_BATTERY_CAPACITY,
        _websocket_battery_capacity,
        _multi_vin_schema({vol.Required("type"): WS_TYPE_BATTERY_CAPACITY}),
    )

    domain_data[WS_API_REGISTERED_KEY] = True
