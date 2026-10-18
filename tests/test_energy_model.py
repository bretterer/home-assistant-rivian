"""Unit tests for the anchored per-vehicle energy model (energy_model.py).

Pure Python, no Home Assistant imports needed: these exercise the physics
feature extraction, the coordinate-descent fit, and the SoC-step anchoring
directly against synthetic GPS tracks.
"""

from __future__ import annotations

import random

import pytest

from custom_components.rivian.drive_track import DriveTrack, TrackPoint
from custom_components.rivian.energy_model import (
    DEFAULT_PARAMS,
    J_PER_KWH,
    METERS_PER_MILE,
    EnergyModelParams,
    _soc_step_events,
    anchored_efficiency,
    fit_params,
    interval_battery_j,
    interval_features,
)

BASE_T = 1_700_000_000.0


def _flat_track(n: int = 6, dt: float = 5.0, speed: float = 20.0) -> DriveTrack:
    """A short, constant-speed, flat track (no altitude change)."""
    points = [
        TrackPoint(
            t=BASE_T + i * dt,
            lat=44.0 + i * 0.0001,
            lon=-116.0,
            speed_mps=speed,
            alt_m=800.0,
        )
        for i in range(n)
    ]
    return DriveTrack(points)


class TestIntervalFeatures:
    """interval_features()."""

    def test_constant_speed_flat_track(self) -> None:
        """Constant speed, flat altitude: kinetic and grade terms are ~zero."""
        track = _flat_track()
        features = interval_features(track)
        assert len(features) == 5
        for feature in features:
            assert feature.dt == pytest.approx(5.0)
            assert feature.vm == pytest.approx(20.0)
            assert feature.dist_m == pytest.approx(100.0, rel=0.05)
            assert feature.aero_j > 0
            assert feature.roll_j > 0
            assert feature.grade_j == pytest.approx(0.0, abs=1e-6)
            assert feature.kinetic_j == pytest.approx(0.0, abs=1e-6)

    def test_gap_over_max_is_skipped(self) -> None:
        """An interval with dt > MAX_INTERVAL_GAP_S is dropped, not just skewed."""
        points = [
            TrackPoint(t=BASE_T, lat=44.0, lon=-116.0, speed_mps=10.0),
            TrackPoint(t=BASE_T + 5, lat=44.0001, lon=-116.0, speed_mps=10.0),
            TrackPoint(t=BASE_T + 200, lat=44.0002, lon=-116.0, speed_mps=10.0),
        ]
        features = interval_features(DriveTrack(points))
        assert len(features) == 1

    def test_short_track_returns_no_features(self) -> None:
        """A single-point (or empty) track has no intervals at all."""
        assert (
            interval_features(DriveTrack([TrackPoint(t=BASE_T, lat=44.0, lon=-116.0)]))
            == []
        )
        assert interval_features(DriveTrack()) == []

    def test_grade_reflects_altitude_change(self) -> None:
        """A steady climb produces a positive grade_j (energy uphill costs more)."""
        points = [
            TrackPoint(
                t=BASE_T + i * 5,
                lat=44.0 + i * 0.0001,
                lon=-116.0,
                speed_mps=10.0,
                alt_m=800.0 + i * 5.0,
            )
            for i in range(6)
        ]
        features = interval_features(DriveTrack(points))
        # The centered altitude smoothing window can flatten one interior
        # interval on a short climb, but every interval is non-negative and
        # the climb nets out positive overall.
        assert all(f.grade_j >= -1e-6 for f in features)
        assert sum(f.grade_j for f in features) > 0


