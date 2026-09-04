"""Rivian Trip Efficiency & Analytics Storage Manager."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers.storage import Store

from .drive_models import (
    MICRO_DRIVE_THRESHOLD_MILES,
    MPGE_FACTOR,
    AggregatedDriveStats,
    DriveRecord,
    VampireDrainRecord,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY_PREFIX: Final[str] = "rivian_drives"
STORAGE_VERSION: Final[int] = 1
STORAGE_MINOR_VERSION: Final[int] = 1


def _parse_iso_timestamp(ts_str: str) -> datetime | None:
    """Parse ISO-8601 timestamp string into timezone-aware datetime."""
    if not ts_str:
        return None
    try:
        normalized = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


class DriveStore:
    """Isolated JSON storage manager for Rivian drive records."""

    def __init__(self, hass: HomeAssistant, vin: str) -> None:
        """Initialize DriveStore for a specific vehicle VIN."""
        self.hass = hass
        self.vin = vin
        self.key = f"{STORAGE_KEY_PREFIX}_{vin}.json"
        self._store: Store[dict[str, Any]] = Store(
            hass,
            version=STORAGE_VERSION,
            key=self.key,
            minor_version=STORAGE_MINOR_VERSION,
        )
        self._drives: list[DriveRecord] = []
        self._drives_by_id: dict[str, DriveRecord] = {}
        self._vampire_events: list[VampireDrainRecord] = []
        self._loaded: bool = False

    @property
    def drives(self) -> list[DriveRecord]:
        """Return cached drive records."""
        return list(self._drives)

    @property
    def vampire_events(self) -> list[VampireDrainRecord]:
        """Return cached vampire drain records."""
        return list(self._vampire_events)

    @property
    def is_loaded(self) -> bool:
        """Return whether storage has been loaded from disk."""
        return self._loaded

    async def async_load(self) -> list[DriveRecord]:
        """Load drive records from isolated JSON storage."""
        data = await self._store.async_load()
        self._drives = []
        self._drives_by_id = {}
        self._vampire_events = []

        if data is None:
            _LOGGER.debug("No existing drive storage found for VIN %s", self.vin)
            self._loaded = True
            return []

        if isinstance(data, dict):
            raw_drives = data.get("drives", [])
            raw_vampire = data.get("vampire_events", [])
        elif isinstance(data, list):
            raw_drives = data
            raw_vampire = []
        else:
            raw_drives = []
            raw_vampire = []

        for raw_drive in raw_drives:
            if isinstance(raw_drive, dict):
                drive = DriveRecord.from_dict(raw_drive)
                self._drives.append(drive)
                self._drives_by_id[drive.drive_id] = drive

        for raw_v in raw_vampire:
            if isinstance(raw_v, dict):
                self._vampire_events.append(VampireDrainRecord.from_dict(raw_v))

        self._loaded = True
        _LOGGER.debug(
            "Loaded %d drive records and %d vampire events for VIN %s",
            len(self._drives),
            len(self._vampire_events),
            self.vin,
        )
        return list(self._drives)

    async def async_get_drives(self, min_distance: float = 0.0) -> list[DriveRecord]:
        """Get drive records, optionally filtering by minimum distance."""
        if not self._loaded:
            await self.async_load()

        if min_distance > 0.0:
            return [d for d in self._drives if d.distance_miles >= min_distance]
        return list(self._drives)

    async def async_save_drive(self, drive: DriveRecord) -> bool:
        """Save or update a drive record with deduplication by drive_id."""
        if not self._loaded:
            await self.async_load()

        if drive.drive_id in self._drives_by_id:
            existing_index = next(
                (i for i, d in enumerate(self._drives) if d.drive_id == drive.drive_id),
                None,
            )
            if existing_index is not None:
                self._drives[existing_index] = drive
            self._drives_by_id[drive.drive_id] = drive
            _LOGGER.debug(
                "Updated existing drive record %s for VIN %s",
                drive.drive_id,
                self.vin,
            )
        else:
            self._drives.append(drive)
            self._drives_by_id[drive.drive_id] = drive
            _LOGGER.debug(
                "Appended new drive record %s for VIN %s",
                drive.drive_id,
                self.vin,
            )

        await self._async_persist()
        return True

    async def async_save_drives_batch(self, drives: list[DriveRecord]) -> int:
        """Batch save drive records with deduplication, returning count of newly added drives."""
        if not self._loaded:
            await self.async_load()

        new_drives_count = 0
        for drive in drives:
            if drive.drive_id in self._drives_by_id:
                existing_index = next(
                    (
                        i
                        for i, d in enumerate(self._drives)
                        if d.drive_id == drive.drive_id
                    ),
                    None,
                )
                if existing_index is not None:
                    self._drives[existing_index] = drive
                self._drives_by_id[drive.drive_id] = drive
            else:
                self._drives.append(drive)
                self._drives_by_id[drive.drive_id] = drive
                new_drives_count += 1

        if drives:
            await self._async_persist()

        _LOGGER.debug(
            "Batch saved %d drives (%d new) for VIN %s",
            len(drives),
            new_drives_count,
            self.vin,
        )
        return new_drives_count

    def get_stats_30d(
        self, reference_time: datetime | None = None
    ) -> AggregatedDriveStats:
        """Calculate rolling 30-day weighted efficiency stats across non-micro drives."""
        if reference_time is None:
            reference_time = datetime.now(timezone.utc)
        elif reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone.utc)

        cutoff_time = reference_time - timedelta(days=30)

        valid_drives: list[DriveRecord] = []
        for drive in self._drives:
            if (
                drive.is_micro_drive
                or drive.distance_miles < MICRO_DRIVE_THRESHOLD_MILES
            ):
                continue

            drive_dt = _parse_iso_timestamp(drive.start_time)
            if drive_dt is None:
                drive_dt = _parse_iso_timestamp(drive.end_time)

            if drive_dt is not None and cutoff_time <= drive_dt <= reference_time:
                valid_drives.append(drive)

        return self._calculate_aggregated_stats(valid_drives)

    def get_stats_all_time(self) -> AggregatedDriveStats:
        """Calculate all-time weighted efficiency stats across non-micro drives."""
        valid_drives = [
            d
            for d in self._drives
            if not d.is_micro_drive and d.distance_miles >= MICRO_DRIVE_THRESHOLD_MILES
        ]
        return self._calculate_aggregated_stats(valid_drives)

    def _calculate_aggregated_stats(
        self, drives: list[DriveRecord]
    ) -> AggregatedDriveStats:
        """Calculate weighted efficiency and MPGe across a slice of drives."""
        total_miles = sum(d.distance_miles for d in drives)
        total_kwh = sum(d.energy_kwh for d in drives)
        total_duration = sum(d.duration_seconds for d in drives)
        drive_count = len(drives)

        efficiency = total_miles / total_kwh if total_kwh > 0.0 else 0.0
        mpge = efficiency * MPGE_FACTOR
        avg_distance = total_miles / drive_count if drive_count > 0 else 0.0

        micro_count = sum(
            1
            for d in self._drives
            if d.is_micro_drive or d.distance_miles < MICRO_DRIVE_THRESHOLD_MILES
        )

        return AggregatedDriveStats(
            total_miles=round(total_miles, 2),
            total_kwh=round(total_kwh, 2),
            efficiency_mi_kwh=round(efficiency, 2),
            mpge=round(mpge, 2),
            drive_count=drive_count,
            total_duration_seconds=round(total_duration, 1),
            avg_distance_miles=round(avg_distance, 2),
            total_micro_drives=micro_count,
        )

    async def async_save_vampire_events(
        self, events: list[VampireDrainRecord]
    ) -> None:
        """Save vampire drain records to storage."""
        if not self._loaded:
            await self.async_load()
        self._vampire_events = list(events)
        await self._async_persist()

    async def async_append_vampire_event(
        self, event: VampireDrainRecord
    ) -> None:
        """Append a single vampire drain record to storage."""
        if not self._loaded:
            await self.async_load()
        self._vampire_events.append(event)
        await self._async_persist()

    async def async_reset(self) -> None:
        """Reset in-memory records and delete storage file with zero database footprint."""
        self._drives = []
        self._drives_by_id = {}
        self._vampire_events = []
        self._loaded = True
        await self._store.async_remove()
        _LOGGER.info(
            "Reset drive storage and removed storage file for VIN %s", self.vin
        )

    async def _async_persist(self) -> None:
        """Persist current drive records and summary to disk atomically."""
        stats_all = self.get_stats_all_time()
        micro_count = sum(
            1
            for d in self._drives
            if d.is_micro_drive or d.distance_miles < MICRO_DRIVE_THRESHOLD_MILES
        )

        payload: dict[str, Any] = {
            "vin": self.vin,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "all_time_miles": stats_all.total_miles,
                "all_time_kwh": stats_all.total_kwh,
                "all_time_efficiency": stats_all.efficiency_mi_kwh,
                "all_time_mpge": stats_all.mpge,
                "total_valid_drives": stats_all.drive_count,
                "total_micro_drives": micro_count,
            },
            "drives": [d.to_dict() for d in self._drives],
            "vampire_events": [v.to_dict() for v in self._vampire_events],
        }
        await self._store.async_save(payload)

