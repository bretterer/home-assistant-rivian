#!/usr/bin/env python3
"""Build the synthetic demo-data fixture for the Rivian HA integration.

This script is run OFFLINE, once (or whenever the fixture needs regenerating),
by a developer -- never by Home Assistant itself. Its output,
``custom_components/rivian/demo/demo_data.json``, is committed to the repo and
loaded verbatim at runtime by the (separately implemented) demo-install code.

NO REAL USER DATA IS USED. Every coordinate, drive and vehicle below is
fictional: a made-up household ("Demo household") at a made-up address in
Eagle, Idaho, driving to public places (a library, a park, downtown Boise,
etc). Road geometry, drive durations and elevation are real (fetched from the
public services below) because the demo is meant to look like a real drive on
a map, but no trip ever happened and no location is tied to any real person.

Public services called (politely: <=1 request/second, a descriptive
User-Agent, and a local on-disk cache under ``--cache`` so re-runs don't
re-fetch anything):
  - OSRM's public demo router (router.project-osrm.org) for road-snapped
    coordinates (``/nearest``) and turn-by-turn route geometry + per-segment
    duration/distance/speed (``/route``).
  - Open-Meteo's public elevation API (api.open-meteo.com) for ground
    elevation along each route.

Everything else -- the resampled GPS track, battery State of Charge, energy
use, speed bins and 3-minute "chunks" -- is computed locally using the
integration's own energy model (``custom_components/rivian/energy_model.py``,
imported directly so the physics are identical to what the live integration
would compute) plus simple, seeded-random noise for realism.

Usage::

    python scripts/build_demo_data.py [--out PATH] [--cache DIR] [--seed N]

Re-running is a no-op network-wise: every OSRM/Open-Meteo request is cached
by URL, so a second run only re-does local computation.

--------------------------------------------------------------------------
Field mapping (fixture JSON -> the integration's own data model)
--------------------------------------------------------------------------
This fixture is intentionally a *simplified* columnar JSON, not a literal
dump of ``drive_models.DriveRecord``/``DriveChunk``/``ChargingSessionRecord``
(whose own ``to_dict()`` uses ISO timestamps and vehicle-absolute odometer,
neither of which exist yet for a fixture that hasn't been "installed" at a
real wall-clock date). A future loader maps fixture keys onto those
dataclasses like so:

  Drive (-> DriveRecord):
    drive_id                -> drive_id
    start_s / end_s          -> start_time / end_time (once day_offset is
                                resolved to a real date by the installer,
                                these become "midnight + N seconds")
    distance_miles           -> distance_miles
    energy_kwh               -> energy_kwh
    efficiency_mi_kwh        -> efficiency_mi_kwh
    start_soc / end_soc      -> start_soc / end_soc
    start_odometer_mi/
      end_odometer_mi        -> start_odometer_mi / end_odometer_mi
    start_range_mi/
      end_range_mi           -> start_range_mi / end_range_mi
    max_speed_mph             -> max_speed_mph
    avg_speed_mph             -> avg_speed_mph
    temperature_f             -> integrated_temperature_f
    battery_capacity_kwh     -> battery_capacity_kwh (reported capacity at
                                that drive; falls back to the vehicle's)
    drive_modes                -> drive_modes
    driver                      -> driver
    track.{t,lat,lon,speed_mps,
      alt_m,soc,odometer_mi}  -> written to drive_tracks.track_json via
                                 drive_track.DriveTrack (t becomes an epoch
                                 second once day_offset -> real date)
    chunks[]                  -> DriveChunk list (start_time is this
                                 fixture's start_s, still relative)
    speed_bins{}               -> SpeedBinData per STANDARD_SPEED_BINS key

  Charging session (-> ChargingSessionRecord):
    session_id, start_s/end_s, start_soc/end_soc, energy_added_kwh,
    max_power_kw, avg_power_kw, samples[] (t_s/power_kw/soc),
    kind ("dc" when absent | "ac" for a home session; an AC session's
    samples are coarse SoC points with power 0), place (key -> lat/lon)
      -> same-named ChargingSessionRecord/ChargingSample fields
         (timestamp built from day_offset + t_s)

  Place (-> places table "user" rows):
    key, name, category, lat, lon, radius_m -> stored directly so labels
    appear without any geocoding lookup.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import importlib
import json
import math
import os
import random
import sys
import time
import types
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUSTOM_COMPONENTS_DIR = os.path.join(REPO_ROOT, "custom_components")
RIVIAN_DIR = os.path.join(CUSTOM_COMPONENTS_DIR, "rivian")

DEFAULT_OUT = os.path.join(RIVIAN_DIR, "demo", "demo_data.json")

USER_AGENT = (
    "rivian-homeassistant-demo-data-builder/1.0 "
    "(synthetic fixture generator; no real user data; "
    "https://github.com/bretterer/home-assistant-rivian)"
)

MIN_REQUEST_INTERVAL_S = 1.05
_last_request_ts = 0.0


# --------------------------------------------------------------------------
# Importing energy_model.py without pulling in Home Assistant.
#
# custom_components/rivian/__init__.py (the real package __init__) imports
# Home Assistant, which isn't installed/running here. energy_model.py only
# needs its sibling drive_track.py (both are pure stdlib Python), so we
# register minimal stub packages in sys.modules with the right __path__ and
# import the submodules directly -- Python's import machinery then resolves
# `from .drive_track import ...` against our stub package without ever
# executing the real __init__.py.
# --------------------------------------------------------------------------
def _load_rivian_submodules():
    if "custom_components" not in sys.modules:
        pkg = types.ModuleType("custom_components")
        pkg.__path__ = [CUSTOM_COMPONENTS_DIR]
        sys.modules["custom_components"] = pkg
    if "custom_components.rivian" not in sys.modules:
        riv = types.ModuleType("custom_components.rivian")
        riv.__path__ = [RIVIAN_DIR]
        sys.modules["custom_components.rivian"] = riv

    energy_model = importlib.import_module("custom_components.rivian.energy_model")
    drive_track = importlib.import_module("custom_components.rivian.drive_track")
    drive_models = importlib.import_module("custom_components.rivian.drive_models")
    charge_curves = importlib.import_module("custom_components.rivian.charge_curves")
    drive_conditions = importlib.import_module(
        "custom_components.rivian.drive_conditions"
    )
    return energy_model, drive_track, drive_models, charge_curves, drive_conditions


(
    energy_model,
    drive_track,
    drive_models,
    charge_curves,
    drive_conditions,
) = _load_rivian_submodules()

EnergyModelParams = energy_model.EnergyModelParams
DEFAULT_PARAMS = energy_model.DEFAULT_PARAMS
interval_features = energy_model.interval_features
J_PER_KWH = energy_model.J_PER_KWH
METERS_PER_MILE = energy_model.METERS_PER_MILE
TrackPoint = drive_track.TrackPoint
STANDARD_SPEED_BINS = drive_models.STANDARD_SPEED_BINS
MPGE_FACTOR = drive_models.MPGE_FACTOR

MPS_PER_MPH = 0.44704

# Per-vehicle energy-model parameters (also used to score the demo drives'
# conditions, so the fixture's expected_kwh is consistent with their energy).
VEHICLE_PARAMS = {
    "DEMO0R2EAGLE00001": replace(
        DEFAULT_PARAMS, mass_kg=2600.0, cda_m2=0.90, crr=0.013, aux_w=900.0
    ),
    "DEMO1R1TEAGLE0002": replace(
        DEFAULT_PARAMS, mass_kg=3250.0, cda_m2=1.05, crr=0.011, aux_w=550.0
    ),
}


class _Track:
    """Minimal stand-in for DriveTrack: energy_model.interval_features() only
    reads ``.points`` off the object it's given, so a real DriveTrack (with
    its stricter validation) isn't needed here."""

    def __init__(self, points):
        self.points = points


