"""Import the Rivian app's completed charging sessions, and keep the capacity history.

Home (AC) charges are often missing from the integration's own records (it
only sees what happened while it was running), and it never learns who ran a
fast charger. Rivian's charging GraphQL API
(``getCompletedSessionSummaries``) lists every completed session of the
account's vehicles, so each summary is matched by time to a stored session
(enriching it with the vendor, home/public flag and the real energy) or, when
none matches, inserted as a session with ``source='rivian'``.

The query is called through the client's private ``_Rivian__graphql_query``
like ``vehicle_picture.py`` does, and requests ONLY the fields listed in
``SUMMARY_FIELDS`` -- never payment, price or account details. The response
schema is undocumented, so everything is parsed defensively; a GraphQL error
is logged once as a warning and the import stops (it is retried on the next
daily run). Demo vehicles are never imported.

This module also holds the daily job that merges the battery-capacity
sensor's long-term statistics and drives into ``capacity_history`` (kept
forever), next to the Rivian import because both run on the same nightly
timer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
import logging
import time
from typing import Any, Final

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import charger_lookup
from .const import DOMAIN
from .drive_models import ChargingSessionRecord
from .statistics import async_entity_statistics
from .vehicle_picture import _graphql

_LOGGER = logging.getLogger(__name__)

CHARGING_URL: Final[str] = "https://rivian.com/api/gql/chrg/user/graphql"
# Only these are requested. Deliberately no cost, currency, payment or
# account fields.
SUMMARY_FIELDS: Final[tuple[str, ...]] = (
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
)
SUMMARIES_QUERY: Final[str] = (
    "query getCompletedSessionSummaries { getCompletedSessionSummaries { "
    + " ".join(SUMMARY_FIELDS)
    + " __typename } }"
)
MATCH_TOLERANCE_S: Final[float] = 600.0
# A session averaging above this is a fast charge when chargerType says nothing.
DC_MIN_AVG_KW: Final[float] = 30.0
HISTORY_META_KEY: Final[str] = "charging_history_ts"
HISTORY_MIN_INTERVAL_S: Final[float] = 20 * 3600.0


@dataclass
class Summary:
    """One completed session from the Rivian app (parsed, SI units)."""

    vehicle_id: str | None
    start_ts: float
    end_ts: float
    energy_kwh: float | None
    vendor: str | None
    charger_type: str | None
    is_home: bool | None
    is_public: bool | None
    city: str | None
    txn_id: str | None


def _epoch(value: Any) -> float | None:
    """Parse an ISO-8601 instant (or epoch number) to epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Milliseconds when implausibly large.
        return float(value) / 1000.0 if value > 1e11 else float(value)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed.timestamp()


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def parse_summaries(
    payload: Any,
) -> tuple[list[Summary], list[str], str | None]:
    """Parse a ``getCompletedSessionSummaries`` response.

    Returns ``(summaries, fields_present, error)``: ``fields_present`` is the
    sorted set of keys that were non-null in at least one entry (logged by
    the probe -- names only, never values); ``error`` is the first GraphQL
    error message, or None. Entries without a usable start/end are skipped.
    """
    if not isinstance(payload, dict):
        return [], [], "unexpected response"
    errors = payload.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        message = first.get("message") if isinstance(first, dict) else str(first)
        return [], [], str(message or "GraphQL error")
    raw = (payload.get("data") or {}).get("getCompletedSessionSummaries")
    if not isinstance(raw, list):
        return [], [], "no getCompletedSessionSummaries in the response"
    present: set[str] = set()
    out: list[Summary] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        present.update(
            k for k, v in item.items() if v is not None and k != "__typename"
        )
        start = _epoch(item.get("startInstant"))
        end = _epoch(item.get("endInstant"))
        if start is None:
            continue
        if end is None or end < start:
            end = start
        energy = _number(item.get("totalEnergyKwh"))
        out.append(
            Summary(
                vehicle_id=str(item["vehicleId"]) if item.get("vehicleId") else None,
                start_ts=start,
                end_ts=end,
                energy_kwh=energy if energy and energy > 0 else None,
                vendor=str(item["vendor"]) if item.get("vendor") else None,
                charger_type=str(item["chargerType"])
                if item.get("chargerType")
                else None,
                is_home=_flag(item.get("isHomeCharger")),
                is_public=_flag(item.get("isPublic")),
                city=str(item["city"]) if item.get("city") else None,
                txn_id=str(item["transactionId"])
                if item.get("transactionId")
                else None,
            )
        )
    return out, sorted(present), None


