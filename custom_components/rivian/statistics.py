"""Long-term statistics integration for Rivian drive data.

Pushes per-drive energy and distance into Home Assistant's long-term
statistics store (via the recorder's *external* statistics API), which is
never purged by the recorder's own retention window. This lets multi-year
efficiency trends survive regardless of how long this integration itself
retains drive history, and gives users native ``statistics-graph`` cards and
Energy-dashboard compatibility for free.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import re
from typing import TYPE_CHECKING, Any, Final, cast

from homeassistant.components import recorder
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .drive_models import MICRO_DRIVE_THRESHOLD_MILES, DriveRecord
from .helpers import redact_vin

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .drive_storage import DriveStore

_LOGGER = logging.getLogger(__name__)

# HA 2026.11 requires `mean_type`/`unit_class` in StatisticMetaData and drops
# `has_mean`. Import defensively so this module keeps working, unchanged,
# against both the current and the future recorder models.
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _HAS_STATISTIC_MEAN_TYPE: Final = True
except ImportError:  # pragma: no cover - depends on installed HA version
    StatisticMeanType = None  # type: ignore[assignment,misc]
    _HAS_STATISTIC_MEAN_TYPE: Final = False

_VIN_INVALID_CHARS_RE: Final = re.compile(r"[^a-z0-9_]+")
_UNDERSCORE_RUN_RE: Final = re.compile(r"_+")

_UNIT_ENERGY_KWH: Final = "kWh"
_UNIT_DISTANCE_MI: Final = "mi"
_UNIT_EFFICIENCY: Final = "mi/kWh"
_UNIT_MPGE: Final = "MPGe"


@dataclass
class _HourBucket:
    """Accumulated drive totals for a single hourly statistics bucket."""

    start: datetime
    energy_kwh: float = 0.0
    distance_miles: float = 0.0
    weighted_efficiency: float = 0.0
    weighted_mpge: float = 0.0

    @property
    def efficiency_mi_kwh(self) -> float:
        """Return the distance-weighted mean efficiency for this bucket."""
        if self.distance_miles <= 0:
            return 0.0
        return self.weighted_efficiency / self.distance_miles

    @property
    def mpge(self) -> float:
        """Return the distance-weighted mean MPGe for this bucket."""
        if self.distance_miles <= 0:
            return 0.0
        return self.weighted_mpge / self.distance_miles


def _sanitize_object_id(vin: str) -> str:
    """Sanitize a VIN into a statistics object_id (``[a-z0-9_]``, no edge/double underscores)."""
    sanitized = _VIN_INVALID_CHARS_RE.sub("_", vin.lower())
    sanitized = _UNDERSCORE_RUN_RE.sub("_", sanitized)
    return sanitized.strip("_")


@dataclass
class _StatIds:
    """The four long-term-statistics ids this integration writes per VIN."""

    energy: str
    distance: str
    efficiency: str
    mpge: str

    def as_tuple(self) -> tuple[str, str, str, str]:
        """Return the four ids as a plain tuple, for iteration/clearing."""
        return (self.energy, self.distance, self.efficiency, self.mpge)


def _stat_ids(vin: str) -> _StatIds:
    """Return this VIN's four statistic ids."""
    object_id = _sanitize_object_id(vin)
    return _StatIds(
        energy=f"{DOMAIN}:{object_id}_energy_kwh",
        distance=f"{DOMAIN}:{object_id}_distance_mi",
        efficiency=f"{DOMAIN}:{object_id}_efficiency",
        mpge=f"{DOMAIN}:{object_id}_mpge",
    )


