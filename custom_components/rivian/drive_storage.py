"""Rivian Trip Efficiency & Analytics Storage Manager.

DriveStore is a thin async wrapper around AnalyticsDatabase: every mutation does
an executor round-trip to SQLite and then rebuilds a small in-memory HotCache,
which is what entities read synchronously on every state update. No unbounded
in-memory list survives a full drive history any more.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers.storage import Store

from .analytics_db import ActiveCheckpoint, AnalyticsDatabase, HotCache
from .const import RIVIAN_ANALYTICS_UPDATED_EVENT
from .drive_models import (
    AggregatedDriveStats,
    ChargingSessionRecord,
    DriveRecord,
    VampireDrainRecord,
)
from .drive_track import DriveTrack

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

LEGACY_STORAGE_KEY_PREFIX: Final[str] = "rivian_drives"
LEGACY_STORAGE_VERSION: Final[int] = 1
LEGACY_STORAGE_MINOR_VERSION: Final[int] = 1


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
    ) -> None:
        """Initialize DriveStore for a specific vehicle VIN against a shared AnalyticsDatabase."""
        self.hass = hass
        self.vin = vin
        self._db = db
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
        self._stats_recompute_seeded = False

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
        self._async_maybe_recompute_stats_once()

    def _async_maybe_recompute_stats_once(self) -> None:
        """Schedule a one-time background stats recompute if any drive needs it.

        Guarded so it only ever runs once per store instance. A drive needs this right after the v6
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

    async def async_recompute_stats(self) -> dict[str, int]:
        """Recompute track-derived summary stats for every drive with a stored track.

        Used by the history backfill (after it writes routes) and by the
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