class TestFitParams:
    """fit_params() coordinate descent."""

    def test_recovers_true_params_approximately(self) -> None:
        """Fitting synthetic drives generated from known params lands in the right ballpark."""
        true_params = EnergyModelParams(
            cda_m2=1.05, crr=0.011, aux_w=700.0, eta_drive=0.88, eta_regen=0.6
        )

        def make_drive(seed: int) -> DriveTrack:
            rng = random.Random(seed)
            points = []
            t = 0.0
            lat = 44.0
            speed = 0.0
            alt = 800.0
            target = 30.0 if seed % 2 == 0 else 15.0
            for i in range(300):
                speed = max(
                    0.0,
                    min(40.0, speed + rng.uniform(-3, 3) + (target - speed) * 0.02),
                )
                if i % 40 == 39:
                    speed = 0.0  # a stop, to give regen braking something to see
                alt += rng.uniform(-0.5, 0.5)
                lat += speed * 5.0 / 111111.0
                points.append(
                    TrackPoint(t=t, lat=lat, lon=-116.0, speed_mps=speed, alt_m=alt)
                )
                t += 5.0
            return DriveTrack(points)

        drives = []
        for seed in range(30):
            track = make_drive(seed)
            features = interval_features(track)
            measured_kwh = interval_battery_j(features, true_params) / J_PER_KWH
            drives.append((features, measured_kwh))

        fitted, rmse_kwh, n = fit_params(drives)
        assert n == 30
        # The four fittable coefficients only need to land in the right
        # ballpark -- whole-drive energy alone underdetermines the exact
        # split between CdA/Crr/aux/regen -- but the fit itself should
        # explain the (noise-free) synthetic data very well.
        assert 0.5 <= fitted.cda_m2 <= 1.8
        assert 0.005 <= fitted.crr <= 0.025
        assert 0.0 <= fitted.aux_w <= 2000.0
        assert 0.3 <= fitted.eta_regen <= 0.85
        assert rmse_kwh < 0.2

    def test_too_few_qualifying_drives_returns_default(self) -> None:
        """Drives under the energy/distance floor are filtered out entirely."""
        track = _flat_track()
        features = interval_features(track)
        # Below MIN_DRIVE_ENERGY_KWH and MIN_DRIVE_DISTANCE_MI.
        params, rmse_kwh, n = fit_params([(features, 0.01)])
        assert n == 0
        assert rmse_kwh == 0.0
        assert params == DEFAULT_PARAMS


class TestSocStepEvents:
    """_soc_step_events(): downward-step detection with up-flicker suppression."""

    def test_simple_decreasing_steps(self) -> None:
        points = [
            TrackPoint(t=0, lat=44.0, lon=-116.0, soc=80.0),
            TrackPoint(t=300, lat=44.001, lon=-116.0, soc=79.5),
            TrackPoint(t=600, lat=44.002, lon=-116.0, soc=79.0),
        ]
        result = _soc_step_events(DriveTrack(points))
        assert result is not None
        t0, soc0, events = result
        assert (t0, soc0) == (0, 80.0)
        assert events == [(300, 79.5), (600, 79.0)]

    def test_one_step_up_flicker_is_ignored(self) -> None:
        points = [
            TrackPoint(t=0, lat=44.0, lon=-116.0, soc=80.0),
            TrackPoint(t=5, lat=44.0001, lon=-116.0, soc=79.9),
            TrackPoint(t=10, lat=44.0002, lon=-116.0, soc=80.0),  # flicker up
            TrackPoint(t=15, lat=44.0003, lon=-116.0, soc=79.9),
            TrackPoint(t=20, lat=44.0004, lon=-116.0, soc=79.8),
        ]
        result = _soc_step_events(DriveTrack(points))
        assert result is not None
        t0, soc0, events = result
        assert (t0, soc0) == (0, 80.0)
        # The flicker at t=10 produces no event, and doesn't reset the
        # baseline, so 79.9 at t=15 is *not* re-registered as a new step.
        assert events == [(5, 79.9), (20, 79.8)]

    def test_no_soc_readings_returns_none(self) -> None:
        points = [TrackPoint(t=0, lat=44.0, lon=-116.0)]
        assert _soc_step_events(DriveTrack(points)) is None


