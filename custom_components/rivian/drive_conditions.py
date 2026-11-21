"""Per-drive driving conditions: headwind, air density and a weather summary.

Pure Python (stdlib only, no Home Assistant imports). Everything here is
derived from a drive's GPS route plus its weather samples (live Open-Meteo
``current`` samples taken by the tracker, or archive hours from the weather
backfill). A weather sample is a dict with any of ``timestamp`` (ISO-8601) or
``t`` (POSIX seconds), ``temp_f``, ``wind_speed_mph``, ``wind_dir_deg`` (the
direction the wind blows *from*), ``pressure_hpa``, ``precip_mm`` and
``humidity_pct``; a sample missing a field simply doesn't contribute to it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from itertools import pairwise
import math
from typing import Any, Final

from .drive_track import DriveTrack, TrackPoint, haversine_m
from .energy_model import EnergyModelParams, expected_battery_kwh

MPH_PER_MPS: Final[float] = 2.2369362920544
# Open-Meteo reports wind at 10 m. A car is ~2 m off the ground, where the wind
# is weaker (surface friction): the log law with a ~0.3 m roughness length for
# a mix of open and built-up terrain gives ln(2/0.3)/ln(10/0.3) ~= 0.54, and
# open country ~0.75; 0.75 is used as a conservative single constant.
WIND_HEIGHT_FACTOR: Final[float] = 0.75
# Track pieces shorter than this have no meaningful heading (GPS noise); a
# piece spanning a long fix gap, or implying an impossible speed, is skipped.
MIN_PIECE_M: Final[float] = 8.0
MAX_PIECE_GAP_S: Final[float] = 120.0
MAX_PIECE_SPEED_MPS: Final[float] = 60.0
# Dry-air gas constant, water-vapour gas constant (J/(kg K)).
_R_DRY: Final[float] = 287.058
_R_VAPOUR: Final[float] = 461.495


def parse_sample_time(sample: dict[str, Any]) -> float | None:
    """Return a weather sample's POSIX time, from ``t`` or an ISO ``timestamp``."""
    t = sample.get("t")
    if isinstance(t, (int, float)):
        return float(t)
    raw = sample.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from north."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        dlon
    )
    return math.degrees(math.atan2(y, x)) % 360.0


def air_density(
    temp_c: float, pressure_hpa: float, rel_humidity_pct: float | None = None
) -> float:
    """Moist-air density in kg/m^3 from temperature, station pressure and humidity.

    Tetens' formula gives the saturation vapour pressure; the density is the
    sum of the dry-air and water-vapour partial densities. Missing humidity is
    taken as 50%. 15 C, 1013.25 hPa, dry gives 1.225 kg/m^3.
    """
    rh = 50.0 if rel_humidity_pct is None else min(max(rel_humidity_pct, 0.0), 100.0)
    kelvin = temp_c + 273.15
    sat_hpa = 6.1078 * 10.0 ** (7.5 * temp_c / (temp_c + 237.3))
    vapour_pa = rh / 100.0 * sat_hpa * 100.0
    dry_pa = pressure_hpa * 100.0 - vapour_pa
    return dry_pa / (_R_DRY * kelvin) + vapour_pa / (_R_VAPOUR * kelvin)


def _wind_samples(samples: list[dict[str, Any]]) -> list[tuple[float, float, float]]:
    """``(t, speed_mph, from_deg)`` for each usable wind sample, sorted by time."""
    out: list[tuple[float, float, float]] = []
    for sample in samples or []:
        if not isinstance(sample, dict):
            continue
        speed = sample.get("wind_speed_mph")
        direction = sample.get("wind_dir_deg")
        t = parse_sample_time(sample)
        if t is None or not isinstance(speed, (int, float)):
            continue
        if not isinstance(direction, (int, float)):
            continue
        out.append((t, float(speed), float(direction)))
    out.sort(key=lambda item: item[0])
    return out


def _nearest(winds: list[tuple[float, float, float]], t: float) -> tuple[float, float]:
    """``(speed_mph, from_deg)`` of the wind sample nearest in time to ``t``."""
    best = min(winds, key=lambda w: abs(w[0] - t))
    return best[1], best[2]


