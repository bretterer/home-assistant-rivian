"""Synthetic demo vehicles (a made-up household in Eagle, ID) for demoing the dashboard.

The data lives in ``demo/demo_data.json`` (built offline by
``scripts/build_demo_data.py``, never from a real user's data). This module
turns it into stored drives for two fake vehicles, registers them so the
dashboard, picker and WebSocket API treat them like real ones, and removes
them again.

* ``build_demo_records`` is pure: it shifts the fixture's relative days so the
  newest drive lands on *yesterday* in Home Assistant's time zone, and builds
  the ``DriveRecord``/``DriveTrack``/``ChargingSessionRecord`` objects.
* ``async_install_demo`` / ``async_remove_demo`` write/delete through the shared
  ``AnalyticsDatabase`` (executor only) and keep the registry in sync.

Registry: the ``demo_vehicles`` meta row (``[{vin, name, model}]``) is the
persistent record. At setup ``async_setup_demo_registry`` builds a detached
``DriveStore`` per demo VIN under ``hass.data[DOMAIN]["_demo_stores"]`` (and a
plain list under ``["_demo_vehicles"]`` for synchronous readers such as
``select.py`` and the dashboard generator). Demo stores have no tracker,
coordinator or entities, and never receive the user's zones or geocode (see
``DriveStore.is_demo``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone, tzinfo
import json
import logging
import math
from pathlib import Path
from typing import Any, Final
import zlib

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import places
from .analytics_db import DEMO_VEHICLES_META_KEY, AnalyticsDatabase
from .const import (
    ATTR_ANALYTICS_DB,
    ATTR_DEMO_STORES,
    ATTR_DEMO_VEHICLES,
    ATTR_VEHICLE,
    DOMAIN,
    RIVIAN_ANALYTICS_UPDATED_EVENT,
    SIGNAL_DEMO_VEHICLES_UPDATED,
    VERSION,
)
from .drive_models import (
    ChargingSample,
    ChargingSessionRecord,
    DriveChunk,
    DriveRecord,
    SpeedBinData,
)
from .drive_storage import DriveStore
from .drive_track import DriveTrack, TrackPoint
from .energy_model import METERS_PER_MILE
from .statistics import async_clear_statistics, async_update_statistics

_LOGGER = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "demo" / "demo_data.json"

# Pictures for the Overview card (demo vehicles have no image entity): two
# side-view illustrations that ship in ``frontend/`` and are served from the
# integration's static path, so a demo vehicle never contacts any Rivian
# server. The static path is cached by browsers for a month, so the ``?v=``
# query also carries a fingerprint of each file's contents: a changed picture
# gets a new URL even when the integration version stays the same.
DEMO_PICTURE_FILES: Final[dict[str, str]] = {
    "DEMO1R1TEAGLE0002": "demo-r1t.svg",
    "DEMO0R2EAGLE00001": "demo-r2.svg",
}


def _file_fingerprint(name: str) -> str:
    """A short checksum of a bundled frontend file, or "" when it is missing."""
    try:
        data = (Path(__file__).parent / "frontend" / name).read_bytes()
    except OSError:
        return ""
    return f"{zlib.crc32(data):08x}"


# Computed once at import (which HA runs off the event loop), never per request.
_DEMO_PICTURE_FINGERPRINTS: Final[dict[str, str]] = {
    name: _file_fingerprint(name) for name in DEMO_PICTURE_FILES.values()
}


def demo_picture_url(vin: str) -> str | None:
    """The Overview-card picture URL for a demo VIN, or None."""
    name = DEMO_PICTURE_FILES.get(vin)
    if name is None:
        return None
    fingerprint = _DEMO_PICTURE_FINGERPRINTS.get(name)
    suffix = f"-{fingerprint}" if fingerprint else ""
    return f"/{DOMAIN}_static/{name}?v={VERSION}{suffix}"


_FEET_PER_METER = 3.28084
_DASHBOARD_BACKUP_KEY = "rivian_dashboard_backup_before_demo"


@dataclass
class DemoPlace:
    """A named place the demo household visits (stored as a ``user`` place)."""

    key: str
    name: str
    category: str | None
    lat: float
    lon: float
    radius_m: float


@dataclass
class DemoVehicleBuild:
    """Everything needed to install one demo vehicle."""

    vin: str
    name: str
    model: str
    battery_capacity_kwh: float
    drives: list[DriveRecord] = field(default_factory=list)
    tracks: list[tuple[str, DriveTrack]] = field(default_factory=list)
    sessions: list[ChargingSessionRecord] = field(default_factory=list)
    places: list[DemoPlace] = field(default_factory=list)
    # ``capacity_history`` rows: {day, kwh, temp_f, temp_source, source='demo'}.
    capacity_history: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, str]:
        """Return the registry entry for this vehicle."""
        return {"vin": self.vin, "name": self.name, "model": self.model}


def load_fixture() -> dict[str, Any]:
    """Load the packaged demo-data fixture (blocking file read: executor only)."""
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _fixture_max_day_offset(fixture: dict[str, Any]) -> int:
    offsets: list[int] = []
    for vehicle in fixture.get("vehicles", []):
        offsets.extend(int(d["day_offset"]) for d in vehicle.get("drives", []))
        offsets.extend(
            int(s["day_offset"]) for s in vehicle.get("charging_sessions", [])
        )
    return max(offsets) if offsets else 0


def _local_to_utc(
    base_date: date, day_offset: int, seconds: float, tz: tzinfo
) -> datetime:
    """Return the UTC instant ``seconds`` after local midnight of the shifted day.

    Built from a local wall-clock midnight plus an elapsed-seconds offset, so a
    DST change inside the day shifts nothing: ``seconds`` is elapsed time from
    that midnight, exactly as the fixture's builder meant it.
    """
    day = base_date + timedelta(days=day_offset)
    midnight_utc = datetime.combine(day, dt_time.min, tzinfo=tz).astimezone(
        timezone.utc
    )
    return midnight_utc + timedelta(seconds=seconds)


def _build_track(
    drive: dict[str, Any], start_epoch: float, mi_to_m: float = METERS_PER_MILE
) -> DriveTrack:
    raw = drive["track"]
    points = [
        TrackPoint(
            t=start_epoch + raw["t"][i],
            lat=raw["lat"][i],
            lon=raw["lon"][i],
            speed_mps=raw["speed_mps"][i],
            alt_m=raw["alt_m"][i],
            soc=raw["soc"][i],
            odo_m=raw["odometer_mi"][i] * mi_to_m,
        )
        for i in range(len(raw["t"]))
    ]
    track = DriveTrack()
    track.extend(points)
    return track


def _build_drive(
    vehicle: dict[str, Any],
    drive: dict[str, Any],
    base_date: date,
    tz: tzinfo,
) -> tuple[DriveRecord, DriveTrack]:
    day_offset = int(drive["day_offset"])
    start_dt = _local_to_utc(base_date, day_offset, drive["start_s"], tz)
    end_dt = _local_to_utc(base_date, day_offset, drive["end_s"], tz)
    track = _build_track(drive, start_dt.timestamp())
    pts = track.points

    chunks = [
        DriveChunk(
            start_time=_local_to_utc(
                base_date, day_offset, chunk["start_s"], tz
            ).isoformat(),
            duration_seconds=chunk["duration_seconds"],
            distance_miles=chunk["distance_miles"],
            energy_kwh=chunk["energy_kwh"],
            efficiency_mi_kwh=chunk["efficiency_mi_kwh"],
            avg_speed_mph=chunk["avg_speed_mph"],
            speed_bin=chunk["speed_bin"],
        )
        for chunk in drive.get("chunks", [])
    ]
    speed_bins = {
        key: SpeedBinData(miles=val["miles"], seconds=val["seconds"])
        for key, val in drive.get("speed_bins", {}).items()
    }
    record = DriveRecord(
        vin=vehicle["vin"],
        drive_id=drive["drive_id"],
        start_time=start_dt.isoformat(),
        end_time=end_dt.isoformat(),
        distance_miles=drive["distance_miles"],
        duration_seconds=round(drive["end_s"] - drive["start_s"], 1),
        start_soc=drive["start_soc"],
        end_soc=drive["end_soc"],
        # Reported capacity drifts down slowly over the fixture's window.
        battery_capacity_kwh=drive.get(
            "battery_capacity_kwh", vehicle["battery_capacity_kwh"]
        ),
        energy_kwh=drive["energy_kwh"],
        efficiency_mi_kwh=drive["efficiency_mi_kwh"],
        mpge=drive["mpge"],
        start_altitude_ft=round(pts[0].alt_m * _FEET_PER_METER, 1) if pts else 0.0,
        end_altitude_ft=round(pts[-1].alt_m * _FEET_PER_METER, 1) if pts else 0.0,
        avg_speed_mph=drive["avg_speed_mph"],
        max_speed_mph=drive["max_speed_mph"],
        integrated_temperature_f=drive.get("temperature_f"),
        speed_bins=speed_bins,
        start_odometer_mi=drive.get("start_odometer_mi"),
        end_odometer_mi=drive.get("end_odometer_mi"),
        start_lat=pts[0].lat if pts else None,
        start_lon=pts[0].lon if pts else None,
        end_lat=pts[-1].lat if pts else None,
        end_lon=pts[-1].lon if pts else None,
        chunks=chunks,
        start_range_mi=drive.get("start_range_mi"),
        end_range_mi=drive.get("end_range_mi"),
        drive_modes=list(drive.get("drive_modes", [])),
        driver=drive.get("driver"),
        # Driving conditions come precomputed from the fixture (never the
        # network): see scripts/build_demo_data.py's add_conditions().
        **{
            key: drive[key]
            for key in (
                "wind_speed_mph",
                "wind_dir_deg",
                "headwind_mph",
                "precip_mm",
                "pressure_hpa",
                "humidity_pct",
                "air_density",
                "expected_kwh",
            )
            if drive.get(key) is not None
        },
    )
    return record, track


def _build_session(
    session: dict[str, Any],
    base_date: date,
    tz: tzinfo,
    places_by_key: dict[str, dict[str, Any]] | None = None,
) -> ChargingSessionRecord:
    day_offset = int(session["day_offset"])
    place = (places_by_key or {}).get(session.get("place") or "")
    outside_f, battery_f = demo_session_temps(session, base_date)
    return ChargingSessionRecord(
        outside_temp_f=outside_f,
        battery_temp_f=battery_f,
        kind=session.get("kind", "dc"),
        lat=place["lat"] if place else None,
        lon=place["lon"] if place else None,
        source="demo",
        session_id=session["session_id"],
        start_time=_local_to_utc(
            base_date, day_offset, session["start_s"], tz
        ).isoformat(),
        end_time=_local_to_utc(base_date, day_offset, session["end_s"], tz).isoformat(),
        vendor=session.get("vendor"),
        network=session.get("network"),
        station_name=session.get("station_name"),
        station_version=session.get("station_version"),
        charger_max_kw=session.get("charger_max_kw"),
        is_home=(
            bool(session["is_home"]) if session.get("is_home") is not None else None
        ),
        start_soc=session["start_soc"],
        end_soc=session["end_soc"],
        energy_added_kwh=session["energy_added_kwh"],
        max_power_kw=session["max_power_kw"],
        avg_power_kw=session["avg_power_kw"],
        samples=[
            ChargingSample(
                timestamp=_local_to_utc(
                    base_date, day_offset, session["start_s"] + sample["t_s"], tz
                ).isoformat(),
                soc=sample["soc"],
                power_kw=sample["power_kw"],
            )
            for sample in session.get("samples", [])
        ],
    )


def demo_session_temps(session: dict[str, Any], base_date: date) -> tuple[float, float]:
    """Made-up ``(outside_temp_f, battery_temp_f)`` for a demo charging session.

    Outside: the seasonal daily mean, a day/night swing (warmest mid-afternoon)
    and a little noise seeded by the session id, so a re-run gives the same
    numbers. Battery: a fast charge preconditions and heats the pack (it
    settles near 95 F); a slow charge runs a few degrees above the air.
    """
    day = base_date + timedelta(days=int(session["day_offset"]))
    hour = ((float(session["start_s"]) + float(session["end_s"])) / 2.0 / 3600.0) % 24
    noise = (zlib.crc32(str(session["session_id"]).encode()) % 7) - 3
    outside = (
        seasonal_temp_f(day)
        + 9.0 * math.cos(2 * math.pi * (hour - 15.0) / 24.0)
        + noise
    )
    if session.get("kind", "dc") == "dc":
        battery = 0.35 * outside + 0.65 * 95.0
    else:
        battery = max(outside + 8.0, 45.0)
    return round(outside, 1), round(battery, 1)


def seasonal_temp_f(day: date) -> float:
    """A plausible daily mean temperature (F) for southwest Idaho: about 24 F
    in late January to about 80 F in late July."""
    angle = 2 * math.pi * (day.timetuple().tm_yday - 110) / 365.0
    return 52.0 + 28.0 * math.sin(angle)


def _build_capacity_history(
    vehicle: dict[str, Any], base_date: date
) -> list[dict[str, Any]]:
    """Turn a fixture vehicle's relative capacity history into dated rows.

    The temperature follows the real calendar (cold winter, hot summer) plus
    the fixture's per-day noise; a day read from the battery runs warmer than
    the air.
    """
    rows: list[dict[str, Any]] = []
    for item in vehicle.get("capacity_history", []):
        day = base_date + timedelta(days=int(item["day_offset"]))
        source = item.get("temp_source")
        temp = seasonal_temp_f(day) + float(item.get("temp_noise_f", 0.0))
        if source == "battery":
            temp += 6.0
        rows.append(
            {
                "day": day.isoformat(),
                "kwh": float(item["kwh"]),
                "temp_f": round(temp, 1),
                "temp_source": source,
                "source": "demo",
            }
        )
    return rows


def build_demo_records(
    fixture: dict[str, Any], tz: tzinfo, today: date
) -> list[DemoVehicleBuild]:
    """Build every demo vehicle's records, shifted so the newest drive is yesterday.

    Pure (no I/O). ``today`` is the current local date in ``tz``. The fixture's
    ``day_offset`` values are relative: the largest one (across all vehicles)
    maps to yesterday, and the rest keep their spacing before it. ``drive_id``s
    come straight from the fixture, so a re-install replaces rather than
    duplicates.
    """
    base_date = (today - timedelta(days=1)) - timedelta(
        days=_fixture_max_day_offset(fixture)
    )
    places_by_key = {p["key"]: p for p in fixture.get("places", [])}
    builds: list[DemoVehicleBuild] = []
    for vehicle in fixture.get("vehicles", []):
        build = DemoVehicleBuild(
            vin=vehicle["vin"],
            name=vehicle["name"],
            model=vehicle.get("model", ""),
            battery_capacity_kwh=vehicle["battery_capacity_kwh"],
        )
        build.capacity_history = _build_capacity_history(vehicle, base_date)
        used_places: list[str] = []
        for drive in vehicle.get("drives", []):
            record, track = _build_drive(vehicle, drive, base_date, tz)
            build.drives.append(record)
            build.tracks.append((record.drive_id, track))
            used_places.extend((drive["start_place"], drive["end_place"]))
        for session in vehicle.get("charging_sessions", []):
            build.sessions.append(_build_session(session, base_date, tz, places_by_key))
            if session.get("place"):
                used_places.append(session["place"])
        # Only the places this vehicle actually visits, so the Places tab
        # shows no orphans.
        for key in dict.fromkeys(used_places):
            p = places_by_key.get(key)
            if p is not None:
                build.places.append(
                    DemoPlace(
                        key=p["key"],
                        name=p["name"],
                        category=p.get("category"),
                        lat=p["lat"],
                        lon=p["lon"],
                        radius_m=p["radius_m"],
                    )
                )
        builds.append(build)
    return builds


# -- blocking (executor) work ------------------------------------------------


def _read_registry(db: AnalyticsDatabase) -> list[dict[str, str]]:
    raw = db.get_meta(DEMO_VEHICLES_META_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [
        {
            "vin": str(v["vin"]),
            "name": str(v.get("name", "")),
            "model": str(v.get("model", "")),
        }
        for v in data
        if isinstance(v, dict) and v.get("vin")
    ]


def _write_registry(db: AnalyticsDatabase, entries: list[dict[str, str]]) -> None:
    db.set_meta(DEMO_VEHICLES_META_KEY, json.dumps(entries))


def _install_vehicle_sync(
    db: AnalyticsDatabase, build: DemoVehicleBuild, tz: Any
) -> None:
    """Replace one demo VIN's data with a clean install (executor only)."""
    vin = build.vin
    db.delete_vin(vin)
    db.upsert_drives(vin, build.drives)
    db.upsert_tracks(vin, build.tracks, source="live")
    if build.sessions:
        db.upsert_dcfc_sessions(vin, build.sessions)
    if build.capacity_history:
        db.upsert_capacity_history(vin, build.capacity_history)
    # Drive stats first: route stats read the drives' moving_seconds.
    db.recompute_drive_stats(vin)
    # Places belong to the demo dataset, not to one car: both demo vehicles
    # share (and never duplicate) them.
    dataset = places.DATASET_DEMO
    known = {(p["name"] or "").lower() for p in db.list_places(dataset)}
    for place in build.places:
        if place.name.lower() in known:
            continue
        known.add(place.name.lower())
        db.create_place(
            dataset,
            place.lat,
            place.lon,
            place.name,
            place.radius_m,
            place.category,
        )
    db.rebuild_places(dataset)
    db.rebuild_routes(dataset)
    db.update_heat(vin, tz)