def summary_kind(summary: Summary) -> str:
    """``'dc'`` or ``'ac'`` for a summary: chargerType first, then home flag, then power."""
    ctype = (summary.charger_type or "").upper()
    if "DC" in ctype or "FAST" in ctype:
        return "dc"
    if "AC" in ctype or "LEVEL" in ctype or summary.is_home:
        return "ac"
    hours = (summary.end_ts - summary.start_ts) / 3600.0
    if summary.energy_kwh and hours > 0 and summary.energy_kwh / hours >= DC_MIN_AVG_KW:
        return "dc"
    return "ac"


def match_stored(
    summary: Summary, stored: list[dict[str, Any]], claimed: set[str]
) -> dict[str, Any] | None:
    """Return the stored session overlapping ``summary`` (±10 minutes) the most.

    A stored session already claimed by another summary is skipped; ties go to
    the nearest start.
    """
    best: tuple[float, float, dict[str, Any]] | None = None
    lo, hi = summary.start_ts - MATCH_TOLERANCE_S, summary.end_ts + MATCH_TOLERANCE_S
    for row in stored:
        if row["session_id"] in claimed:
            continue
        s_start = row["start_ts"]
        s_end = row["end_ts"] if row["end_ts"] is not None else s_start
        overlap = min(hi, s_end) - max(lo, s_start)
        if overlap < 0:
            continue
        key = (overlap, -abs(s_start - summary.start_ts))
        if best is None or key > (best[0], best[1]):
            best = (key[0], key[1], row)
    return None if best is None else best[2]


def _enrichment(summary: Summary, row: dict[str, Any] | None) -> dict[str, Any]:
    """Fields to set on a stored session from a Rivian summary (never blanking any)."""
    fields: dict[str, Any] = {}
    if summary.vendor:
        fields["vendor"] = summary.vendor
        if not summary.is_home:
            key = charger_lookup.normalize_brand(summary.vendor)
            fields["network"] = (
                charger_lookup.brand_label(key)
                if key != charger_lookup.BRAND_OTHER
                else summary.vendor
            )
            version = charger_lookup.station_version(key, None)
            if version:
                fields["station_version"] = version
    if summary.is_home is not None:
        fields["is_home"] = int(summary.is_home)
    elif summary.is_public is not None:
        fields["is_home"] = int(not summary.is_public)
    if summary.energy_kwh:
        fields["energy_added_kwh"] = round(summary.energy_kwh, 2)
    if summary.txn_id:
        fields["rivian_txn_id"] = summary.txn_id
    return fields


def merge_summaries(db: Any, vin: str, summaries: list[Summary]) -> dict[str, int]:
    """Match a VIN's summaries to its stored sessions (executor only).

    Returns ``{"matched", "inserted", "skipped"}``. ``skipped`` counts
    summaries that matched nothing and could not be inserted because the SoC
    the car had at that time is unknown (the table requires one).
    """
    stored = db.sessions_for_history_match(vin)
    known_txn = {r["rivian_txn_id"] for r in stored if r.get("rivian_txn_id")}
    claimed: set[str] = set()
    counts = {"matched": 0, "inserted": 0, "skipped": 0}
    inserts: list[ChargingSessionRecord] = []
    for summary in sorted(summaries, key=lambda s: s.start_ts):
        if summary.txn_id and summary.txn_id in known_txn:
            counts["matched"] += 1
            continue
        row = match_stored(summary, stored, claimed)
        if row is not None:
            claimed.add(row["session_id"])
            fields = _enrichment(summary, row)
            db.update_session_fields(vin, row["session_id"], fields)
            if summary.txn_id:
                known_txn.add(summary.txn_id)
            counts["matched"] += 1
            continue
        record = _new_session(db, vin, summary)
        if record is None:
            counts["skipped"] += 1
            continue
        inserts.append(record)
        claimed.add(record.session_id)
        stored.append(
            {
                "session_id": record.session_id,
                "start_ts": summary.start_ts,
                "end_ts": summary.end_ts,
            }
        )
        if summary.txn_id:
            known_txn.add(summary.txn_id)
        counts["inserted"] += 1
    if inserts:
        db.upsert_dcfc_sessions(vin, inserts)
    return counts


