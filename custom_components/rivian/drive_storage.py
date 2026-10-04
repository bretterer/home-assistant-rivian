"""Rivian Trip Efficiency & Analytics Storage Manager.

DriveStore is a thin async wrapper around AnalyticsDatabase: every mutation does
an executor round-trip to SQLite and then rebuilds a small in-memory HotCache,
which is what entities read synchronously on every state update. No unbounded
in-memory list survives a full drive history any more.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import date, datetime, timezone, tzinfo
import functools
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import battery_analytics, charger_lookup, geocode, road_snap
from .analytics_db import (
    ENERGY_MODEL_MIN_DRIVES,
    ENERGY_MODEL_WINDOW_DAYS,
    PLACES_GEOCODE_DEFAULT_LIMIT,
    ActiveCheckpoint,
    AnalyticsDatabase,
    HotCache,
    VehiclePicture,
)
from .const import (
    ATTR_DEMO_STORES,
    ATTR_DRIVE_STORE,
    DOMAIN,
    RIVIAN_ANALYTICS_UPDATED_EVENT,
)
from .drive_conditions import archive_samples
from .drive_models import (
    AggregatedDriveStats,
    ChargingSessionRecord,
    DriveRecord,
    VampireDrainRecord,
)
from .drive_track import DriveTrack
from .energy_model import EnergyModelParams
from .places import DATASET_DEMO, DATASET_REAL
from .statistics import (
    async_clear_statistics,
    async_rewrite_statistics,
    async_soc_history,
)
from .weather import OpenMeteoWeatherClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

LEGACY_STORAGE_KEY_PREFIX: Final[str] = "rivian_drives"
LEGACY_STORAGE_VERSION: Final[int] = 1
LEGACY_STORAGE_MINOR_VERSION: Final[int] = 1
SNAP_GAPS_BATCH_LIMIT: Final[int] = 20
# Inferred charging sessions: how far back the battery-level history is read
# when the vehicle has no earlier drive/session, and how recent a drive's end
# must be for its location to stand in for where the car charged.
INFER_MAX_HISTORY_DAYS: Final[int] = 730
INFER_LOCATION_MAX_AGE_S: Final[float] = 7 * 86400.0
# The full back-scan runs once per VIN (and again only when INFER_VERSION
# changes, i.e. the detection itself changed); later runs re-check just the
# last INFER_RESCAN_DAYS, while their 5-minute statistics still exist. A span
# starting in the first INFER_OVERLAP_DAYS of that window was already found
# by an earlier run, so it is left as stored (it may be cut off here).
INFER_VERSION: Final[int] = 1
INFER_RESCAN_DAYS: Final[float] = 10.0
INFER_OVERLAP_DAYS: Final[float] = 1.0


def _inferred_record(
    vin: str, span: dict[str, Any], location: tuple[float, float] | None
) -> ChargingSessionRecord:
    """Build the stored session for one ``detect_charge_spans`` span."""
    start_ts, end_ts = float(span["start_ts"]), float(span["end_ts"])
    energy = span.get("energy_added_kwh")
    avg = span.get("avg_power_kw")
    return ChargingSessionRecord(
        session_id=f"inferred:{vin}:{int(start_ts)}",
        start_time=datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
        end_time=datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
        start_soc=float(span["start_soc"]),
        end_soc=float(span["end_soc"]),
        energy_added_kwh=float(energy) if energy is not None else 0.0,
        max_power_kw=float(avg) if avg is not None else 0.0,
        avg_power_kw=float(avg) if avg is not None else 0.0,
        kind="dc" if span.get("kind") == "dc" else "ac",
        lat=location[0] if location else None,
        lon=location[1] if location else None,
        source="inferred",
    )


def read_zone_states(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Read HA's configured zones as plain dicts for AnalyticsDatabase.sync_zones.

    Event-loop-only (reads ``hass.states``); the caller hands the resulting
    plain-dict list into the executor. Shared by DriveStore's one-time seed
    and __init__.py's setup-time sync and debounced zone-change listener.
    """
    zones: list[dict[str, Any]] = []
    for state in hass.states.async_all("zone"):
        lat = state.attributes.get("latitude")
        lon = state.attributes.get("longitude")
        if lat is None or lon is None:
            continue
        try:
            zones.append(
                {
                    "entity_id": state.entity_id,
                    "name": state.attributes.get("friendly_name") or state.name,
                    "latitude": float(lat),
                    "longitude": float(lon),
                    "radius": state.attributes.get("radius"),
                }
            )
        except (TypeError, ValueError):
            continue
    return zones


def _empty_cache() -> HotCache:
    """Return a placeholder cache used before the first async_load/refresh completes."""
    return HotCache(
        stats_30d=AggregatedDriveStats(),
        stats_90d=AggregatedDriveStats(),
        stats_365d=AggregatedDriveStats(),
        stats_all_time=AggregatedDriveStats(),
        last_drive=None,
        recent_drives=[],
        recent_vampire_events=[],
        dcfc_sessions=[],
        drive_count=0,
        revision=0,
        generated_at=0.0,
    )


# Weather backfill (driving conditions): how far back, how many days one archive
# request may span, the pause between requests (polite to the free API) and how
# many failed requests in a row end a run.
WEATHER_BACKFILL_DEFAULT_DAYS: Final[int] = 365
WEATHER_BACKFILL_WINDOW_DAYS: Final[int] = 31
WEATHER_BACKFILL_REQUEST_INTERVAL_S: Final[float] = 1.0
WEATHER_BACKFILL_MAX_FAILURES: Final[int] = 3
# Charging-session outside temperatures: at most this many Open-Meteo requests
# per run; sessions newer than this many days skip the archive (it lags a few
# days behind) and use the forecast API's past days.
SESSION_WEATHER_MAX_REQUESTS: Final[int] = 20
SESSION_WEATHER_RECENT_DAYS: Final[int] = 5