def _wheel_and_battery_j(feature, params):
    """Mirror of energy_model._wheel_and_battery_j (kept private there)."""
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
    return battery_j


def get_speed_bin_key(speed_mph: float) -> str:
    """Mirror of drive_tracker.get_speed_bin_key (that module imports HA)."""
    if speed_mph < 0.0:
        return "0-9"
    if speed_mph >= 80.0:
        return "80+"
    bin_lower = int(speed_mph // 10) * 10
    return f"{bin_lower}-{bin_lower + 9}"


# --------------------------------------------------------------------------
# HTTP + cache
# --------------------------------------------------------------------------
def _throttle():
    global _last_request_ts
    now = time.monotonic()
    wait = MIN_REQUEST_INTERVAL_S - (now - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.monotonic()


def cached_get_json(url: str, cache_dir: str) -> dict:
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    path = os.path.join(cache_dir, f"{key}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:  # pragma: no cover - network failure
        raise RuntimeError(f"request failed: {url}: {exc}") from exc

    data = json.loads(raw)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw)
    return data


def osrm_nearest(lat: float, lon: float, cache_dir: str) -> tuple[float, float]:
    """Snap (lat, lon) to the nearest road via OSRM's `nearest` service."""
    url = (
        "https://router.project-osrm.org/nearest/v1/driving/"
        f"{lon:.6f},{lat:.6f}?number=1"
    )
    data = cached_get_json(url, cache_dir)
    loc = data["waypoints"][0]["location"]  # [lon, lat]
    return loc[1], loc[0]


def osrm_route(lat1, lon1, lat2, lon2, cache_dir: str):
    """Return (coords[[lon,lat],...], durations[], distances[], speeds[])."""
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}"
        "?overview=full&geometries=geojson&annotations=duration,distance,speed"
    )
    data = cached_get_json(url, cache_dir)
    route = data["routes"][0]
    coords = route["geometry"]["coordinates"]
    leg = route["legs"][0]
    ann = leg["annotation"]
    return coords, ann["duration"], ann["distance"], ann["speed"]


def open_meteo_elevation(points: list[tuple[float, float]], cache_dir: str):
    """points: list of (lat, lon). Returns parallel list of elevations (m)."""
    results: list[float] = []
    for i in range(0, len(points), 100):
        batch = points[i : i + 100]
        lats = ",".join(f"{p[0]:.6f}" for p in batch)
        lons = ",".join(f"{p[1]:.6f}" for p in batch)
        url = (
            f"https://api.open-meteo.com/v1/elevation?latitude={lats}&longitude={lons}"
        )
        data = cached_get_json(url, cache_dir)
        results.extend(data["elevation"])
    return results


# --------------------------------------------------------------------------
# Route resampling
# --------------------------------------------------------------------------
def resample_route(coords, durations, distances, speeds, dt=5.0):
    """Resample an OSRM route to one point every ~dt seconds.

    Returns a list of [t, lat, lon, speed_mps] rows. coords are [lon, lat];
    durations/distances/speeds have len(coords)-1 entries (one per segment).
    """
    n = len(coords)
    cum_t = [0.0]
    for d in durations:
        cum_t.append(cum_t[-1] + max(d, 0.001))
    total_t = cum_t[-1]

    rows = []
    t = 0.0
    seg = 0
    n_segs = n - 1
    while t <= total_t and n_segs > 0:
        while seg < n_segs - 1 and cum_t[seg + 1] < t:
            seg += 1
        t0, t1 = cum_t[seg], cum_t[seg + 1]
        frac = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        frac = min(max(frac, 0.0), 1.0)
        lon0, lat0 = coords[seg]
        lon1, lat1 = coords[seg + 1]
        lat = lat0 + (lat1 - lat0) * frac
        lon = lon0 + (lon1 - lon0) * frac
        speed = speeds[seg] if seg < len(speeds) else 0.0
        rows.append([t, lat, lon, max(0.0, speed)])
        t += dt

    if not rows:
        lon0, lat0 = coords[0]
        rows.append([0.0, lat0, lon0, 0.0])
    if n_segs > 0 and rows[-1][0] < total_t - 0.01:
        lon1, lat1 = coords[-1]
        rows.append([total_t, lat1, lon1, speeds[-1] if speeds else 0.0])
    return rows


def add_speed_variation(rows, rng: random.Random):
    for row in rows:
        noise = rng.gauss(1.0, 0.08)
        row[3] = max(0.0, row[3] * noise)


def insert_stops(rows, rng: random.Random, dt=5.0):
    """Add a couple of brief (10-30s) near-zero-speed stops on longer drives."""
    total_duration = rows[-1][0]
    if total_duration < 180 or len(rows) < 10:
        return rows

    n_stops = 1 if total_duration < 600 else rng.randint(1, 3)
    out = list(rows)
    for _ in range(n_stops):
        lo = max(1, int(len(out) * 0.15))
        hi = max(lo + 1, int(len(out) * 0.85))
        idx = rng.randint(lo, min(hi, len(out) - 2))
        lat, lon = out[idx][1], out[idx][2]
        stop_s = rng.uniform(10, 30)
        n_extra = max(1, round(stop_s / dt))
        insert_rows = [[0.0, lat, lon, 0.0] for _ in range(n_extra)]
        out = out[: idx + 1] + insert_rows + out[idx + 1 :]

    # Re-derive strictly increasing times at the fixed sample spacing.
    for i, row in enumerate(out):
        row[0] = round(i * dt, 1)
    return out


def attach_elevation(rows, cache_dir: str):
    """Sample elevation every ~10th point (<=100/request) and interpolate."""
    idxs = list(range(0, len(rows), 10))
    if idxs[-1] != len(rows) - 1:
        idxs.append(len(rows) - 1)
    sample_points = [(rows[i][1], rows[i][2]) for i in idxs]
    elevations = open_meteo_elevation(sample_points, cache_dir)
    elev_by_idx = dict(zip(idxs, elevations))

    sorted_idxs = sorted(elev_by_idx)
    alt = [0.0] * len(rows)
    for k in range(len(sorted_idxs) - 1):
        i0, i1 = sorted_idxs[k], sorted_idxs[k + 1]
        e0, e1 = elev_by_idx[i0], elev_by_idx[i1]
        for j in range(i0, i1 + 1):
            frac = 0.0 if i1 == i0 else (j - i0) / (i1 - i0)
            alt[j] = e0 + (e1 - e0) * frac

    for i, row in enumerate(rows):
        row.append(alt[i])  # row is now [t, lat, lon, speed_mps, alt_m]
    return rows