def _pieces(
    track: DriveTrack, winds: list[tuple[float, float, float]]
) -> list[tuple[float, float, float, float]]:
    """``(dist_m, heading_deg, wind_mph_at_2m, wind_from_deg)`` per usable track piece."""
    pieces: list[tuple[float, float, float, float]] = []
    pts = track.points
    for a, b in pairwise(pts):
        dt = b.t - a.t
        if dt <= 0 or dt > MAX_PIECE_GAP_S:
            continue
        dist = haversine_m(a.lat, a.lon, b.lat, b.lon)
        if dist < MIN_PIECE_M or dist / dt > MAX_PIECE_SPEED_MPS:
            continue
        speed, from_deg = _nearest(winds, (a.t + b.t) / 2.0)
        pieces.append(
            (
                dist,
                bearing_deg(a.lat, a.lon, b.lat, b.lon),
                speed * WIND_HEIGHT_FACTOR,
                from_deg,
            )
        )
    return pieces


def headwind_component(
    track: DriveTrack, samples: list[dict[str, Any]]
) -> float | None:
    """Distance-weighted headwind (mph) a drive saw; positive is a headwind.

    For each usable track piece the wind sample nearest in time supplies a
    10 m wind speed (scaled to vehicle height by ``WIND_HEIGHT_FACTOR``) and
    the direction it blows *from*. The component along the direction of travel
    is ``speed * cos(wind_from - heading)``: wind from the north on a
    northbound drive is a full headwind, a crosswind is ~0 and a tailwind is
    negative. Pieces are weighted by their length, so a long highway stretch
    outweighs a parking-lot shuffle. ``None`` with no wind data or no usable
    piece.
    """
    winds = _wind_samples(samples)
    if not winds or len(track.points) < 2:
        return None
    total = 0.0
    weight = 0.0
    for dist, heading, speed, from_deg in _pieces(track, winds):
        total += dist * speed * math.cos(math.radians(from_deg - heading))
        weight += dist
    return total / weight if weight > 0 else None


