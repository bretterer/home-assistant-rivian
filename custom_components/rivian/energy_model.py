"""Anchored per-vehicle energy model.

The car reports no driving power and SoC only moves in ~0.1% steps (about
0.14 kWh on a typical pack), so a pure physics model fitted to whole-drive
totals is too weak to reconstruct a smooth efficiency curve on its own (see
the prototype this was validated against). Instead, this module supplies only
the *shape* of energy use between two measured SoC-step events from a simple
longitudinal physics model (aerodynamic drag, rolling resistance, grade,
kinetic energy, plus a fixed auxiliary draw), and each step re-anchors that
shape to the SoC drop actually measured over that interval. Pure Python
(stdlib only, no Home Assistant imports) so it can be shared by the SQLite
storage layer and tested in isolation.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable
from dataclasses import dataclass, replace
import math
from typing import Any, Final

from .drive_track import DriveTrack, TrackPoint, haversine_m

G: Final[float] = 9.81  # m/s^2
J_PER_KWH: Final[float] = 3.6e6
METERS_PER_MILE: Final[float] = 1609.344

MAX_INTERVAL_GAP_S: Final[float] = 60.0
ALT_SMOOTH_HALF_WINDOW_S: Final[float] = 15.0
# The modeled shape at each point averages ~1 km of driving (at most
# +/- 2 min), so a coast or a hard acceleration doesn't swing it wildly, and
# is left blank where the car covered under 50 m in that time.
SHAPE_WINDOW_M: Final[float] = 1000.0
SHAPE_WINDOW_MAX_S: Final[float] = 120.0
SHAPE_WINDOW_MIN_M: Final[float] = 50.0
# How much of the model's ups and downs show between measured points (1 =
# the model's full swing, 0 = straight lines between the points). Half keeps
# the stop-and-go detail without the model's raw accelerate/regen swings.
SHAPE_WEIGHT: Final[float] = 0.5

MIN_DRIVE_ENERGY_KWH: Final[float] = 0.3
MIN_DRIVE_DISTANCE_MI: Final[float] = 1.0
MIN_STEP_INTERVAL_DISTANCE_MI: Final[float] = 0.1
# Each measured point spans at least this SoC drop (several 0.1% steps, the
# same window as the card's rolling efficiency). One 0.1% step is only ~0.3
# mi, so the step's timing jitter alone swings a one-step value by +/-30%.
MEASURE_MIN_SOC_DROP_PCT: Final[float] = 0.3

# A SoC reading rising by more than this many percentage points above the
# running baseline is treated as a real event (new baseline, no manufactured
# negative-energy step); at or below it, it's a one-step flicker and ignored.
SOC_FLICKER_MAX_PCT: Final[float] = 0.15
_SOC_EPS: Final[float] = 1e-6

# Below this, a step interval's modeled energy is too close to zero to divide
# by; its scale factor falls back to 1.0 rather than blowing up.
_MIN_MODEL_KWH: Final[float] = 1e-4
_STATIONARY_WINDOW_DIST_M: Final[float] = 5.0

_EFF_CLIP_MIN: Final[float] = 0.0
_EFF_CLIP_MAX: Final[float] = 8.0

CDA_BOUNDS: Final[tuple[float, float]] = (0.6, 1.6)
CRR_BOUNDS: Final[tuple[float, float]] = (0.006, 0.020)
AUX_BOUNDS: Final[tuple[float, float]] = (0.0, 4000.0)
ETA_REGEN_BOUNDS: Final[tuple[float, float]] = (0.3, 0.85)

_FIT_ROUNDS: Final[int] = 6
_FIT_PARAM_RADII: Final[dict[str, float]] = {
    "cda_m2": 0.5,
    "crr": 0.007,
    "aux_w": 2000.0,
    "eta_regen": 0.275,
}
_FIT_PARAM_BOUNDS: Final[dict[str, tuple[float, float]]] = {
    "cda_m2": CDA_BOUNDS,
    "crr": CRR_BOUNDS,
    "aux_w": AUX_BOUNDS,
    "eta_regen": ETA_REGEN_BOUNDS,
}


@dataclass(frozen=True, slots=True)
class EnergyModelParams:
    """Fitted (or default) longitudinal energy-model coefficients for one vehicle."""

    cda_m2: float
    crr: float
    aux_w: float
    eta_drive: float = 0.88
    eta_regen: float = 0.65
    mass_kg: float = 3200.0
    rho: float = 1.10

    def clamped(self) -> EnergyModelParams:
        """Return a copy with the fittable fields clamped to their physical bounds."""
        return replace(
            self,
            cda_m2=min(max(self.cda_m2, CDA_BOUNDS[0]), CDA_BOUNDS[1]),
            crr=min(max(self.crr, CRR_BOUNDS[0]), CRR_BOUNDS[1]),
            aux_w=min(max(self.aux_w, AUX_BOUNDS[0]), AUX_BOUNDS[1]),
            eta_regen=min(
                max(self.eta_regen, ETA_REGEN_BOUNDS[0]), ETA_REGEN_BOUNDS[1]
            ),
        )

    def to_dict(self) -> dict[str, float]:
        """Serialize to a plain dict of the seven fields, for JSON storage."""
        return {
            "cda_m2": self.cda_m2,
            "crr": self.crr,
            "aux_w": self.aux_w,
            "eta_drive": self.eta_drive,
            "eta_regen": self.eta_regen,
            "mass_kg": self.mass_kg,
            "rho": self.rho,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnergyModelParams:
        """Inverse of to_dict(); unknown/missing keys fall back to defaults."""
        defaults = DEFAULT_PARAMS
        return cls(
            cda_m2=float(data.get("cda_m2", defaults.cda_m2)),
            crr=float(data.get("crr", defaults.crr)),
            aux_w=float(data.get("aux_w", defaults.aux_w)),
            eta_drive=float(data.get("eta_drive", defaults.eta_drive)),
            eta_regen=float(data.get("eta_regen", defaults.eta_regen)),
            mass_kg=float(data.get("mass_kg", defaults.mass_kg)),
            rho=float(data.get("rho", defaults.rho)),
        )


DEFAULT_PARAMS: Final[EnergyModelParams] = EnergyModelParams(
    cda_m2=0.90, crr=0.010, aux_w=500.0, eta_drive=0.88, eta_regen=0.65
)


@dataclass(frozen=True, slots=True)
class IntervalFeature:
    """Physics features for one interval between two consecutive track points.

    ``aero_j``/``roll_j`` are the aerodynamic-drag and rolling-resistance work
    terms *before* multiplying by CdA/Crr (those are supplied by
    :class:`EnergyModelParams` at battery-energy time); ``grade_j``/
    ``kinetic_j`` need no per-vehicle coefficient and are already final.
    """

    t_mid: float
    dt: float
    v1: float
    v2: float
    vm: float
    dist_m: float
    aero_j: float
    roll_j: float
    grade_j: float
    kinetic_j: float


def _smooth_altitude(
    ts: list[float], alts: list[float | None], half_window_s: float
) -> list[float | None]:
    """Centered moving average of altitude over a +/- half_window_s window.

    Two-pointer sliding window; ``ts`` must be sorted ascending (track points
    always are). A point with no altitude reading of its own still gets an
    averaged value if any neighbor within the window has one.
    """
    n = len(ts)
    out: list[float | None] = [None] * n
    lo = 0
    hi = 0
    for i in range(n):
        while lo < i and ts[i] - ts[lo] > half_window_s:
            lo += 1
        hi = max(hi, lo)
        while hi < n - 1 and ts[hi + 1] - ts[i] <= half_window_s:
            hi += 1
        vals = [alts[k] for k in range(lo, hi + 1) if alts[k] is not None]
        out[i] = (sum(vals) / len(vals)) if vals else alts[i]
    return out


def interval_features(
    track: DriveTrack,
    mass_kg: float = DEFAULT_PARAMS.mass_kg,
    rho: float = DEFAULT_PARAMS.rho,
    headwind_fn: Callable[[TrackPoint, TrackPoint], float] | None = None,
) -> list[IntervalFeature]:
    """Compute per-interval physics features between consecutive track points.

    Intervals with dt <= 0 or dt > MAX_INTERVAL_GAP_S (a GPS/telemetry gap
    with no reliable speed information) are skipped entirely.

    ``rho`` is the air density (kg/m^3). ``headwind_fn(a, b)``, when given,
    returns the headwind in m/s over the interval (negative = tailwind); the
    aerodynamic work then uses the airspeed ``vm + headwind`` (sign kept, so a
    strong tailwind pushes the car). Without it the result is identical to a
    still-air model.
    """
    pts = track.points
    n = len(pts)
    if n < 2:
        return []

    ts = [p.t for p in pts]
    alts = _smooth_altitude(ts, [p.alt_m for p in pts], ALT_SMOOTH_HALF_WINDOW_S)

    features: list[IntervalFeature] = []
    for i in range(1, n):
        a, b = pts[i - 1], pts[i]
        dt = b.t - a.t
        if dt <= 0 or dt > MAX_INTERVAL_GAP_S:
            continue
        dist_gps = haversine_m(a.lat, a.lon, b.lat, b.lon)
        v1 = a.speed_mps if a.speed_mps is not None else dist_gps / dt
        v2 = b.speed_mps if b.speed_mps is not None else dist_gps / dt
        vm = (v1 + v2) / 2.0
        dist = vm * dt
        alt_a, alt_b = alts[i - 1], alts[i]
        dh = (alt_b - alt_a) if alt_a is not None and alt_b is not None else 0.0
        if headwind_fn is None:
            aero = 0.5 * rho * (vm**3) * dt
        else:
            airspeed = vm + headwind_fn(a, b)
            aero = 0.5 * rho * airspeed * abs(airspeed) * vm * dt
        features.append(
            IntervalFeature(
                t_mid=(a.t + b.t) / 2.0,
                dt=dt,
                v1=v1,
                v2=v2,
                vm=vm,
                dist_m=dist,
                aero_j=aero,
                roll_j=mass_kg * G * dist,
                grade_j=mass_kg * G * dh,
                kinetic_j=0.5 * mass_kg * (v2 * v2 - v1 * v1),
            )
        )
    return features


def _wheel_and_battery_j(
    feature: IntervalFeature, params: EnergyModelParams
) -> tuple[float, float]:
    """Return (wheel_j, battery_j) for one interval under the given params."""
    wheel_j = (
        params.cda_m2 * feature.aero_j
        + params.crr * feature.roll_j
        + feature.grade_j
        + feature.kinetic_j
    )
    battery_j = (
        wheel_j / params.eta_drive if wheel_j > 0 else wheel_j * params.eta_regen
    )
    battery_j += params.aux_w * feature.dt
    return wheel_j, battery_j


def interval_battery_j(
    features: list[IntervalFeature], params: EnergyModelParams
) -> float:
    """Return the total modeled battery energy (Joules) across a list of intervals."""
    total = 0.0
    for feature in features:
        _, battery_j = _wheel_and_battery_j(feature, params)
        total += battery_j
    return total


def _drive_model_kwh(
    features: list[IntervalFeature], params: EnergyModelParams
) -> float:
    return interval_battery_j(features, params) / J_PER_KWH


# A track whose usable intervals cover less of the drive than this (GPS gaps)
# can't stand in for the whole drive, so no expected energy is reported.
EXPECTED_MIN_COVERAGE: Final[float] = 0.8


def expected_battery_kwh(
    track: DriveTrack,
    params: EnergyModelParams,
    distance_miles: float,
    rho: float | None = None,
    headwind_fn: Callable[[TrackPoint, TrackPoint], float] | None = None,
) -> float | None:
    """Battery kWh the fitted model predicts for a drive's route.

    ``rho`` defaults to the model's own density; passing the drive's measured
    air density (and a ``headwind_fn``) makes the prediction reflect that day's
    weather. When GPS gaps leave some of ``distance_miles`` uncovered the
    prediction is scaled up from the covered part, and ``None`` is returned if
    under ``EXPECTED_MIN_COVERAGE`` of it is covered or there are no features.
    """
    features = interval_features(
        track,
        params.mass_kg,
        params.rho if rho is None else rho,
        headwind_fn,
    )
    if not features or distance_miles <= 0:
        return None
    covered_mi = sum(f.dist_m for f in features) / METERS_PER_MILE
    if covered_mi < EXPECTED_MIN_COVERAGE * distance_miles:
        return None
    kwh = interval_battery_j(features, params) / J_PER_KWH
    return kwh * distance_miles / covered_mi


def fit_params(
    drives: list[tuple[list[IntervalFeature], float]],
) -> tuple[EnergyModelParams, float, int]:
    """Fit CdA, Crr, aux power and regen efficiency to measured per-drive energy.

    ``drives`` is a list of (per-interval features, measured drive energy in
    kWh). Drives under MIN_DRIVE_ENERGY_KWH or MIN_DRIVE_DISTANCE_MI are
    skipped. Uses bounded coordinate descent (one parameter refined at a time,
    over a grid whose radius halves each round) starting from DEFAULT_PARAMS,
    minimizing sum((model_kwh - measured_kwh)**2 / max(measured_kwh, 1.0)).
    Mass and air density are held fixed at their default values.

    Returns (fitted_params, rmse_kwh, n_drives_used). If no drive qualifies,
    returns (DEFAULT_PARAMS, 0.0, 0).
    """
    filtered: list[tuple[list[IntervalFeature], float]] = []
    for features, measured_kwh in drives:
        if not features or measured_kwh < MIN_DRIVE_ENERGY_KWH:
            continue
        dist_mi = sum(f.dist_m for f in features) / METERS_PER_MILE
        if dist_mi < MIN_DRIVE_DISTANCE_MI:
            continue
        filtered.append((features, measured_kwh))

    n = len(filtered)
    if n == 0:
        return DEFAULT_PARAMS, 0.0, 0

    def total_sq_err(candidate: EnergyModelParams) -> float:
        err = 0.0
        for features, measured_kwh in filtered:
            model_kwh = _drive_model_kwh(features, candidate)
            err += (model_kwh - measured_kwh) ** 2 / max(measured_kwh, 1.0)
        return err

    params = DEFAULT_PARAMS
    radii = dict(_FIT_PARAM_RADII)
    for _round in range(_FIT_ROUNDS):
        for name in ("cda_m2", "crr", "aux_w", "eta_regen"):
            lo, hi = _FIT_PARAM_BOUNDS[name]
            radius = radii[name]
            current = getattr(params, name)
            step = radius / 4.0
            candidates = sorted(
                {round(min(hi, max(lo, current + k * step)), 6) for k in range(-4, 5)}
            )
            best_val = current
            best_err = total_sq_err(params)
            for cand in candidates:
                trial = replace(params, **{name: cand})
                err = total_sq_err(trial)
                if err < best_err:
                    best_err = err
                    best_val = cand
            params = replace(params, **{name: best_val})
        radii = {k: v * 0.5 for k, v in radii.items()}

    params = params.clamped()
    sq_errs = [
        (_drive_model_kwh(features, params) - measured_kwh) ** 2
        for features, measured_kwh in filtered
    ]
    rmse_kwh = math.sqrt(sum(sq_errs) / n)
    return params, rmse_kwh, n


def _soc_step_events(
    track: DriveTrack,
) -> tuple[float, float, list[tuple[float, float]]] | None:
    """Find downward SoC-step events, ignoring one-step up-flicker.

    Returns (t0, soc0, events) where events is an ordered list of (t, soc) at
    each accepted downward step, or None if the track has no SoC readings at
    all. A SoC reading that rises by at most SOC_FLICKER_MAX_PCT above the
    running baseline is ignored (neither a step nor a new baseline); a larger
    rise resets the baseline without generating a (negative-energy) event.
    """
    t0: float | None = None
    soc0: float | None = None
    baseline: float | None = None
    events: list[tuple[float, float]] = []
    for point in track.points:
        if point.soc is None:
            continue
        if baseline is None:
            baseline = point.soc
            t0 = point.t
            soc0 = point.soc
            continue
        if point.soc < baseline - _SOC_EPS:
            events.append((point.t, point.soc))
            baseline = point.soc
        elif point.soc > baseline + SOC_FLICKER_MAX_PCT:
            baseline = point.soc
    if t0 is None or soc0 is None:
        return None
    return t0, soc0, events


def _windowed_consumption(
    track: DriveTrack, feat_tmid: list[float], dist: list[float], kwh: list[float]
) -> list[float | None]:
    """Per track point: modeled kWh/mi over a centered ~SHAPE_WINDOW_M window.

    Consumption, not efficiency: it stays finite (or goes negative, regen)
    where the modeled energy nears zero, e.g. coasting downhill, whereas
    mi/kWh would blow up. None where the window covers under
    SHAPE_WINDOW_MIN_M (stationary).
    """
    n_feat = len(feat_tmid)
    out: list[float | None] = []
    for point in track.points:
        t = point.t
        centre = min(bisect.bisect_left(feat_tmid, t), n_feat - 1)
        lo = hi = centre
        dist_m = dist[centre]
        energy_kwh = kwh[centre]
        while dist_m < SHAPE_WINDOW_M:
            grew = False
            if lo > 0 and t - feat_tmid[lo - 1] <= SHAPE_WINDOW_MAX_S:
                lo -= 1
                dist_m += dist[lo]
                energy_kwh += kwh[lo]
                grew = True
            if hi < n_feat - 1 and feat_tmid[hi + 1] - t <= SHAPE_WINDOW_MAX_S:
                hi += 1
                dist_m += dist[hi]
                energy_kwh += kwh[hi]
                grew = True
            if not grew:
                break
        if dist_m < SHAPE_WINDOW_MIN_M:
            out.append(None)
        else:
            out.append(energy_kwh / (dist_m / METERS_PER_MILE))
    return out


def _eff_from_consumption(kwh_per_mi: float) -> float:
    """mi/kWh from kWh/mi, clipped to 0-8 mi/kWh (zero or negative use reads 8)."""
    if kwh_per_mi <= 1.0 / _EFF_CLIP_MAX:
        return _EFF_CLIP_MAX
    return max(_EFF_CLIP_MIN, 1.0 / kwh_per_mi)


def _measure_intervals(
    t0: float, soc0: float, events: list[tuple[float, float]]
) -> tuple[list[float], list[float]]:
    """Group SoC-step events into intervals of >= MEASURE_MIN_SOC_DROP_PCT.

    Returns parallel (boundary times, SoC at each boundary). A short
    remainder at the end joins the last full interval; a drive whose whole
    drop is under the minimum is one interval.
    """
    boundaries = [t0]
    socs = [soc0]
    for t, soc in events:
        if socs[-1] - soc >= MEASURE_MIN_SOC_DROP_PCT - _SOC_EPS:
            boundaries.append(t)
            socs.append(soc)
    last_t, last_soc = events[-1]
    if boundaries[-1] != last_t:
        if len(boundaries) > 1:
            boundaries[-1] = last_t
            socs[-1] = last_soc
        else:
            boundaries.append(last_t)
            socs.append(last_soc)
    return boundaries, socs


def _interp_knots(knots: list[tuple[float, float]], t: float) -> float:
    """Linear interpolation through (t, value) knots, held flat past either end."""
    if t <= knots[0][0]:
        return knots[0][1]
    if t >= knots[-1][0]:
        return knots[-1][1]
    j = bisect.bisect_right([k[0] for k in knots], t)
    (t0, v0), (t1, v1) = knots[j - 1], knots[j]
    frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
    return v0 + frac * (v1 - v0)


def anchored_efficiency(
    track: DriveTrack,
    params: EnergyModelParams,
    capacity_kwh: float | None,
    drive_energy_kwh: float | None = None,
) -> dict[str, Any] | None:
    """Reconstruct a per-point mi/kWh curve that runs through the measured values.

    ``points`` holds each measured interval's mi/kWh (distance / (SoC drop x
    capacity), no model involved). An interval spans at least
    MEASURE_MIN_SOC_DROP_PCT of SoC (see _measure_intervals), and its point is
    placed at the track point nearest the interval's distance midpoint -- an
    interval average belongs in its middle, not at its end.

    ``eff`` works in consumption (kWh/mi), which stays finite where modeled
    energy nears zero. Between measured points, consumption is the measured
    values interpolated linearly in time (held flat before the first and
    after the last), plus SHAPE_WEIGHT x the physics model's deviation from
    its own interpolated value at those points (modeled kWh/mi over ~1 km
    around each point). At a measured point that deviation is zero, so the
    curve passes exactly through it; the model only supplies the ups and
    downs between measurements. Values are converted to mi/kWh, clipped to
    0-8, and None where the car is stationary.

    Falls back to scaling the whole track to ``drive_energy_kwh`` when there's
    no usable capacity/SoC-step data; returns None if that fallback energy is
    also unavailable, or the track has no usable intervals.
    """
    features = interval_features(track, params.mass_kg, params.rho)
    if not features:
        return None

    feat_tmid = [f.t_mid for f in features]
    dist = [f.dist_m for f in features]
    kwh = [_wheel_and_battery_j(f, params)[1] / J_PER_KWH for f in features]
    shape = _windowed_consumption(track, feat_tmid, dist, kwh)
    point_t = [p.t for p in track.points]

    step_info = _soc_step_events(track) if capacity_kwh and capacity_kwh > 0 else None
    points: list[list[float]] = []
    # (t, measured kWh/mi, modeled kWh/mi) at each anchor point.
    knots: list[tuple[float, float, float]] = []
    if step_info is not None and step_info[2]:
        t0, soc0, events = step_info
        boundaries, soc_seq = _measure_intervals(t0, soc0, events)
        for i in range(len(boundaries) - 1):
            start, end = boundaries[i], boundaries[i + 1]
            measured_kwh = max(
                0.0, (soc_seq[i] - soc_seq[i + 1]) / 100.0 * capacity_kwh
            )  # type: ignore[operator]
            in_interval = [k for k, tm in enumerate(feat_tmid) if start <= tm < end]
            dist_mi = sum(dist[k] for k in in_interval) / METERS_PER_MILE
            if dist_mi < MIN_STEP_INTERVAL_DISTANCE_MI or measured_kwh <= 0:
                continue
            value = dist_mi / measured_kwh
            # Distance midpoint of the interval, then the nearest track point
            # inside it that has a modeled value to anchor against.
            half = dist_mi * METERS_PER_MILE / 2.0
            cum = 0.0
            t_half = feat_tmid[in_interval[-1]]
            for k in in_interval:
                cum += dist[k]
                if cum >= half:
                    t_half = feat_tmid[k]
                    break
            candidates = [
                j
                for j, tp in enumerate(point_t)
                if start <= tp <= end and shape[j] is not None
            ]
            if candidates:
                j = min(candidates, key=lambda c: abs(point_t[c] - t_half))
                points.append([point_t[j], value])
                knots.append((point_t[j], 1.0 / value, shape[j]))  # type: ignore[arg-type]
            else:
                points.append([t_half, value])
    elif drive_energy_kwh is None:
        return None

    if not knots:
        # No anchor inside the drive: scale the model to the drive's total
        # (the measured SoC drop when there is one, else drive_energy_kwh).
        total_kwh = sum(kwh)
        if step_info is not None and step_info[2]:
            target_kwh = (step_info[1] - step_info[2][-1][1]) / 100.0 * capacity_kwh  # type: ignore[operator]
        else:
            target_kwh = drive_energy_kwh or 0.0
        if total_kwh <= _MIN_MODEL_KWH or target_kwh <= 0:
            return None
        scale = target_kwh / total_kwh
        eff_fallback = [
            None if c is None else _eff_from_consumption(c * scale) for c in shape
        ]
        return {"eff": eff_fallback, "points": points}

    # Consumption between anchors: the measured values interpolated, plus the
    # model's deviation from its own interpolated anchor values. At an anchor
    # the deviation is zero, so the curve passes through the measurement.
    measured_knots = [(t, c) for t, c, _ in knots]
    model_knots = [(t, m) for t, _, m in knots]
    eff: list[float | None] = []
    for j, modeled in enumerate(shape):
        if modeled is None:
            eff.append(None)
            continue
        t = point_t[j]
        deviation = modeled - _interp_knots(model_knots, t)
        eff.append(
            _eff_from_consumption(
                _interp_knots(measured_knots, t) + SHAPE_WEIGHT * deviation
            )
        )
    return {"eff": eff, "points": points}