def _refresh_demo_dataset(db: AnalyticsDatabase, any_left: bool) -> None:
    """Drop the demo dataset's places/routes, or rebuild them for the cars left."""
    if any_left:
        db.rebuild_places(places.DATASET_DEMO)
        db.rebuild_routes(places.DATASET_DEMO)
    else:
        db.clear_dataset(places.DATASET_DEMO)


# -- registry ---------------------------------------------------------------


def get_demo_vehicles(hass: HomeAssistant) -> list[dict[str, str]]:
    """Return the registered demo vehicles (synchronous, from the in-memory registry)."""
    return list(hass.data.get(DOMAIN, {}).get(ATTR_DEMO_VEHICLES) or [])


def is_demo_vin(hass: HomeAssistant, vin: str) -> bool:
    """Return whether ``vin`` is a registered demo vehicle."""
    return any(v["vin"] == vin for v in get_demo_vehicles(hass))


def demo_vehicle_names(hass: HomeAssistant) -> list[str]:
    """Return the demo vehicles' display names, in registry order."""
    return [v["name"] for v in get_demo_vehicles(hass)]


async def _async_add_store(
    hass: HomeAssistant, db: AnalyticsDatabase, vin: str
) -> DriveStore:
    domain_data = hass.data.setdefault(DOMAIN, {})
    stores: dict[str, DriveStore] = domain_data.setdefault(ATTR_DEMO_STORES, {})
    store = stores.get(vin)
    if store is None:
        store = DriveStore(hass, vin, db, place_geocoding=False, is_demo=True)
        stores[vin] = store
    if store.is_loaded:
        await store.async_refresh_cache()
    else:
        await store.async_load()
    return store