def _new_session(db: Any, vin: str, summary: Summary) -> ChargingSessionRecord | None:
    """Build a session record for a summary no stored session matches, or None."""
    level = db.level_before(vin, summary.start_ts)
    energy = summary.energy_kwh
    if level is None or not energy:
        return None
    start_soc, capacity = level
    if not capacity or capacity <= 0:
        return None
    end_soc = min(100.0, start_soc + energy / capacity * 100.0)
    hours = max((summary.end_ts - summary.start_ts) / 3600.0, 1 / 60)
    avg_kw = energy / hours
    fields = _enrichment(summary, None)
    return ChargingSessionRecord(
        session_id=f"rivian-{summary.txn_id or int(summary.start_ts)}",
        start_time=datetime.fromtimestamp(summary.start_ts, tz=dt_util.UTC).isoformat(),
        end_time=datetime.fromtimestamp(summary.end_ts, tz=dt_util.UTC).isoformat(),
        start_soc=round(start_soc, 1),
        end_soc=round(end_soc, 1),
        energy_added_kwh=round(energy, 2),
        max_power_kw=round(avg_kw, 1),
        avg_power_kw=round(avg_kw, 1),
        kind=summary_kind(summary),
        source="rivian",
        vendor=fields.get("vendor"),
        network=fields.get("network"),
        station_version=fields.get("station_version"),
        is_home=(bool(fields["is_home"]) if "is_home" in fields else None),
        rivian_txn_id=summary.txn_id,
    )


async def async_import_rivian_history(
    hass: Any,
    client: Any,
    vins_by_vehicle_id: dict[str, str],
    db: Any,
) -> dict[str, Any]:
    """Fetch the account's completed sessions and merge them into ``db``.

    ``vins_by_vehicle_id`` maps Rivian vehicle ids to VINs (real vehicles
    only). Never raises: returns ``{"error": message}`` after one warning when
    the query fails, else per-VIN merge counts under ``"vehicles"``.
    """
    if not vins_by_vehicle_id:
        return {"vehicles": {}}
    try:
        payload = await _graphql(
            client,
            CHARGING_URL,
            {
                "operationName": "getCompletedSessionSummaries",
                "query": SUMMARIES_QUERY,
                "variables": {},
            },
        )
    except Exception as err:  # noqa: BLE001 - history is never worth failing setup
        _LOGGER.warning("Rivian charging history is unavailable: %s", err)
        return {"error": str(err)}
    summaries, fields, error = parse_summaries(payload)
    if error is not None:
        _LOGGER.warning("Rivian charging history is unavailable: %s", error)
        return {"error": error}
    by_vin: dict[str, list[Summary]] = {}
    for summary in summaries:
        vin = vins_by_vehicle_id.get(summary.vehicle_id or "")
        if vin is None and len(vins_by_vehicle_id) == 1 and summary.vehicle_id is None:
            vin = next(iter(vins_by_vehicle_id.values()))
        if vin is not None:
            by_vin.setdefault(vin, []).append(summary)
    _LOGGER.info(
        "Rivian charging history: %d summaries, %d vehicles, fields present: %s",
        len(summaries),
        len(by_vin),
        fields,
    )
    result: dict[str, Any] = {"vehicles": {}}
    for vin, items in by_vin.items():
        result["vehicles"][vin] = await hass.async_add_executor_job(
            merge_summaries, db, vin, items
        )
    return result