# --------------------------------------------------------------------------
# Energy / SoC / odometer
# --------------------------------------------------------------------------
def compute_energy(rows, params: EnergyModelParams):
    points = [
        TrackPoint(t=row[0], lat=row[1], lon=row[2], speed_mps=row[3], alt_m=row[4])
        for row in rows
    ]
    track = _Track(points)
    feats = interval_features(track, mass_kg=params.mass_kg, rho=params.rho)
    energies_j = [_wheel_and_battery_j(f, params) for f in feats]
    return feats, energies_j


def build_arrays(rows, feats, energies_j, start_soc, capacity_kwh, start_odo_mi):
    n = len(rows)
    raw_cum_j = [0.0] * n
    dist_cum_m = [0.0] * n
    for i, feat in enumerate(feats):
        raw_cum_j[i + 1] = raw_cum_j[i] + energies_j[i]
        dist_cum_m[i + 1] = dist_cum_m[i] + feat.dist_m

    # Ratchet: the car's reported SoC never rises from regen (see
    # drive_stats.py docstring), so the *drained* energy used for the SoC
    # curve is the running max of the raw (possibly dipping) cumulative
    # energy, not the raw value itself.
    eff_cum_j = [0.0] * n
    for i in range(1, n):
        eff_cum_j[i] = max(eff_cum_j[i - 1], raw_cum_j[i])

    # The displayed SoC curve is quantized to the car's real 0.1% gauge
    # granularity (see module docstring), but that quantization is far too
    # coarse to drive a short drive's reported energy/efficiency (a 2-3 mile
    # errand can be a single 0.1% SoC step, swinging "efficiency" wildly). So
    # energy/efficiency fields use the continuous (ratcheted) physics energy
    # directly; only the SoC curve itself, and the start/end SoC shown to the
    # user, are floored to 0.1%.
    eff_cum_kwh = [j / J_PER_KWH for j in eff_cum_j]
    soc = []
    for i in range(n):
        drop_pct = eff_cum_kwh[i] / capacity_kwh * 100.0
        value = start_soc - drop_pct
        soc.append(math.floor(value * 10.0) / 10.0)
    for i in range(1, n):  # guard: strictly non-increasing
        soc[i] = min(soc[i], soc[i - 1])

    odo_mi = [start_odo_mi + d / METERS_PER_MILE for d in dist_cum_m]
    return soc, odo_mi, dist_cum_m, eff_cum_kwh


def build_chunks(rows, eff_cum_kwh, odo_mi):
    chunks = []
    n = len(rows)
    i0 = 0
    while i0 < n - 1:
        t0 = rows[i0][0]
        i = i0
        while i < n - 1 and rows[i][0] - t0 < 180.0:
            i += 1
        dur = rows[i][0] - t0
        is_leftover = i == n - 1 and dur < 180.0
        if is_leftover and dur < 90.0:
            break

        dist_mi = odo_mi[i] - odo_mi[i0]
        chunk_kwh = max(0.0, eff_cum_kwh[i] - eff_cum_kwh[i0])
        speeds_mph = [row[3] / MPS_PER_MPH for row in rows[i0 : i + 1]]
        avg_speed_mph = sum(speeds_mph) / len(speeds_mph) if speeds_mph else 0.0
        efficiency = round(dist_mi / chunk_kwh, 2) if chunk_kwh > 0 else 0.0
        chunks.append(
            {
                "start_s": round(t0, 1),
                "duration_seconds": round(dur, 1),
                "distance_miles": round(dist_mi, 2),
                "energy_kwh": round(chunk_kwh, 2),
                "efficiency_mi_kwh": efficiency,
                "avg_speed_mph": round(avg_speed_mph, 1),
                "speed_bin": get_speed_bin_key(avg_speed_mph),
            }
        )
        i0 = i
    return chunks


def build_speed_bins(feats):
    bins = {b: {"miles": 0.0, "seconds": 0.0} for b in STANDARD_SPEED_BINS}
    for feat in feats:
        speed_mph = feat.vm / MPS_PER_MPH
        key = get_speed_bin_key(speed_mph)
        bins[key]["miles"] += feat.dist_m / METERS_PER_MILE
        bins[key]["seconds"] += feat.dt
    for key, val in bins.items():
        val["miles"] = round(val["miles"], 3)
        val["seconds"] = round(val["seconds"], 1)
    return bins


# --------------------------------------------------------------------------
# Drive assembly
# --------------------------------------------------------------------------
def temperature_f_for(start_s: float) -> float:
    """Plausible early-October Boise temperature by time of day."""
    hour = (start_s / 3600.0) % 24.0
    # Rough diurnal curve: low ~47F around 6am, high ~73F around 3-4pm.
    phase = (hour - 6.0) / 24.0 * 2 * math.pi
    return round(60.0 - 13.0 * math.cos(phase), 1)


def add_conditions(record: dict, params: EnergyModelParams) -> None:
    """Add plausible, deterministic driving conditions to a drive record.

    Wind, precipitation, pressure and humidity are drawn from a generator
    seeded by the drive id alone (never the build's shared ``rng``, so adding
    this never shifts any other generated value), then reduced exactly as the
    live integration reduces real weather samples
    (``drive_conditions.compute_condition_columns``): headwind along the
    route, air density, and the energy model's expected kWh with them.
    Idempotent: the same drive always gets the same values.
    """
    gen = random.Random(
        int.from_bytes(hashlib.sha256(record["drive_id"].encode()).digest()[:8], "big")
    )
    # Boise-valley-ish: a prevailing south-westerly, light to breezy.
    wind_mph = round(gen.uniform(2.0, 15.0), 1)
    wind_from = round((225.0 + gen.uniform(-70.0, 70.0)) % 360.0, 0)
    raining = gen.random() < 0.15
    precip_mm = round(gen.uniform(0.2, 1.5), 1) if raining else 0.0
    pressure = round(918.0 + gen.uniform(-7.0, 7.0), 1)
    humidity = round(gen.uniform(25.0, 60.0) + (25.0 if raining else 0.0), 1)
    track = record["track"]
    points = [
        TrackPoint(
            t=track["t"][i],
            lat=track["lat"][i],
            lon=track["lon"][i],
            speed_mps=track["speed_mps"][i],
            alt_m=track["alt_m"][i],
        )
        for i in range(len(track["t"]))
    ]
    base = {
        "temp_f": record["temperature_f"],
        "pressure_hpa": pressure,
        "precip_mm": precip_mm,
        "humidity_pct": humidity,
    }
    samples = [
        {
            "t": points[0].t,
            "wind_speed_mph": wind_mph,
            "wind_dir_deg": wind_from,
            **base,
        },
        {
            "t": points[-1].t,
            "wind_speed_mph": round(wind_mph * 1.1, 1),
            "wind_dir_deg": (wind_from + 10.0) % 360.0,
            **base,
        },
    ]
    cols = drive_conditions.compute_condition_columns(
        _Track(points),
        samples,
        params,
        duration_s=record["end_s"] - record["start_s"],
        distance_miles=record["distance_miles"],
        temp_f=record["temperature_f"],
    )
    for key, value in cols.items():
        record[key] = value