async def async_setup_demo_registry(hass: HomeAssistant, db: AnalyticsDatabase) -> None:
    """Create detached stores for the registered demo vehicles (called at setup).

    Idempotent across config entries/reloads: an existing registry is kept.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if ATTR_DEMO_VEHICLES in domain_data:
        return
    entries = await hass.async_add_executor_job(_read_registry, db)
    domain_data[ATTR_DEMO_VEHICLES] = entries
    domain_data.setdefault(ATTR_DEMO_STORES, {})
    for entry in entries:
        try:
            await _async_add_store(hass, db, entry["vin"])
        except Exception:
            _LOGGER.exception("Could not load demo vehicle %s", entry["vin"])


def async_teardown_demo_registry(hass: HomeAssistant) -> None:
    """Drop the in-memory demo registry (the last config entry unloaded)."""
    domain_data = hass.data.get(DOMAIN, {})
    domain_data.pop(ATTR_DEMO_STORES, None)
    domain_data.pop(ATTR_DEMO_VEHICLES, None)


# -- install / remove ---------------------------------------------------------


async def async_install_demo(hass: HomeAssistant) -> list[dict[str, str]]:
    """Install (or replace) the demo vehicles and refresh everything that shows them."""
    domain_data = hass.data.get(DOMAIN, {})
    db: AnalyticsDatabase | None = domain_data.get(ATTR_ANALYTICS_DB)
    if db is None:
        raise RuntimeError("The Rivian analytics database is not ready")

    fixture = await hass.async_add_executor_job(load_fixture)
    tz = dt_util.get_default_time_zone()
    today = dt_util.now().date()
    builds = build_demo_records(fixture, tz, today)

    existing = {v["vin"] for v in get_demo_vehicles(hass)}
    entries = [
        v for v in get_demo_vehicles(hass) if v["vin"] not in {b.vin for b in builds}
    ]
    entries.extend(b.summary() for b in builds)
    # Registered first: the analytics DB derives a VIN's places/routes dataset
    # ('demo' vs 'real') from this row, so the demo drives must never be
    # counted as real ones while they are being written.
    await hass.async_add_executor_job(_write_registry, db, entries)
    domain_data[ATTR_DEMO_VEHICLES] = entries
    for build in builds:
        if build.vin in existing:
            async_clear_statistics(hass, build.vin)
        await hass.async_add_executor_job(_install_vehicle_sync, db, build, tz)
        await async_update_statistics(hass, build.vin, build.drives)

    # Refresh first: when the picker select doesn't exist yet this reloads
    # the config entry, which closes and reopens the shared DB and rebuilds
    # the demo registry from the meta row just written. A store created
    # before that would start its background seeds on the closed DB.
    await async_refresh_after_change(hass)

    domain_data = hass.data.get(DOMAIN, {})
    db = domain_data.get(ATTR_ANALYTICS_DB) or db
    for build in builds:
        # Creates the store, or refreshes the cache of one the reload (or an
        # earlier install) already made.
        await _async_add_store(hass, db, build.vin)
        hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": build.vin})
    return [b.summary() for b in builds]


async def async_remove_demo(hass: HomeAssistant, vin: str | None = None) -> list[str]:
    """Remove one demo vehicle (or all) completely; return the VINs removed.

    Deletes the VIN's stored data and long-term statistics, drops it from the
    registry and ``_demo_stores``, then updates the picker and regenerates the
    dashboard so it disappears everywhere. Never touches a non-demo VIN.
    """
    domain_data = hass.data.get(DOMAIN, {})
    db: AnalyticsDatabase | None = domain_data.get(ATTR_ANALYTICS_DB)
    if db is None:
        raise RuntimeError("The Rivian analytics database is not ready")

    entries = get_demo_vehicles(hass)
    targets = [v["vin"] for v in entries if vin is None or v["vin"] == vin]
    if not targets:
        return []

    for target in targets:
        await hass.async_add_executor_job(db.delete_vin, target)
        async_clear_statistics(hass, target)
    remaining = [v for v in entries if v["vin"] not in targets]
    await hass.async_add_executor_job(_write_registry, db, remaining)
    domain_data[ATTR_DEMO_VEHICLES] = remaining
    # Places and routes belong to no vehicle: the demo dataset's go away with
    # the last demo car; while one remains they are refreshed for it.
    await hass.async_add_executor_job(_refresh_demo_dataset, db, bool(remaining))
    stores: dict[str, DriveStore] = domain_data.get(ATTR_DEMO_STORES, {})
    for target in targets:
        stores.pop(target, None)
        hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": target})

    await async_refresh_after_change(hass)
    return targets


# -- picker + dashboard -------------------------------------------------------


async def async_refresh_after_change(hass: HomeAssistant) -> None:
    """Update the picker select, then regenerate the Rivian dashboard.

    Never raises: the data change already succeeded.
    """
    try:
        await _async_update_picker(hass)
    except Exception:
        _LOGGER.exception("Could not update the dashboard vehicle picker")
    try:
        await _async_regenerate_dashboard(hass)
    except Exception:
        _LOGGER.exception("Could not regenerate the Rivian dashboard")


async def _async_update_picker(hass: HomeAssistant) -> None:
    """Refresh the picker's options, or reload an entry that now needs one.

    The select only exists for an entry with at least two vehicles (demo
    ones count), so adding a demo vehicle to a one-vehicle entry needs a
    reload to create it.
    """
    domain_data = hass.data.get(DOMAIN, {})
    demo_count = len(get_demo_vehicles(hass))
    registry = er.async_get(hass)
    reload_ids: list[str] = []
    for entry_id, entry_data in list(domain_data.items()):
        # Special (non-entry) keys all start with an underscore.
        if str(entry_id).startswith("_") or not isinstance(entry_data, dict):
            continue
        real_count = len(entry_data.get(ATTR_VEHICLE) or {})
        has_select = (
            registry.async_get_entity_id(
                "select", DOMAIN, f"{entry_id}-dashboard_vehicle"
            )
            is not None
        )
        if not has_select and real_count + demo_count >= 2:
            reload_ids.append(entry_id)
    async_dispatcher_send(hass, SIGNAL_DEMO_VEHICLES_UPDATED)
    for entry_id in reload_ids:
        await hass.config_entries.async_reload(entry_id)


async def _async_regenerate_dashboard(hass: HomeAssistant) -> None:
    """Regenerate the user's existing Rivian dashboard with the current vehicles.

    Only an existing dashboard (default ``rivian-dashboard``) is regenerated,
    keeping its title and icon; its stored config is backed up once first.
    """
    from .dashboard_generator import (
        DEFAULT_ICON,
        DEFAULT_TITLE,
        DEFAULT_URL_PATH,
        async_create_efficiency_dashboard,
    )

    dashboards: Store[Any] = Store(hass, 1, "lovelace_dashboards")
    data = await dashboards.async_load() or {}
    item = next(
        (i for i in data.get("items", []) if i.get("url_path") == DEFAULT_URL_PATH),
        None,
    )
    if item is None:
        _LOGGER.debug("No Rivian dashboard to regenerate for demo vehicles")
        return

    dashboard_key = f"lovelace.{DEFAULT_URL_PATH.replace('-', '_')}"
    current: Store[Any] = Store(hass, 1, dashboard_key)
    backup: Store[Any] = Store(hass, 1, _DASHBOARD_BACKUP_KEY)
    if await backup.async_load() is None:
        saved = await current.async_load()
        if saved:
            await backup.async_save(saved)

    await async_create_efficiency_dashboard(
        hass,
        title=item.get("title") or DEFAULT_TITLE,
        icon=item.get("icon") or DEFAULT_ICON,
        url_path=DEFAULT_URL_PATH,
    )


async def async_remove_demo_vehicle_history(hass: HomeAssistant, vin: str) -> bool:
    """Remove ``vin`` completely if it is a demo vehicle; return whether it was."""
    if not is_demo_vin(hass, vin):
        return False
    await async_remove_demo(hass, vin)
    return True


__all__ = [
    "DEMO_VEHICLES_META_KEY",
    "DemoPlace",
    "DemoVehicleBuild",
    "async_install_demo",
    "async_remove_demo",
    "async_remove_demo_vehicle_history",
    "async_setup_demo_registry",
    "async_teardown_demo_registry",
    "build_demo_records",
    "demo_picture_url",
    "demo_vehicle_names",
    "get_demo_vehicles",
    "is_demo_vin",
    "load_fixture",
]