def make_headwind_fn(
    track: DriveTrack, samples: list[dict[str, Any]]
) -> Callable[[TrackPoint, TrackPoint], float] | None:
    """Return ``f(point_a, point_b) -> headwind m/s`` for the energy model, or None.

    Same geometry and wind scaling as :func:`headwind_component`, evaluated per
    interval. ``None`` when the samples hold no wind.
    """
    winds = _wind_samples(samples)
    if not winds:
        return None

    def headwind_mps(a: TrackPoint, b: TrackPoint) -> float:
        if haversine_m(a.lat, a.lon, b.lat, b.lon) < MIN_PIECE_M:
            return 0.0
        speed, from_deg = _nearest(winds, (a.t + b.t) / 2.0)
        heading = bearing_deg(a.lat, a.lon, b.lat, b.lon)
        return (
            speed
            * WIND_HEIGHT_FACTOR
            * math.cos(math.radians(from_deg - heading))
            / MPH_PER_MPS
        )

    return headwind_mps


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize_conditions(
    track: DriveTrack | None,
    samples: list[dict[str, Any]],
    duration_s: float = 0.0,
    temp_f: float | None = None,
) -> dict[str, float | None]:
    """Reduce a drive's weather samples to the per-drive condition columns.

    Returns ``wind_speed_mph``/``wind_dir_deg`` (the distance-weighted mean wind
    *vector* at 10 m, so opposing legs of a loop cancel; direction is where it
    blows from), ``headwind_mph`` (:func:`headwind_component`),
    ``precip_mm`` (mean sample precipitation, an hourly rate, times the drive's
    duration in hours -- an estimate), ``pressure_hpa`` and ``humidity_pct``
    (sample means) and ``air_density`` (kg/m^3, from the mean temperature --
    or ``temp_f`` when the samples carry none -- pressure and humidity). Every
    value is ``None`` when its inputs are missing.
    """
    samples = [s for s in samples or [] if isinstance(s, dict)]
    out: dict[str, float | None] = dict.fromkeys(
        (
            "wind_speed_mph",
            "wind_dir_deg",
            "headwind_mph",
            "precip_mm",
            "pressure_hpa",
            "humidity_pct",
            "air_density",
        )
    )

    winds = _wind_samples(samples)
    if winds:
        # Weights: route piece length when there is a track, else each sample equally.
        weighted: list[tuple[float, float, float]] = []
        if track is not None and len(track.points) >= 2:
            weighted = [
                (dist, speed / WIND_HEIGHT_FACTOR, from_deg)
                for dist, _h, speed, from_deg in _pieces(track, winds)
            ]
        if not weighted:
            weighted = [(1.0, speed, from_deg) for _t, speed, from_deg in winds]
        total_w = sum(w for w, _s, _d in weighted)
        # Unit vector pointing where the wind comes FROM, east/north components.
        east = sum(w * s * math.sin(math.radians(d)) for w, s, d in weighted) / total_w
        north = sum(w * s * math.cos(math.radians(d)) for w, s, d in weighted) / total_w
        out["wind_speed_mph"] = round(math.hypot(east, north), 1)
        out["wind_dir_deg"] = round(math.degrees(math.atan2(east, north)) % 360.0, 0)
        if track is not None:
            hw = headwind_component(track, samples)
            out["headwind_mph"] = round(hw, 1) if hw is not None else None

    precip = [
        float(s["precip_mm"])
        for s in samples
        if isinstance(s.get("precip_mm"), (int, float))
    ]
    if precip:
        out["precip_mm"] = round(_mean(precip) * max(duration_s, 0.0) / 3600.0, 2)  # type: ignore[operator]
    pressure = _mean(
        [
            float(s["pressure_hpa"])
            for s in samples
            if isinstance(s.get("pressure_hpa"), (int, float))
        ]
    )
    humidity = _mean(
        [
            float(s["humidity_pct"])
            for s in samples
            if isinstance(s.get("humidity_pct"), (int, float))
        ]
    )
    if pressure is not None:
        out["pressure_hpa"] = round(pressure, 1)
    if humidity is not None:
        out["humidity_pct"] = round(humidity, 1)

    temps = [
        float(s["temp_f"]) for s in samples if isinstance(s.get("temp_f"), (int, float))
    ]
    mean_temp_f = _mean(temps) if temps else temp_f
    if pressure is not None and mean_temp_f is not None:
        out["air_density"] = round(
            air_density((mean_temp_f - 32.0) * 5.0 / 9.0, pressure, humidity), 4
        )
    return out


def archive_samples(
    hourly: dict[str, dict[str, float]], start_ts: float, end_ts: float
) -> list[dict[str, Any]]:
    """Weather samples (``t`` in POSIX seconds) from archive hours around a drive.

    ``hourly`` maps a UTC ISO hour (``2026-08-20T14:00``) to its condition
    dict (see ``OpenMeteoWeatherClient.async_get_historical_conditions``).
    Hours from one hour before ``start_ts`` to one hour after ``end_ts`` are
    returned in time order, so the drive is bracketed by real values.
    """
    out: list[dict[str, Any]] = []
    for key, cond in hourly.items():
        try:
            dt = datetime.fromisoformat(str(key))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        t = dt.timestamp()
        if start_ts - 3600.0 <= t <= end_ts + 3600.0:
            out.append({"t": t, **cond})
    out.sort(key=lambda sample: sample["t"])
    return out


def compute_condition_columns(
    track: DriveTrack | None,
    samples: list[dict[str, Any]],
    params: EnergyModelParams,
    *,
    duration_s: float,
    distance_miles: float,
    temp_f: float | None = None,
) -> dict[str, float | None]:
    """All the per-drive condition columns, plus the model's ``expected_kwh``.

    :func:`summarize_conditions` for the weather, then the energy model's
    expected battery kWh for the route with the drive's headwind added to the
    airspeed and its measured air density (the model's own density when none
    is known). ``expected_kwh`` is ``None`` without a usable track.
    """
    cols = summarize_conditions(track, samples, duration_s, temp_f)
    expected = None
    if track is not None and len(track.points) >= 2:
        expected = expected_battery_kwh(
            track,
            params,
            distance_miles,
            rho=cols["air_density"],
            headwind_fn=make_headwind_fn(track, samples),
        )
    cols["expected_kwh"] = round(expected, 3) if expected is not None else None
    return cols
