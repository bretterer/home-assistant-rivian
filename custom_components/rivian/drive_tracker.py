"""Rivian Real-Time Drive Tracker & Lifecycle Engine."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from enum import StrEnum
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .charging import coarse_soc_samples, estimate_charge_curve, should_append_soc_point
from .config_flow import CONF_TRACK_CAPTURE, DEFAULT_TRACK_CAPTURE
from .const import DRIVE_MODE_MAP, INVALID_SENSOR_STATES
from .drive_models import (
    AC_SESSION_MAX_SOC_POINTS,
    AC_SESSION_MIN_DURATION_S,
    AC_SESSION_MIN_SOC_GAIN_PCT,
    DCFC_MIN_POWER_KW,
    MICRO_DRIVE_THRESHOLD_MILES,
    MPGE_FACTOR,
    SESSION_KIND_AC,
    SESSION_KIND_DC,
    STANDARD_SPEED_BINS,
    ChargingSample,
    ChargingSessionRecord,
    DriveChunk,
    DriveRecord,
    DriveState,
    DriveStatus,
    SpeedBinData,
    VampireDrainRecord,
    trailer_status_to_bool,
)
from .drive_stats import compute_track_stats
from .drive_storage import DriveStore
from .drive_track import DriveTrack, TrackPoint
from .weather import OpenMeteoWeatherClient, calculate_distance_weighted_temperature

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .analytics_db import ActiveCheckpoint
    from .coordinator import VehicleCoordinator

_LOGGER = logging.getLogger(__name__)

SPEED_GPS_LOCK_THRESHOLD_MPS: Final[float] = 0.89408  # > 2.0 mph in m/s
DISTANCE_GPS_LOCK_THRESHOLD_METERS: Final[float] = 50.0  # > 50 meters
PARK_DEBOUNCE_SECONDS: Final[int] = 60
WEATHER_SAMPLE_INTERVAL_MILES: Final[float] = 15.0
WEATHER_SAMPLE_INTERVAL_SECONDS: Final[float] = 1200.0  # 20 minutes
METERS_PER_MILE: Final[float] = 1609.344
METERS_TO_FEET: Final[float] = 3.28084
MPS_TO_MPH: Final[float] = 2.23693629
KM_TO_MILES: Final[float] = 0.621371
MAX_CHARGE_SOC_POINTS: Final[int] = 2000

# Durability tuning: how often the in-progress drive is checkpointed to SQLite,
# and how stale a checkpoint may be (relative to its updated_ts) before it is
# considered too old to safely resume and is finalized instead.
CHECKPOINT_INTERVAL_SECONDS: Final[float] = 30.0
RESUME_MAX_AGE_SECONDS: Final[float] = 600.0
# A GPS fix older than the drive start by more than this is considered stale
# (left over from before the drive began) and is not captured, unless the
# track is still empty (see _capture_point).
STALE_FIX_MAX_AGE_SECONDS: Final[float] = 120.0
# When the track is still empty, a fix is accepted as the drive's starting
# point even if it predates the drive start, as long as it isn't older than this.
EMPTY_TRACK_MAX_FIX_AGE_SECONDS: Final[float] = 1800.0
# Epoch values above this are assumed to be milliseconds, not seconds.
_EPOCH_MS_THRESHOLD: Final[float] = 1e12


class DriveEvent(StrEnum):
    """Type of drive state change notification dispatched to listeners."""

    LIVE = "live"
    DRIVE_COMPLETE = "drive_complete"


def get_speed_bin_key(speed_mph: float) -> str:
    """Return the speed bin identifier for a given speed in mph."""
    if speed_mph < 0.0:
        return "0-9"
    if speed_mph >= 80.0:
        return "80+"
    bin_lower = int(speed_mph // 10) * 10
    return f"{bin_lower}-{bin_lower + 9}"


class DriveTracker:
    """Real-time vehicle drive lifecycle and efficiency tracking engine."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: VehicleCoordinator,
        vehicle_info: dict[str, Any],
        store: DriveStore,
        weather_client: OpenMeteoWeatherClient | None = None,
    ) -> None:
        """Initialize the DriveTracker."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.vehicle_info = vehicle_info
        self.vin = str(vehicle_info.get("vin", ""))
        self.vehicle_id = str(vehicle_info.get("id", ""))
        self.store = store
        self.weather_client = weather_client or OpenMeteoWeatherClient(hass=hass)

        self.drive_state = DriveState(
            is_driving=False,
            status=DriveStatus.PARKED.value,
        )

        self._unsub_coordinator_listener: Callable[[], None] | None = None
        self._unsub_stop_listener: Callable[[], None] | None = None
        self._park_debounce_unsub: Callable[[], None] | None = None
        self._active_drive: dict[str, Any] | None = None
        self._listeners: list[Callable[[DriveState, DriveEvent], None]] = []
        self._last_gear: str | None = None
        self._park_start_dt: datetime | None = None
        self._park_start_soc: float | None = None
        self._park_lat: float | None = None
        self._park_lon: float | None = None
        self._active_charge_session: dict[str, Any] | None = None

        # GPS route capture + mid-drive checkpointing.
        self._active_track: DriveTrack | None = None
        self._checkpoint_seq: int = 0
        self._checkpointed_points: int = 0
        self._last_checkpoint_dt: datetime | None = None
        self._checkpoint_in_flight: bool = False
        self._checkpoint_task: asyncio.Task[Any] | None = None
        self._finalizing: bool = False
        self._resume_guard_active: bool = False

    @property
    def is_driving(self) -> bool:
        """Return whether vehicle is currently driving."""
        return self.drive_state.is_driving

    @property
    def is_debouncing_park(self) -> bool:
        """Return whether park debounce timer is currently active."""
        return self._park_debounce_unsub is not None

    @property
    def active_charge_session(self) -> dict[str, Any] | None:
        """Return active charging session dictionary if currently charging."""
        return self._active_charge_session

    @property
    def active_drive(self) -> dict[str, Any] | None:
        """Return active drive telemetry dictionary if driving."""
        return self._active_drive

    def _utcnow(self) -> datetime:
        """Return the current UTC time.

        A seam so tests can control the clock (e.g. to deterministically
        exercise the checkpoint interval) without patching the datetime
        module globally.
        """
        return datetime.now(UTC)

    async def async_setup(self) -> None:
        """Set up DriveTracker, load storage, and register coordinator listener."""
        await self.store.async_load()

        resumed_or_finalized = await self._async_restore_checkpoint()

        if not resumed_or_finalized and (last_d := self.store.last_drive):
            try:
                self._park_start_dt = datetime.fromisoformat(last_d.end_time)
                self._park_start_soc = last_d.end_soc
                self._park_lat = last_d.end_lat
                self._park_lon = last_d.end_lon
            except (ValueError, TypeError):
                pass

        self._unsub_coordinator_listener = self.coordinator.async_add_listener(
            self.handle_coordinator_update
        )
        self._unsub_stop_listener = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_handle_hass_stop
        )
        self.handle_coordinator_update()
        _LOGGER.info(
            "DriveTracker initialized for VIN %s (%s drives loaded)",
            self.vin,
            self.store.drive_count,
        )

    async def _async_restore_checkpoint(self) -> bool:
        """Resume or finalize an in-progress drive left over from a restart.

        Returns True if a checkpoint was found and either resumed or
        finalized (in which case the normal "last drive" park baseline should
        not also be applied, since it was just set by the finalize).
        """
        try:
            cp = await self.store.async_load_checkpoint()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to load drive checkpoint for VIN %s: %s", self.vin, err
            )
            return False

        if cp is None:
            return False

        # Defensive: if the checkpointed drive somehow already exists as a
        # finished drive (e.g. a crash between finalize's commit and the
        # normal checkpoint-clear), just drop the stale checkpoint.
        try:
            existing = await self.store.async_get_drive_detail(cp.drive_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not check for existing drive %s: %s", cp.drive_id, err)
            existing = None
        if existing is not None:
            try:
                await self.store.async_clear_checkpoint()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to clear stale drive checkpoint: %s", err)
            return False

        # A checkpoint that can't be read (e.g. keys from an unknown version)
        # must not stop the tracker from starting: drop it and carry on.
        try:
            self._deserialize_active_drive(cp.state)
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "Discarding unreadable drive checkpoint %s for VIN %s: %s",
                cp.drive_id,
                self.vin,
                err,
            )
            try:
                await self.store.async_clear_checkpoint()
            except Exception as clear_err:  # noqa: BLE001
                _LOGGER.warning("Failed to clear drive checkpoint: %s", clear_err)
            return False

        now_ts = self._utcnow().timestamp()
        age = now_ts - cp.updated_ts
        raw_gear = self.coordinator.get("gearStatus") if self.coordinator.data else None
        gear = str(raw_gear).lower() if raw_gear is not None else None

        if age <= RESUME_MAX_AGE_SECONDS and gear in ("drive", "reverse", "neutral"):
            self._resume_active_drive(cp)
            _LOGGER.info(
                "Resumed in-progress drive %s for VIN %s (checkpoint age %.0fs)",
                cp.drive_id,
                self.vin,
                age,
            )
            return True

        if len(cp.track) > 0:
            as_of = datetime.fromtimestamp(cp.track.points[-1].t, tz=UTC)
        else:
            as_of = datetime.fromtimestamp(cp.updated_ts, tz=UTC)

        self._active_drive = self._deserialize_active_drive(cp.state)
        self._active_track = cp.track
        self._checkpoint_seq = cp.next_seq
        self._checkpointed_points = len(cp.track)
        await self._async_finalize_drive(as_of=as_of)
        _LOGGER.info(
            "Finalized stale/parked in-progress drive %s for VIN %s from checkpoint",
            cp.drive_id,
            self.vin,
        )
        return True

    def _resume_active_drive(self, cp: ActiveCheckpoint) -> None:
        """Restore live tracker state from a checkpoint and continue the drive."""
        self._active_drive = self._deserialize_active_drive(cp.state)
        self._active_track = cp.track
        self._checkpoint_seq = cp.next_seq
        self._checkpointed_points = len(cp.track)
        self._last_checkpoint_dt = datetime.fromtimestamp(cp.updated_ts, tz=UTC)
        self._resume_guard_active = True

        active = self._active_drive
        self.drive_state = DriveState(
            is_driving=True,
            status=DriveStatus.DRIVING.value,
            current_trip_distance_mi=round(active.get("distance_miles", 0.0), 2),
            gps_locked=bool(active.get("gps_locked", False)),
        )

    async def _async_handle_hass_stop(self, _event: Any = None) -> None:
        """Flush a final checkpoint on Home Assistant shutdown.

        Config entries are NOT unloaded on HA shutdown, so async_unload's
        flush never runs in that path; this is the restart durability path.
        """
        await self._async_flush_checkpoint()

    async def async_unload(self) -> None:
        """Clean up DriveTracker listeners and pending debounce timers."""
        if self._park_debounce_unsub is not None:
            self._park_debounce_unsub()
            self._park_debounce_unsub = None

        if self._unsub_stop_listener is not None:
            self._unsub_stop_listener()
            self._unsub_stop_listener = None

        if self._unsub_coordinator_listener is not None:
            self._unsub_coordinator_listener()
            self._unsub_coordinator_listener = None

        await self._async_flush_checkpoint()

        _LOGGER.debug("DriveTracker unloaded for VIN %s", self.vin)

    async def _async_flush_checkpoint(self) -> None:
        """Persist a final checkpoint for the active drive, if any. Never raises."""
        if self._active_drive is None:
            return
        try:
            if self._checkpoint_task is not None:
                try:
                    await self._checkpoint_task
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Pending checkpoint failed during flush: %s", err)
                self._checkpoint_task = None

            active = self._active_drive
            if active is None:
                return
            drive_id = f"{self.vin}_{active['start_epoch']}"
            state = self._serialize_active_drive(active)
            new_points = DriveTrack()
            if self._active_track is not None:
                new_points = DriveTrack(
                    list(self._active_track.points[self._checkpointed_points :])
                )
            await self.store.async_save_checkpoint(
                drive_id, state, new_points, self._checkpoint_seq
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to flush drive checkpoint for VIN %s: %s", self.vin, err
            )

    def async_add_listener(
        self, update_callback: Callable[[DriveState, DriveEvent], None]
    ) -> Callable[[], None]:
        """Register a callback for drive state changes."""
        self._listeners.append(update_callback)

        def remove_listener() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove_listener

    def _notify_listeners(self, event: DriveEvent = DriveEvent.DRIVE_COMPLETE) -> None:
        """Notify all registered listeners of drive state changes.

        The default of ``DriveEvent.DRIVE_COMPLETE`` preserves the behavior of
        the legacy zero-argument call in ``__init__.py`` after a backfill,
        which should always trigger a full listener refresh.
        """
        for listener in list(self._listeners):
            try:
                listener(self.drive_state, event)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error in DriveTracker state listener: %s", err)

    @callback
    def handle_coordinator_update(self) -> None:
        """Handle coordinator state update and drive state machine transitions."""
        if not self.coordinator.data:
            return

        self._handle_charging_state()

        raw_gear = self.coordinator.get("gearStatus")
        gear = str(raw_gear).lower() if raw_gear is not None else None

        if gear is None:
            return

        self._last_gear = gear

        # Check for transition into driving gear ("drive" or "reverse")
        if gear in ("drive", "reverse"):
            if self._park_debounce_unsub is not None:
                # Cancel park debounce and resume drive seamlessly
                _LOGGER.info(
                    "Vehicle shifted back to '%s' within %ss debounce; resuming drive for VIN %s",
                    gear,
                    PARK_DEBOUNCE_SECONDS,
                    self.vin,
                )
                self._park_debounce_unsub()
                self._park_debounce_unsub = None

            if not self.drive_state.is_driving or self._active_drive is None:
                self._start_drive(gear)
            else:
                self._update_active_drive_telemetry()

        elif (
            gear == "park"
            and self.drive_state.is_driving
            and self._active_drive is not None
            and self._park_debounce_unsub is None
        ):
            _LOGGER.info(
                "Vehicle shifted to 'park'; starting %ss debounce timer for VIN %s",
                PARK_DEBOUNCE_SECONDS,
                self.vin,
            )
            self._update_active_drive_telemetry()
            self._park_debounce_unsub = async_call_later(
                self.hass,
                PARK_DEBOUNCE_SECONDS,
                self._handle_park_debounce_expired,
            )

        elif gear == "neutral":
            if self.drive_state.is_driving and self._active_drive is not None:
                self._update_active_drive_telemetry()

    def _start_drive(self, current_gear: str) -> None:
        """Initialize a new active drive session."""
        now_dt = self._utcnow()
        now_iso = now_dt.isoformat()
        now_epoch = int(now_dt.timestamp())

        odometer_m = self._get_float_coordinator_val("vehicleMileage")
        battery_soc = self._get_float_coordinator_val("batteryLevel", default=0.0)

        if self._active_charge_session is not None:
            self._finalize_active_charge_session(now_iso, battery_soc)
        battery_cap = self._get_float_coordinator_val(
            "batteryCapacity",
            default=float(self.vehicle_info.get("battery_capacity", 135.0) or 135.0),
        )
        altitude_m = self._get_float_coordinator_val("gnssAltitude")

        location = self.coordinator.data.get("gnssLocation", {})
        start_lat = (
            float(location["latitude"])
            if isinstance(location, dict) and location.get("latitude") is not None
            else None
        )
        start_lon = (
            float(location["longitude"])
            if isinstance(location, dict) and location.get("longitude") is not None
            else None
        )

        # Check if we were previously parked and can record a vampire drain event
        if self._park_start_dt is not None:
            idle_sec = max(0.0, (now_dt - self._park_start_dt).total_seconds())
            idle_hours = idle_sec / 3600.0
            start_soc = self._park_start_soc
            end_soc = battery_soc
            if (
                idle_hours >= 0.5
                and start_soc is not None
                and end_soc is not None
                and end_soc < start_soc
            ):
                drain_soc = round(start_soc - end_soc, 2)
                drain_kwh = round((drain_soc * battery_cap) / 100.0, 2)
                if drain_soc > 0.0 and drain_kwh > 0.0:
                    rate_pct_day = (
                        round((drain_soc / idle_hours) * 24.0, 2)
                        if idle_hours > 0
                        else 0.0
                    )
                    avg_watts = (
                        round((drain_kwh * 1000.0) / idle_hours, 1)
                        if idle_hours > 0
                        else 0.0
                    )
                    v_record = VampireDrainRecord(
                        start_time=self._park_start_dt.isoformat(),
                        end_time=now_iso,
                        idle_hours=round(idle_hours, 2),
                        start_soc=round(start_soc, 2),
                        end_soc=round(end_soc, 2),
                        drain_soc=drain_soc,
                        drain_kwh=drain_kwh,
                        rate_pct_per_day=rate_pct_day,
                        avg_watts=avg_watts,
                        latitude=self._park_lat
                        if self._park_lat is not None
                        else start_lat,
                        longitude=self._park_lon
                        if self._park_lon is not None
                        else start_lon,
                    )
                    self._schedule_coro(self._async_record_vampire_event(v_record))
            self._park_start_dt = None
            self._park_start_soc = None

        speed_bins = {b: SpeedBinData() for b in STANDARD_SPEED_BINS}

        self._active_drive = {
            "vin": self.vin,
            "start_epoch": now_epoch,
            "start_time_iso": now_iso,
            "start_dt": now_dt,
            "start_odometer_m": odometer_m,
            "last_odometer_m": odometer_m,
            "last_update_dt": now_dt,
            "start_soc": battery_soc,
            "last_soc": battery_soc,
            "battery_capacity_kwh": battery_cap,
            "start_altitude_m": altitude_m,
            "last_altitude_m": altitude_m,
            "start_lat": start_lat,
            "start_lon": start_lon,
            "last_lat": start_lat,
            "last_lon": start_lon,
            "gps_locked": False,
            "max_speed_mph": 0.0,
            "speed_bins": speed_bins,
            "weather_samples": [],
            "last_weather_sample_distance_mi": 0.0,
            "last_weather_sample_dt": now_dt,
            "distance_miles": 0.0,
            "chunks": [],
            "current_chunk_start_dt": now_dt,
            "current_chunk_start_odo_m": odometer_m,
            "current_chunk_start_soc": battery_soc,
            "current_chunk_start_alt_m": altitude_m,
            "current_chunk_speeds": [],
            "last_fix_raw": None,
            "start_range_mi": None,
            "last_range_mi": None,
            "drive_modes": [],
            "trailer": None,
            "driver": None,
        }
        self._capture_context(self._active_drive)

        self._active_track = DriveTrack()
        self._checkpoint_seq = 0
        self._checkpointed_points = 0
        # Seed the checkpoint clock at drive start (not None) so the first
        # checkpoint is due CHECKPOINT_INTERVAL_SECONDS after the drive
        # begins, rather than immediately on the first telemetry update.
        self._last_checkpoint_dt = now_dt
        self._resume_guard_active = False

        self.drive_state = DriveState(
            is_driving=True,
            status=DriveStatus.DRIVING.value,
            current_trip_distance_mi=0.0,
            current_trip_duration=0.0,
            current_trip_kwh=0.0,
            current_trip_efficiency=0.0,
            current_speed_mph=0.0,
            current_altitude_ft=(
                round(altitude_m * METERS_TO_FEET, 1) if altitude_m is not None else 0.0
            ),
            gps_locked=False,
        )

        _LOGGER.info(
            "Drive started for VIN %s at %s in gear '%s'",
            self.vin,
            now_iso,
            current_gear,
        )
        self._update_active_drive_telemetry()

    def _update_active_drive_telemetry(self) -> None:
        """Update live telemetry, speed binning, and weather sampling for active drive."""
        if self._active_drive is None:
            return

        now_dt = self._utcnow()
        active = self._active_drive

        prev_dt: datetime = active["last_update_dt"]
        delta_t_sec = max(0.0, (now_dt - prev_dt).total_seconds())
        active["last_update_dt"] = now_dt

        if self._resume_guard_active:
            # The first telemetry update after a mid-drive resume can see a
            # huge gap (HA was down); don't let that gap inflate speed-bin
            # seconds. Distance is still derived from the odometer total,
            # not from this delta, so it is unaffected.
            self._resume_guard_active = False
            if delta_t_sec > 60.0:
                delta_t_sec = 0.0

        # Telemetry values
        speed_mps = self._get_float_coordinator_val("gnssSpeed")
        odometer_m = self._get_float_coordinator_val("vehicleMileage")
        battery_soc = self._get_float_coordinator_val("batteryLevel")
        altitude_m = self._get_float_coordinator_val("gnssAltitude")

        location = self.coordinator.data.get("gnssLocation", {})
        curr_lat = (
            float(location["latitude"])
            if isinstance(location, dict) and location.get("latitude") is not None
            else None
        )
        curr_lon = (
            float(location["longitude"])
            if isinstance(location, dict) and location.get("longitude") is not None
            else None
        )

        if curr_lat is not None:
            active["last_lat"] = curr_lat
        if curr_lon is not None:
            active["last_lon"] = curr_lon

        if battery_soc is not None:
            active["last_soc"] = battery_soc

        if altitude_m is not None:
            active["last_altitude_m"] = altitude_m
            if active["start_altitude_m"] is None:
                active["start_altitude_m"] = altitude_m

        self._capture_context(active)

        # Speed calculation
        speed_mph = round(speed_mps * MPS_TO_MPH, 1) if speed_mps is not None else 0.0
        active["max_speed_mph"] = max(active["max_speed_mph"], speed_mph)

        # Odometer and distance calculation
        start_odo_m = active["start_odometer_m"]
        if start_odo_m is None and odometer_m is not None:
            active["start_odometer_m"] = odometer_m
            active["last_odometer_m"] = odometer_m
            start_odo_m = odometer_m

        last_odo_m = active["last_odometer_m"]
        if odometer_m is not None:
            active["last_odometer_m"] = odometer_m

        # Delta distance for speed binning
        if odometer_m is not None and last_odo_m is not None:
            delta_dist_meters = max(0.0, odometer_m - last_odo_m)
            delta_dist_miles = delta_dist_meters / METERS_PER_MILE
        elif speed_mph > 0 and delta_t_sec > 0:
            delta_dist_miles = (speed_mph * delta_t_sec) / 3600.0
        else:
            delta_dist_miles = 0.0

        # Total distance
        if odometer_m is not None and start_odo_m is not None:
            total_dist_miles = max(0.0, (odometer_m - start_odo_m) / METERS_PER_MILE)
        else:
            total_dist_miles = active["distance_miles"] + delta_dist_miles

        active["distance_miles"] = total_dist_miles

        # Speed bin accumulation
        bin_key = get_speed_bin_key(speed_mph)
        bin_data: SpeedBinData = active["speed_bins"][bin_key]
        bin_data.miles += delta_dist_miles
        bin_data.seconds += delta_t_sec

        # Collect speed sample for current 3-minute chunk
        if speed_mph > 0.0:
            active["current_chunk_speeds"].append(speed_mph)

        # Check if 3 minutes (180 seconds) have elapsed for the current chunk
        chunk_start_dt: datetime = active.get("current_chunk_start_dt", now_dt)
        chunk_elapsed = (now_dt - chunk_start_dt).total_seconds()
        if chunk_elapsed >= 180.0:
            self._finalize_current_chunk(
                active, now_dt, odometer_m, battery_soc, altitude_m
            )

        # GPS Lock sync validation
        if not active["gps_locked"]:
            odo_delta_m = (
                (odometer_m - start_odo_m)
                if (odometer_m is not None and start_odo_m is not None)
                else 0.0
            )
            speed_val_mps = speed_mps if speed_mps is not None else 0.0

            if (
                speed_val_mps >= SPEED_GPS_LOCK_THRESHOLD_MPS
                or odo_delta_m >= DISTANCE_GPS_LOCK_THRESHOLD_METERS
            ):
                active["gps_locked"] = True
                _LOGGER.info(
                    "GPS lock validated for VIN %s (speed: %.2f m/s, delta odo: %.1f m)",
                    self.vin,
                    speed_val_mps,
                    odo_delta_m,
                )
                # Sample initial weather waypoint
                if curr_lat is not None and curr_lon is not None:
                    self._schedule_weather_sample(curr_lat, curr_lon, total_dist_miles)

        # Route Weather Sampling (every 15 miles or 20 minutes after GPS lock)
        if active["gps_locked"] and curr_lat is not None and curr_lon is not None:
            dist_since_sample = (
                total_dist_miles - active["last_weather_sample_distance_mi"]
            )
            time_since_sample = (
                now_dt - active["last_weather_sample_dt"]
            ).total_seconds()

            if (
                dist_since_sample >= WEATHER_SAMPLE_INTERVAL_MILES
                or time_since_sample >= WEATHER_SAMPLE_INTERVAL_SECONDS
            ):
                active["last_weather_sample_distance_mi"] = total_dist_miles
                active["last_weather_sample_dt"] = now_dt
                self._schedule_weather_sample(curr_lat, curr_lon, total_dist_miles)

        # Live energy calculation
        start_soc = active["start_soc"]
        curr_soc = battery_soc if battery_soc is not None else active["last_soc"]
        bat_cap = active["battery_capacity_kwh"]
        gross_kwh = max(0.0, (start_soc - curr_soc) * bat_cap / 100.0)
        efficiency = total_dist_miles / gross_kwh if gross_kwh > 0.0 else 0.0
        duration_sec = max(0.0, (now_dt - active["start_dt"]).total_seconds())

        curr_alt_m = altitude_m if altitude_m is not None else active["last_altitude_m"]
        alt_ft = (
            round(curr_alt_m * METERS_TO_FEET, 1) if curr_alt_m is not None else 0.0
        )

        # Update DriveState
        self.drive_state = DriveState(
            is_driving=True,
            status=DriveStatus.DRIVING.value,
            current_trip_distance_mi=round(total_dist_miles, 2),
            current_trip_duration=round(duration_sec, 1),
            current_trip_kwh=round(gross_kwh, 2),
            current_trip_efficiency=round(efficiency, 2),
            current_speed_mph=round(speed_mph, 1),
            current_altitude_ft=round(alt_ft, 1),
            gps_locked=active["gps_locked"],
        )

        self._capture_point()
        self._maybe_schedule_checkpoint(active, now_dt)

        self._notify_listeners(DriveEvent.LIVE)

    def _schedule_weather_sample(
        self, lat: float, lon: float, distance_at_sample: float
    ) -> None:
        """Schedule an asynchronous weather waypoint fetch."""
        coro = self._async_sample_weather(lat, lon, distance_at_sample)
        self._schedule_coro(coro)

    async def _async_sample_weather(
        self, lat: float, lon: float, distance_at_sample: float
    ) -> None:
        """Fetch weather from Open-Meteo and append to active drive samples."""
        temp_f = await self.weather_client.async_get_current_temperature(lat, lon)
        if temp_f is not None and self._active_drive is not None:
            sample = {
                "timestamp": datetime.now(UTC).isoformat(),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "temp_f": round(temp_f, 1),
                "distance_at_sample": round(distance_at_sample, 2),
            }
            self._active_drive["weather_samples"].append(sample)
            _LOGGER.debug(
                "Added weather sample for VIN %s: %.1f°F at %.2f mi",
                self.vin,
                temp_f,
                distance_at_sample,
            )

    def _finalize_current_chunk(
        self,
        active: dict[str, Any],
        now_dt: datetime,
        odometer_m: float | None,
        battery_soc: float | None,
        altitude_m: float | None,
    ) -> None:
        """Finalize a 3-minute chunk and start a new one."""
        chunk_start_dt: datetime = active.get("current_chunk_start_dt", now_dt)
        dt_win = (now_dt - chunk_start_dt).total_seconds()
        if dt_win < 90.0:
            return

        s_odo_m = active.get("current_chunk_start_odo_m")
        if s_odo_m is not None and odometer_m is not None:
            chunk_dist = max(0.0, (odometer_m - s_odo_m) / METERS_PER_MILE)
        else:
            chunk_dist = 0.0

        # `is not None`, not `or`: 0 % is a real reading.
        s_soc = active.get("current_chunk_start_soc")
        if s_soc is None:
            s_soc = battery_soc if battery_soc is not None else 0.0
        e_soc = battery_soc if battery_soc is not None else s_soc
        chunk_dsoc = max(0.0, s_soc - e_soc)
        bat_cap = active.get("battery_capacity_kwh", 135.0)
        chunk_kwh = (chunk_dsoc * bat_cap) / 100.0

        speeds = active.get("current_chunk_speeds", [])
        if speeds:
            chunk_avg_spd = round(sum(speeds) / len(speeds), 1)
        elif chunk_dist > 0 and dt_win > 0:
            chunk_avg_spd = round(chunk_dist / (dt_win / 3600.0), 1)
        else:
            chunk_avg_spd = 0.0

        s_alt = active.get("current_chunk_start_alt_m")
        e_alt = altitude_m if altitude_m is not None else s_alt
        chunk_elev = (
            round((e_alt - s_alt) * METERS_TO_FEET, 1)
            if s_alt is not None and e_alt is not None
            else 0.0
        )

        chunk_bin = get_speed_bin_key(chunk_avg_spd)

        if chunk_kwh > 0.0 and chunk_dist >= 0.05:
            chunk_eff = round(chunk_dist / chunk_kwh, 2)
            active["chunks"].append(
                DriveChunk(
                    start_time=chunk_start_dt.isoformat(),
                    duration_seconds=round(dt_win, 1),
                    distance_miles=round(chunk_dist, 2),
                    energy_kwh=round(chunk_kwh, 2),
                    efficiency_mi_kwh=chunk_eff,
                    avg_speed_mph=chunk_avg_spd,
                    speed_bin=chunk_bin,
                    elevation_change_ft=chunk_elev,
                )
            )

        active["current_chunk_start_dt"] = now_dt
        active["current_chunk_start_odo_m"] = odometer_m
        active["current_chunk_start_soc"] = battery_soc
        active["current_chunk_start_alt_m"] = altitude_m
        active["current_chunk_speeds"] = []

    def _track_capture_enabled(self) -> bool:
        """Return whether GPS route capture is enabled in config entry options."""
        try:
            options = self.entry.options
        except AttributeError:
            return DEFAULT_TRACK_CAPTURE
        if options is None:
            return DEFAULT_TRACK_CAPTURE
        return bool(options.get(CONF_TRACK_CAPTURE, DEFAULT_TRACK_CAPTURE))

    @staticmethod
    def _parse_fix_timestamp(raw_ts: Any) -> float:
        """Parse a gnssLocation timeStamp into POSIX epoch seconds.

        Handles an ISO-8601 string (the format Rivian sends), an epoch number
        in seconds or milliseconds, or a missing/unparseable value (falls
        back to now).
        """
        now = datetime.now(UTC).timestamp()
        if raw_ts is None:
            return now
        if isinstance(raw_ts, bool):
            return now
        if isinstance(raw_ts, (int, float)):
            value = float(raw_ts)
            if value > _EPOCH_MS_THRESHOLD:
                value /= 1000.0
            return value
        if isinstance(raw_ts, str):
            try:
                dt = datetime.fromisoformat(raw_ts)
                return dt.timestamp()
            except (ValueError, TypeError):
                try:
                    value = float(raw_ts)
                except (ValueError, TypeError):
                    return now
                if value > _EPOCH_MS_THRESHOLD:
                    value /= 1000.0
                return value
        return now

    def _capture_point(self) -> None:
        """Capture the current gnssLocation fix into the active GPS track, if new."""
        if self._active_drive is None or self._active_track is None:
            return
        if not self._track_capture_enabled():
            return

        location = self.coordinator.data.get("gnssLocation")
        if not isinstance(location, dict):
            return

        active = self._active_drive
        raw_ts = location.get("timeStamp")
        # Dedupe on the fix timestamp; without one, fall back to the position
        # so a missing timeStamp can't make every later push look identical.
        fix_key = (
            raw_ts
            if raw_ts is not None
            else f"{location.get('latitude')},{location.get('longitude')}"
        )
        if fix_key == active.get("last_fix_raw"):
            return  # Identical push; nothing new to capture.
        active["last_fix_raw"] = fix_key

        t = self._parse_fix_timestamp(raw_ts)
        start_epoch = float(active["start_epoch"])
        if len(self._active_track) == 0:
            if t < start_epoch - EMPTY_TRACK_MAX_FIX_AGE_SECONDS:
                return
        elif t < start_epoch - STALE_FIX_MAX_AGE_SECONDS:
            return

        lat = location.get("latitude")
        lon = location.get("longitude")
        point = TrackPoint(
            t=t,
            lat=float(lat) if isinstance(lat, (int, float)) else float("nan"),
            lon=float(lon) if isinstance(lon, (int, float)) else float("nan"),
            speed_mps=self._get_float_coordinator_val("gnssSpeed"),
            alt_m=self._get_float_coordinator_val("gnssAltitude"),
            soc=self._get_float_coordinator_val("batteryLevel"),
            odo_m=self._get_float_coordinator_val("vehicleMileage"),
        )
        self._active_track.append(point)

    def _maybe_schedule_checkpoint(
        self, active: dict[str, Any], now_dt: datetime
    ) -> None:
        """Schedule a checkpoint save if enough time has passed and none is in flight."""
        if self._finalizing or self._checkpoint_in_flight:
            return
        last = self._last_checkpoint_dt
        if (
            last is not None
            and (now_dt - last).total_seconds() < CHECKPOINT_INTERVAL_SECONDS
        ):
            return

        self._last_checkpoint_dt = now_dt
        drive_id = f"{self.vin}_{active['start_epoch']}"
        state = self._serialize_active_drive(active)

        new_points = DriveTrack()
        if self._active_track is not None:
            new_points = DriveTrack(
                list(self._active_track.points[self._checkpointed_points :])
            )
        pending_point_count = len(new_points)
        seq = self._checkpoint_seq

        self._checkpoint_in_flight = True
        self._checkpoint_task = self._schedule_coro(
            self._async_save_checkpoint(
                drive_id, state, new_points, seq, pending_point_count
            )
        )

    async def _async_save_checkpoint(
        self,
        drive_id: str,
        state: dict[str, Any],
        new_points: DriveTrack,
        seq: int,
        pending_point_count: int,
    ) -> None:
        """Persist a drive checkpoint; on failure, leave counters for a retry."""
        try:
            active = self._active_drive
            if active is None or f"{self.vin}_{active['start_epoch']}" != drive_id:
                # The drive was finalized or replaced while this was scheduled.
                return
            await self.store.async_save_checkpoint(drive_id, state, new_points, seq)
            self._checkpoint_seq = seq + 1
            self._checkpointed_points += pending_point_count
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to save drive checkpoint for VIN %s: %s", self.vin, err
            )
        finally:
            self._checkpoint_in_flight = False

    def _serialize_active_drive(self, active: dict[str, Any]) -> dict[str, Any]:
        """Serialize the active drive dict to a JSON-safe checkpoint state."""
        return {
            "schema": 3,
            "vin": active["vin"],
            "start_epoch": active["start_epoch"],
            "start_time_iso": active["start_time_iso"],
            "start_dt": active["start_dt"].isoformat(),
            "start_odometer_m": active["start_odometer_m"],
            "last_odometer_m": active["last_odometer_m"],
            "last_update_dt": active["last_update_dt"].isoformat(),
            "start_soc": active["start_soc"],
            "last_soc": active["last_soc"],
            "battery_capacity_kwh": active["battery_capacity_kwh"],
            "start_altitude_m": active["start_altitude_m"],
            "last_altitude_m": active["last_altitude_m"],
            "start_lat": active["start_lat"],
            "start_lon": active["start_lon"],
            "last_lat": active["last_lat"],
            "last_lon": active["last_lon"],
            "gps_locked": active["gps_locked"],
            "max_speed_mph": active["max_speed_mph"],
            "speed_bins": {k: v.to_dict() for k, v in active["speed_bins"].items()},
            "weather_samples": active["weather_samples"],
            "last_weather_sample_distance_mi": active[
                "last_weather_sample_distance_mi"
            ],
            "last_weather_sample_dt": active["last_weather_sample_dt"].isoformat(),
            "distance_miles": active["distance_miles"],
            "chunks": [c.to_dict() for c in active.get("chunks", [])],
            "current_chunk_start_dt": active["current_chunk_start_dt"].isoformat(),
            "current_chunk_start_odo_m": active["current_chunk_start_odo_m"],
            "current_chunk_start_soc": active["current_chunk_start_soc"],
            "current_chunk_start_alt_m": active["current_chunk_start_alt_m"],
            "current_chunk_speeds": active.get("current_chunk_speeds", []),
            "last_fix_raw": active.get("last_fix_raw"),
            "start_range_mi": active.get("start_range_mi"),
            "last_range_mi": active.get("last_range_mi"),
            "drive_modes": active.get("drive_modes", []),
            "trailer": active.get("trailer"),
            "driver": active.get("driver"),
        }

    @staticmethod
    def _deserialize_active_drive(state: dict[str, Any]) -> dict[str, Any]:
        """Reconstruct an active drive dict from a checkpoint state.

        Accepts schema 3 (adds ``start_range_mi``/``last_range_mi``/
        ``drive_modes``/``trailer``/``driver``), schema 2 (``chunks``/
        ``current_chunk_*`` keys), and legacy schema 1 (``segments``/
        ``current_segment_*`` keys) -- a drive in progress during an upgrade
        can still resume, with the new schema-3 keys simply defaulting to
        empty/None when absent from an older checkpoint.
        """

        def _dt(value: str) -> datetime:
            return datetime.fromisoformat(value)

        chunks_raw = state.get("chunks", state.get("segments", []))
        current_chunk_start_dt = state.get(
            "current_chunk_start_dt", state.get("current_segment_start_dt")
        )
        current_chunk_start_odo_m = state.get(
            "current_chunk_start_odo_m", state.get("current_segment_start_odo_m")
        )
        current_chunk_start_soc = state.get(
            "current_chunk_start_soc", state.get("current_segment_start_soc")
        )
        current_chunk_start_alt_m = state.get(
            "current_chunk_start_alt_m", state.get("current_segment_start_alt_m")
        )
        current_chunk_speeds = state.get(
            "current_chunk_speeds", state.get("current_segment_speeds", [])
        )

        return {
            "vin": state["vin"],
            "start_epoch": state["start_epoch"],
            "start_time_iso": state["start_time_iso"],
            "start_dt": _dt(state["start_dt"]),
            "start_odometer_m": state["start_odometer_m"],
            "last_odometer_m": state["last_odometer_m"],
            "last_update_dt": _dt(state["last_update_dt"]),
            "start_soc": state["start_soc"],
            "last_soc": state["last_soc"],
            "battery_capacity_kwh": state["battery_capacity_kwh"],
            "start_altitude_m": state["start_altitude_m"],
            "last_altitude_m": state["last_altitude_m"],
            "start_lat": state["start_lat"],
            "start_lon": state["start_lon"],
            "last_lat": state["last_lat"],
            "last_lon": state["last_lon"],
            "gps_locked": state["gps_locked"],
            "max_speed_mph": state["max_speed_mph"],
            "speed_bins": {
                k: SpeedBinData.from_dict(v)
                for k, v in state.get("speed_bins", {}).items()
            },
            "weather_samples": state.get("weather_samples", []),
            "last_weather_sample_distance_mi": state["last_weather_sample_distance_mi"],
            "last_weather_sample_dt": _dt(state["last_weather_sample_dt"]),
            "distance_miles": state["distance_miles"],
            "chunks": [DriveChunk.from_dict(c) for c in chunks_raw],
            "current_chunk_start_dt": _dt(current_chunk_start_dt),
            "current_chunk_start_odo_m": current_chunk_start_odo_m,
            "current_chunk_start_soc": current_chunk_start_soc,
            "current_chunk_start_alt_m": current_chunk_start_alt_m,
            "current_chunk_speeds": current_chunk_speeds,
            "last_fix_raw": state.get("last_fix_raw"),
            "start_range_mi": state.get("start_range_mi"),
            "last_range_mi": state.get("last_range_mi"),
            "drive_modes": list(state.get("drive_modes", [])),
            "trailer": state.get("trailer"),
            "driver": state.get("driver"),
        }

    @callback
    def _handle_park_debounce_expired(self, _now: Any = None) -> None:
        """Handle expiration of the 60s park debounce timer."""
        _LOGGER.info(
            "Park debounce timer expired for VIN %s; finalizing drive",
            self.vin,
        )
        self._park_debounce_unsub = None
        if self._active_drive is not None:
            self._schedule_coro(self._async_finalize_drive())

    async def async_finalize_drive(self) -> DriveRecord | None:
        """Public method to finalize drive (used by debounce or tests)."""
        return await self._async_finalize_drive()

    async def _async_finalize_drive(
        self, as_of: datetime | None = None
    ) -> DriveRecord | None:
        """Finalize the active drive, compute all summary metrics, and save to DriveStore.

        If ``as_of`` is given (restoring a stale/parked checkpoint on setup),
        the drive ends at that timestamp using the checkpoint's own last-known
        telemetry rather than the live coordinator, since the vehicle may have
        driven or charged further since the checkpoint was written.
        """
        if self._active_drive is None:
            return None

        if self._park_debounce_unsub is not None:
            self._park_debounce_unsub()
            self._park_debounce_unsub = None

        self._finalizing = True
        if self._checkpoint_task is not None:
            try:
                await self._checkpoint_task
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Pending checkpoint failed before finalize: %s", err)
            self._checkpoint_task = None

        active = self._active_drive
        now_dt = as_of if as_of is not None else self._utcnow()
        now_iso = now_dt.isoformat()

        if as_of is None:
            # Telemetry updates from the live coordinator.
            odometer_m = self._get_float_coordinator_val("vehicleMileage")
            if odometer_m is not None:
                active["last_odometer_m"] = odometer_m

            battery_soc = self._get_float_coordinator_val("batteryLevel")
            if battery_soc is not None:
                active["last_soc"] = battery_soc

            altitude_m = self._get_float_coordinator_val("gnssAltitude")
            if altitude_m is not None:
                active["last_altitude_m"] = altitude_m
        else:
            # Restoring a checkpointed drive: trust only what was checkpointed.
            battery_soc = active["last_soc"]
            altitude_m = active["last_altitude_m"]

        start_odo_m = active["start_odometer_m"]
        end_odo_m = active["last_odometer_m"]

        if start_odo_m is not None and end_odo_m is not None:
            distance_miles = max(0.0, (end_odo_m - start_odo_m) / METERS_PER_MILE)
        else:
            distance_miles = active["distance_miles"]

        start_dt: datetime = active["start_dt"]
        duration_sec = max(0.0, (now_dt - start_dt).total_seconds())

        start_soc = active["start_soc"]
        end_soc = active["last_soc"]
        bat_cap = active["battery_capacity_kwh"]

        energy_kwh = max(0.0, (start_soc - end_soc) * bat_cap / 100.0)
        efficiency_mi_kwh = distance_miles / energy_kwh if energy_kwh > 0.0 else 0.0
        mpge = efficiency_mi_kwh * MPGE_FACTOR

        start_alt_m = active["start_altitude_m"]
        end_alt_m = active["last_altitude_m"]
        start_alt_ft = start_alt_m * METERS_TO_FEET if start_alt_m is not None else 0.0
        end_alt_ft = end_alt_m * METERS_TO_FEET if end_alt_m is not None else 0.0
        elev_change_ft = (
            (end_alt_m - start_alt_m) * METERS_TO_FEET
            if (start_alt_m is not None and end_alt_m is not None)
            else 0.0
        )

        avg_speed_mph = (
            (distance_miles / (duration_sec / 3600.0)) if duration_sec > 0.0 else 0.0
        )
        max_speed_mph = active["max_speed_mph"]

        # Integrated temperature calculation
        weather_samples = active["weather_samples"]
        integrated_temp_f = calculate_distance_weighted_temperature(
            weather_samples=weather_samples,
            total_distance_miles=distance_miles,
        )

        # Micro-drive detection
        is_micro = distance_miles < MICRO_DRIVE_THRESHOLD_MILES

        drive_id = f"{self.vin}_{active['start_epoch']}"

        # Finalize any pending 3-minute chunk
        self._finalize_current_chunk(active, now_dt, end_odo_m, battery_soc, altitude_m)

        # Determined here (rather than after building the record) because the
        # track-derived summary stats below need it too.
        track = (
            self._active_track
            if self._track_capture_enabled()
            and self._active_track is not None
            and len(self._active_track) >= 2
            else None
        )
        track_stats_kwargs = self._compute_track_stats_kwargs(track)

        record = DriveRecord(
            vin=self.vin,
            drive_id=drive_id,
            start_time=active["start_time_iso"],
            end_time=now_iso,
            distance_miles=round(distance_miles, 2),
            duration_seconds=round(duration_sec, 1),
            start_soc=round(start_soc, 2),
            end_soc=round(end_soc, 2),
            battery_capacity_kwh=round(bat_cap, 2),
            energy_kwh=round(energy_kwh, 2),
            efficiency_mi_kwh=round(efficiency_mi_kwh, 2),
            mpge=round(mpge, 2),
            start_altitude_ft=round(start_alt_ft, 1),
            end_altitude_ft=round(end_alt_ft, 1),
            elevation_change_ft=round(elev_change_ft, 1),
            avg_speed_mph=round(avg_speed_mph, 1),
            max_speed_mph=round(max_speed_mph, 1),
            integrated_temperature_f=integrated_temp_f,
            speed_bins=active["speed_bins"],
            is_micro_drive=is_micro,
            start_odometer_mi=(
                round(start_odo_m / METERS_PER_MILE, 2)
                if start_odo_m is not None
                else None
            ),
            end_odometer_mi=(
                round(end_odo_m / METERS_PER_MILE, 2) if end_odo_m is not None else None
            ),
            start_lat=active["start_lat"],
            start_lon=active["start_lon"],
            end_lat=active["last_lat"],
            end_lon=active["last_lon"],
            weather_samples=weather_samples,
            chunks=active.get("chunks", []),
            start_range_mi=active.get("start_range_mi"),
            end_range_mi=active.get("last_range_mi"),
            drive_modes=list(active.get("drive_modes", [])),
            trailer=active.get("trailer"),
            driver=active.get("driver"),
            **track_stats_kwargs,
        )

        # Save the drive and its GPS track (if capture is enabled and there's
        # enough of a track to be worth storing) together; this also clears
        # the live checkpoint atomically.
        try:
            await self.store.async_finalize_drive(record, track)
        except Exception:
            # Leave the drive active (its checkpoint still holds it) and let
            # checkpointing resume, rather than wedging on _finalizing.
            self._finalizing = False
            raise
        _LOGGER.info(
            "Finalized drive %s for VIN %s: %.2f mi, %.2f kWh (%.2f mi/kWh, micro=%s)",
            drive_id,
            self.vin,
            record.distance_miles,
            record.energy_kwh,
            record.efficiency_mi_kwh,
            record.is_micro_drive,
        )

        self._park_start_dt = now_dt
        self._park_start_soc = end_soc
        self._park_lat = active.get("last_lat")
        self._park_lon = active.get("last_lon")

        self._active_drive = None
        self._active_track = None
        self._checkpoint_seq = 0
        self._checkpointed_points = 0
        self._last_checkpoint_dt = None
        self._checkpoint_in_flight = False
        self._finalizing = False
        self.drive_state = DriveState(
            is_driving=False,
            status=DriveStatus.PARKED.value,
            current_trip_distance_mi=0.0,
            current_trip_duration=0.0,
            current_trip_kwh=0.0,
            current_trip_efficiency=0.0,
            current_speed_mph=0.0,
            current_altitude_ft=round(end_alt_ft, 1),
            gps_locked=False,
        )
        self._notify_listeners(DriveEvent.DRIVE_COMPLETE)
        return record

    def _compute_track_stats_kwargs(self, track: DriveTrack | None) -> dict[str, Any]:
        """Return DriveRecord kwargs for the track-derived summary stats.

        Empty (no kwargs, all fields default to None on the record) when
        there is no usable track, or if the computation itself fails --
        this must never break finalizing the drive.
        """
        if track is None or len(track) < 2:
            return {}
        try:
            stats = compute_track_stats(track)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to compute drive summary stats for VIN %s: %s", self.vin, err
            )
            return {}
        return {
            "moving_seconds": stats.moving_seconds,
            "stopped_seconds": stats.stopped_seconds,
            "stop_count": stats.stop_count,
            "climb_ft": (
                round(stats.climb_m * METERS_TO_FEET, 1)
                if stats.climb_m is not None
                else None
            ),
            "descent_ft": (
                round(stats.descent_m * METERS_TO_FEET, 1)
                if stats.descent_m is not None
                else None
            ),
            "track_max_speed_mph": (
                round(stats.max_speed_mps * MPS_TO_MPH, 1)
                if stats.max_speed_mps is not None
                else None
            ),
            "pct_distance_over_70mph": stats.pct_distance_over_70mph,
        }

    def _handle_charging_state(self) -> None:
        """Track live DC fast charging sessions and sample charging curve points."""
        raw_charger_state = self.coordinator.get("chargerState")
        charger_state = (
            str(raw_charger_state).lower() if raw_charger_state is not None else ""
        )
        is_charging = charger_state in ("charging_active", "charging_connecting")

        power_val: float | None = None
        if (
            hasattr(self.coordinator, "charging_coordinator")
            and self.coordinator.charging_coordinator.data
        ):
            cdata = self.coordinator.charging_coordinator.data
            raw_p = cdata.get("power")
            if raw_p is not None:
                try:
                    power_val = float(raw_p)
                except (ValueError, TypeError):
                    pass

        if power_val is None:
            raw_p = self.coordinator.get("power") or self.coordinator.get(
                "chargerPower"
            )
            if raw_p is not None:
                try:
                    power_val = float(raw_p)
                except (ValueError, TypeError):
                    pass

        now_dt = self._utcnow()
        now_iso = now_dt.isoformat()
        soc_val = self._get_float_coordinator_val("batteryLevel")

        if is_charging:
            # When charging is active, pause park idle timer so charge intake isn't seen as phantom drain
            self._park_start_dt = None
            self._park_start_soc = None

            if self._active_charge_session is None:
                self._active_charge_session = {
                    "session_id": f"{self.vin}_{int(now_dt.timestamp())}",
                    "start_time": now_iso,
                    "start_soc": soc_val if soc_val is not None else 0.0,
                    "max_power_kw": power_val or 0.0,
                    "samples": [],
                    "soc_points": [],
                    "battery_temps": [],
                    "lat": None,
                    "lon": None,
                }
                location = self.coordinator.data.get("gnssLocation")
                if isinstance(location, dict):
                    try:
                        if (
                            location.get("latitude") is not None
                            and location.get("longitude") is not None
                        ):
                            self._active_charge_session["lat"] = float(
                                location["latitude"]
                            )
                            self._active_charge_session["lon"] = float(
                                location["longitude"]
                            )
                    except (TypeError, ValueError):
                        pass

            session = self._active_charge_session
            # The battery temperature, whenever the vehicle reports one (not
            # every vehicle does): its mean is stored with the session.
            battery_temp = self._get_float_coordinator_val("batteryTemperature")
            if battery_temp is not None:
                temps: list[float] = session.setdefault("battery_temps", [])
                temps.append(battery_temp)
                if len(temps) > MAX_CHARGE_SOC_POINTS:
                    del temps[: len(temps) - MAX_CHARGE_SOC_POINTS]
            if power_val is not None:
                session["max_power_kw"] = max(session["max_power_kw"], power_val)
                if soc_val is not None:
                    samples: list[ChargingSample] = session["samples"]
                    should_append = True
                    if samples:
                        last_s = samples[-1]
                        if (
                            abs(last_s.soc - soc_val) < 0.2
                            and abs(last_s.power_kw - power_val) < 1.0
                        ):
                            should_append = False
                    if should_append:
                        temp_val = self._get_float_coordinator_val("batteryTemperature")
                        samples.append(
                            ChargingSample(
                                timestamp=now_iso,
                                soc=round(soc_val, 1),
                                power_kw=round(power_val, 1),
                                battery_temp_f=temp_val,
                            )
                        )

            if soc_val is not None:
                soc_points: list[tuple[float, float]] = session["soc_points"]
                candidate = (now_dt.timestamp(), soc_val)
                if should_append_soc_point(
                    soc_points[-1] if soc_points else None, candidate
                ):
                    soc_points.append(candidate)
                    if len(soc_points) > MAX_CHARGE_SOC_POINTS:
                        del soc_points[: len(soc_points) - MAX_CHARGE_SOC_POINTS]

        elif self._active_charge_session is not None:
            self._finalize_active_charge_session(now_iso, soc_val)

    def _finalize_active_charge_session(
        self, end_iso: str, end_soc: float | None = None
    ) -> None:
        """Finalize the active charging session and persist it if it qualifies.

        A session is DC fast charging when its peak or average power (real,
        or estimated from the SoC rise) reaches ``DCFC_MIN_POWER_KW``; it keeps
        its power curve. A slower one is a home/AC session, kept (with a few
        coarse SoC points, no curve) only if it added at least
        ``AC_SESSION_MIN_SOC_GAIN_PCT`` and lasted ``AC_SESSION_MIN_DURATION_S``.
        """
        if self._active_charge_session is None:
            return

        session = self._active_charge_session
        self._active_charge_session = None

        samples: list[ChargingSample] = session["samples"]
        max_power = session["max_power_kw"]
        start_soc = session["start_soc"]
        battery_cap = self._get_float_coordinator_val("batteryCapacity") or float(
            self.vehicle_info.get("battery_capacity", 135.0) or 135.0
        )
        soc_points: list[tuple[float, float]] = session.get("soc_points", [])

        # Real power samples (a power field was reported) vs a curve estimated
        # from the SoC rise: an estimated *peak* is too noisy (one 0.1 % step
        # in 30 s reads as 16 kW) to classify on, so only its average counts.
        real_power = bool(samples)
        if not samples and len(soc_points) >= 2:
            # No real power reading was ever available (Rivian's
            # getLiveSessionData API is gone and the vehicle doesn't report
            # power directly) - estimate a curve from the SoC-over-time
            # points collected while charging.
            estimated_samples, estimated_max = estimate_charge_curve(
                soc_points, battery_cap
            )
            if estimated_samples:
                samples = estimated_samples
                max_power = estimated_max

        final_soc = (
            end_soc
            if end_soc is not None
            else (samples[-1].soc if samples else start_soc)
        )
        energy_added = max(0.0, (final_soc - start_soc) * battery_cap / 100.0)
        start_dt = datetime.fromisoformat(session["start_time"])
        end_dt = datetime.fromisoformat(end_iso)
        duration_s = max(0.0, (end_dt - start_dt).total_seconds())
        energy_kw = energy_added / (duration_s / 3600.0) if duration_s > 0 else 0.0
        sample_avg = (
            sum(s.power_kw for s in samples) / len(samples) if samples else max_power
        )
        avg_power = sample_avg
        peak_for_class = max_power if real_power else sample_avg
        is_dc = (
            peak_for_class >= DCFC_MIN_POWER_KW
            or avg_power >= DCFC_MIN_POWER_KW
            or energy_kw >= DCFC_MIN_POWER_KW
        )

        if is_dc:
            kind = SESSION_KIND_DC
        else:
            if (
                final_soc - start_soc < AC_SESSION_MIN_SOC_GAIN_PCT
                or duration_s < AC_SESSION_MIN_DURATION_S
            ):
                _LOGGER.debug(
                    "Discarding charging blip for VIN %s: %.1f%% in %.0f s",
                    self.vin,
                    final_soc - start_soc,
                    duration_s,
                )
                return
            kind = SESSION_KIND_AC
            samples = coarse_soc_samples(soc_points, AC_SESSION_MAX_SOC_POINTS)
            # The estimated peak is not trusted for an AC session; the average
            # rate is what home charging actually delivered.
            avg_power = energy_kw if energy_kw > 0 else sample_avg
            if not real_power:
                max_power = avg_power

        battery_temps = list(session.get("battery_temps") or []) or [
            s.battery_temp_f for s in samples if s.battery_temp_f is not None
        ]
        record = ChargingSessionRecord(
            battery_temp_f=(
                round(sum(battery_temps) / len(battery_temps), 1)
                if battery_temps
                else None
            ),
            session_id=session["session_id"],
            start_time=session["start_time"],
            end_time=end_iso,
            start_soc=round(start_soc, 1),
            end_soc=round(final_soc, 1),
            energy_added_kwh=round(energy_added, 2),
            max_power_kw=round(max_power, 1),
            avg_power_kw=round(avg_power, 1),
            samples=samples,
            kind=kind,
            lat=session.get("lat"),
            lon=session.get("lon"),
            source="live",
        )

        _LOGGER.info(
            "Recording %s charging session for VIN %s: %.1f%% to %.1f%%, peak %.1f kW, %.2f kWh (%d samples)",
            kind.upper(),
            self.vin,
            start_soc,
            final_soc,
            max_power,
            energy_added,
            len(samples),
        )
        self._schedule_coro(self.store.async_append_dcfc_session(record))

    async def _async_record_vampire_event(self, event: VampireDrainRecord) -> None:
        """Fetch weather and save vampire drain event."""
        if event.latitude is not None and event.longitude is not None:
            try:
                temp_f = await self.weather_client.async_get_current_temperature(
                    event.latitude, event.longitude
                )
                if temp_f is not None:
                    event.avg_temp_f = round(temp_f, 1)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Could not fetch weather for vampire event: %s", err)
        await self.store.async_append_vampire_event(event)
        _LOGGER.info(
            "Recorded vampire drain event for VIN %s: %.1f hrs, %.2f kWh (%.2f%%)",
            self.vin,
            event.idle_hours,
            event.drain_kwh,
            event.drain_soc,
        )

    def _get_float_coordinator_val(
        self, field: str, default: float | None = None
    ) -> float | None:
        """Helper to safely retrieve numeric coordinator value."""
        val = self.coordinator.get(field)
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    def _get_str_coordinator_val(self, field: str) -> str | None:
        """Helper to safely retrieve a string coordinator value.

        Returns None for a missing value or one of ``INVALID_SENSOR_STATES``
        (e.g. "fault", "signal_not_available", "undefined").
        """
        val = self.coordinator.get(field)
        if val is None:
            return None
        val_str = str(val)
        if val_str.lower() in INVALID_SENSOR_STATES:
            return None
        return val_str

    def _capture_context(self, active: dict[str, Any]) -> None:
        """Capture live vehicle context: range, drive mode, trailer, driver.

        ``distanceToEmpty`` is reported in kilometres; converted to miles for
        storage. Called on drive start (to seed the values) and on every
        telemetry update (to keep the "last seen" values current and grow the
        distinct drive-modes list).
        """
        range_km = self._get_float_coordinator_val("distanceToEmpty")
        if range_km is not None:
            range_mi = round(range_km * KM_TO_MILES, 1)
            active["last_range_mi"] = range_mi
            if active.get("start_range_mi") is None:
                active["start_range_mi"] = range_mi

        raw_drive_mode = self._get_str_coordinator_val("driveMode")
        if raw_drive_mode is not None:
            display_mode = DRIVE_MODE_MAP.get(raw_drive_mode, raw_drive_mode)
            drive_modes: list[str] = active.setdefault("drive_modes", [])
            if display_mode not in drive_modes:
                drive_modes.append(display_mode)

        raw_trailer = self._get_str_coordinator_val("trailerStatus")
        mapped_trailer = trailer_status_to_bool(raw_trailer)
        if mapped_trailer is True:
            active["trailer"] = True
        elif mapped_trailer is False and active.get("trailer") is None:
            active["trailer"] = False

        raw_driver = self._get_str_coordinator_val("activeDriverName")
        if raw_driver is not None:
            active["driver"] = raw_driver

    def _schedule_coro(
        self, coro: Coroutine[Any, Any, Any]
    ) -> asyncio.Task[Any] | None:
        """Schedule coroutine in Home Assistant event loop safely.

        Returns the created task when one could be scheduled (so callers can
        await it later, e.g. before finalizing a drive), or None if the
        coroutine had to be run synchronously via ``asyncio.run``.
        """
        if hasattr(self.hass, "async_create_task"):
            result = self.hass.async_create_task(coro)
            return result if isinstance(result, asyncio.Task) else None
        try:
            loop = getattr(self.hass, "loop", None) or asyncio.get_running_loop()
            return loop.create_task(coro)
        except RuntimeError:
            asyncio.run(coro)
            return None