def _parse_start_time(value: str) -> datetime | None:
    """Parse a drive's ISO-8601 start_time, returning a tz-aware datetime or None."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed.astimezone(dt_util.UTC)


def _bucket_drives(drives: list[DriveRecord]) -> list[_HourBucket]:
    """Group valid drives into hourly buckets, sorted ascending by bucket start."""
    buckets: dict[datetime, _HourBucket] = {}

    for drive in drives:
        if drive.is_micro_drive or drive.distance_miles < MICRO_DRIVE_THRESHOLD_MILES:
            _LOGGER.debug(
                "Skipping micro-drive %s (%.3f mi) for statistics",
                redact_vin(drive.drive_id),
                drive.distance_miles,
            )
            continue
        if drive.energy_kwh <= 0:
            _LOGGER.debug(
                "Skipping drive %s with non-positive energy_kwh=%.3f for statistics",
                redact_vin(drive.drive_id),
                drive.energy_kwh,
            )
            continue

        start = _parse_start_time(drive.start_time)
        if start is None:
            _LOGGER.debug(
                "Skipping drive %s with unparseable start_time=%r for statistics",
                redact_vin(drive.drive_id),
                drive.start_time,
            )
            continue

        bucket_start = start.replace(minute=0, second=0, microsecond=0)
        bucket = buckets.setdefault(bucket_start, _HourBucket(start=bucket_start))
        bucket.energy_kwh += drive.energy_kwh
        bucket.distance_miles += drive.distance_miles
        # Weight efficiency/mpge by distance so a short drive doesn't swing an
        # hour's mean as much as a long one.
        bucket.weighted_efficiency += drive.efficiency_mi_kwh * drive.distance_miles
        bucket.weighted_mpge += drive.mpge * drive.distance_miles

    return sorted(buckets.values(), key=lambda b: b.start)


def _build_metadata(
    statistic_id: str,
    name: str,
    unit: str,
    *,
    is_mean: bool,
    unit_class: str | None,
) -> StatisticMetaData:
    """Build StatisticMetaData compatible with both current and future HA cores."""
    metadata: dict[str, Any] = {
        "has_mean": is_mean,
        "has_sum": not is_mean,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    # `mean_type`/`unit_class` only exist once HA's recorder models support
    # them; `has_mean` is scheduled for removal in HA 2026.11 but is kept here
    # too since it is harmless (a plain dict key) on the versions that still
    # read it.
    if _HAS_STATISTIC_MEAN_TYPE:
        metadata["mean_type"] = (
            StatisticMeanType.ARITHMETIC if is_mean else StatisticMeanType.NONE
        )
        metadata["unit_class"] = unit_class
    return cast(StatisticMetaData, metadata)


def _coerce_stat_start(value: Any) -> datetime | None:
    """Coerce a recorder statistics row's start value into a datetime.

    Recorder versions have varied in whether row timestamps come back as
    `datetime`, a POSIX timestamp, or an ISO string; handle all three so a
    subtle API shift doesn't take down statistics writing.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_util.UTC)
    if isinstance(value, (int, float)):
        return dt_util.utc_from_timestamp(value)
    if isinstance(value, str):
        return _parse_start_time(value)
    return None


_STATS_EPOCH = datetime(2000, 1, 1, tzinfo=dt_util.UTC)


# Look-back windows for the seed query, widest last. Rows are sparse (only
# hours that had drives), so the previous hour or day is often empty; the
# normal case still reads one or a few rows from a short window.
_SEED_WINDOWS: Final = (
    timedelta(days=1),
    timedelta(days=7),
    timedelta(days=60),
    timedelta(days=400),
)


