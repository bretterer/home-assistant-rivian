"""Rivian Trip Efficiency & Analytics Storage Manager.

DriveStore is a thin async wrapper around AnalyticsDatabase: every mutation does
an executor round-trip to SQLite and then rebuilds a small in-memory HotCache,
which is what entities read synchronously on every state update. No unbounded
in-memory list survives a full drive history any more.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, date, datetime, tzinfo
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import road_snap
from .analytics_db import (
    ENERGY_MODEL_MIN_DRIVES,
    ENERGY_MODEL_WINDOW_DAYS,
    ActiveCheckpoint,
    AnalyticsDatabase,
    HotCache,
    VehiclePicture,
)
from .const import RIVIAN_ANALYTICS_UPDATED_EVENT
from .drive_models import (
    AggregatedDriveStats,
    ChargingSessionRecord,
    DriveRecord,
    VampireDrainRecord,
)
from .drive_track import DriveTrack
from .energy_model import EnergyModelParams
from .statistics import async_clear_statistics, async_rewrite_statistics
from .weather import OpenMeteoWeatherClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

LEGACY_STORAGE_KEY_PREFIX: Final[str] = "rivian_drives"
LEGACY_STORAGE_VERSION: Final[int] = 1
LEGACY_STORAGE_MINOR_VERSION: Final[int] = 1
SNAP_GAPS_BATCH_LIMIT: Final[int] = 20


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


class DriveStore:
    """SQLite-backed drive analytics store for a single vehicle, with a hot cache."""

    def __init__(
        self,
        hass: HomeAssistant,
        vin: str,
        db: AnalyticsDatabase,
        weather_lookups: bool = True,
    ) -> None:
        """Initialize DriveStore for a specific vehicle VIN against a shared AnalyticsDatabase."""
        self.hass = hass
        self.vin = vin
        self._db = db
        # The weather_lookups option: off sends nothing to Open-Meteo
        self.weather_lookups = weather_lookups
        self._weather_client: OpenMeteoWeatherClient | None = None
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

    # -- weather ---------------------------------------------------------------

    def get_weather_client(self) -> OpenMeteoWeatherClient:
        """Return the Open-Meteo client, honouring the weather_lookups option."""
        if self._weather_client is None:
            self._weather_client = OpenMeteoWeatherClient(
                hass=self.hass, enabled=self.weather_lookups
            )
        return self._weather_client

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

    async def async_charging_session_intervals(self) -> list[tuple[float, float]]:
        """Return every stored charging session's (start_ts, end_ts)."""
        if not self._loaded:
            await self.async_load()
        return await self.hass.async_add_executor_job(
            self._db.charging_session_intervals, self.vin
        )

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
        now_ts = datetime.now(UTC).timestamp()
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
        now_ts = datetime.now(UTC).timestamp()
        return await self.hass.async_add_executor_job(
            self._db.series_window, self.vin, days, now_ts
        )

    async def async_drives_since(
        self, from_ts: float, now_ts: float | None = None
    ) -> list[DriveRecord]:
        """Return every drive with sort_ts in [from_ts, now_ts], ascending, uncapped.

        Used by ``statistics.async_rewrite_statistics`` after a delete or a
        backfill.
        """
        if not self._loaded:
            await self.async_load()
        if now_ts is None:
            now_ts = datetime.now(UTC).timestamp()
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
        if self._gap_snap_seeded:
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

    async def async_delete_vehicle_history(self) -> None:
        """Delete all analytics data for this VIN and clear its long-term statistics.

        Used by the Overview tab's "Delete vehicle history" action. The
        vehicle keeps recording new drives afterward.
        """
        if not self._loaded:
            await self.async_load()
        await self.async_reset()
        async_clear_statistics(self.hass, self.vin)
        self.hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": self.vin})

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
        cutoff_ts = datetime.now(UTC).timestamp() - (days * 86400.0)
        affected = await self.hass.async_add_executor_job(
            self._db.prune, self.vin, cutoff_ts
        )
        await self.async_refresh_cache()
        return affected

    async def async_refresh_cache(self) -> None:
        """Rebuild the in-memory hot cache from SQLite and bump the revision counter."""
        now_ts = datetime.now(UTC).timestamp()
        raw_cache = await self.hass.async_add_executor_job(
            self._db.build_cache, self.vin, now_ts
        )
        self._revision += 1
        self._cache = dataclasses.replace(raw_cache, revision=self._revision)

    @staticmethod
    def _reference_ts(reference_time: datetime | None) -> float:
        """Normalize an optional reference datetime to a POSIX epoch float (default: now)."""
        if reference_time is None:
            return datetime.now(UTC).timestamp()
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=UTC)
        return reference_time.timestamp()