class TestAnchoredEfficiency:
    """anchored_efficiency(): SoC-step anchoring of the modeled energy shape."""

    def test_single_interval_reconstruction_matches_measured(self) -> None:
        """With one SoC step spanning a constant-speed, flat track, the modeled
        shape is constant, so every point exactly reconstructs distance /
        measured energy, and the measured point sits mid-track.
        """
        points = [
            TrackPoint(
                t=BASE_T + i * 5,
                lat=44.0 + i * 0.0001,
                lon=-116.0,
                speed_mps=20.0,
                alt_m=800.0,
                soc=80.0 if i == 0 else None,
            )
            for i in range(6)
        ]
        points[-1] = TrackPoint(
            t=points[-1].t,
            lat=points[-1].lat,
            lon=points[-1].lon,
            speed_mps=20.0,
            alt_m=800.0,
            soc=79.5,
        )
        track = DriveTrack(points)

        result = anchored_efficiency(track, DEFAULT_PARAMS, capacity_kwh=135.0)
        assert result is not None

        features = interval_features(track)
        total_dist_mi = sum(f.dist_m for f in features) / METERS_PER_MILE
        measured_kwh = 0.5 / 100.0 * 135.0
        expected_eff = total_dist_mi / measured_kwh

        assert len(result["points"]) == 1
        # 5 x 100 m intervals: the distance midpoint (250 m) falls between the
        # 3rd and 4th fixes, never at the drive's end.
        assert result["points"][0][0] in (points[2].t, points[3].t)
        assert result["points"][0][1] == pytest.approx(expected_eff)
        assert all(v == pytest.approx(expected_eff) for v in result["eff"])

    def test_curve_passes_through_every_measured_point(self) -> None:
        """Varying speed and grade, several SoC steps: at each measured point
        the curve equals the measured value, and between points it follows
        the model rather than drawing a straight line."""
        rng = random.Random(7)
        points = []
        soc = 80.0
        alt = 800.0
        for i in range(240):
            speed = 12.0 + 10.0 * ((i // 20) % 2) + rng.uniform(-1.0, 1.0)
            alt += rng.uniform(-0.6, 0.6)
            if i and i % 12 == 0:
                soc = round(soc - 0.1, 1)
            points.append(
                TrackPoint(
                    t=BASE_T + i * 5,
                    lat=44.0 + i * speed * 5 / 111_000,
                    lon=-116.0,
                    speed_mps=speed,
                    alt_m=alt,
                    soc=soc,
                )
            )
        track = DriveTrack(points)
        result = anchored_efficiency(track, DEFAULT_PARAMS, capacity_kwh=135.0)
        assert result is not None
        assert len(result["points"]) >= 5
        times = [p.t for p in points]
        for t, value in result["points"]:
            j = times.index(t)
            assert result["eff"][j] == pytest.approx(value)
        # Measured intervals span 0.3% (three 0.1% steps here), and each
        # point sits inside its interval, never on the step that closes it.
        step_times = [
            p.t for k, p in enumerate(points) if k and points[k - 1].soc != p.soc
        ]
        assert len(result["points"]) == len(step_times) // 3
        assert not set(step_times[2::3]) & {t for t, _ in result["points"]}
        values = [v for v in result["eff"] if v is not None]
        assert len({round(v, 6) for v in values}) > len(result["points"])

    def test_no_capacity_falls_back_to_drive_total_energy(self) -> None:
        """Without capacity/SoC data, the whole track scales to drive_energy_kwh."""
        track = _flat_track()
        result = anchored_efficiency(
            track, DEFAULT_PARAMS, capacity_kwh=None, drive_energy_kwh=1.0
        )
        assert result is not None
        assert any(v is not None for v in result["eff"])

    def test_no_capacity_and_no_drive_energy_returns_none(self) -> None:
        track = _flat_track()
        assert anchored_efficiency(track, DEFAULT_PARAMS, capacity_kwh=None) is None

    def test_track_with_no_intervals_returns_none(self) -> None:
        track = DriveTrack([TrackPoint(t=BASE_T, lat=44.0, lon=-116.0, soc=80.0)])
        assert anchored_efficiency(track, DEFAULT_PARAMS, capacity_kwh=135.0) is None


class TestEnergyModelParams:
    def test_clamped_bounds_out_of_range_values(self) -> None:
        wild = EnergyModelParams(cda_m2=5.0, crr=0.5, aux_w=-100.0, eta_regen=1.5)
        clamped = wild.clamped()
        assert clamped.cda_m2 == 1.6
        assert clamped.crr == 0.020
        assert clamped.aux_w == 0.0
        assert clamped.eta_regen == 0.85

    def test_to_dict_from_dict_roundtrip(self) -> None:
        params = EnergyModelParams(cda_m2=0.95, crr=0.012, aux_w=600.0)
        restored = EnergyModelParams.from_dict(params.to_dict())
        assert restored == params

    def test_from_dict_missing_keys_use_defaults(self) -> None:
        restored = EnergyModelParams.from_dict({"cda_m2": 1.0})
        assert restored.cda_m2 == 1.0
        assert restored.crr == DEFAULT_PARAMS.crr
