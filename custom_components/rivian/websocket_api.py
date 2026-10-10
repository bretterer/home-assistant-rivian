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
30d, 365d, all-time) plus the most recent drive, for per-vehicle
overview cards.

None of this bulk data is ever attached to entity state attributes; the
recorder's 16 KiB attribute limit and the state DB are irrelevant here.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Any, Final

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import Event, HomeAssistant, callback

from .const import ATTR_DRIVE_STORE, DOMAIN, RIVIAN_ANALYTICS_UPDATED_EVENT
from .drive_models import (
    MPGE_FACTOR,
    AggregatedDriveStats,
    DriveChunk,
    DriveRecord,
    VampireDrainRecord,
)
from .drive_storage import DriveStore

_LOGGER = logging.getLogger(__name__)

WS_TYPE_ANALYTICS_SERIES: Final[str] = "rivian/analytics/series"
WS_TYPE_ANALYTICS_DRIVES: Final[str] = "rivian/analytics/drives"
WS_TYPE_ANALYTICS_DRIVE: Final[str] = "rivian/analytics/drive"
WS_TYPE_ANALYTICS_SUMMARY: Final[str] = "rivian/analytics/summary"
WS_TYPE_ANALYTICS_SUBSCRIBE: Final[str] = "rivian/analytics/subscribe"
SUMMARY_WINDOWS: Final[tuple[tuple[str, int | None], ...]] = (
    ("7d", 7),
    ("30d", 30),
    ("365d", 365),
    ("all", None),
)
VALID_SERIES_KEYS: Final[tuple[str, ...]] = (
    "drives",
    "chunks",
    # "segments" is a legacy alias for "chunks", from before the
    # segment->chunk rename; clients still requesting it keep working.
    "segments",
    "vampire",
    "dcfc",
    "speed_bins",
)
MAX_CHUNKS: Final[int] = 600

MAX_DCFC_SAMPLES: Final[int] = 60
SERIES_CACHE_MAX_DAYS: Final[int] = 90

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


# The drive and chunk shapes below are the chart-ready contract that frontends
# read (e.g. d.distance, d.efficiency, d.temp_f).
def _drive_chart_dict(drive: DriveRecord) -> dict[str, Any]:
    """Shape a drive the way chart expressions read it."""
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
        WS_TYPE_ANALYTICS_SUBSCRIBE,
        _websocket_analytics_subscribe,
        _multi_vin_schema(
            {
                vol.Required("type"): WS_TYPE_ANALYTICS_SUBSCRIBE,
            }
        ),
    )

    domain_data[WS_API_REGISTERED_KEY] = True
