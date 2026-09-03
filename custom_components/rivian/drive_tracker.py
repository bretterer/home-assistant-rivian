"""Rivian Real-Time Drive Tracker & Lifecycle Engine."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .drive_models import (
    MICRO_DRIVE_THRESHOLD_MILES,
    MPGE_FACTOR,
    STANDARD_SPEED_BINS,
    DriveRecord,
    DriveSegment,
    DriveState,
    DriveStatus,
    SpeedBinData,
)
from .drive_storage import DriveStore
from .weather import OpenMeteoWeatherClient, calculate_distance_weighted_temperature

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

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
        self._park_debounce_unsub: Callable[[], None] | None = None
        self._active_drive: dict[str, Any] | None = None
        self._listeners: list[Callable[[DriveState], None]] = []
        self._last_gear: str | None = None

    @property
    def is_driving(self) -> bool:
        """Return whether vehicle is currently driving."""
        return self.drive_state.is_driving

    @property
    def is_debouncing_park(self) -> bool:
        """Return whether park debounce timer is currently active."""
        return self._park_debounce_unsub is not None

    @property
    def active_drive(self) -> dict[str, Any] | None:
        """Return active drive telemetry dictionary if driving."""
        return self._active_drive

    async def async_setup(self) -> None:
        """Set up DriveTracker, load storage, and register coordinator listener."""
        await self.store.async_load()
        self._unsub_coordinator_listener = self.coordinator.async_add_listener(
            self.handle_coordinator_update
        )
        self.handle_coordinator_update()
        _LOGGER.info(
            "DriveTracker initialized for VIN %s (%s drives loaded)",
            self.vin,
            len(self.store.drives),
        )

    async def async_unload(self) -> None:
        """Clean up DriveTracker listeners and pending debounce timers."""
        if self._park_debounce_unsub is not None:
            self._park_debounce_unsub()
            self._park_debounce_unsub = None

        if self._unsub_coordinator_listener is not None:
            self._unsub_coordinator_listener()
            self._unsub_coordinator_listener = None

        _LOGGER.debug("DriveTracker unloaded for VIN %s", self.vin)

    def async_add_listener(
        self, update_callback: Callable[[DriveState], None]
    ) -> Callable[[], None]:
        """Register a callback for drive state changes."""
        self._listeners.append(update_callback)

        def remove_listener() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove_listener

    def _notify_listeners(self) -> None:
        """Notify all registered listeners of drive state changes."""
        for listener in list(self._listeners):
            try:
                listener(self.drive_state)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error in DriveTracker state listener: %s", err)

    @callback
    def handle_coordinator_update(self) -> None:
        """Handle coordinator state update and drive state machine transitions."""
        if not self.coordinator.data:
            return

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
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        now_epoch = int(now_dt.timestamp())

        odometer_m = self._get_float_coordinator_val("vehicleMileage")
        battery_soc = self._get_float_coordinator_val("batteryLevel", default=0.0)
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
            "segments": [],
            "current_segment_start_dt": now_dt,
            "current_segment_start_odo_m": odometer_m,
            "current_segment_start_soc": battery_soc,
            "current_segment_start_alt_m": altitude_m,
            "current_segment_speeds": [],
        }

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

        now_dt = datetime.now(timezone.utc)
        active = self._active_drive

        prev_dt: datetime = active["last_update_dt"]
        delta_t_sec = max(0.0, (now_dt - prev_dt).total_seconds())
        active["last_update_dt"] = now_dt

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

        # Collect speed sample for current 3-minute segment
        if speed_mph > 0.0:
            active["current_segment_speeds"].append(speed_mph)

        # Check if 3 minutes (180 seconds) have elapsed for the current segment
        seg_start_dt: datetime = active.get("current_segment_start_dt", now_dt)
        seg_elapsed = (now_dt - seg_start_dt).total_seconds()
        if seg_elapsed >= 180.0:
            self._finalize_current_segment(active, now_dt, odometer_m, battery_soc, altitude_m)

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
        self._notify_listeners()

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
                "timestamp": datetime.now(timezone.utc).isoformat(),
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

    def _finalize_current_segment(
        self,
        active: dict[str, Any],
        now_dt: datetime,
        odometer_m: float | None,
        battery_soc: float | None,
        altitude_m: float | None,
    ) -> None:
        """Finalize a 3-minute segment and start a new one."""
        seg_start_dt: datetime = active.get("current_segment_start_dt", now_dt)
        dt_win = (now_dt - seg_start_dt).total_seconds()
        if dt_win < 90.0:
            return

        s_odo_m = active.get("current_segment_start_odo_m")
        if s_odo_m is not None and odometer_m is not None:
            seg_dist = max(0.0, (odometer_m - s_odo_m) / METERS_PER_MILE)
        else:
            seg_dist = 0.0

        s_soc = active.get("current_segment_start_soc") or (battery_soc or 0.0)
        e_soc = battery_soc or s_soc
        seg_dsoc = max(0.0, s_soc - e_soc)
        bat_cap = active.get("battery_capacity_kwh", 135.0)
        seg_kwh = (seg_dsoc * bat_cap) / 100.0

        speeds = active.get("current_segment_speeds", [])
        if speeds:
            seg_avg_spd = round(sum(speeds) / len(speeds), 1)
        elif seg_dist > 0 and dt_win > 0:
            seg_avg_spd = round(seg_dist / (dt_win / 3600.0), 1)
        else:
            seg_avg_spd = 0.0

        s_alt = active.get("current_segment_start_alt_m")
        e_alt = altitude_m or s_alt
        seg_elev = (
            round((e_alt - s_alt) * METERS_TO_FEET, 1)
            if s_alt is not None and e_alt is not None
            else 0.0
        )

        seg_bin = get_speed_bin_key(seg_avg_spd)

        if seg_kwh > 0.0 and seg_dist >= 0.05:
            seg_eff = round(seg_dist / seg_kwh, 2)
            active["segments"].append(
                DriveSegment(
                    start_time=seg_start_dt.isoformat(),
                    duration_seconds=round(dt_win, 1),
                    distance_miles=round(seg_dist, 2),
                    energy_kwh=round(seg_kwh, 2),
                    efficiency_mi_kwh=seg_eff,
                    avg_speed_mph=seg_avg_spd,
                    speed_bin=seg_bin,
                    elevation_change_ft=seg_elev,
                )
            )

        active["current_segment_start_dt"] = now_dt
        active["current_segment_start_odo_m"] = odometer_m
        active["current_segment_start_soc"] = battery_soc
        active["current_segment_start_alt_m"] = altitude_m
        active["current_segment_speeds"] = []

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

    async def _async_finalize_drive(self) -> DriveRecord | None:
        """Finalize the active drive, compute all summary metrics, and save to DriveStore."""
        if self._active_drive is None:
            return None

        if self._park_debounce_unsub is not None:
            self._park_debounce_unsub()
            self._park_debounce_unsub = None

        active = self._active_drive
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        # Telemetry updates
        odometer_m = self._get_float_coordinator_val("vehicleMileage")
        if odometer_m is not None:
            active["last_odometer_m"] = odometer_m

        battery_soc = self._get_float_coordinator_val("batteryLevel")
        if battery_soc is not None:
            active["last_soc"] = battery_soc

        altitude_m = self._get_float_coordinator_val("gnssAltitude")
        if altitude_m is not None:
            active["last_altitude_m"] = altitude_m

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

        # Finalize any pending 3-minute segment
        self._finalize_current_segment(
            active, now_dt, end_odo_m, battery_soc, altitude_m
        )

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
            segments=active.get("segments", []),
        )

        # Save to DriveStore
        await self.store.async_save_drive(record)
        _LOGGER.info(
            "Finalized drive %s for VIN %s: %.2f mi, %.2f kWh (%.2f mi/kWh, micro=%s)",
            drive_id,
            self.vin,
            record.distance_miles,
            record.energy_kwh,
            record.efficiency_mi_kwh,
            record.is_micro_drive,
        )

        self._active_drive = None
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
        self._notify_listeners()
        return record

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

    def _schedule_coro(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Schedule coroutine in Home Assistant event loop safely."""
        if hasattr(self.hass, "async_create_task"):
            self.hass.async_create_task(coro)
        else:
            try:
                loop = getattr(self.hass, "loop", None) or asyncio.get_running_loop()
                loop.create_task(coro)
            except RuntimeError:
                asyncio.run(coro)