class DriveStore:
    """SQLite-backed drive analytics store for a single vehicle, with a hot cache."""

    def __init__(
        self,
        hass: HomeAssistant,
        vin: str,
        db: AnalyticsDatabase,
        place_geocoding: bool = True,
        is_demo: bool = False,
    ) -> None:
        """Initialize DriveStore for a specific vehicle VIN against a shared AnalyticsDatabase.

        ``is_demo`` marks a detached store for a synthetic demo vehicle (see
        ``demo.py``). It never receives the user's HA zones (they would put
        the user's real home on the demo map), never geocodes and never
        queries Overpass: every zone-sync entry point is a no-op for it.
        """
        self.hass = hass
        self.vin = vin
        self._db = db
        self.is_demo = is_demo
        # Places and routes belong to no vehicle; the only partition is this
        # dataset, so the demo cars' made-up places never mix with real ones.
        self.dataset = DATASET_DEMO if is_demo else DATASET_REAL
        self._place_geocoding = False if is_demo else place_geocoding
        # Legacy per-VIN JSON store: read once for the one-time import, then left
        # untouched on disk as a downgrade/recovery fallback.
        self._legacy_store: Store[Any] = Store(
            hass,
            version=LEGACY_STORAGE_VERSION,
            key=f"{LEGACY_STORAGE_KEY_PREFIX}_{vin}.json",
            minor_version=LEGACY_STORAGE_MINOR_VERSION,
        )
        self._migration_lock = asyncio.Lock()
        self._loaded = False
        self._revision = 0
        self._cache: HotCache = _empty_cache()
        self._heat_seeded = False
        self._stats_recompute_seeded = False
        self._energy_model_seeded = False
        self._gap_snap_seeded = False
        self._places_seeded = False
        self._routes_seeded = False
        self._weather_seeded = False
        self._weather_client: OpenMeteoWeatherClient | None = None

    # -- sync, cache-only accessors (never touch SQLite) ---------------------

    @property
    def last_drive(self) -> DriveRecord | None:
        """Return the most recent completed drive (by sort_ts, then created_ts)."""
        return self._cache.last_drive

    @property
    def drive_count(self) -> int:
        """Return the all-time count of non-micro drives."""
        return self._cache.drive_count

    @property
    def recent_drives(self) -> list[DriveRecord]:
        """Return cached non-micro drives from the last 90 days, ascending by time."""
        return self._cache.recent_drives

    @property
    def recent_vampire_events(self) -> list[VampireDrainRecord]:
        """Return cached vampire drain events from the last 90 days, ascending by time."""
        return self._cache.recent_vampire_events

    @property
    def speed_bin_totals(self) -> dict[str, dict[str, float]]:
        """Return cached miles and seconds per speed bin across all retained drives."""
        return self._cache.speed_bin_totals

    @property
    def is_loaded(self) -> bool:
        """Return whether storage has completed its initial load."""
        return self._loaded

    @property
    def revision(self) -> int:
        """Return the monotonically increasing cache revision (bumps on every mutation)."""
        return self._revision

    def get_dcfc_sessions(self, limit: int = 50) -> list[ChargingSessionRecord]:
        """Return cached DC Fast Charging sessions, newest-capped, up to limit."""
        return list(self._cache.dcfc_sessions[-limit:])

    def get_stats_30d(self) -> AggregatedDriveStats:
        """Return cached rolling 30-day weighted efficiency stats."""
        return self._cache.stats_30d

    def get_stats_90d(self) -> AggregatedDriveStats:
        """Return cached rolling 90-day weighted efficiency stats."""
        return self._cache.stats_90d

    def get_stats_365d(self) -> AggregatedDriveStats:
        """Return cached rolling 365-day weighted efficiency stats."""
        return self._cache.stats_365d

    def get_stats_all_time(self) -> AggregatedDriveStats:
        """Return cached all-time weighted efficiency stats."""
        return self._cache.stats_all_time

    # -- async lifecycle -------------------------------------------------------

    async def async_load(self) -> None:
        """Load this VIN's analytics, running the one-time legacy JSON import if needed."""
        async with self._migration_lock:
            if not self._loaded:
                marker_key = f"json_migrated_{self.vin}"
                already_migrated = await self.hass.async_add_executor_job(
                    self._db.get_meta, marker_key
                )
                if already_migrated is None:
                    await self._async_import_legacy_json()
                self._loaded = True
        await self.async_refresh_cache()
        self._async_seed_heat_once()
        self._async_maybe_recompute_stats_once()
        self._async_maybe_fit_energy_model_once()
        self._async_seed_snap_gaps_once()
        self._async_seed_places_once()
        self._async_seed_weather_once()

    def _async_maybe_recompute_stats_once(self) -> None:
        """Schedule a one-time background stats recompute if any drive needs it.

        Guarded so it only ever runs once per store instance; mirrors
        ``_async_seed_heat_once``. A drive needs this right after the v6
        schema migration (new track-derived columns start out NULL) -- the
        cheap existence check means a normal restart with nothing to do is a
        no-op.
        """
        if self._stats_recompute_seeded:
            return
        self._stats_recompute_seeded = True
        self.hass.async_create_background_task(
            self._async_recompute_stats_if_needed(),
            name=f"rivian stats recompute {self.vin}",
        )

    async def _async_recompute_stats_if_needed(self) -> None:
        """Best-effort: recompute track-derived stats if any drive still lacks them."""
        try:
            needed = await self.hass.async_add_executor_job(
                self._db.has_unrecomputed_drive_stats, self.vin
            )
            if not needed:
                return
            result = await self.async_recompute_stats()
            _LOGGER.info(
                "Recomputed drive summary stats for VIN %s: %s", self.vin, result
            )
        except Exception:
            _LOGGER.exception(
                "Post-migration drive stats recompute failed for VIN %s (non-fatal)",
                self.vin,
            )

    def _async_maybe_fit_energy_model_once(self) -> None:
        """Schedule a one-time background energy-model fit if none exists yet.

        Guarded so it only ever runs once per store instance, like
        ``_async_seed_heat_once``/``_async_maybe_recompute_stats_once``. The
        daily retention-prune timer in ``__init__.py`` refits unconditionally
        once a day; this just means a fresh install doesn't wait a full day
        for its first fit.
        """
        if self._energy_model_seeded:
            return
        self._energy_model_seeded = True
        self.hass.async_create_background_task(
            self._async_fit_energy_model_if_needed(),
            name=f"rivian energy model seed {self.vin}",
        )

    async def _async_fit_energy_model_if_needed(self) -> None:
        """Best-effort: fit the energy model if this VIN has no stored fit yet."""
        try:
            existing = await self.async_get_energy_model()
            if existing is not None:
                return
            result = await self.async_fit_energy_model()
            _LOGGER.info("Initial energy-model fit for VIN %s: %s", self.vin, result)
        except Exception:
            _LOGGER.exception(
                "Initial energy-model fit failed for VIN %s (non-fatal)", self.vin
            )

    # -- driving conditions (weather backfill) -------------------------------

    def _get_weather_client(self) -> OpenMeteoWeatherClient:
        if self._weather_client is None:
            self._weather_client = OpenMeteoWeatherClient(hass=self.hass)
        return self._weather_client

    def _async_seed_weather_once(self) -> None:
        """Schedule the one-time background weather/conditions backfill.

        Mirrors ``_async_maybe_recompute_stats_once``: guarded per store
        instance, stamped in ``meta`` as ``weather_version:<vin>`` once a run
        finishes without a network failure, and never run for a demo VIN
        (demo drives get their conditions from the fixture).
        """
        if self._weather_seeded or self.is_demo:
            return
        self._weather_seeded = True
        self.hass.async_create_background_task(
            self._async_seed_weather_safe(),
            name=f"rivian weather seed {self.vin}",
        )

    async def _async_seed_weather_safe(self) -> None:
        """Best-effort one-time backfill over the last year; never raises."""
        try:
            needed = await self.hass.async_add_executor_job(
                self._db.has_unseeded_weather, self.vin
            )
            if not needed:
                return
            result = await self.async_backfill_weather(WEATHER_BACKFILL_DEFAULT_DAYS)
            _LOGGER.info("Weather backfill for VIN %s: %s", self.vin, result)
            if result.get("complete"):
                await self.hass.async_add_executor_job(
                    self._db.mark_weather_seeded, self.vin
                )
            if result.get("updated"):
                self.hass.bus.async_fire(
                    RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": self.vin}
                )
        except Exception:
            _LOGGER.exception(
                "Weather backfill failed for VIN %s (non-fatal)", self.vin
            )

    async def async_backfill_weather(
        self, days: int = WEATHER_BACKFILL_DEFAULT_DAYS
    ) -> dict[str, Any]:
        """Fill NULL wind/precip/pressure/humidity/density/headwind/expected-kWh columns.

        Add-only, for stored routed drives of the last ``days`` days, from the
        Open-Meteo archive. To keep the load on that free service small the
        drives are grouped by ~11 km location tile and, within a tile, into
        windows of at most ``WEATHER_BACKFILL_WINDOW_DAYS`` days: one request
        per window, ``WEATHER_BACKFILL_REQUEST_INTERVAL_S`` apart, stopping
        early after ``WEATHER_BACKFILL_MAX_FAILURES`` failed requests in a row.
        A demo store does nothing. Returns ``{"drives", "updated", "requests",
        "failed_requests", "complete"}`` (``complete`` is False if any request
        failed, so the one-time seed is retried on the next start).
        """
        result: dict[str, Any] = {
            "drives": 0,
            "updated": 0,
            "requests": 0,
            "failed_requests": 0,
            "complete": True,
        }
        if self.is_demo:
            return result
        if not self._loaded:
            await self.async_load()
        since_ts = datetime.now(timezone.utc).timestamp() - days * 86400.0
        candidates = await self.hass.async_add_executor_job(
            self._db.drives_for_weather_backfill, self.vin, since_ts
        )
        result["drives"] = len(candidates)
        tiles: dict[tuple[float, float], list[dict[str, Any]]] = {}
        for drive in candidates:
            if drive.get("start_ts") is None:
                continue
            key = (round(drive["lat"], 1), round(drive["lon"], 1))
            tiles.setdefault(key, []).append(drive)

        client = self._get_weather_client()
        consecutive_failures = 0
        for tile_drives in tiles.values():
            tile_drives.sort(key=lambda d: d["start_ts"])
            windows: list[list[dict[str, Any]]] = []
            for drive in tile_drives:
                if (
                    windows
                    and drive["start_ts"] - windows[-1][0]["start_ts"]
                    <= WEATHER_BACKFILL_WINDOW_DAYS * 86400.0
                ):
                    windows[-1].append(drive)
                else:
                    windows.append([drive])
            for window in windows:
                if result["requests"]:
                    await asyncio.sleep(WEATHER_BACKFILL_REQUEST_INTERVAL_S)
                first = window[0]
                end_ts = max(d.get("end_ts") or d["start_ts"] for d in window)
                start_date = datetime.fromtimestamp(
                    first["start_ts"] - 3600.0, timezone.utc
                ).date()
                end_date = datetime.fromtimestamp(end_ts + 3600.0, timezone.utc).date()
                result["requests"] += 1
                hourly = await client.async_get_historical_conditions(
                    first["lat"],
                    first["lon"],
                    start_date.isoformat(),
                    end_date.isoformat(),
                )
                if hourly is None:
                    result["failed_requests"] += 1
                    consecutive_failures += 1
                    if consecutive_failures >= WEATHER_BACKFILL_MAX_FAILURES:
                        break
                    continue
                consecutive_failures = 0
                items = [
                    (
                        d["drive_id"],
                        archive_samples(
                            hourly, d["start_ts"], d.get("end_ts") or d["start_ts"]
                        ),
                    )
                    for d in window
                ]
                result["updated"] += await self.hass.async_add_executor_job(
                    self._db.apply_weather_backfill, self.vin, items
                )
            if consecutive_failures >= WEATHER_BACKFILL_MAX_FAILURES:
                break
        if result["failed_requests"]:
            result["complete"] = False
        return result

    async def async_efficiency(
        self, days: int | None, tz: tzinfo, include_micro: bool = False
    ) -> dict[str, Any]:
        """Per-drive rows, speed bands and trends for the Efficiency page (executor)."""
        if not self._loaded:
            await self.async_load()
        since_ts = (
            None
            if days is None
            else datetime.now(timezone.utc).timestamp() - days * 86400.0
        )
        return await self.hass.async_add_executor_job(
            self._db.efficiency_data, self.vin, since_ts, tz, include_micro
        )

    async def async_get_energy_model(self) -> EnergyModelParams | None:
        """Return this VIN's stored fitted energy-model params, or None if never fitted."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.get_energy_model, self.vin
        )

    async def async_fit_energy_model(
        self,
        window_days: int = ENERGY_MODEL_WINDOW_DAYS,
        min_drives: int = ENERGY_MODEL_MIN_DRIVES,
    ) -> dict[str, Any]:
        """Refit this VIN's anchored energy-model coefficients; see AnalyticsDatabase.fit_energy_model."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.fit_energy_model, self.vin, window_days, min_drives
        )

    def _async_seed_heat_once(self) -> None:
        """Schedule a one-time background road-heat catch-up after this store first loads.

        Seeds heat for any routes already stored (e.g. before this feature
        existed) without blocking setup; guarded so it only ever runs once
        per store instance.
        """
        if self._heat_seeded:
            return
        self._heat_seeded = True
        self.hass.async_create_background_task(
            self._async_update_heat_safe(),
            name=f"rivian heat seed {self.vin}",
        )

    async def _async_import_legacy_json(self) -> None:
        """One-time import of the legacy per-VIN JSON store into SQLite.

        The legacy file is left byte-identical on disk: it is the recovery
        source if the SQLite database is ever declared corrupt, so a downgrade
        lands on stale-but-present data rather than nothing.
        """
        data = await self._legacy_store.async_load()
        drives, vampire_events, dcfc_sessions = self._parse_legacy_payload(data)

        if not (drives or vampire_events or dcfc_sessions):
            # Still record the marker so we don't re-check the legacy file every load.
            await self.hass.async_add_executor_job(
                self._db.set_meta, f"json_migrated_{self.vin}", "{}"
            )
            return

        counts = await self.hass.async_add_executor_job(
            self._db.migrate_legacy_json,
            self.vin,
            drives,
            vampire_events,
            dcfc_sessions,
        )
        _LOGGER.info(
            "Migrated legacy JSON drive storage for VIN %s into analytics database: %s",
            self.vin,
            counts,
        )

    @staticmethod
    def _parse_legacy_payload(
        data: Any,
    ) -> tuple[
        list[DriveRecord], list[VampireDrainRecord], list[ChargingSessionRecord]
    ]:
        """Tolerate the legacy JSON shapes: a dict payload, or a bare list of drives."""
        if isinstance(data, dict):
            raw_drives = data.get("drives", [])
            raw_vampire = data.get("vampire_events", [])
            raw_dcfc = data.get("dcfc_sessions", [])
        elif isinstance(data, list):
            raw_drives = data
            raw_vampire = []
            raw_dcfc = []
        else:
            raw_drives, raw_vampire, raw_dcfc = [], [], []

        drives = [DriveRecord.from_dict(d) for d in raw_drives if isinstance(d, dict)]
        vampire_events = [
            VampireDrainRecord.from_dict(v) for v in raw_vampire if isinstance(v, dict)
        ]
        dcfc_sessions = [
            ChargingSessionRecord.from_dict(c) for c in raw_dcfc if isinstance(c, dict)
        ]
        return drives, vampire_events, dcfc_sessions

    # -- async mutations ---------------------------------------------------------

    async def async_save_drive(self, drive: DriveRecord) -> bool:
        """Save or update a single drive record with deduplication by drive_id."""
        if not self._loaded:
            await self.async_load()
        await self.hass.async_add_executor_job(
            self._db.upsert_drives, self.vin, [drive]
        )
        await self.async_refresh_cache()
        return True

    async def async_save_drives_batch(self, drives: list[DriveRecord]) -> int:
        """Batch save drive records with deduplication; return count of newly added drives."""
        if not self._loaded:
            await self.async_load()
        if not drives:
            return 0
        new_count = await self.hass.async_add_executor_job(
            self._db.upsert_drives, self.vin, drives
        )
        await self.async_refresh_cache()
        _LOGGER.debug(
            "Batch saved %d drives (%d new) for VIN %s",
            len(drives),
            new_count,
            self.vin,
        )
        return new_count

    async def async_save_vampire_events(self, events: list[VampireDrainRecord]) -> None:
        """Merge vampire drain records into storage.

        Deliberate semantic change from the legacy JSON store: this upserts by
        (vin, start_time, end_time) instead of wholesale-replacing the list, so a
        historical backfill can no longer silently overwrite live-recorded events.
        """
        if not self._loaded:
            await self.async_load()
        if not events:
            return
        await self.hass.async_add_executor_job(
            self._db.merge_vampire_events, self.vin, events
        )
        await self.async_refresh_cache()

    async def async_append_vampire_event(self, event: VampireDrainRecord) -> None:
        """Append (upsert) a single vampire drain record."""
        if not self._loaded:
            await self.async_load()
        await self.hass.async_add_executor_job(
            self._db.insert_vampire_event, self.vin, event
        )
        await self.async_refresh_cache()

    async def async_save_dcfc_sessions(
        self, sessions: list[ChargingSessionRecord]
    ) -> None:
        """Save DC fast charging records to storage (upsert by session_id)."""
        if not self._loaded:
            await self.async_load()
        if not sessions:
            return
        await self.hass.async_add_executor_job(
            self._db.upsert_dcfc_sessions, self.vin, sessions
        )
        await self.async_refresh_cache()

    async def async_append_dcfc_session(self, session: ChargingSessionRecord) -> None:
        """Append (upsert) a single DC fast charging record."""
        if not self._loaded:
            await self.async_load()
        await self.hass.async_add_executor_job(
            self._db.upsert_dcfc_sessions, self.vin, [session]
        )
        await self.async_refresh_cache()
        self.hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": self.vin})
        if (
            self._place_geocoding
            and not self.is_demo
            and session.kind == "dc"
            and session.lat is not None
        ):
            self.hass.async_create_background_task(
                self._async_enrich_stations_safe(),
                name=f"rivian charger lookup {self.vin}",
            )
        if not self.is_demo and session.lat is not None:
            self.hass.async_create_background_task(
                self._async_fill_session_temperatures_safe(),
                name=f"rivian charging weather {self.vin}",
            )

    async def _async_fill_session_temperatures_safe(self) -> None:
        """Best-effort background run of ``async_fill_session_temperatures``."""
        try:
            result = await self.async_fill_session_temperatures()
            if result.get("updated"):
                self.hass.bus.async_fire(
                    RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": self.vin}
                )
        except Exception:
            _LOGGER.exception(
                "Charging weather lookup failed for VIN %s (non-fatal)", self.vin
            )

    async def async_fill_session_temperatures(
        self, max_requests: int = SESSION_WEATHER_MAX_REQUESTS
    ) -> dict[str, Any]:
        """Fill located sessions' outside temperature from Open-Meteo.

        Add-only: only sessions with no ``outside_temp_f`` yet. Grouped like the
        drive weather backfill (by ~11 km location tile, windows of at most
        ``WEATHER_BACKFILL_WINDOW_DAYS``), one archive request per window; when
        the archive doesn't have the hours yet (the last few days) the forecast
        API's past days fill them. At most ``max_requests`` requests, 1 s apart,
        stopping after ``WEATHER_BACKFILL_MAX_FAILURES`` failures in a row; a
        session still without a temperature is retried on the next run. A demo
        store does nothing. Returns ``{"sessions", "updated", "requests"}``.
        """
        result: dict[str, Any] = {"sessions": 0, "updated": 0, "requests": 0}
        if self.is_demo:
            return result
        if not self._loaded:
            await self.async_load()
        now_ts = datetime.now(timezone.utc).timestamp()
        candidates = await self.hass.async_add_executor_job(
            self._db.sessions_needing_outside_temp, self.vin, now_ts
        )
        result["sessions"] = len(candidates)
        tiles: dict[tuple[float, float], list[dict[str, Any]]] = {}
        for row in candidates:
            tiles.setdefault((round(row["lat"], 1), round(row["lon"], 1)), []).append(
                row
            )
        client = self._get_weather_client()
        failures = 0

        async def request(coro: Any) -> Any:
            nonlocal failures
            if result["requests"]:
                await asyncio.sleep(WEATHER_BACKFILL_REQUEST_INTERVAL_S)
            result["requests"] += 1
            data = await coro
            failures = 0 if data else failures + 1
            return data

        for rows in tiles.values():
            rows.sort(key=lambda r: r["start_ts"])
            windows: list[list[dict[str, Any]]] = []
            for row in rows:
                if (
                    windows
                    and row["start_ts"] - windows[-1][0]["start_ts"]
                    <= WEATHER_BACKFILL_WINDOW_DAYS * 86400.0
                ):
                    windows[-1].append(row)
                else:
                    windows.append([row])
            for window in windows:
                if (
                    result["requests"] >= max_requests
                    or failures >= WEATHER_BACKFILL_MAX_FAILURES
                ):
                    return result
                lat, lon = window[0]["lat"], window[0]["lon"]
                end_ts = max(r["end_ts"] or r["start_ts"] for r in window)
                start_date = datetime.fromtimestamp(
                    window[0]["start_ts"] - 3600.0, timezone.utc
                ).date()
                end_date = datetime.fromtimestamp(end_ts + 3600.0, timezone.utc).date()
                temps: dict[str, float] = {}
                if (
                    now_ts - window[0]["start_ts"]
                    > SESSION_WEATHER_RECENT_DAYS * 86400.0
                ):
                    hourly = await request(
                        client.async_get_historical_conditions(
                            lat, lon, start_date.isoformat(), end_date.isoformat()
                        )
                    )
                    temps = {
                        k: c["temp_f"]
                        for k, c in (hourly or {}).items()
                        if "temp_f" in c
                    }
                missing = [
                    r
                    for r in window
                    if battery_analytics.mean_hourly_temp(
                        temps, r["start_ts"], r["end_ts"]
                    )
                    is None
                ]
                if missing and now_ts - end_ts <= 90 * 86400.0:
                    past_days = int((now_ts - missing[0]["start_ts"]) // 86400) + 2
                    recent = await request(
                        client.async_get_recent_hourly_temperatures(lat, lon, past_days)
                    )
                    temps.update(recent or {})
                updates = []
                for r in window:
                    value = battery_analytics.mean_hourly_temp(
                        temps, r["start_ts"], r["end_ts"]
                    )
                    if value is not None:
                        updates.append((r["session_id"], value))
                for session_id, value in updates:
                    await self.hass.async_add_executor_job(
                        self._db.update_session_fields,
                        self.vin,
                        session_id,
                        {"outside_temp_f": value},
                    )
                result["updated"] += len(updates)
        return result

    async def _async_enrich_stations_safe(self) -> None:
        """Best-effort: name the station of newly finished fast charges via OSM."""
        try:
            if await charger_lookup.async_enrich_sessions(
                self.hass, self._db, self.vin
            ):
                self.hass.bus.async_fire(
                    RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": self.vin}
                )
        except Exception:
            _LOGGER.exception("Charger lookup failed for VIN %s (non-fatal)", self.vin)

    async def async_list_charging_sessions(
        self, since_ts: float | None = None, until_ts: float | None = None
    ) -> list[dict[str, Any]]:
        """Return this VIN's charging sessions (DC and AC), ascending, with place labels."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.list_charging_sessions, self.vin, since_ts, until_ts
        )

    async def async_capacity_history(self) -> list[dict[str, Any]]:
        """Return this VIN's stored per-day capacity history (kept forever)."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.capacity_history_rows, self.vin
        )

    async def async_charging_session_intervals(
        self, exclude_inferred: bool = False
    ) -> list[tuple[float, float]]:
        """Return every stored charging session's (start_ts, end_ts)."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.charging_session_intervals, self.vin, exclude_inferred
        )

    async def async_infer_charging_sessions(
        self, reader: Any = None, *, full: bool = False
    ) -> dict[str, int] | None:
        """Store the charges the battery-level history shows but nothing recorded.

        Real vehicles only (None for a demo store or with no battery-level
        sensor statistics). Reads the ``{vin}-battery_level`` statistics (hourly
        over the whole history, 5-minute over the last ~10 days), runs
        ``battery_analytics.detect_charge_spans`` against the recorded
        sessions (inferred ones excluded: they are re-derived) and replaces the
        VIN's ``source='inferred'`` sessions with the result (see
        ``AnalyticsDatabase.replace_inferred_sessions``). Fires the update event
        when the stored set changed. Returns ``{"detected", "changed", "full"}``.

        The first run for a VIN (no ``inferred_scan:<vin>`` meta stamp, or one
        from an older ``INFER_VERSION``) scans the whole history; after that a
        run re-checks only the last ``INFER_RESCAN_DAYS`` before the previous
        run and replaces just the inferred rows starting after its overlap
        day. ``full=True`` forces a whole-history scan.
        """
        if self.is_demo:
            return None
        if not self._loaded:
            await self.async_load()
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "sensor", DOMAIN, f"{self.vin}-battery_level"
        )
        if not entity_id:
            return None
        now_ts = dt_util.utcnow().timestamp()
        stamp = await self.hass.async_add_executor_job(
            self._db.get_inferred_scan, self.vin
        )
        incremental = (
            not full
            and stamp is not None
            and stamp.get("version") == INFER_VERSION
            and isinstance(stamp.get("through"), (int, float))
        )
        keep_from: float | None = None
        if incremental:
            start_ts = stamp["through"] - INFER_RESCAN_DAYS * 86400.0
            keep_from = start_ts + INFER_OVERLAP_DAYS * 86400.0
        else:
            first = await self.hass.async_add_executor_job(
                self._db.earliest_activity_ts, self.vin
            )
            floor_ts = now_ts - INFER_MAX_HISTORY_DAYS * 86400.0
            start_ts = max(first - 86400.0, floor_ts) if first else floor_ts
        points = await async_soc_history(
            self.hass, entity_id, start_ts, now_ts, reader=reader
        )
        if not points:
            return None
        recorded = await self.async_charging_session_intervals(exclude_inferred=True)
        drive_ends = await self.async_drive_end_times(
            start_ts - battery_analytics.DETECT_DC_DRIVE_GAP_S, now_ts
        )
        capacity = self.last_drive.battery_capacity_kwh if self.last_drive else None
        spans = battery_analytics.detect_charge_spans(
            points, recorded, capacity or None, drive_ends=drive_ends
        )
        if keep_from is not None:
            spans = [s for s in spans if s["start_ts"] >= keep_from]
        records: list[ChargingSessionRecord] = []
        for span in spans:
            location = await self.hass.async_add_executor_job(
                self._db.last_drive_end_location,
                self.vin,
                span["start_ts"],
                INFER_LOCATION_MAX_AGE_S,
            )
            records.append(_inferred_record(self.vin, span, location))
        changed = await self.hass.async_add_executor_job(
            self._db.replace_inferred_sessions, self.vin, records, keep_from
        )
        await self.hass.async_add_executor_job(
            self._db.set_inferred_scan,
            self.vin,
            {"version": INFER_VERSION, "through": now_ts},
        )
        if changed:
            await self.async_refresh_cache()
            self.hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": self.vin})
        return {
            "detected": len(spans),
            "changed": int(bool(changed)),
            "full": int(not incremental),
        }

    async def async_drive_end_times(
        self, start_ts: float, end_ts: float
    ) -> list[float]:
        """Return the end times of this VIN's drives ending in a window."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.drive_end_times, self.vin, start_ts, end_ts
        )

    async def async_soc_events(self, start_ts: float, end_ts: float) -> dict[str, Any]:
        """Return the drives/sessions overlapping a window (synthesized SoC timeline)."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.soc_events, self.vin, start_ts, end_ts
        )

    async def async_capacity_rows(self) -> list[dict[str, Any]]:
        """Return each drive's capacity/range readings for the battery-health series."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(self._db.capacity_rows, self.vin)

    async def async_reset(self) -> None:
        """Delete all analytics rows for this VIN and reset the hot cache."""
        await self.hass.async_add_executor_job(self._db.delete_vin, self.vin)
        self._cache = _empty_cache()
        self._revision += 1
        self._loaded = True
        _LOGGER.info("Reset analytics storage for VIN %s", self.vin)

    # -- GPS drive tracks -----------------------------------------------------

    async def async_finalize_drive(
        self, record: DriveRecord, track: DriveTrack | None
    ) -> bool:
        """Persist a completed drive and its GPS track, clearing the live checkpoint."""
        if not self._loaded:
            await self.async_load()
        is_new = await self.hass.async_add_executor_job(
            self._db.finalize_drive, self.vin, record, track
        )
        await self.hass.async_add_executor_job(
            self._db.assign_drive_places, self.vin, record.drive_id
        )
        await self.async_refresh_cache()
        # Counted in the background: a heat run already in progress (e.g. the
        # startup seed over a long history) must not delay finishing the drive.
        # It fires its own update event once the heat map includes this drive.
        self.hass.async_create_background_task(
            self._async_update_heat_safe(),
            name=f"rivian heat update {self.vin}",
        )
        if track is not None:
            self.hass.async_create_background_task(
                self._async_snap_gaps_safe(),
                name=f"rivian gap snap {self.vin}",
            )
        self.hass.async_create_background_task(
            self._async_geocode_places_safe(),
            name=f"rivian places geocode {self.vin}",
        )
        self.hass.async_create_background_task(
            self._async_rebuild_routes_safe(),
            name=f"rivian routes rebuild {self.vin}",
        )
        return is_new

    async def async_upsert_tracks(
        self, items: list[tuple[str, DriveTrack]], source: str = "live"
    ) -> int:
        """Insert or update GPS tracks for a batch of drives; return count written."""
        if not self._loaded:
            await self.async_load()
        written = await self.hass.async_add_executor_job(
            self._db.upsert_tracks, self.vin, items, source
        )
        await self.async_refresh_cache()
        if written:
            await self._async_update_heat_safe()
            self.hass.async_create_background_task(
                self._async_snap_gaps_safe(),
                name=f"rivian gap snap {self.vin}",
            )
        return written

    async def async_get_track(self, drive_id: str) -> DriveTrack | None:
        """Return the GPS track for one drive, if any."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.get_track, self.vin, drive_id
        )

    async def async_list_drives(
        self,
        before_ts: float | None = None,
        limit: int = 50,
        include_micro: bool = False,
    ) -> list[dict[str, Any]]:
        """Return a page of drive summaries, newest first."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.list_drives, self.vin, before_ts, limit, include_micro
        )

    async def async_get_track_previews(
        self, drive_ids: list[str]
    ) -> dict[str, dict[str, list]]:
        """Return {drive_id: {"lat": [...], "lon": [...]}} preview points."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.get_track_previews, self.vin, drive_ids
        )

    async def async_get_drive_detail(self, drive_id: str) -> dict[str, Any] | None:
        """Return the full drive detail payload (summary + speed bins/chunks + track)."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.get_drive_detail, self.vin, drive_id
        )

    async def async_calendar(
        self,
        tz: tzinfo,
        year: int | None = None,
        month: int | None = None,
        include_micro: bool = False,
        vins: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return the All time -> years -> months -> days grouping for this VIN.

        ``vins`` (which must include this store's VIN; the database is shared)
        returns the combined tree with a ``by_vin`` breakdown on every node.
        """
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.calendar,
            vins if vins is not None else self.vin,
            tz,
            year,
            month,
            include_micro,
        )

    async def async_day(
        self, tz: tzinfo, day: date, include_micro: bool = False
    ) -> dict[str, Any]:
        """Return one local calendar day's drives (segments), stops, and endpoints."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.day, self.vin, tz, day, include_micro
        )

    async def async_drives_missing_tracks(
        self, since_ts: float
    ) -> list[tuple[str, float, float]]:
        """Return (drive_id, start_ts, end_ts) for trackless drives, oldest first."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.drives_missing_tracks, self.vin, since_ts
        )

    async def async_save_checkpoint(
        self,
        drive_id: str,
        state: dict[str, Any],
        new_points: DriveTrack | None,
        seq: int,
    ) -> None:
        """Persist live drive-tracker state and (optionally) a new track chunk."""
        if not self._loaded:
            await self.async_load()
        await self.hass.async_add_executor_job(
            self._db.save_active_checkpoint,
            self.vin,
            drive_id,
            state,
            new_points,
            seq,
        )

    async def async_load_checkpoint(self) -> ActiveCheckpoint | None:
        """Return the in-progress drive checkpoint for this VIN, or None."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.load_active_checkpoint, self.vin
        )

    async def async_clear_checkpoint(self) -> None:
        """Delete the in-progress drive checkpoint (state + chunks) for this VIN."""
        if not self._loaded:
            await self.async_load()
        await self.hass.async_add_executor_job(
            self._db.clear_active_checkpoint, self.vin
        )

    async def async_prune_tracks(
        self, track_retention_days: int, full_detail_days: int
    ) -> dict[str, int]:
        """Prune/thin GPS tracks per the configured retention options.

        A value of 0 for either argument means "skip that step" (None is
        passed through to the database layer).
        """
        if not self._loaded:
            await self.async_load()
        now_ts = datetime.now(timezone.utc).timestamp()
        delete_before_ts = (
            now_ts - track_retention_days * 86400.0
            if track_retention_days > 0
            else None
        )
        thin_before_ts = (
            now_ts - full_detail_days * 86400.0 if full_detail_days > 0 else None
        )
        result = await self.hass.async_add_executor_job(
            self._db.prune_tracks, self.vin, delete_before_ts, thin_before_ts
        )
        await self.async_refresh_cache()
        return result

    async def async_storage_stats(self) -> dict[str, Any]:
        """Return drive/track row counts and byte sizes for diagnostics."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(self._db.storage_stats, self.vin)

    async def async_series_window(
        self, days: int
    ) -> tuple[list[DriveRecord], list[VampireDrainRecord]]:
        """Return non-micro drives and vampire events over an arbitrary window, ascending."""
        if not self._loaded:
            await self.async_load()
        now_ts = datetime.now(timezone.utc).timestamp()
        return await self.hass.async_add_executor_job(
            self._db.series_window, self.vin, days, now_ts
        )

    async def async_drives_since(
        self, from_ts: float, now_ts: float | None = None
    ) -> list[DriveRecord]:
        """Return every drive with sort_ts in [from_ts, now_ts], ascending, uncapped.

        Used by ``statistics.async_rewrite_statistics`` after a delete.
        """
        if not self._loaded:
            await self.async_load()
        if now_ts is None:
            now_ts = datetime.now(timezone.utc).timestamp()
        return await self.hass.async_add_executor_job(
            self._db.drives_since, self.vin, from_ts, now_ts
        )

    async def async_get_meta(self, key: str) -> str | None:
        """Read a value from the shared analytics database's ``meta`` table."""
        return await self.hass.async_add_executor_job(self._db.get_meta, key)

    async def async_set_meta(self, key: str, value: str) -> None:
        """Write a value to the shared analytics database's ``meta`` table."""
        await self.hass.async_add_executor_job(self._db.set_meta, key, value)

    async def async_get_vehicle_picture(self) -> VehiclePicture | None:
        """Return this vehicle's saved picture record, if one exists."""
        return await self.hass.async_add_executor_job(
            self._db.get_vehicle_picture, self.vin
        )

    async def async_save_vehicle_picture(self, picture: VehiclePicture) -> None:
        """Save this vehicle's picture record."""
        await self.hass.async_add_executor_job(
            self._db.save_vehicle_picture, self.vin, picture
        )

    async def async_recompute_stats(self) -> dict[str, int]:
        """Recompute track-derived summary stats for every drive with a stored track.

        Used by the ``rivian.recompute_drive_stats`` service and by the
        one-time post-migration catch-up. Never touches the live-only
        vehicle-context columns (range, drive modes, trailer, driver).
        """
        if not self._loaded:
            await self.async_load()
        result = await self.hass.async_add_executor_job(
            self._db.recompute_drive_stats, self.vin
        )
        await self.async_refresh_cache()
        return result

    # -- road heat map -----------------------------------------------------------

    async def async_heat_info(
        self, period: str, key: str | None = None, vins: list[str] | None = None
    ) -> dict[str, Any]:
        """Return road-heat summary info (bbox/scale_max/cells/drives) for a period.

        ``vins`` returns the merged grid of those vehicles (database is shared).
        """
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.heat_info, vins if vins is not None else self.vin, period, key
        )

    async def async_heat_tile(
        self,
        period: str,
        key: str | None,
        z: int,
        x: int,
        y: int,
        margin: int = 0,
        vins: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return one XYZ tile's road-heat cells (plus scale_max) for a period."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.heat_tile,
            vins if vins is not None else self.vin,
            period,
            key,
            z,
            x,
            y,
            margin,
        )

    async def async_update_heat(self) -> int:
        """Count every stored route not yet counted into this VIN's road-heat map."""
        if not self._loaded:
            await self.async_load()
        tz = dt_util.get_default_time_zone()
        return await self.hass.async_add_executor_job(
            self._db.update_heat, self.vin, tz
        )

    async def async_rebuild_heat(self) -> dict[str, Any]:
        """Recount road heat from scratch (recovery after a time-zone change)."""
        if not self._loaded:
            await self.async_load()
        tz = dt_util.get_default_time_zone()
        return await self.hass.async_add_executor_job(
            self._db.rebuild_heat, self.vin, tz
        )

    async def _async_update_heat_safe(self, fire_event: bool = True) -> int:
        """Best-effort road-heat catch-up: a failure here must never propagate.

        Called after a drive finalizes, after a backfill writes tracks, and
        once as a background task after this store first loads. Fires
        ``RIVIAN_ANALYTICS_UPDATED_EVENT`` only when it actually counted a
        drive, and only when the caller hasn't already fired (or isn't about
        to fire) that event itself for this same change.
        """
        try:
            counted = await self.async_update_heat()
        except Exception:
            _LOGGER.exception(
                "Road heat map update failed for VIN %s (non-fatal)", self.vin
            )
            return 0
        if counted and fire_event:
            self.hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": self.vin})
        return counted

    # -- road-snapped gap filling -------------------------------------------------

    def _async_seed_snap_gaps_once(self) -> None:
        """Schedule a one-time background gap-snap catch-up after this store first loads.

        Mirrors ``_async_seed_heat_once``: guarded so it only ever runs once
        per store instance, and covers routes stored before this feature
        existed without blocking setup.
        """
        if self._gap_snap_seeded or self.is_demo:
            return
        self._gap_snap_seeded = True
        self.hass.async_create_background_task(
            self._async_snap_gaps_safe(seed=True),
            name=f"rivian gap snap seed {self.vin}",
        )

    async def _async_snap_gaps_safe(self, seed: bool = False) -> None:
        """Best-effort: snap this VIN's unresolved GPS gaps; never raises.

        When `seed` is set (the once-after-load catch-up only) and any fill
        landed on a drive whose heat was already counted, a plain
        ``update_heat()`` would skip that drive (it only counts drives not
        yet in road_heat_drives), so its month is recounted from scratch
        once here instead of on every future seed run.
        """
        try:
            result = await self.async_snap_gaps()
            if seed and result.get("heat_stale_drives"):
                await self.async_rebuild_heat()
            if result.get("added"):
                self.hass.bus.async_fire(
                    RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": self.vin}
                )
        except Exception:
            _LOGGER.exception(
                "Road gap snapping failed for VIN %s (non-fatal)", self.vin
            )

    async def async_snap_gaps(self) -> dict[str, Any]:
        """Fill recorded GPS gaps by snapping them onto OSM roads via Overpass.

        Processes ``gaps_to_snap`` in batches: for each gap, a bbox is
        computed and its OSM road data is fetched (or reused from cache),
        then the gap is snapped in the executor (pure CPU). A successful
        snap is saved as a fill; a definitive no-path result is saved as a
        'none' record so it isn't retried forever; a network/parse failure
        from Overpass leaves the gap unresolved entirely (retried on the
        next pass, live or seeded). Returns ``{"added": <fills written>,
        "heat_stale_drives": <drive_ids already counted in road heat that got
        a new fill>}``. Callers decide whether/when to fire
        ``RIVIAN_ANALYTICS_UPDATED_EVENT`` and rebuild heat for stale drives.
        """
        if not self._loaded:
            await self.async_load()
        added = 0
        heat_stale: set[str] = set()
        # A gap left unresolved by a network/parse failure (see below) keeps
        # no track_fills row, so gaps_to_snap would return it again on every
        # pass -- track what this call has already attempted so it can't
        # loop forever on a persistently-unreachable Overpass instance.
        attempted: set[tuple[str, float]] = set()
        while True:
            raw_batch = await self.hass.async_add_executor_job(
                self._db.gaps_to_snap, self.vin, SNAP_GAPS_BATCH_LIMIT
            )
            batch = [
                (drive_id, gap)
                for drive_id, gap in raw_batch
                if (drive_id, gap.start.t) not in attempted
            ]
            if not batch:
                break
            for drive_id, gap in batch:
                attempted.add((drive_id, gap.start.t))
            drive_ids = list({drive_id for drive_id, _gap in batch})
            already_counted = await self.hass.async_add_executor_job(
                self._db.drives_counted_in_heat, self.vin, drive_ids
            )
            for drive_id, gap in batch:
                bbox = road_snap.gap_bbox(gap)
                if road_snap.bbox_area_m2(bbox) > road_snap.MAX_BBOX_AREA_M2:
                    await self.hass.async_add_executor_job(
                        self._db.save_track_fill,
                        self.vin,
                        drive_id,
                        gap.start.t,
                        [],
                        "none",
                    )
                    continue

                key = road_snap.bbox_key(bbox)
                ways = await self.hass.async_add_executor_job(
                    self._db.get_cached_roads, key
                )
                if ways is None:
                    ways = await road_snap.async_fetch_roads(self.hass, bbox)
                    if ways is None:
                        # Network/parse failure: not a definitive "no road
                        # here", so leave it unresolved rather than record
                        # 'none' -- the next pass will try again.
                        continue
                    await self.hass.async_add_executor_job(
                        self._db.save_cached_roads, key, ways
                    )

                points = await self.hass.async_add_executor_job(
                    road_snap.snap_gap, gap, ways
                )
                if points:
                    await self.hass.async_add_executor_job(
                        self._db.save_track_fill,
                        self.vin,
                        drive_id,
                        gap.start.t,
                        points,
                        "osm",
                    )
                    added += 1
                    if drive_id in already_counted:
                        heat_stale.add(drive_id)
                else:
                    await self.hass.async_add_executor_job(
                        self._db.save_track_fill,
                        self.vin,
                        drive_id,
                        gap.start.t,
                        [],
                        "none",
                    )
        return {"added": added, "heat_stale_drives": sorted(heat_stale)}

    # -- favorite places -----------------------------------------------------

    def _async_seed_places_once(self) -> None:
        """Schedule a one-time background places seed after this store first loads.

        Mirrors ``_async_seed_heat_once``: syncs HA zones (seeding/removing
        zone places) and then rebuilds auto places from every stored drive,
        so a fresh install or an upgrade across this feature doesn't wait for
        a new drive to get its first places. Guarded so it only ever runs
        once per store instance.
        """
        if self._places_seeded:
            return
        self._places_seeded = True
        self.hass.async_create_background_task(
            self._async_seed_places_safe(),
            name=f"rivian places seed {self.vin}",
        )

    async def _async_seed_places_safe(self) -> None:
        """Best-effort: sync zones, rebuild places, then geocode; never raises.

        Fires the update event only when the rebuild actually produced (or
        kept) at least one place -- a brand-new VIN with no zones and no
        drives yet has nothing worth refreshing a card for. Routes depend on
        places, so their own one-time seed runs right after, in this same
        task (see ``_async_seed_routes_safe``).
        """
        # Places and routes are shared by every vehicle in the dataset, so
        # only the first store to load seeds them (once per database).
        if not self._db.claim_once(f"places_seed:{self.dataset}"):
            self._routes_seeded = True
            return
        try:
            if not self.is_demo:
                zones = read_zone_states(self.hass)
                result = await self.async_sync_zones(zones)
                if result.get("places"):
                    self._fire_dataset_updated()
            else:
                result = await self.async_rebuild_places()
                if result.get("places"):
                    self._fire_dataset_updated()
            await self.async_geocode_places()
        except Exception:
            _LOGGER.exception("Places seed failed for VIN %s (non-fatal)", self.vin)
        await self._async_seed_routes_safe()

    async def _async_geocode_places_safe(self) -> None:
        """Best-effort: geocode any places newly due for it; never raises."""
        try:
            await self.async_geocode_places()
        except Exception:
            _LOGGER.exception("Place geocoding failed for VIN %s (non-fatal)", self.vin)

    async def async_sync_zones(self, zones: list[dict[str, Any]]) -> dict[str, int]:
        """Upsert HA zones as places and rebuild (see AnalyticsDatabase.sync_zones).

        Does not itself fire the update event -- callers that trigger this
        directly (the zone-change listener, a deliberate rebuild) fire it;
        the background first-load seed fires conditionally (see
        ``_async_seed_places_safe``).
        """
        if self.is_demo:
            # Privacy: demo vehicles never see the user's real zones.
            return {"places": 0}
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.sync_zones, self.dataset, zones
        )

    async def async_rebuild_places(self) -> dict[str, int]:
        """Re-cluster auto places and reassign every drive (see AnalyticsDatabase.rebuild_places).

        Does not itself fire the update event; callers (the service, the
        WebSocket command, a post-backfill rebuild) fire it after a
        successful call, since this is always an explicit, low-frequency
        action.
        """
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.rebuild_places, self.dataset
        )

    def _fire_dataset_updated(self) -> None:
        """Fire the update event for every vehicle sharing this store's dataset.

        Places and routes belong to the whole dataset, so an edit refreshes
        the cards of every selected vehicle, not just this store's.
        """
        domain_data = self.hass.data.get(DOMAIN, {})
        vins: dict[str, None] = {self.vin: None}
        if self.is_demo:
            for vin in domain_data.get(ATTR_DEMO_STORES) or {}:
                vins[vin] = None
        else:
            for entry_data in domain_data.values():
                if not isinstance(entry_data, dict):
                    continue
                for store in (entry_data.get(ATTR_DRIVE_STORE) or {}).values():
                    vins[store.vin] = None
        for vin in vins:
            self.hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": vin})

    async def async_list_places(
        self, vins: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return the dataset's places with visit counts (per vehicle) and last-visit.

        With ``vins``, counts cover only those vehicles and the list is
        filtered to the places they visit.
        """
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.list_places, self.dataset, vins
        )

    async def async_update_place(self, place_id: int, **fields: Any) -> None:
        """Update a place's editable fields (see AnalyticsDatabase.update_place)."""
        if not self._loaded:
            await self.async_load()
        await self.hass.async_add_executor_job(
            functools.partial(self._db.update_place, self.dataset, place_id, **fields)
        )
        self._fire_dataset_updated()

    async def async_create_place(
        self,
        lat: float,
        lon: float,
        name: str,
        radius_m: float | None = None,
        category: str | None = None,
    ) -> int:
        """Create a user-defined place (see AnalyticsDatabase.create_place)."""
        if not self._loaded:
            await self.async_load()
        place_id = await self.hass.async_add_executor_job(
            self._db.create_place, self.dataset, lat, lon, name, radius_m, category
        )
        self._fire_dataset_updated()
        return place_id

    async def async_merge_places(self, into: int, place_ids: list[int]) -> None:
        """Merge places into one (see AnalyticsDatabase.merge_places)."""
        if not self._loaded:
            await self.async_load()
        await self.hass.async_add_executor_job(
            self._db.merge_places, self.dataset, into, place_ids
        )
        self._fire_dataset_updated()

    async def async_geocode_places(self) -> dict[str, int]:
        """Reverse-geocode places still due for it, respecting the place_geocoding option.

        Processes at most ``PLACES_GEOCODE_DEFAULT_LIMIT`` places per call (a
        new background task is scheduled after every finalize and seed, so a
        large backlog drains over successive calls rather than one long run).
        """
        if not self._place_geocoding:
            return {"geocoded": 0}
        if not self._loaded:
            await self.async_load()
        candidates = await self.hass.async_add_executor_job(
            self._db.places_needing_geocode, self.dataset, PLACES_GEOCODE_DEFAULT_LIMIT
        )
        if not candidates:
            return {"geocoded": 0}
        geocoded = 0
        now_ts = datetime.now(timezone.utc).timestamp()
        for candidate in candidates:
            name = await geocode.async_reverse(
                self.hass, candidate["lat"], candidate["lon"]
            )
            await self.hass.async_add_executor_job(
                self._db.save_geocode, self.dataset, candidate["place_id"], name, now_ts
            )
            if name:
                geocoded += 1
        if geocoded:
            self._fire_dataset_updated()
        return {"geocoded": geocoded}

    # -- favorite drives (repeated routes) ------------------------------------

    async def _async_seed_routes_safe(self) -> None:
        """Best-effort one-time background routes seed; never raises.

        Called once, right after the places seed completes (routes depend on
        places), from ``_async_seed_places_safe``. Guarded so it only ever
        runs once per store instance, like ``_async_seed_heat_once``.
        """
        if self._routes_seeded:
            return
        self._routes_seeded = True
        try:
            result = await self.async_rebuild_routes()
            if result.get("routes"):
                self._fire_dataset_updated()
        except Exception:
            _LOGGER.exception("Routes seed failed for VIN %s (non-fatal)", self.vin)

    async def _async_rebuild_routes_safe(self) -> None:
        """Best-effort background routes rebuild after a finalize; never raises.

        Only fires the update event when the rebuild actually produced (or
        kept) at least one route -- most finalizes don't change which pairs
        clear the route threshold, so firing unconditionally would spam a
        refresh for every single drive.
        """
        try:
            result = await self.async_rebuild_routes()
            if result.get("routes"):
                self._fire_dataset_updated()
            _LOGGER.debug("Rebuilt routes for VIN %s: %s", self.vin, result)
        except Exception:
            _LOGGER.exception(
                "Post-finalize routes rebuild failed for VIN %s (non-fatal)", self.vin
            )

    async def async_rebuild_routes(self) -> dict[str, int]:
        """Re-group routes/variants and reassign every drive (see AnalyticsDatabase.rebuild_routes).

        Does not itself fire the update event; callers (the service, the
        WebSocket command, the one-time seed, the post-finalize refresh) fire
        it after a successful call.
        """
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.rebuild_routes, self.dataset
        )

    async def async_list_routes(
        self, vins: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return the dataset's routes with stored stats.

        Without ``vins``, by total drive count; with ``vins``, only routes
        those vehicles drove, ordered by their own drive count.
        """
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.list_routes, self.dataset, vins
        )

    async def async_route_detail(
        self, route_id: int, vins: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Return one route's stats plus every drive's summary and preview polyline."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.route_detail, self.dataset, route_id, vins
        )

    async def async_rename_route(self, route_id: int, name: str | None) -> None:
        """Set (or clear) a route's display name override (see AnalyticsDatabase.rename_route)."""
        if not self._loaded:
            await self.async_load()
        await self.hass.async_add_executor_job(
            self._db.rename_route, self.dataset, route_id, name
        )
        self._fire_dataset_updated()

    # -- delete, with confirmation (caller asks first; see the frontend cards) ----

    async def async_delete_drive(self, drive_id: str) -> dict[str, Any]:
        """Delete one drive and everything derived from it; rewrite statistics."""
        if not self._loaded:
            await self.async_load()
        result = await self.hass.async_add_executor_job(
            self._db.delete_drives, self.vin, [drive_id]
        )
        await self._async_post_delete(result)
        return result

    async def async_delete_day(self, tz: tzinfo, day: date) -> dict[str, Any]:
        """Delete every drive on one local calendar day; rewrite statistics."""
        if not self._loaded:
            await self.async_load()
        result = await self.hass.async_add_executor_job(
            self._db.delete_day, self.vin, tz, day
        )
        await self._async_post_delete(result)
        return result

    async def _async_post_delete(self, result: dict[str, Any]) -> None:
        """Shared tail of a drive/day delete: rewrite statistics, refresh, fire event."""
        affected_hours = result.get("affected_hours") or []
        if affected_hours:
            await async_rewrite_statistics(self.hass, self, min(affected_hours))
        await self.async_refresh_cache()
        self.hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": self.vin})

    async def async_delete_place(self, place_id: int) -> dict[str, Any]:
        """Delete (or hide, for an auto suggestion) a place (see AnalyticsDatabase.delete_place)."""
        if not self._loaded:
            await self.async_load()
        result = await self.hass.async_add_executor_job(
            self._db.delete_place, self.dataset, place_id
        )
        self._fire_dataset_updated()
        return result

    async def async_delete_dcfc_session(self, session_id: str) -> int:
        """Delete one DC fast-charge session; return rows removed (0 or 1)."""
        if not self._loaded:
            await self.async_load()
        removed = await self.hass.async_add_executor_job(
            self._db.delete_dcfc_session, self.vin, session_id
        )
        if removed:
            await self.async_refresh_cache()
            self.hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": self.vin})
        return removed

    async def async_delete_vehicle_history(self) -> None:
        """Delete all analytics data for this VIN and clear its long-term statistics.

        Used by the Overview tab's "Delete vehicle history" action. A real
        vehicle keeps recording new drives afterward; a demo vehicle's
        removal (Step B) also drops it from the registry and dashboard.
        """
        if not self._loaded:
            await self.async_load()
        await self.async_reset()
        async_clear_statistics(self.hass, self.vin)
        # Places and routes outlive the vehicle; refresh them so a place only
        # this car visited (and nobody named) drops off and route counts update.
        await self.hass.async_add_executor_job(self._db.rebuild_places, self.dataset)
        await self.hass.async_add_executor_job(self._db.rebuild_routes, self.dataset)
        self._fire_dataset_updated()

    # -- diagnostics / test surface -----------------------------------------------

    async def async_get_stats(
        self, days: int | None = None, reference_time: datetime | None = None
    ) -> AggregatedDriveStats:
        """Compute rolling or all-time stats directly from SQLite, bypassing the cache."""
        if not self._loaded:
            await self.async_load()
        now_ts = self._reference_ts(reference_time)
        return await self.hass.async_add_executor_job(
            self._db.window_stats, self.vin, days, now_ts
        )

    async def async_prune(self, days: int) -> int:
        """Prune drives/vampire events older than the given retention window; return rows removed."""
        if not self._loaded:
            await self.async_load()
        cutoff_ts = datetime.now(timezone.utc).timestamp() - (days * 86400.0)
        affected = await self.hass.async_add_executor_job(
            self._db.prune, self.vin, cutoff_ts
        )
        await self.async_refresh_cache()
        return affected

    async def async_refresh_cache(self) -> None:
        """Rebuild the in-memory hot cache from SQLite and bump the revision counter."""
        now_ts = datetime.now(timezone.utc).timestamp()
        raw_cache = await self.hass.async_add_executor_job(
            self._db.build_cache, self.vin, now_ts
        )
        self._revision += 1
        self._cache = dataclasses.replace(raw_cache, revision=self._revision)

    @staticmethod
    def _reference_ts(reference_time: datetime | None) -> float:
        """Normalize an optional reference datetime to a POSIX epoch float (default: now)."""
        if reference_time is None:
            return datetime.now(timezone.utc).timestamp()
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone.utc)
        return reference_time.timestamp()