async def async_run_history_job(
    hass: Any,
    client: Any,
    vins_by_vehicle_id: dict[str, str],
    db: Any,
    force: bool = False,
    lookup_stations: bool = True,
) -> dict[str, Any]:
    """Run the Rivian import (at most once a day unless ``force``) and then
    the OpenStreetMap lookup for fast charges that still have no station.
    ``lookup_stations`` is the ``place_geocoding`` option: off skips the
    OpenStreetMap lookups entirely.

    Returns the import result plus ``"stations"`` (sessions enriched, per VIN)
    or ``{"skipped": True}`` when the daily guard says it already ran.
    """
    now = time.time()
    last = await hass.async_add_executor_job(db.get_meta, HISTORY_META_KEY)
    try:
        last_ts = float(last) if last else 0.0
    except ValueError:
        last_ts = 0.0
    if not force and now - last_ts < HISTORY_MIN_INTERVAL_S:
        return {"skipped": True}
    result = await async_import_rivian_history(hass, client, vins_by_vehicle_id, db)
    if "error" not in result:
        await hass.async_add_executor_job(db.set_meta, HISTORY_META_KEY, str(now))
    stations: dict[str, int] = {}
    for vin in vins_by_vehicle_id.values() if lookup_stations else ():
        stations[vin] = await charger_lookup.async_enrich_sessions(hass, db, vin)
    result["stations"] = stations
    return result


# -- capacity history ---------------------------------------------------------


def merge_capacity_days(
    existing: list[dict[str, Any]],
    stat_days: list[tuple[float, float]],
    inputs: dict[str, dict[str, Any]],
    tz: tzinfo,
) -> list[dict[str, Any]]:
    """Merge the sensor's daily statistics and drive inputs into history rows.

    ``stat_days`` are ``(ts, kwh)`` pairs; ``inputs`` is
    ``AnalyticsDatabase.capacity_day_inputs``. Per local day the capacity is
    the largest value seen (existing row included, so history only ever
    grows); the temperature is the mean battery temperature from that day's
    fast-charge samples (``temp_source`` 'battery'), else the mean outside
    temperature of its drives ('outside'), else whatever the row already had.
    Returns only rows that are new or changed.
    """
    have = {r["day"]: r for r in existing}
    kwh_by_day: dict[str, tuple[float, str]] = {}
    for ts, kwh in stat_days:
        if kwh and kwh > 0:
            day = datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d")
            if day not in kwh_by_day or kwh > kwh_by_day[day][0]:
                kwh_by_day[day] = (float(kwh), "statistics")
    for day, entry in inputs.items():
        kwh = entry.get("kwh")
        if kwh and (day not in kwh_by_day or kwh > kwh_by_day[day][0]):
            kwh_by_day[day] = (float(kwh), "drive")
    out: list[dict[str, Any]] = []
    for day, (kwh, source) in sorted(kwh_by_day.items()):
        prior = have.get(day)
        entry = inputs.get(day, {})
        if entry.get("battery_f") is not None:
            temp_f, temp_source = round(entry["battery_f"], 1), "battery"
        elif entry.get("outside_f") is not None:
            temp_f, temp_source = round(entry["outside_f"], 1), "outside"
        elif prior is not None:
            temp_f, temp_source = prior["temp_f"], prior["temp_source"]
        else:
            temp_f, temp_source = None, None
        if prior is not None and prior["kwh"] > kwh:
            kwh, source = prior["kwh"], prior["source"]
        row = {
            "day": day,
            "kwh": round(kwh, 2),
            "temp_f": temp_f,
            "temp_source": temp_source,
            "source": source,
        }
        if prior is None or any(prior[k] != row[k] for k in row):
            out.append(row)
    return out


async def async_update_capacity_history(hass: Any, db: Any, vin: str) -> int:
    """Merge a real vehicle's capacity statistics and drives into ``capacity_history``.

    Reads ALL long-term statistics of its battery-capacity sensor (per day,
    from the earliest available), so the first run seeds the whole history
    and later runs only add new days. Returns the number of rows written.
    Never raises.
    """
    try:
        tz = dt_util.get_default_time_zone()
        stat_days: list[tuple[float, float]] = []
        entity_id = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{vin}-battery_capacity"
        )
        if entity_id:
            stat_days = await async_entity_statistics(
                hass,
                entity_id,
                0.0,
                (dt_util.utcnow() + timedelta(days=1)).timestamp(),
                "day",
                "max",
            )
        existing = await hass.async_add_executor_job(db.capacity_history_rows, vin)
        inputs = await hass.async_add_executor_job(db.capacity_day_inputs, vin, tz)
        rows = merge_capacity_days(existing, stat_days, inputs, tz)
        return await hass.async_add_executor_job(db.upsert_capacity_history, vin, rows)
    except Exception as err:  # noqa: BLE001 - must never break the nightly job
        _LOGGER.warning("Capacity history update failed for a vehicle: %s", err)
        return 0