def _last_statistic_sum_before(
    hass: HomeAssistant, statistic_id: str, before: datetime
) -> float:
    """Return the last cumulative sum for statistic_id strictly before `before`.

    This seeds a rewrite that is about to replace every row from `before`
    onward, so it must not read one of the rows being replaced. It queries a
    short window ending at `before` and widens it only while that is empty,
    falling back to the epoch, so history length doesn't make it slow.
    Executor-bound.
    """
    starts = [before - window for window in _SEED_WINDOWS]
    starts.append(_STATS_EPOCH)
    for start in starts:
        # The period end is exclusive, so a row at `before` is never returned.
        result = statistics_during_period(
            hass,
            max(start, _STATS_EPOCH),
            before,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = result.get(statistic_id)
        if rows:
            total = rows[-1].get("sum")
            return float(total) if total is not None else 0.0
    return 0.0


STATISTICS_REWRITE_LOCKS_KEY: Final = "_statistics_rewrite_locks"
# How long a rewrite waits for the recorder to commit its import
RECORDER_COMMIT_TIMEOUT_S: Final = 10.0


async def async_rewrite_statistics(
    hass: HomeAssistant, store: DriveStore, from_hour_ts: float
) -> None:
    """Recompute and re-import long-term statistics for every hour from from_hour_ts on.

    Called after a delete removes one or more drives (any hour that lost a
    drive needs its running sums and distance-weighted means recomputed from
    what's left) and after a backfill adds older ones (appending them would
    restart the running sums). Rewrites every hour from `from_hour_ts` to now
    that either already has a row or still has a drive; an hour that had a
    row but now has no drives left keeps its running sum unchanged (sum
    carried forward, mean written as None -- a gap, not a false 0). Seeds the
    running totals from the last row strictly before `from_hour_ts`, hours before that -- including ones past the
    retention window -- are never touched.

    Never raises: a statistics failure must not break a delete or a backfill.
    """
    vin = store.vin
    if "recorder" not in hass.config.components:
        _LOGGER.debug("Recorder is not loaded; skipping statistics rewrite")
        return

    # One rewrite per VIN at a time: a second one could read the running sums
    # before the first one's queued import is committed, then write lower sums
    # from there on. The locks live under hass.data so they outlast any store.
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(DOMAIN, {}).setdefault(
        STATISTICS_REWRITE_LOCKS_KEY, {}
    )
    async with locks.setdefault(vin, asyncio.Lock()):
        await _async_rewrite_statistics_locked(hass, store, from_hour_ts)


async def _async_rewrite_statistics_locked(
    hass: HomeAssistant, store: DriveStore, from_hour_ts: float
) -> None:
    """Body of async_rewrite_statistics; the caller holds the VIN's lock."""
    vin = store.vin
    try:
        ids = _stat_ids(vin)
        from_dt = dt_util.utc_from_timestamp(from_hour_ts).replace(
            minute=0, second=0, microsecond=0
        )
        now_dt = dt_util.utcnow()
        now_ts = now_dt.timestamp()

        recorder_instance = recorder.get_instance(hass)

        existing = await recorder_instance.async_add_executor_job(
            statistics_during_period,
            hass,
            from_dt,
            None,
            {ids.energy},
            "hour",
            None,
            {"sum"},
        )
        existing_hours: set[datetime] = set()
        for row in existing.get(ids.energy, []):
            start = _coerce_stat_start(row.get("start"))
            if start is not None:
                existing_hours.add(start)

        drives = await store.async_drives_since(from_hour_ts, now_ts)
        buckets = {b.start: b for b in _bucket_drives(drives)}

        hours = sorted(existing_hours | set(buckets.keys()))
        if not hours:
            return

        running_energy = await recorder_instance.async_add_executor_job(
            _last_statistic_sum_before, hass, ids.energy, from_dt
        )
        running_distance = await recorder_instance.async_add_executor_job(
            _last_statistic_sum_before, hass, ids.distance, from_dt
        )

        energy_rows: list[StatisticData] = []
        distance_rows: list[StatisticData] = []
        efficiency_rows: list[StatisticData] = []
        mpge_rows: list[StatisticData] = []

        for hour in hours:
            bucket = buckets.get(hour)
            running_energy += bucket.energy_kwh if bucket else 0.0
            running_distance += bucket.distance_miles if bucket else 0.0
            energy_rows.append(
                {"start": hour, "sum": running_energy, "state": running_energy}
            )
            distance_rows.append(
                {"start": hour, "sum": running_distance, "state": running_distance}
            )
            # An hour whose drives were all deleted gets no mean (a gap in
            # the trend chart), not 0, which would plot as a false dip.
            efficiency_rows.append(
                {"start": hour, "mean": bucket.efficiency_mi_kwh if bucket else None}
            )
            mpge_rows.append({"start": hour, "mean": bucket.mpge if bucket else None})

        vin_upper = vin.upper()
        async_add_external_statistics(
            hass,
            _build_metadata(
                ids.energy,
                f"Rivian {vin_upper} Energy",
                _UNIT_ENERGY_KWH,
                is_mean=False,
                unit_class="energy",
            ),
            energy_rows,
        )
        async_add_external_statistics(
            hass,
            _build_metadata(
                ids.distance,
                f"Rivian {vin_upper} Distance",
                _UNIT_DISTANCE_MI,
                is_mean=False,
                unit_class="distance",
            ),
            distance_rows,
        )
        async_add_external_statistics(
            hass,
            _build_metadata(
                ids.efficiency,
                f"Rivian {vin_upper} Efficiency",
                _UNIT_EFFICIENCY,
                is_mean=True,
                unit_class=None,
            ),
            efficiency_rows,
        )
        async_add_external_statistics(
            hass,
            _build_metadata(
                ids.mpge,
                f"Rivian {vin_upper} MPGe",
                _UNIT_MPGE,
                is_mean=True,
                unit_class=None,
            ),
            mpge_rows,
        )
        # The imports above are only queued. Wait for the recorder to commit
        # them before releasing the lock, so the next rewrite seeds from them.
        # Bounded: a recorder that has already stopped (late in shutdown) would
        # never finish the wait.
        try:
            async with asyncio.timeout(RECORDER_COMMIT_TIMEOUT_S):
                await recorder_instance.async_block_till_done()
        except TimeoutError:
            _LOGGER.debug(
                "Recorder didn't commit the statistics for VIN %s", redact_vin(vin)
            )
    except Exception as err:  # noqa: BLE001 - statistics must never break a delete
        _LOGGER.warning(
            "Failed to rewrite long-term statistics for VIN %s: %s",
            redact_vin(vin),
            err,
        )


def async_clear_statistics(hass: HomeAssistant, vin: str) -> None:
    """Clear all long-term statistics for a VIN (whole-vehicle delete).

    Fire-and-forget: ``async_clear_statistics`` queues the recorder work and
    returns immediately. Never raises.
    """
    if "recorder" not in hass.config.components:
        return
    try:
        ids = _stat_ids(vin)
        recorder.get_instance(hass).async_clear_statistics(list(ids.as_tuple()))
    except Exception as err:  # noqa: BLE001 - statistics must never break a delete
        _LOGGER.warning(
            "Failed to clear long-term statistics for VIN %s: %s", redact_vin(vin), err
        )
