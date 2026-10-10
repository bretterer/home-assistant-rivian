"""Rivian Trip Efficiency & Analytics Data Models."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

MPGE_FACTOR: Final[float] = 33.705
MICRO_DRIVE_THRESHOLD_MILES: Final[float] = 0.5
STANDARD_SPEED_BINS: Final[list[str]] = [
    "0-9",
    "10-19",
    "20-29",
    "30-39",
    "40-49",
    "50-59",
    "60-69",
    "70-79",
    "80+",
]


# trailerStatus values that clearly mean a trailer is physically connected /
# disconnected. Rivian's GraphQL schema does not document this enum publicly;
# these are the values observed in practice. An unrecognized value maps to
# None (ignored) rather than guessed at.
TRAILER_CONNECTED_STATES: Final[frozenset[str]] = frozenset({"connected", "attached"})
TRAILER_DISCONNECTED_STATES: Final[frozenset[str]] = frozenset(
    {"not_connected", "disconnected", "no_trailer", "none", "unknown"}
)


def trailer_status_to_bool(raw: str | None) -> bool | None:
    """Map a raw trailerStatus value to True/False, or None if unrecognized."""
    if raw is None:
        return None
    normalized = str(raw).strip().lower()
    if normalized in TRAILER_CONNECTED_STATES:
        return True
    if normalized in TRAILER_DISCONNECTED_STATES:
        return False
    return None


def trailer_attached_any(raw_values: Iterable[str | None]) -> bool | None:
    """Return True if any raw trailerStatus value in the sequence means attached.

    False if none did but at least one clearly meant "not attached", or None
    if nothing in the sequence was recognized.
    """
    seen_false = False
    for raw in raw_values:
        mapped = trailer_status_to_bool(raw)
        if mapped is True:
            return True
        if mapped is False:
            seen_false = True
    return False if seen_false else None


class DriveStatus(StrEnum):
    """Drive lifecycle status."""

    PARKED = "Parked"
    DRIVING = "Driving"


@dataclass
class SpeedBinData:
    """Accumulated distance and duration for a speed bin."""

    miles: float = 0.0
    seconds: float = 0.0

    def to_dict(self) -> dict[str, float]:
        """Serialize speed bin to dictionary."""
        return {
            "miles": round(self.miles, 3),
            "seconds": round(self.seconds, 1),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | float) -> SpeedBinData:
        """Instantiate speed bin data from dictionary or numeric miles."""
        if isinstance(data, (int, float)):
            return cls(miles=float(data), seconds=0.0)
        if isinstance(data, dict):
            return cls(
                miles=float(data.get("miles", 0.0)),
                seconds=float(data.get("seconds", 0.0)),
            )
        return cls()


@dataclass
class DriveChunk:
    """Fixed-duration (e.g. 3-minute) driving chunk for speed-bin efficiency analysis."""

    start_time: str
    duration_seconds: float
    distance_miles: float
    energy_kwh: float
    efficiency_mi_kwh: float
    avg_speed_mph: float
    speed_bin: str
    elevation_change_ft: float = 0.0
    temp_f: float | None = None

    @property
    def mpge(self) -> float:
        """Return MPGe equivalent."""
        return round(self.efficiency_mi_kwh * MPGE_FACTOR, 1)

    def to_dict(self) -> dict[str, Any]:
        """Serialize drive chunk to dictionary."""
        return {
            "start_time": self.start_time,
            "duration_seconds": round(self.duration_seconds, 1),
            "distance_miles": round(self.distance_miles, 2),
            "energy_kwh": round(self.energy_kwh, 2),
            "efficiency_mi_kwh": round(self.efficiency_mi_kwh, 2),
            "mpge": self.mpge,
            "avg_speed_mph": round(self.avg_speed_mph, 1),
            "speed_bin": self.speed_bin,
            "elevation_change_ft": round(self.elevation_change_ft, 1),
            "temp_f": round(self.temp_f, 1) if self.temp_f is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DriveChunk:
        """Instantiate drive chunk from dictionary."""
        return cls(
            start_time=str(data.get("start_time", "")),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            distance_miles=float(data.get("distance_miles", 0.0)),
            energy_kwh=float(data.get("energy_kwh", 0.0)),
            efficiency_mi_kwh=float(data.get("efficiency_mi_kwh", 0.0)),
            avg_speed_mph=float(data.get("avg_speed_mph", 0.0)),
            speed_bin=str(data.get("speed_bin", "0-9")),
            elevation_change_ft=float(data.get("elevation_change_ft", 0.0)),
            temp_f=float(data["temp_f"]) if data.get("temp_f") is not None else None,
        )


@dataclass
class AggregatedDriveStats:
    """Aggregated statistics across multiple drives."""

    total_miles: float = 0.0
    total_kwh: float = 0.0
    efficiency_mi_kwh: float = 0.0
    mpge: float = 0.0
    drive_count: int = 0
    total_duration_seconds: float = 0.0
    avg_distance_miles: float = 0.0
    total_micro_drives: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize aggregated statistics to dictionary."""
        return {
            "total_miles": round(self.total_miles, 2),
            "total_kwh": round(self.total_kwh, 2),
            "efficiency_mi_kwh": round(self.efficiency_mi_kwh, 2),
            "mpge": round(self.mpge, 2),
            "drive_count": self.drive_count,
            "total_duration_seconds": round(self.total_duration_seconds, 1),
            "avg_distance_miles": round(self.avg_distance_miles, 2),
            "total_micro_drives": self.total_micro_drives,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AggregatedDriveStats:
        """Instantiate aggregated statistics from dictionary."""
        total_miles = float(data.get("total_miles", 0.0))
        total_kwh = float(data.get("total_kwh", 0.0))
        efficiency_mi_kwh = float(data.get("efficiency_mi_kwh", 0.0))
        if efficiency_mi_kwh == 0.0 and total_kwh > 0.0:
            efficiency_mi_kwh = total_miles / total_kwh

        mpge = float(data.get("mpge", 0.0))
        if mpge == 0.0 and efficiency_mi_kwh > 0.0:
            mpge = efficiency_mi_kwh * MPGE_FACTOR

        drive_count = int(data.get("drive_count", 0))
        avg_dist = float(data.get("avg_distance_miles", 0.0))
        if avg_dist == 0.0 and drive_count > 0:
            avg_dist = total_miles / drive_count

        return cls(
            total_miles=total_miles,
            total_kwh=total_kwh,
            efficiency_mi_kwh=efficiency_mi_kwh,
            mpge=mpge,
            drive_count=drive_count,
            total_duration_seconds=float(data.get("total_duration_seconds", 0.0)),
            avg_distance_miles=avg_dist,
            total_micro_drives=int(data.get("total_micro_drives", 0)),
        )


@dataclass
class DriveState:
    """Current live in-memory drive state."""

    is_driving: bool = False
    status: str = DriveStatus.PARKED.value
    current_trip_distance_mi: float = 0.0
    current_trip_duration: float = 0.0
    current_trip_kwh: float = 0.0
    current_trip_efficiency: float = 0.0
    current_speed_mph: float = 0.0
    current_altitude_ft: float = 0.0
    gps_locked: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize drive state to dictionary."""
        return {
            "is_driving": self.is_driving,
            "status": self.status,
            "current_trip_distance_mi": round(self.current_trip_distance_mi, 2),
            "current_trip_duration": round(self.current_trip_duration, 1),
            "current_trip_kwh": round(self.current_trip_kwh, 2),
            "current_trip_efficiency": round(self.current_trip_efficiency, 2),
            "current_speed_mph": round(self.current_speed_mph, 1),
            "current_altitude_ft": round(self.current_altitude_ft, 1),
            "gps_locked": self.gps_locked,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DriveState:
        """Instantiate drive state from dictionary."""
        return cls(
            is_driving=bool(data.get("is_driving", False)),
            status=str(data.get("status", DriveStatus.PARKED.value)),
            current_trip_distance_mi=float(data.get("current_trip_distance_mi", 0.0)),
            current_trip_duration=float(data.get("current_trip_duration", 0.0)),
            current_trip_kwh=float(data.get("current_trip_kwh", 0.0)),
            current_trip_efficiency=float(data.get("current_trip_efficiency", 0.0)),
            current_speed_mph=float(data.get("current_speed_mph", 0.0)),
            current_altitude_ft=float(data.get("current_altitude_ft", 0.0)),
            gps_locked=bool(data.get("gps_locked", False)),
        )


@dataclass
class DriveRecord:
    """Completed drive record and telemetry."""

    vin: str
    drive_id: str
    start_time: str
    end_time: str
    distance_miles: float
    duration_seconds: float
    start_soc: float
    end_soc: float
    battery_capacity_kwh: float
    energy_kwh: float
    efficiency_mi_kwh: float = 0.0
    mpge: float = 0.0
    start_altitude_ft: float = 0.0
    end_altitude_ft: float = 0.0
    elevation_change_ft: float = 0.0
    avg_speed_mph: float = 0.0
    max_speed_mph: float = 0.0
    integrated_temperature_f: float | None = None
    speed_bins: dict[str, Any] = field(default_factory=dict)
    is_micro_drive: bool = False
    start_odometer_mi: float | None = None
    end_odometer_mi: float | None = None
    start_lat: float | None = None
    start_lon: float | None = None
    end_lat: float | None = None
    end_lon: float | None = None
    weather_samples: list[dict[str, Any]] = field(default_factory=list)
    chunks: list[DriveChunk] = field(default_factory=list)
    # Track-derived (Strava-style) summary stats: computed once from the
    # drive's GPS route by drive_stats.compute_track_stats(), None when the
    # drive has no stored track or a needed telemetry column was missing.
    moving_seconds: float | None = None
    stopped_seconds: float | None = None
    stop_count: int | None = None
    climb_ft: float | None = None
    descent_ft: float | None = None
    track_max_speed_mph: float | None = None
    pct_distance_over_70mph: float | None = None
    # Live vehicle context captured during the drive by DriveTracker.
    start_range_mi: float | None = None
    end_range_mi: float | None = None
    drive_modes: list[str] = field(default_factory=list)
    trailer: bool | None = None
    driver: str | None = None

    def __post_init__(self) -> None:
        """Compute derived fields if not populated."""
        if self.elevation_change_ft == 0.0 and (
            self.end_altitude_ft != 0.0 or self.start_altitude_ft != 0.0
        ):
            self.elevation_change_ft = round(
                self.end_altitude_ft - self.start_altitude_ft, 1
            )

        if self.efficiency_mi_kwh == 0.0 and self.energy_kwh > 0.0:
            self.efficiency_mi_kwh = round(self.distance_miles / self.energy_kwh, 2)

        if self.mpge == 0.0 and self.efficiency_mi_kwh > 0.0:
            self.mpge = round(self.efficiency_mi_kwh * MPGE_FACTOR, 2)

        if (
            not self.is_micro_drive
            and self.distance_miles < MICRO_DRIVE_THRESHOLD_MILES
        ):
            self.is_micro_drive = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize drive record to JSON-compatible dictionary."""
        serialized_speed_bins: dict[str, Any] = {}
        for k, v in self.speed_bins.items():
            if isinstance(v, SpeedBinData):
                serialized_speed_bins[k] = v.to_dict()
            elif isinstance(v, dict):
                serialized_speed_bins[k] = v
            elif isinstance(v, (int, float)):
                serialized_speed_bins[k] = {
                    "miles": round(float(v), 3),
                    "seconds": 0.0,
                }
            else:
                serialized_speed_bins[k] = v

        data: dict[str, Any] = {
            "vin": self.vin,
            "drive_id": self.drive_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "distance_miles": round(self.distance_miles, 2),
            "duration_seconds": round(self.duration_seconds, 1),
            "start_soc": round(self.start_soc, 2),
            "end_soc": round(self.end_soc, 2),
            "battery_capacity_kwh": round(self.battery_capacity_kwh, 2),
            "energy_kwh": round(self.energy_kwh, 2),
            "efficiency_mi_kwh": round(self.efficiency_mi_kwh, 2),
            "mpge": round(self.mpge, 2),
            "start_altitude_ft": round(self.start_altitude_ft, 1),
            "end_altitude_ft": round(self.end_altitude_ft, 1),
            "elevation_change_ft": round(self.elevation_change_ft, 1),
            "avg_speed_mph": round(self.avg_speed_mph, 1),
            "max_speed_mph": round(self.max_speed_mph, 1),
            "integrated_temperature_f": (
                round(self.integrated_temperature_f, 1)
                if self.integrated_temperature_f is not None
                else None
            ),
            "speed_bins": serialized_speed_bins,
            "is_micro_drive": self.is_micro_drive,
        }

        if self.start_odometer_mi is not None:
            data["start_odometer_mi"] = round(self.start_odometer_mi, 2)
        if self.end_odometer_mi is not None:
            data["end_odometer_mi"] = round(self.end_odometer_mi, 2)
        if self.start_lat is not None:
            data["start_lat"] = round(self.start_lat, 6)
        if self.start_lon is not None:
            data["start_lon"] = round(self.start_lon, 6)
        if self.end_lat is not None:
            data["end_lat"] = round(self.end_lat, 6)
        if self.end_lon is not None:
            data["end_lon"] = round(self.end_lon, 6)
        if self.weather_samples:
            data["weather_samples"] = self.weather_samples
        if self.chunks:
            data["chunks"] = [c.to_dict() for c in self.chunks]

        if self.moving_seconds is not None:
            data["moving_seconds"] = round(self.moving_seconds, 1)
        if self.stopped_seconds is not None:
            data["stopped_seconds"] = round(self.stopped_seconds, 1)
        if self.stop_count is not None:
            data["stop_count"] = self.stop_count
        if self.climb_ft is not None:
            data["climb_ft"] = round(self.climb_ft, 1)
        if self.descent_ft is not None:
            data["descent_ft"] = round(self.descent_ft, 1)
        if self.track_max_speed_mph is not None:
            data["track_max_speed_mph"] = round(self.track_max_speed_mph, 1)
        if self.pct_distance_over_70mph is not None:
            data["pct_distance_over_70mph"] = round(self.pct_distance_over_70mph, 2)
        if self.start_range_mi is not None:
            data["start_range_mi"] = round(self.start_range_mi, 1)
        if self.end_range_mi is not None:
            data["end_range_mi"] = round(self.end_range_mi, 1)
        if self.drive_modes:
            data["drive_modes"] = list(self.drive_modes)
        if self.trailer is not None:
            data["trailer"] = self.trailer
        if self.driver is not None:
            data["driver"] = self.driver

        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DriveRecord:
        """Instantiate drive record from dictionary."""
        speed_bins: dict[str, Any] = {}
        raw_bins = data.get("speed_bins", {})
        if isinstance(raw_bins, dict):
            for k, v in raw_bins.items():
                if isinstance(v, dict):
                    speed_bins[k] = SpeedBinData.from_dict(v)
                elif isinstance(v, (int, float)):
                    speed_bins[k] = SpeedBinData(miles=float(v), seconds=0.0)
                elif isinstance(v, SpeedBinData):
                    speed_bins[k] = v

        distance_miles = float(data.get("distance_miles", 0.0))
        energy_kwh = float(data.get("energy_kwh", 0.0))
        efficiency_mi_kwh = float(data.get("efficiency_mi_kwh", 0.0))
        if efficiency_mi_kwh == 0.0 and energy_kwh > 0.0:
            efficiency_mi_kwh = distance_miles / energy_kwh

        mpge = float(data.get("mpge", 0.0))
        if mpge == 0.0 and efficiency_mi_kwh > 0.0:
            mpge = efficiency_mi_kwh * MPGE_FACTOR

        start_altitude_ft = float(data.get("start_altitude_ft", 0.0))
        end_altitude_ft = float(data.get("end_altitude_ft", 0.0))
        elevation_change_ft = float(
            data.get("elevation_change_ft", end_altitude_ft - start_altitude_ft)
        )

        is_micro = data.get("is_micro_drive")
        if is_micro is None:
            is_micro = distance_miles < MICRO_DRIVE_THRESHOLD_MILES
        else:
            is_micro = bool(is_micro)

        return cls(
            vin=str(data.get("vin", "")),
            drive_id=str(data.get("drive_id", "")),
            start_time=str(data.get("start_time", "")),
            end_time=str(data.get("end_time", "")),
            distance_miles=distance_miles,
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            start_soc=float(data.get("start_soc", 0.0)),
            end_soc=float(data.get("end_soc", 0.0)),
            battery_capacity_kwh=float(data.get("battery_capacity_kwh", 0.0)),
            energy_kwh=energy_kwh,
            efficiency_mi_kwh=efficiency_mi_kwh,
            mpge=mpge,
            start_altitude_ft=start_altitude_ft,
            end_altitude_ft=end_altitude_ft,
            elevation_change_ft=elevation_change_ft,
            avg_speed_mph=float(data.get("avg_speed_mph", 0.0)),
            max_speed_mph=float(data.get("max_speed_mph", 0.0)),
            integrated_temperature_f=(
                float(data["integrated_temperature_f"])
                if data.get("integrated_temperature_f") is not None
                else None
            ),
            speed_bins=speed_bins,
            is_micro_drive=is_micro,
            start_odometer_mi=(
                float(data["start_odometer_mi"])
                if data.get("start_odometer_mi") is not None
                else None
            ),
            end_odometer_mi=(
                float(data["end_odometer_mi"])
                if data.get("end_odometer_mi") is not None
                else None
            ),
            start_lat=(
                float(data["start_lat"]) if data.get("start_lat") is not None else None
            ),
            start_lon=(
                float(data["start_lon"]) if data.get("start_lon") is not None else None
            ),
            end_lat=(
                float(data["end_lat"]) if data.get("end_lat") is not None else None
            ),
            end_lon=(
                float(data["end_lon"]) if data.get("end_lon") is not None else None
            ),
            weather_samples=data.get("weather_samples", []),
            # "segments" is a legacy fallback: pre-rename JSON exports (from the
            # one-time import of old rivian_drives_<VIN>.json files) used that key.
            chunks=[
                DriveChunk.from_dict(c)
                for c in data.get("chunks", data.get("segments", []))
                if isinstance(c, dict)
            ],
            moving_seconds=(
                float(data["moving_seconds"])
                if data.get("moving_seconds") is not None
                else None
            ),
            stopped_seconds=(
                float(data["stopped_seconds"])
                if data.get("stopped_seconds") is not None
                else None
            ),
            stop_count=(
                int(data["stop_count"]) if data.get("stop_count") is not None else None
            ),
            climb_ft=(
                float(data["climb_ft"]) if data.get("climb_ft") is not None else None
            ),
            descent_ft=(
                float(data["descent_ft"])
                if data.get("descent_ft") is not None
                else None
            ),
            track_max_speed_mph=(
                float(data["track_max_speed_mph"])
                if data.get("track_max_speed_mph") is not None
                else None
            ),
            pct_distance_over_70mph=(
                float(data["pct_distance_over_70mph"])
                if data.get("pct_distance_over_70mph") is not None
                else None
            ),
            start_range_mi=(
                float(data["start_range_mi"])
                if data.get("start_range_mi") is not None
                else None
            ),
            end_range_mi=(
                float(data["end_range_mi"])
                if data.get("end_range_mi") is not None
                else None
            ),
            drive_modes=list(data.get("drive_modes", [])),
            trailer=(
                bool(data["trailer"]) if data.get("trailer") is not None else None
            ),
            driver=(str(data["driver"]) if data.get("driver") is not None else None),
        )


@dataclass
class VampireDrainRecord:
    """Parked vampire drain event telemetry and metrics."""

    start_time: str
    end_time: str
    idle_hours: float
    start_soc: float
    end_soc: float
    drain_soc: float
    drain_kwh: float
    rate_pct_per_day: float
    avg_watts: float
    avg_temp_f: float | None = None
    latitude: float | None = None
    longitude: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize vampire drain record to dictionary."""
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "idle_hours": round(self.idle_hours, 2),
            "start_soc": round(self.start_soc, 2),
            "end_soc": round(self.end_soc, 2),
            "drain_soc": round(self.drain_soc, 2),
            "drain_kwh": round(self.drain_kwh, 2),
            "rate_pct_per_day": round(self.rate_pct_per_day, 2),
            "avg_watts": round(self.avg_watts, 1),
            "avg_temp_f": (
                round(self.avg_temp_f, 1) if self.avg_temp_f is not None else None
            ),
            "latitude": round(self.latitude, 6) if self.latitude is not None else None,
            "longitude": (
                round(self.longitude, 6) if self.longitude is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VampireDrainRecord:
        """Instantiate vampire drain record from dictionary."""
        return cls(
            start_time=str(data.get("start_time", "")),
            end_time=str(data.get("end_time", "")),
            idle_hours=float(data.get("idle_hours", 0.0)),
            start_soc=float(data.get("start_soc", 0.0)),
            end_soc=float(data.get("end_soc", 0.0)),
            drain_soc=float(data.get("drain_soc", 0.0)),
            drain_kwh=float(data.get("drain_kwh", 0.0)),
            rate_pct_per_day=float(data.get("rate_pct_per_day", 0.0)),
            avg_watts=float(data.get("avg_watts", 0.0)),
            avg_temp_f=(
                float(data["avg_temp_f"])
                if data.get("avg_temp_f") is not None
                else None
            ),
            latitude=(
                float(data["latitude"]) if data.get("latitude") is not None else None
            ),
            longitude=(
                float(data["longitude"]) if data.get("longitude") is not None else None
            ),
        )


DCFC_MIN_POWER_KW: Final[float] = 22.0
MAX_DCFC_HISTORY_SESSIONS: Final[int] = 50
# A session below DCFC_MIN_POWER_KW is kept as a home/AC ("ac") session only
# if it added at least this much charge and lasted at least this long; a blip
# (a plug-in that barely charged) is dropped.
AC_SESSION_MIN_SOC_GAIN_PCT: Final[float] = 1.0
AC_SESSION_MIN_DURATION_S: Final[float] = 300.0
# An AC session keeps no power curve, only a few coarse SoC points.
AC_SESSION_MAX_SOC_POINTS: Final[int] = 20
SESSION_KIND_DC: Final[str] = "dc"
SESSION_KIND_AC: Final[str] = "ac"
SESSION_SOURCES: Final[tuple[str, ...]] = (
    "live",
    "backfill",
    "demo",
    "rivian",
    "inferred",
)


@dataclass
class ChargingSample:
    """Individual telemetry sample during a charging session."""

    timestamp: str
    soc: float
    power_kw: float
    battery_temp_f: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize charging sample to dictionary."""
        return {
            "timestamp": self.timestamp,
            "soc": round(self.soc, 1),
            "power_kw": round(self.power_kw, 1),
            "battery_temp_f": (
                round(self.battery_temp_f, 1)
                if self.battery_temp_f is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChargingSample:
        """Instantiate charging sample from dictionary."""
        return cls(
            timestamp=str(data.get("timestamp", "")),
            soc=float(data.get("soc", 0.0)),
            power_kw=float(data.get("power_kw", 0.0)),
            battery_temp_f=(
                float(data["battery_temp_f"])
                if data.get("battery_temp_f") is not None
                else None
            ),
        )


@dataclass
class ChargingSessionRecord:
    """Charging session record (DC fast or AC/home) and curve telemetry.

    ``kind`` is ``"dc"`` or ``"ac"``; ``is_dcfc`` mirrors it (kept for the
    older callers and the stored ``is_dcfc`` column) and is derived from
    ``kind`` when that is given, else ``kind`` from ``is_dcfc``. An AC
    session's ``samples`` are coarse SoC points (power 0), not a curve.
    """

    session_id: str
    start_time: str
    end_time: str
    start_soc: float
    end_soc: float
    energy_added_kwh: float
    max_power_kw: float
    avg_power_kw: float
    samples: list[ChargingSample] = field(default_factory=list)
    is_dcfc: bool = True
    kind: str | None = None
    lat: float | None = None
    lon: float | None = None
    source: str = "live"
    # Station details: from the Rivian app's session list or an OpenStreetMap
    # lookup (all None until known). ``is_home`` is None when unknown.
    vendor: str | None = None
    network: str | None = None
    station_name: str | None = None
    station_version: str | None = None
    charger_max_kw: float | None = None
    is_home: bool | None = None
    rivian_txn_id: str | None = None
    # Mean outside air temperature where and while it charged (Open-Meteo,
    # filled after the session) and mean battery temperature (only when the
    # vehicle reports one). None until known.
    outside_temp_f: float | None = None
    battery_temp_f: float | None = None

    def __post_init__(self) -> None:
        """Keep ``kind`` and ``is_dcfc`` consistent."""
        if self.kind is None:
            self.kind = SESSION_KIND_DC if self.is_dcfc else SESSION_KIND_AC
        self.is_dcfc = self.kind == SESSION_KIND_DC

    def to_dict(self) -> dict[str, Any]:
        """Serialize charging session to dictionary."""
        return {
            "session_id": self.session_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "start_soc": round(self.start_soc, 1),
            "end_soc": round(self.end_soc, 1),
            "energy_added_kwh": round(self.energy_added_kwh, 2),
            "max_power_kw": round(self.max_power_kw, 1),
            "avg_power_kw": round(self.avg_power_kw, 1),
            "is_dcfc": self.is_dcfc,
            "kind": self.kind,
            "lat": self.lat,
            "lon": self.lon,
            "source": self.source,
            "vendor": self.vendor,
            "network": self.network,
            "station_name": self.station_name,
            "station_version": self.station_version,
            "charger_max_kw": self.charger_max_kw,
            "is_home": self.is_home,
            "rivian_txn_id": self.rivian_txn_id,
            "outside_temp_f": self.outside_temp_f,
            "battery_temp_f": self.battery_temp_f,
            "samples": [s.to_dict() for s in self.samples],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChargingSessionRecord:
        """Instantiate charging session from dictionary."""
        raw_samples = data.get("samples", [])
        return cls(
            session_id=str(data.get("session_id", "")),
            start_time=str(data.get("start_time", "")),
            end_time=str(data.get("end_time", "")),
            start_soc=float(data.get("start_soc", 0.0)),
            end_soc=float(data.get("end_soc", 0.0)),
            energy_added_kwh=float(data.get("energy_added_kwh", 0.0)),
            max_power_kw=float(data.get("max_power_kw", 0.0)),
            avg_power_kw=float(data.get("avg_power_kw", 0.0)),
            is_dcfc=bool(data.get("is_dcfc", True)),
            kind=data.get("kind"),
            lat=float(data["lat"]) if data.get("lat") is not None else None,
            lon=float(data["lon"]) if data.get("lon") is not None else None,
            source=str(data.get("source") or "live"),
            vendor=data.get("vendor"),
            network=data.get("network"),
            station_name=data.get("station_name"),
            station_version=data.get("station_version"),
            charger_max_kw=(
                float(data["charger_max_kw"])
                if data.get("charger_max_kw") is not None
                else None
            ),
            is_home=(
                bool(data["is_home"]) if data.get("is_home") is not None else None
            ),
            rivian_txn_id=data.get("rivian_txn_id"),
            outside_temp_f=(
                float(data["outside_temp_f"])
                if data.get("outside_temp_f") is not None
                else None
            ),
            battery_temp_f=(
                float(data["battery_temp_f"])
                if data.get("battery_temp_f") is not None
                else None
            ),
            samples=[
                ChargingSample.from_dict(s) for s in raw_samples if isinstance(s, dict)
            ],
        )