def build_drive(
    *,
    vin,
    drive_id,
    day_offset,
    start_s,
    start_place,
    end_place,
    places,
    start_soc,
    start_odo_mi,
    capacity_kwh,
    params: EnergyModelParams,
    nominal_range_eff,
    driver,
    drive_mode,
    cache_dir,
    rng: random.Random,
):
    a = places[start_place]
    b = places[end_place]
    coords, durations, distances, speeds = osrm_route(
        a["lat"], a["lon"], b["lat"], b["lon"], cache_dir
    )
    rows = resample_route(coords, durations, distances, speeds)
    add_speed_variation(rows, rng)
    rows = insert_stops(rows, rng)
    rows = attach_elevation(rows, cache_dir)

    feats, energies_j = compute_energy(rows, params)
    soc, odo_mi, dist_cum_m, eff_cum_kwh = build_arrays(
        rows, feats, energies_j, start_soc, capacity_kwh, start_odo_mi
    )

    distance_miles = dist_cum_m[-1] / METERS_PER_MILE
    duration_seconds = rows[-1][0]
    energy_kwh = round(max(0.0, eff_cum_kwh[-1]), 3)
    end_soc = soc[-1]

    efficiency = round(distance_miles / energy_kwh, 2) if energy_kwh > 0 else 0.0
    mpge = round(efficiency * MPGE_FACTOR, 1) if efficiency else 0.0
    max_speed_mph = max(row[3] for row in rows) / MPS_PER_MPH
    avg_speed_mph = (
        distance_miles / (duration_seconds / 3600.0) if duration_seconds > 0 else 0.0
    )

    chunks = build_chunks(rows, eff_cum_kwh, odo_mi)
    speed_bins = build_speed_bins(feats)

    record = {
        "drive_id": drive_id,
        "vin": vin,
        "day_offset": day_offset,
        "start_s": round(start_s, 1),
        "end_s": round(start_s + duration_seconds, 1),
        "battery_capacity_kwh": round(capacity_kwh, 2),
        "start_place": start_place,
        "end_place": end_place,
        "distance_miles": round(distance_miles, 2),
        "energy_kwh": energy_kwh,
        "efficiency_mi_kwh": efficiency,
        "mpge": mpge,
        "start_soc": round(start_soc, 1),
        "end_soc": round(end_soc, 1),
        "start_odometer_mi": round(start_odo_mi, 2),
        "end_odometer_mi": round(odo_mi[-1], 2),
        "start_range_mi": round(
            start_soc / 100.0 * capacity_kwh * nominal_range_eff, 1
        ),
        "end_range_mi": round(end_soc / 100.0 * capacity_kwh * nominal_range_eff, 1),
        "max_speed_mph": round(max_speed_mph, 1),
        "avg_speed_mph": round(avg_speed_mph, 1),
        "temperature_f": temperature_f_for(start_s),
        "drive_modes": [drive_mode],
        "driver": driver,
        "chunks": chunks,
        "speed_bins": speed_bins,
        "track": {
            "t": [round(row[0], 1) for row in rows],
            "lat": [round(row[1], 6) for row in rows],
            "lon": [round(row[2], 6) for row in rows],
            "speed_mps": [round(row[3], 2) for row in rows],
            "alt_m": [round(row[4], 1) for row in rows],
            "soc": [round(v, 1) for v in soc],
            "odometer_mi": [round(v, 2) for v in odo_mi],
        },
    }
    add_conditions(record, params)
    return record


def build_charging_session(
    *, session_id, vin, day_offset, start_s, capacity_kwh, place_key, start_soc
):
    # Added percentage is a realistic DCFC top-up (~40 points), not a fixed
    # 35->75: the starting SoC comes from wherever ordinary driving actually
    # left the pack (see the R1T schedule's comment), so the exact numbers
    # vary a little -- that's the point, it keeps every drive's own
    # efficiency physically consistent instead of being back-solved to hit a
    # specific SoC target.
    end_soc = min(97.0, start_soc + 40.0)
    energy_added_kwh = (end_soc - start_soc) / 100.0 * capacity_kwh
    duration_s = 30 * 60
    n = duration_s // 60 + 1
    p_start, p_end = 170.0, 25.0  # kW, DCFC taper

    raw_powers = [p_start - (p_start - p_end) * (i / (n - 1)) for i in range(n)]
    raw_energy_kwh = [0.0]
    for i in range(1, n):
        avg_p = (raw_powers[i - 1] + raw_powers[i]) / 2.0
        raw_energy_kwh.append(raw_energy_kwh[-1] + avg_p * (60.0 / 3600.0))

    scale = energy_added_kwh / raw_energy_kwh[-1] if raw_energy_kwh[-1] > 0 else 1.0
    samples = []
    for i in range(n):
        soc = start_soc + raw_energy_kwh[i] * scale / capacity_kwh * 100.0
        samples.append(
            {
                "t_s": i * 60,
                "power_kw": round(raw_powers[i] * scale, 1),
                "soc": round(min(soc, end_soc), 1),
            }
        )

    return {
        "session_id": session_id,
        "vin": vin,
        "day_offset": day_offset,
        "start_s": start_s,
        "end_s": start_s + duration_s,
        "place": place_key,
        "start_soc": start_soc,
        "end_soc": end_soc,
        "energy_added_kwh": round(energy_added_kwh, 2),
        "max_power_kw": round(max(s["power_kw"] for s in samples), 1),
        "avg_power_kw": round(energy_added_kwh / (duration_s / 3600.0), 1),
        "samples": samples,
    }


def build_curve_charging_session(
    *,
    session_id,
    vin,
    day_offset,
    start_s,
    capacity_kwh,
    place_key,
    start_soc,
    end_soc,
    pack,
    rng: random.Random,
    cap_kw=None,
):
    """A DC session following a reference pack curve (see charge_curves.py).

    Steps the pack's power-vs-SoC curve forward in 30 s ticks (with a little
    seeded noise), so the session sits a few percent off the pack's expected
    curve, as a real one would.
    """
    ref = charge_curves.reference(pack)
    soc = start_soc
    t = 0.0
    energy_kwh = 0.0
    samples = []
    next_sample_t = 0.0
    while soc < end_soc and t < 3 * 3600:
        power = charge_curves.power_at(ref, soc) * rng.uniform(0.93, 0.99)
        if cap_kw:
            power = min(power, cap_kw * 0.97)
        if t >= next_sample_t:
            samples.append(
                {"t_s": int(t), "power_kw": round(power, 1), "soc": round(soc, 1)}
            )
            next_sample_t += 60.0
        step_kwh = power * 30.0 / 3600.0
        energy_kwh += step_kwh
        soc += step_kwh / capacity_kwh * 100.0
        t += 30.0
    samples.append(
        {"t_s": int(t), "power_kw": round(power * 0.9, 1), "soc": round(end_soc, 1)}
    )
    return {
        "session_id": session_id,
        "vin": vin,
        "day_offset": day_offset,
        "start_s": round(start_s, 1),
        "end_s": round(start_s + t, 1),
        "place": place_key,
        "kind": "dc",
        "start_soc": round(start_soc, 1),
        "end_soc": round(end_soc, 1),
        "energy_added_kwh": round(energy_kwh, 2),
        "max_power_kw": round(max(s["power_kw"] for s in samples), 1),
        "avg_power_kw": round(energy_kwh / (t / 3600.0), 1),
        "samples": samples,
    }


