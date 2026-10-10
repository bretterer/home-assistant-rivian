"""Strava-style per-drive summary statistics computed from a GPS route.

Pure Python (stdlib only, no Home Assistant imports), matching drive_track.py's
style: a single :func:`compute_track_stats` entry point that turns a
:class:`~.drive_track.DriveTrack` into a frozen :class:`TrackStats`. Every
field may be ``None`` when the track lacks the telemetry column it needs (or
has fewer than 2 points) -- callers must tolerate that rather than treating it
as zero.

There is deliberately no regen figure: the SoC Rivian reports doesn't rise
during regenerative braking (on 53 real routes, 5 of 8,089 readings rose, each
a single 0.1% flicker), so regen can't be derived from the route.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Final

from .drive_track import haversine_m

if TYPE_CHECKING:
    from .drive_track import DriveTrack, TrackPoint

# A fix is "stopped" when the speed at both ends of the interval is below
# this threshold.
STOPPED_SPEED_THRESHOLD_MPS: Final[float] = 0.5
# A gap between consecutive fixes longer than this is neither "moving" nor
# "stopped" -- the vehicle's state during the gap is simply unknown (e.g. GPS
# briefly lost signal, or the tracker was paused).
GPS_GAP_MAX_SECONDS: Final[float] = 120.0
# A stopped stretch must last at least this long to count as a "stop" (red
# light, stop sign, etc.) rather than GPS/speed noise around 0 mph.
STOP_MIN_DURATION_SECONDS: Final[float] = 20.0
# Altitude hysteresis: a change only counts toward climb/descent once the
# altitude has moved this many metres from the last counted extreme, so GPS
# altitude noise doesn't inflate the totals.
ALTITUDE_HYSTERESIS_M: Final[float] = 4.0
# 70 mph in metres/second.
HIGHWAY_SPEED_THRESHOLD_MPS: Final[float] = 31.29
# Percentile used for the "robust" max speed (spike-resistant).
MAX_SPEED_PERCENTILE: Final[float] = 0.99


@dataclass(frozen=True)
class TrackStats:
    """Per-drive summary statistics derived from a GPS route.

    Every field is ``None`` when the track has fewer than 2 points, or lacks
    the telemetry column the field needs (e.g. ``climb_m``/``descent_m``
    require at least 2 points with ``alt_m`` set).
    """

    moving_seconds: float | None = None
    stopped_seconds: float | None = None
    stop_count: int | None = None
    climb_m: float | None = None
    descent_m: float | None = None
    min_alt_m: float | None = None
    max_alt_m: float | None = None
    max_speed_mps: float | None = None
    pct_distance_over_70mph: float | None = None


def _interval_speeds_mps(
    p1: TrackPoint, p2: TrackPoint, dt: float
) -> tuple[float, float]:
    """Return (speed_at_p1, speed_at_p2), falling back to distance/time.

    If either point's ``speed_mps`` is missing, both ends use the same
    distance-over-time estimate for the interval instead.
    """
    if p1.speed_mps is not None and p2.speed_mps is not None:
        return p1.speed_mps, p2.speed_mps
    if dt <= 0.0:
        return 0.0, 0.0
    dist = haversine_m(p1.lat, p1.lon, p2.lat, p2.lon)
    est = dist / dt
    return est, est


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    lower = sorted_values[int(f)] * (c - k)
    upper = sorted_values[int(c)] * (k - f)
    return lower + upper


def _hysteresis_climb_descent(
    altitudes: list[float], threshold_m: float
) -> tuple[float, float]:
    """Return (climb_m, descent_m) using a zig-zag hysteresis peak/trough filter.

    Only a reversal of at least ``threshold_m`` from the running extreme
    confirms and commits the pending climb/descent since the last confirmed
    pivot, so GPS altitude noise smaller than the threshold never
    accumulates.
    """
    gain = 0.0
    loss = 0.0
    last_pivot = altitudes[0]
    trend = 0  # 0 = undetermined, 1 = up, -1 = down
    extreme = altitudes[0]

    for alt in altitudes[1:]:
        if trend == 0:
            if alt - last_pivot >= threshold_m:
                trend = 1
                extreme = alt
            elif last_pivot - alt >= threshold_m:
                trend = -1
                extreme = alt
        elif trend == 1:
            if alt >= extreme:
                extreme = alt
            elif extreme - alt >= threshold_m:
                gain += extreme - last_pivot
                last_pivot = extreme
                trend = -1
                extreme = alt
        else:  # trend == -1
            if alt <= extreme:
                extreme = alt
            elif alt - extreme >= threshold_m:
                loss += last_pivot - extreme
                last_pivot = extreme
                trend = 1
                extreme = alt

    if trend == 1:
        gain += extreme - last_pivot
    elif trend == -1:
        loss += last_pivot - extreme

    return gain, loss


def compute_track_stats(track: DriveTrack) -> TrackStats:
    """Compute Strava-style summary statistics for a completed drive's route."""
    points = track.points
    n = len(points)
    if n < 2:
        return TrackStats()

    moving_seconds = 0.0
    stopped_seconds = 0.0
    # One entry per consecutive-fix interval: "moving", "stopped", or "gap".
    interval_kinds: list[str] = []
    interval_durations: list[float] = []
    total_distance_m = 0.0
    highway_distance_m = 0.0

    for i in range(n - 1):
        p1, p2 = points[i], points[i + 1]
        dt = p2.t - p1.t
        dist = haversine_m(p1.lat, p1.lon, p2.lat, p2.lon)
        total_distance_m += dist

        if dt > GPS_GAP_MAX_SECONDS:
            interval_kinds.append("gap")
            interval_durations.append(dt)
            continue

        s1, s2 = _interval_speeds_mps(p1, p2, dt)
        mean_speed = (s1 + s2) / 2.0
        if mean_speed > HIGHWAY_SPEED_THRESHOLD_MPS:
            highway_distance_m += dist

        if s1 < STOPPED_SPEED_THRESHOLD_MPS and s2 < STOPPED_SPEED_THRESHOLD_MPS:
            interval_kinds.append("stopped")
            stopped_seconds += dt
        else:
            interval_kinds.append("moving")
            moving_seconds += dt
        interval_durations.append(dt)

    # Count maximal "stopped" runs lasting >= STOP_MIN_DURATION_SECONDS,
    # excluding a run touching the very first or very last interval (the car
    # is simply stopped before recording starts / after it ends, not at a
    # red light mid-drive).
    stop_count = 0
    last_interval_idx = len(interval_kinds) - 1
    run_start: int | None = None
    run_duration = 0.0
    for idx, kind in enumerate(interval_kinds):
        if kind == "stopped":
            if run_start is None:
                run_start = idx
                run_duration = 0.0
            run_duration += interval_durations[idx]
        else:
            if run_start is not None:
                run_end = idx - 1
                if (
                    run_duration >= STOP_MIN_DURATION_SECONDS
                    and run_start != 0
                    and run_end != last_interval_idx
                ):
                    stop_count += 1
            run_start = None
            run_duration = 0.0
    # A stopped run still open when the loop ends always touches the last
    # interval, so it is always the "stopped at the end" case and is never
    # counted -- no special handling needed here.

    pct_over_70 = (
        round(100.0 * highway_distance_m / total_distance_m, 2)
        if total_distance_m > 0.0
        else None
    )

    # Altitude: climb/descent/min/max, hysteresis-filtered.
    altitudes = [p.alt_m for p in points if p.alt_m is not None]
    climb_m: float | None = None
    descent_m: float | None = None
    min_alt_m: float | None = None
    max_alt_m: float | None = None
    if len(altitudes) >= 2:
        climb_m, descent_m = _hysteresis_climb_descent(altitudes, ALTITUDE_HYSTERESIS_M)
        min_alt_m = min(altitudes)
        max_alt_m = max(altitudes)

    # Robust (spike-resistant) max speed: 99th percentile of point speeds.
    speeds = sorted(p.speed_mps for p in points if p.speed_mps is not None)
    max_speed_mps = _percentile(speeds, MAX_SPEED_PERCENTILE)

    return TrackStats(
        moving_seconds=round(moving_seconds, 1),
        stopped_seconds=round(stopped_seconds, 1),
        stop_count=stop_count,
        climb_m=round(climb_m, 1) if climb_m is not None else None,
        descent_m=round(descent_m, 1) if descent_m is not None else None,
        min_alt_m=min_alt_m,
        max_alt_m=max_alt_m,
        max_speed_mps=round(max_speed_mps, 2) if max_speed_mps is not None else None,
        pct_distance_over_70mph=pct_over_70,
    )
