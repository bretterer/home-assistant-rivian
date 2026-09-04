"""Rivian Trip Efficiency & Analytics Data Models."""

from __future__ import annotations

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
class DriveSegment:
    """Fixed-duration (e.g. 3-minute) driving segment for speed-bin efficiency analysis."""

    start_time: str
    duration_seconds: float
    distance_miles: float
    energy_kwh: float
    efficiency_mi_kwh: float
    avg_speed_mph: float
    speed_bin: str
    elevation_change_ft: float = 0.0
    temp_f: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize drive segment to dictionary."""
        return {
            "start_time": self.start_time,
            "duration_seconds": round(self.duration_seconds, 1),
            "distance_miles": round(self.distance_miles, 2),
            "energy_kwh": round(self.energy_kwh, 2),
            "efficiency_mi_kwh": round(self.efficiency_mi_kwh, 2),
            "avg_speed_mph": round(self.avg_speed_mph, 1),
            "speed_bin": self.speed_bin,
            "elevation_change_ft": round(self.elevation_change_ft, 1),
            "temp_f": round(self.temp_f, 1) if self.temp_f is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DriveSegment:
        """Instantiate drive segment from dictionary."""
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
    segments: list[DriveSegment] = field(default_factory=list)

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
        if self.segments:
            data["segments"] = [s.to_dict() for s in self.segments]

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
            segments=[
                DriveSegment.from_dict(s)
                for s in data.get("segments", [])
                if isinstance(s, dict)
            ],
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