def build_ac_session(
    *,
    session_id,
    vin,
    day_offset,
    start_s,
    capacity_kwh,
    place_key,
    start_soc,
    end_soc,
    power_kw,
    rng: random.Random,
):
    """A home (AC) session: constant ~power_kw until ``end_soc``.

    Keeps only ~a dozen coarse SoC points (power 0), like a live AC session.
    """
    energy_kwh = (end_soc - start_soc) / 100.0 * capacity_kwh
    avg_kw = power_kw * rng.uniform(0.96, 1.0)
    duration_s = energy_kwh / avg_kw * 3600.0
    n_points = max(2, min(12, int(duration_s // 600) + 1))
    samples = []
    for i in range(n_points):
        frac = i / (n_points - 1)
        samples.append(
            {
                "t_s": int(duration_s * frac),
                "power_kw": 0.0,
                "soc": round(start_soc + (end_soc - start_soc) * frac, 1),
            }
        )
    return {
        "session_id": session_id,
        "vin": vin,
        "day_offset": day_offset,
        "start_s": round(start_s, 1),
        "end_s": round(start_s + duration_s, 1),
        "place": place_key,
        "kind": "ac",
        "start_soc": round(start_soc, 1),
        "end_soc": round(end_soc, 1),
        "energy_added_kwh": round(energy_kwh, 2),
        "max_power_kw": round(power_kw, 1),
        "avg_power_kw": round(avg_kw, 1),
        "samples": samples,
    }


def declining_capacity(nominal, final, day_offset, first_day, last_day):
    """Reported capacity drifting from ``nominal`` to ``final`` over the window."""
    if last_day <= first_day:
        return nominal
    frac = (day_offset - first_day) / (last_day - first_day)
    return round(nominal + (final - nominal) * frac, 2)


def evening_plug_in_s(last_drive_end_s, rng: random.Random):
    """When a car gets plugged in at home: ~6-7 pm, or 30 min after a late drive."""
    return max(last_drive_end_s + 1800.0, 18 * 3600.0 + rng.uniform(0, 45 * 60))


# --------------------------------------------------------------------------
# Places / household
# --------------------------------------------------------------------------
def build_places(cache_dir: str) -> dict:
    raw = {
        # A residential subdivision in west Eagle, ID (near W Hereford Dr),
        # chosen to be close in elevation to the Eagle/Boise destinations
        # below so grade doesn't dominate the energy model on short local
        # errands. Snapped to the nearest road centerline below (not a
        # house parcel).
        "home": (43.701, -116.378, "home", 40),
        "downtown_eagle": (43.6958, -116.3542, "other", 60),
        "library": (43.6944, -116.3524, "other", 40),
        "merrill_park": (43.6933, -116.3581, "other", 60),
        "grocery": (43.6961, -116.3568, "shop", 40),
        "office": (43.6166, -116.2023, "work", 50),
        "boise_state": (43.6035, -116.1963, "school", 60),
        "charger": (43.5931, -116.3412, "charging", 30),
    }
    names = {
        "home": "Home",
        "downtown_eagle": "Downtown Eagle",
        "library": "Eagle Public Library",
        "merrill_park": "Merrill Park",
        "grocery": "Eagle Grocery Co-op",
        "office": "Office",
        "boise_state": "Boise State University",
        "charger": "Meridian DC Fast Charger",
    }

    places = {}
    for key, (lat, lon, category, radius) in raw.items():
        snapped_lat, snapped_lon = osrm_nearest(lat, lon, cache_dir)
        places[key] = {
            "key": key,
            "name": names[key],
            "category": category,
            "lat": round(snapped_lat, 6),
            "lon": round(snapped_lon, 6),
            "radius_m": radius,
        }
    return places


# --------------------------------------------------------------------------
# Vehicle schedules
# --------------------------------------------------------------------------
def build_r2(places: dict, cache_dir: str, rng: random.Random, ev_rng: random.Random):
    vin = "DEMO0R2EAGLE00001"
    capacity_kwh = 87.9  # nominal; the reported capacity drifts to 87.7
    final_capacity_kwh = 87.7
    params = VEHICLE_PARAMS[vin]
    nominal_range_eff = 3.2  # mi/kWh, used only for the start/end "range" fields
    driver = "Demo Driver"
    drive_mode = "All-Purpose"

    # Three Home<->Library round trips' worth of legs (sharing the same
    # Library endpoint so they cluster on the map), ending with a trip into
    # Boise on the last outing: Home->Library x3, Library->Home x2,
    # Library->Boise State x1, Boise State->Home x1. (Library->Merrill Park
    # was only 0.46 mi -- under the 0.5 mi micro-drive threshold, so it was
    # hidden from the calendar.)
    #
    # Charging story: the pack starts low (27 %) and the owner skipped
    # plugging in after day 0, so the first outing on day 3 leaves it near
    # 25 % -- one DC fast charge at the Meridian charger (25 -> 70 %) in the
    # middle of that outing, then a home AC charge to 80 % that evening and
    # again after the last drive on day 9. A drive's start SoC is always the
    # previous drive's end SoC, or the charge's end SoC when one happened in
    # between.
    schedule = [
        # (drive_id_suffix, day_offset, start_s, start_place, end_place)
        ("d01", 0, 10 * 3600, "home", "library"),
        ("d02", 0, 12 * 3600, "library", "home"),
        ("d03", 3, 17 * 3600, "home", "library"),
        ("d04", 3, 18 * 3600 + 1800, "library", "home"),
        ("d05", 9, 9 * 3600 + 1800, "home", "library"),
        ("d06", 9, 11 * 3600, "library", "boise_state"),
        ("d07", 9, 15 * 3600, "boise_state", "home"),
    ]
    first_day = schedule[0][1]
    last_day = schedule[-1][1]
    # After these drives the car is plugged in at home (AC to 80 %).
    ac_after = {"d04", "d07"}

    soc = 27.0
    odo_mi = 1800.0
    drives = []
    charging_sessions = []
    for suffix, day_offset, start_s, start_place, end_place in schedule:
        record = build_drive(
            vin=vin,
            drive_id=f"{vin}_{suffix}",
            day_offset=day_offset,
            start_s=start_s,
            start_place=start_place,
            end_place=end_place,
            places=places,
            start_soc=soc,
            start_odo_mi=odo_mi,
            capacity_kwh=declining_capacity(
                capacity_kwh, final_capacity_kwh, day_offset, first_day, last_day
            ),
            params=params,
            nominal_range_eff=nominal_range_eff,
            driver=driver,
            drive_mode=drive_mode,
            cache_dir=cache_dir,
            rng=rng,
        )
        drives.append(record)
        soc = record["end_soc"]
        odo_mi = record["end_odometer_mi"]

        if suffix == "d03":
            session = build_curve_charging_session(
                session_id=f"{vin}_cs01",
                vin=vin,
                day_offset=day_offset,
                start_s=record["end_s"] + 5 * 60,
                capacity_kwh=record["battery_capacity_kwh"],
                place_key="charger",
                start_soc=soc,
                end_soc=70.0,
                pack="r2",
                rng=ev_rng,
            )
            charging_sessions.append(session)
            soc = session["end_soc"]
        elif suffix in ac_after and soc < 79.0:
            session = build_ac_session(
                session_id=f"{vin}_ac_{suffix}",
                vin=vin,
                day_offset=day_offset,
                start_s=evening_plug_in_s(record["end_s"], ev_rng),
                capacity_kwh=record["battery_capacity_kwh"],
                place_key="home",
                start_soc=soc,
                end_soc=80.0,
                power_kw=9.6,
                rng=ev_rng,
            )
            charging_sessions.append(session)
            soc = session["end_soc"]

    return {
        "vin": vin,
        "name": "Demo R2",
        "model": "R2",
        "battery_capacity_kwh": capacity_kwh,
        "drives": drives,
        "charging_sessions": charging_sessions,
    }


def build_r1t(places: dict, cache_dir: str, rng: random.Random, ev_rng: random.Random):
    vin = "DEMO1R1TEAGLE0002"
    capacity_kwh = 135.0  # nominal; the reported capacity drifts to 134.4
    final_capacity_kwh = 134.4
    params = VEHICLE_PARAMS[vin]
    nominal_range_eff = 2.8
    driver = "Demo Driver"
    drive_mode = "All-Purpose"

    # Deliberately NOT plugged in at home every night for the first few days
    # (``plug_in`` False), so ordinary commute driving alone brings the pack
    # down into DC-fast-charge territory by day 3 -- that's when the one DC
    # session in this fixture happens, at the Meridian charger, on the way
    # home from the office. Where a ``plug_in`` is flagged, the car charges
    # on the home AC charger that evening (to 80 %), and the next drive
    # starts at that session's end SoC. SoC is otherwise always continuous
    # between consecutive drives: equal to the previous drive's actual end
    # SoC.
    schedule = [
        ("d01", 1, 7 * 3600 + 45 * 60, "home", "office", False),
        ("d02", 1, 17 * 3600, "office", "home", False),
        ("d03", 3, 8 * 3600, "home", "office", False),
        ("d04", 3, 16 * 3600 + 45 * 60, "office", "charger", False),
        # DC-fast-charge session happens here, wherever this leg naturally
        # left the pack.
        ("d05", 3, 17 * 3600 + 45 * 60, "charger", "home", True),
        ("d06", 5, 7 * 3600 + 30 * 60, "home", "office", False),
        ("d07", 5, 17 * 3600 + 15 * 60, "office", "home", True),
        ("d08", 8, 8 * 3600 + 15 * 60, "home", "office", False),
        ("d09", 8, 16 * 3600 + 50 * 60, "office", "home", True),
    ]
    first_day = schedule[0][1]
    last_day = schedule[-1][1]

    soc = 49.0  # starting point tuned so natural driving reaches ~35-40% by day 3
    odo_mi = 14200.0
    drives = []
    charging_sessions = []
    for (
        suffix,
        day_offset,
        start_s,
        start_place,
        end_place,
        plug_in,
    ) in schedule:
        record = build_drive(
            vin=vin,
            drive_id=f"{vin}_{suffix}",
            day_offset=day_offset,
            start_s=start_s,
            start_place=start_place,
            end_place=end_place,
            places=places,
            start_soc=soc,
            start_odo_mi=odo_mi,
            capacity_kwh=declining_capacity(
                capacity_kwh, final_capacity_kwh, day_offset, first_day, last_day
            ),
            params=params,
            nominal_range_eff=nominal_range_eff,
            driver=driver,
            drive_mode=drive_mode,
            cache_dir=cache_dir,
            rng=rng,
        )
        drives.append(record)
        soc = record["end_soc"]
        odo_mi = record["end_odometer_mi"]

        if suffix == "d04":
            session_start_s = record["end_s"] + 5 * 60  # a short walk-in delay
            session = build_charging_session(
                session_id=f"{vin}_cs01",
                vin=vin,
                day_offset=day_offset,
                start_s=round(session_start_s, 1),
                capacity_kwh=record["battery_capacity_kwh"],
                place_key="charger",
                start_soc=soc,
            )
            charging_sessions.append(session)
            soc = session["end_soc"]  # the next drive starts freshly charged
        elif plug_in and soc < 79.0:
            session = build_ac_session(
                session_id=f"{vin}_ac_{suffix}",
                vin=vin,
                day_offset=day_offset,
                start_s=evening_plug_in_s(record["end_s"], ev_rng),
                capacity_kwh=record["battery_capacity_kwh"],
                place_key="home",
                start_soc=soc,
                end_soc=80.0,
                power_kw=11.5,
                rng=ev_rng,
            )
            charging_sessions.append(session)
            soc = session["end_soc"]

    return {
        "vin": vin,
        "name": "Demo R1T",
        "model": "R1T",
        "battery_capacity_kwh": capacity_kwh,
        "drives": drives,
        "charging_sessions": charging_sessions,
    }


# --------------------------------------------------------------------------
# Sanity report
# --------------------------------------------------------------------------
def check_place_radii(vehicles: list[dict], places: dict):
    """Verify every drive's start/end point lies within its place's radius_m.

    Prints the max observed endpoint-to-place distance per place (so a
    radius that's too tight shows up immediately) and raises if any endpoint
    actually falls outside its place's radius.
    """
    haversine_m = drive_track.haversine_m
    max_dist: dict[str, float] = {}
    violations: list[str] = []
    for vehicle in vehicles:
        for d in vehicle["drives"]:
            track = d["track"]
            for place_key, lat, lon in (
                (d["start_place"], track["lat"][0], track["lon"][0]),
                (d["end_place"], track["lat"][-1], track["lon"][-1]),
            ):
                place = places[place_key]
                dist = haversine_m(lat, lon, place["lat"], place["lon"])
                max_dist[place_key] = max(max_dist.get(place_key, 0.0), dist)
                if dist > place["radius_m"]:
                    violations.append(
                        f"{d['drive_id']} endpoint at {place_key} is "
                        f"{dist:.1f} m from the place (radius {place['radius_m']} m)"
                    )

    print("\nMax endpoint-to-place distance (place radius_m in parens):")
    for key, place in places.items():
        dist = max_dist.get(key, 0.0)
        print(f"  {key:>14}: {dist:6.1f} m  (radius {place['radius_m']} m)")

    if violations:
        raise RuntimeError(
            "Drive endpoint(s) fall outside their place's radius_m:\n  "
            + "\n  ".join(violations)
        )


def print_sanity_report(vehicle: dict):
    drives = vehicle["drives"]
    total_miles = sum(d["distance_miles"] for d in drives)
    print(f"\n=== {vehicle['name']} ({vehicle['vin']}) ===")
    print(f"  drives: {len(drives)}   total miles: {total_miles:.1f}")
    pair_counts: dict[tuple[str, str], int] = {}
    for d in drives:
        key = (d["start_place"], d["end_place"])
        pair_counts[key] = pair_counts.get(key, 0) + 1
        print(
            f"  {d['drive_id']:>22}  day {d['day_offset']}  "
            f"{d['start_place']:>14} -> {d['end_place']:<14}  "
            f"{d['distance_miles']:5.1f} mi  "
            f"{d['energy_kwh'] / max(d['end_s'] - d['start_s'], 1) * 3600:5.1f} kWh/h  "
            f"{d['efficiency_mi_kwh']:4.2f} mi/kWh  "
            f"soc {d['start_soc']:5.1f}->{d['end_soc']:5.1f}  "
            f"dur {d['end_s'] - d['start_s']:5.0f}s"
        )
    print("  repeated pairs:")
    for (a, b), count in sorted(pair_counts.items(), key=lambda kv: -kv[1]):
        if count > 1:
            print(f"    {a} -> {b}: x{count}")
    for session in vehicle.get("charging_sessions", []):
        print(
            f"  charging {session['session_id']}: "
            f"{session['start_soc']}->{session['end_soc']}% "
            f"{session['energy_added_kwh']} kWh over "
            f"{(session['end_s'] - session['start_s']) / 60:.0f} min, "
            f"peak {session['max_power_kw']} kW"
        )


# --------------------------------------------------------------------------
# History: extra charging sessions + a year of capacity readings (offline)
# --------------------------------------------------------------------------
# Public chargers the demo cars use. The sites are made up (nothing here is a
# real station's coordinates); the brands are the ones the app tells apart.
HISTORY_SESSION_PREFIX = "_hx"
CAPACITY_HISTORY_DAYS = 365
# (key, display name, lat, lon) -> station metadata stored on the session
EXTRA_CHARGER_PLACES = {
    "ran_boise": ("Rivian Adventure Network - Boise", 43.5931, -116.2724),
    "tesla_meridian": ("Tesla Supercharger - Meridian", 43.6127, -116.3912),
    "tesla_nampa": ("Tesla Supercharger - Nampa", 43.5843, -116.5532),
}
STATIONS = {
    "charger": {
        "vendor": "Electrify America",
        "network": "Electrify America",
        "station_name": "Electrify America - Meridian",
        "station_version": None,
        "charger_max_kw": 350.0,
    },
    "ran_boise": {
        "vendor": "Rivian",
        "network": "Rivian Adventure Network",
        "station_name": "Rivian Adventure Network - Boise",
        "station_version": None,
        "charger_max_kw": 200.0,
    },
    "tesla_meridian": {
        "vendor": "Tesla",
        "network": "Tesla Supercharger",
        "station_name": "Tesla Supercharger - Meridian",
        "station_version": "V3",
        "charger_max_kw": 250.0,
    },
    "tesla_nampa": {
        "vendor": "Tesla",
        "network": "Tesla Supercharger",
        "station_name": "Tesla Supercharger - Nampa",
        "station_version": "V4",
        "charger_max_kw": 325.0,
    },
}
HOME_STATION = {
    "vendor": "Rivian Wall Charger",
    "network": None,
    "station_name": None,
    "station_version": None,
    "charger_max_kw": None,
}
# Charging story before the recorded window (negative day offsets). Each car
# charges at home most nights and fast-charges on trip days. No drives are
# recorded in this stretch, so the level between two sessions drops by
# unrecorded driving; a session's start SoC is the level the car had then.
# (day_offset, kind, start_soc, end_soc, place, hour)
PREROLL = {
    "DEMO1R1TEAGLE0002": [
        (-21, "ac", 61.0, 80.0, "home", 19.2),
        (-20, "ac", 66.0, 80.0, "home", 18.4),
        (-19, "ac", 52.0, 80.0, "home", 19.6),
        (-18, "dc", 17.0, 68.0, "ran_boise", 12.7),
        (-17, "ac", 58.0, 80.0, "home", 18.9),
        (-16, "ac", 71.0, 80.0, "home", 19.1),
        (-15, "ac", 63.0, 80.0, "home", 18.2),
        (-14, "dc", 21.0, 72.0, "tesla_meridian", 13.2),
        (-13, "ac", 55.0, 80.0, "home", 19.4),
        (-12, "ac", 68.0, 80.0, "home", 18.6),
        (-11, "ac", 49.0, 80.0, "home", 19.0),
        (-10, "dc", 15.0, 60.0, "charger", 12.2),
        (-9, "ac", 56.0, 80.0, "home", 18.8),
        (-8, "ac", 70.0, 80.0, "home", 19.3),
        (-7, "ac", 57.0, 80.0, "home", 18.5),
        (-6, "dc", 19.0, 75.0, "ran_boise", 17.3),
        (-5, "ac", 60.0, 80.0, "home", 19.7),
        (-4, "ac", 66.0, 80.0, "home", 18.3),
        (-3, "ac", 53.0, 80.0, "home", 19.0),
        (-2, "dc", 24.0, 66.0, "tesla_meridian", 13.7),
        (-1, "ac", 59.0, 80.0, "home", 18.9),
    ],
    "DEMO0R2EAGLE00001": [
        (-21, "ac", 58.0, 80.0, "home", 19.0),
        (-20, "ac", 64.0, 80.0, "home", 18.5),
        (-19, "dc", 14.0, 64.0, "charger", 12.4),
        (-18, "ac", 57.0, 80.0, "home", 19.2),
        (-17, "ac", 69.0, 80.0, "home", 18.1),
        (-16, "ac", 61.0, 80.0, "home", 19.5),
        (-15, "dc", 20.0, 71.0, "tesla_nampa", 13.0),
        (-14, "ac", 54.0, 80.0, "home", 18.7),
        (-13, "ac", 67.0, 80.0, "home", 19.1),
        (-12, "ac", 50.0, 80.0, "home", 18.4),
        (-11, "dc", 16.0, 62.0, "charger", 11.9),
        (-10, "ac", 57.0, 80.0, "home", 19.3),
        (-9, "ac", 72.0, 80.0, "home", 18.8),
        (-8, "dc", 22.0, 69.0, "tesla_nampa", 16.8),
        (-7, "ac", 56.0, 80.0, "home", 19.6),
        (-6, "ac", 65.0, 80.0, "home", 18.2),
        (-5, "ac", 59.0, 80.0, "home", 19.0),
        (-4, "dc", 18.0, 66.0, "charger", 12.8),
        (-3, "ac", 61.0, 80.0, "home", 18.9),
        (-2, "ac", 68.0, 80.0, "home", 19.2),
        (-1, "ac", 55.0, 80.0, "home", 18.6),
    ],
}
# Pack reference curve and home-charger power per demo vehicle.
VEHICLE_CHARGING = {
    "DEMO1R1TEAGLE0002": ("large", 11.5),
    "DEMO0R2EAGLE00001": ("r2", 9.6),
}
CAPACITY_FADE = {
    "DEMO1R1TEAGLE0002": (135.0, 134.2),
    "DEMO0R2EAGLE00001": (87.9, 87.5),
}


def _capacity_for(vin: str, day_offset: int, max_offset: int) -> float:
    """The fade curve's capacity (kWh) on a day, before any per-day noise."""
    start, end = CAPACITY_FADE[vin]
    frac = 1.0 - (max_offset - day_offset) / (CAPACITY_HISTORY_DAYS - 1)
    return start + (end - start) * min(max(frac, 0.0), 1.0)


def augment_charging(fixture: dict, seed: int) -> None:
    """Add pre-roll fast/home sessions, station metadata and capacity history.

    Offline and deterministic; idempotent (generated sessions carry the
    ``_hx`` id marker and are replaced on re-run). Drive and route counts are
    untouched; drives only get their reported capacity from the yearly fade.
    """
    rng = random.Random(seed + 2)
    place_keys = {p["key"] for p in fixture["places"]}
    for key, (name, lat, lon) in EXTRA_CHARGER_PLACES.items():
        if key not in place_keys:
            fixture["places"].append(
                {
                    "key": key,
                    "name": name,
                    "category": "charging",
                    "lat": lat,
                    "lon": lon,
                    "radius_m": 30,
                }
            )
    max_offset = max(
        int(item["day_offset"])
        for vehicle in fixture["vehicles"]
        for item in vehicle["drives"] + vehicle["charging_sessions"]
        if HISTORY_SESSION_PREFIX not in item.get("session_id", "")
    )
    for vehicle in fixture["vehicles"]:
        vin = vehicle["vin"]
        pack, ac_kw = VEHICLE_CHARGING[vin]
        capacity = vehicle["battery_capacity_kwh"]
        sessions = [
            s
            for s in vehicle["charging_sessions"]
            if HISTORY_SESSION_PREFIX not in s["session_id"]
        ]
        # Station details on the sessions already in the window.
        for session in sessions:
            station = (
                HOME_STATION
                if session.get("kind") == "ac"
                else STATIONS[session["place"]]
            )
            session.update(station)
            session["is_home"] = 1 if session.get("kind") == "ac" else 0
        for n, (offset, kind, soc0, soc1, place, hour) in enumerate(PREROLL[vin]):
            sid = f"{vin}{HISTORY_SESSION_PREFIX}{n:02d}"
            start_s = round(hour * 3600.0)
            if kind == "ac":
                session = build_ac_session(
                    session_id=sid,
                    vin=vin,
                    day_offset=offset,
                    start_s=start_s,
                    capacity_kwh=capacity,
                    place_key=place,
                    start_soc=soc0,
                    end_soc=soc1,
                    power_kw=ac_kw,
                    rng=rng,
                )
                session.update(HOME_STATION)
                session["is_home"] = 1
            else:
                station = STATIONS[place]
                session = build_curve_charging_session(
                    session_id=sid,
                    vin=vin,
                    day_offset=offset,
                    start_s=start_s,
                    capacity_kwh=capacity,
                    place_key=place,
                    start_soc=soc0,
                    end_soc=soc1,
                    pack=pack,
                    rng=rng,
                    cap_kw=station["charger_max_kw"],
                )
                session.update(station)
                session["is_home"] = 0
            sessions.append(session)
        sessions.sort(key=lambda s: (s["day_offset"], s["start_s"]))
        vehicle["charging_sessions"] = sessions
        # Reported capacity follows the yearly fade on every drive.
        for drive in vehicle["drives"]:
            drive["battery_capacity_kwh"] = round(
                _capacity_for(vin, int(drive["day_offset"]), max_offset), 2
            )
        history = []
        for k in range(CAPACITY_HISTORY_DAYS):
            offset = max_offset - (CAPACITY_HISTORY_DAYS - 1) + k
            kwh = _capacity_for(vin, offset, max_offset)
            if k:
                kwh += rng.uniform(-0.03, 0.03)
            history.append(
                {
                    "day_offset": offset,
                    "kwh": round(kwh, 2),
                    "temp_noise_f": round(rng.gauss(0.0, 5.0), 1),
                    # Days with a fast charge read the battery's own temperature.
                    "temp_source": "battery" if k % 3 == 0 else "outside",
                }
            )
        vehicle["capacity_history"] = history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument(
        "--cache",
        default=os.path.join(os.environ.get("TEMP", "/tmp"), "rivian_demo_build_cache"),
    )
    parser.add_argument("--seed", type=int, default=20261002)
    parser.add_argument(
        "--augment",
        action="store_true",
        help=(
            "Offline: read the existing fixture at --out, (re)compute each "
            "drive's driving conditions, add the extra charging sessions, "
            "station details and capacity history, and write it back. No "
            "network; drive and route counts never change."
        ),
    )
    args = parser.parse_args()

    if args.augment:
        with open(args.out, encoding="utf-8") as fh:
            fixture = json.load(fh)
        for vehicle in fixture["vehicles"]:
            for drive in vehicle["drives"]:
                add_conditions(drive, VEHICLE_PARAMS[vehicle["vin"]])
        augment_charging(fixture, args.seed)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(fixture, fh, separators=(",", ":"))
        print(f"Added driving conditions to {args.out}")
        return

    rng = random.Random(args.seed)
    # Charging sessions draw from their own stream so adding them never
    # shifts the drives generated from `rng`.
    ev_rng = random.Random(args.seed + 1)
    cache_dir = args.cache
    os.makedirs(cache_dir, exist_ok=True)

    print(f"Using OSRM/Open-Meteo cache: {cache_dir}")
    places = build_places(cache_dir)
    print("Places (road-snapped):")
    for key, place in places.items():
        print(f"  {key:>14}: {place['name']:<26} {place['lat']:.6f},{place['lon']:.6f}")

    r2 = build_r2(places, cache_dir, rng, ev_rng)
    r1t = build_r1t(places, cache_dir, rng, ev_rng)

    print_sanity_report(r2)
    print_sanity_report(r1t)
    check_place_radii([r2, r1t], places)

    output = {
        "version": 1,
        "household": {
            "name": "Demo household",
            "address": "1450 W Larkspur Ridge Dr, Eagle, ID",
            "home": {"lat": places["home"]["lat"], "lon": places["home"]["lon"]},
        },
        "places": list(places.values()),
        "vehicles": [r2, r1t],
    }

    augment_charging(output, args.seed)
    out_path = args.out
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, separators=(",", ":"))

    size_kb = os.path.getsize(out_path) / 1024.0
    print(f"\nWrote {out_path} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
