"""Pure helpers for estimating a DC fast-charge power curve from SoC readings.

Rivian's ``getLiveSessionData`` API (the source `ChargingCoordinator` used for
live charger power) has been removed server-side, and the vehicle itself does
not report a `power`/`chargerPower` field. When no real power reading is
available -- live or from history -- the only signal left is how fast the
reported state-of-charge rises, so both the live tracker
(`drive_tracker.py`) and the historical reconstruction
(`history_backfill.py`) derive an approximate power curve from SoC-over-time
samples using the helpers below.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .drive_models import ChargingSample

_DEDUP_MIN_SOC_DELTA: float = 0.05
_DEDUP_MIN_SECONDS: float = 20.0
_WINDOW_SECONDS: float = 60.0
_MIN_WINDOW_SECONDS: float = 30.0
_MAX_ESTIMATED_POWER_KW: float = 225.0
_SAMPLE_MIN_SOC_DELTA: float = 0.2
_SAMPLE_MIN_POWER_DELTA: float = 1.0


def _is_new_soc_point(
    last: tuple[float, float], candidate: tuple[float, float]
) -> bool:
    """Return True if ``candidate`` differs enough from ``last`` to keep."""
    last_ts, last_soc = last
    ts, soc = candidate
    return (
        abs(soc - last_soc) >= _DEDUP_MIN_SOC_DELTA
        or (ts - last_ts) >= _DEDUP_MIN_SECONDS
    )


def dedupe_soc_points(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Drop SoC points too close in time and value to the previous kept one.

    ``points`` must be ``(epoch_seconds, soc_percent)`` pairs sorted ascending
    by timestamp. A point is kept if its SoC differs from the last kept
    point by at least 0.05%, or its timestamp is at least 20s later.
    """
    dedup: list[tuple[float, float]] = []
    for point in points:
        if not dedup or _is_new_soc_point(dedup[-1], point):
            dedup.append(point)
    return dedup


def should_append_soc_point(
    last: tuple[float, float] | None, candidate: tuple[float, float]
) -> bool:
    """Return True if a live sampler should keep ``candidate``.

    Same rule as `dedupe_soc_points`, exposed for callers (like the live
    drive tracker) that append one point at a time instead of deduplicating
    a whole list at once.
    """
    return last is None or _is_new_soc_point(last, candidate)


def estimate_charge_curve(
    soc_points: list[tuple[float, float]], capacity_kwh: float
) -> tuple[list[ChargingSample], float]:
    """Estimate a charging power curve from SoC-over-time readings alone.

    ``soc_points`` are ``(epoch_seconds, soc_percent)`` pairs, already
    deduplicated (see `dedupe_soc_points`) and sorted ascending by timestamp.

    For each point, power is derived from the SoC delta across a window
    reaching up to 60s into the past and future
    (``dsoc/100 * capacity_kwh / (dt/3600)``), capped at 225 kW as a sanity
    ceiling, and windows shorter than 30s are skipped as too noisy. Samples
    are thinned so consecutive kept samples differ meaningfully in SoC or
    estimated power.

    Returns ``(samples, max_power_kw)``; both are empty/0.0 if no window in
    ``soc_points`` has enough separation to estimate a rate.
    """
    samples: list[ChargingSample] = []
    max_power = 0.0
    for ts_i, soc_i in soc_points:
        past = [p for p in soc_points if 0.0 < (ts_i - p[0]) <= _WINDOW_SECONDS]
        future = [f for f in soc_points if 0.0 < (f[0] - ts_i) <= _WINDOW_SECONDS]

        t_start = past[0][0] if past else ts_i
        s_start = past[0][1] if past else soc_i
        t_end = future[-1][0] if future else ts_i
        s_end = future[-1][1] if future else soc_i

        dt = t_end - t_start
        dsoc = s_end - s_start
        if dt >= _MIN_WINDOW_SECONDS and dsoc > 0.0:
            p_kw = (dsoc / 100.0 * capacity_kwh) / (dt / 3600.0)
            p_kw = min(_MAX_ESTIMATED_POWER_KW, p_kw)
            max_power = max(max_power, p_kw)

            if not samples or (
                abs(samples[-1].soc - soc_i) >= _SAMPLE_MIN_SOC_DELTA
                and abs(samples[-1].power_kw - p_kw) >= _SAMPLE_MIN_POWER_DELTA
            ):
                samples.append(
                    ChargingSample(
                        timestamp=datetime.fromtimestamp(ts_i, tz=UTC).isoformat(),
                        soc=round(soc_i, 1),
                        power_kw=round(p_kw, 1),
                        battery_temp_f=None,
                    )
                )

    return samples, max_power


def coarse_soc_samples(
    soc_points: list[tuple[float, float]], max_points: int
) -> list[ChargingSample]:
    """Thin ``(epoch_seconds, soc)`` points to at most ``max_points`` samples.

    Used for an AC session, which keeps no power curve: the samples carry
    ``power_kw`` 0 and exist only so the battery-% timeline has points inside
    the session. The first and last point are always kept.
    """
    if not soc_points or max_points < 2:
        return []
    if len(soc_points) <= max_points:
        picked = list(soc_points)
    else:
        step = (len(soc_points) - 1) / (max_points - 1)
        picked = [soc_points[round(i * step)] for i in range(max_points)]
    return [
        ChargingSample(
            timestamp=datetime.fromtimestamp(ts, tz=UTC).isoformat(),
            soc=round(soc, 1),
            power_kw=0.0,
            battery_temp_f=None,
        )
        for ts, soc in picked
    ]
