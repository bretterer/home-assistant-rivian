"""Unit tests for drive_stats.compute_track_stats (Strava-style per-drive stats)."""

from __future__ import annotations

import pytest

from custom_components.rivian.drive_stats import (
    ALTITUDE_HYSTERESIS_M,
    GPS_GAP_MAX_SECONDS,
    HIGHWAY_SPEED_THRESHOLD_MPS,
    STOP_MIN_DURATION_SECONDS,
    STOPPED_SPEED_THRESHOLD_MPS,
    compute_track_stats,
)
from custom_components.rivian.drive_track import DriveTrack, TrackPoint

BASE_T = 1_700_000_000.0


def _track(
    rows: list[tuple[float, float, float | None, float | None, float | None]],
) -> DriveTrack:
    """Build a track from (t_offset, lat, speed_mps, alt_m, soc) rows.

    lon is derived from lat so points move (haversine distance isn't zero),
    and t is BASE_T + t_offset.
    """
    track = DriveTrack()
    for i, (t_off, lat, speed, alt, soc) in enumerate(rows):
        track.append(
            TrackPoint(
                t=BASE_T + t_off,
                lat=lat,
                lon=-122.0 + i * 0.0005,
                speed_mps=speed,
                alt_m=alt,
                soc=soc,
            )
        )
    return track


class TestEdgeCases:
    """Empty/short tracks and missing columns must yield all-None stats."""

    def test_empty_track_is_all_none(self) -> None:
        stats = compute_track_stats(DriveTrack())
        assert stats.moving_seconds is None
        assert stats.stopped_seconds is None
        assert stats.stop_count is None
        assert stats.climb_m is None
        assert stats.descent_m is None
        assert stats.min_alt_m is None
        assert stats.max_alt_m is None
        assert stats.max_speed_mps is None
        assert stats.pct_distance_over_70mph is None

    def test_single_point_track_is_all_none(self) -> None:
        track = DriveTrack()
        track.append(TrackPoint(t=BASE_T, lat=37.0, lon=-122.0))
        stats = compute_track_stats(track)
        assert stats.moving_seconds is None
        assert stats.stop_count is None

    def test_missing_altitude_leaves_climb_descent_none(self) -> None:
        track = _track([(0, 37.0, 10.0, None, None), (10, 37.001, 10.0, None, None)])
        stats = compute_track_stats(track)
        assert stats.climb_m is None
        assert stats.descent_m is None
        assert stats.min_alt_m is None
        assert stats.max_alt_m is None

    def test_missing_speed_column_falls_back_to_distance_over_time(self) -> None:
        # No speed_mps anywhere: moving/stopped is still classified via
        # haversine distance / dt. max_speed_mps has no point speeds to
        # percentile over, so it stays None (it never uses the fallback).
        track = _track([(0, 37.0, None, None, None), (10, 37.001, None, None, None)])
        stats = compute_track_stats(track)
        assert stats.moving_seconds is not None
        assert stats.max_speed_mps is None


class TestMovingStoppedAndGaps:
    """Moving/stopped classification, and GPS-gap intervals count as neither."""

    def test_all_moving(self) -> None:
        track = _track(
            [
                (0, 37.0, 10.0, None, None),
                (10, 37.001, 10.0, None, None),
                (20, 37.002, 10.0, None, None),
            ]
        )
        stats = compute_track_stats(track)
        assert stats.moving_seconds == pytest.approx(20.0)
        assert stats.stopped_seconds == pytest.approx(0.0)

    def test_all_stopped_below_threshold(self) -> None:
        track = _track(
            [
                (0, 37.0, 0.1, None, None),
                (10, 37.0001, 0.2, None, None),
                (20, 37.0002, 0.0, None, None),
            ]
        )
        stats = compute_track_stats(track)
        assert stats.moving_seconds == pytest.approx(0.0)
        assert stats.stopped_seconds == pytest.approx(20.0)

    def test_speed_exactly_at_threshold_is_not_stopped(self) -> None:
        track = _track(
            [
                (0, 37.0, STOPPED_SPEED_THRESHOLD_MPS, None, None),
                (10, 37.001, STOPPED_SPEED_THRESHOLD_MPS, None, None),
            ]
        )
        stats = compute_track_stats(track)
        assert stats.moving_seconds == pytest.approx(10.0)
        assert stats.stopped_seconds == pytest.approx(0.0)

    def test_gap_interval_counts_as_neither(self) -> None:
        gap = GPS_GAP_MAX_SECONDS + 30.0
        track = _track(
            [
                (0, 37.0, 10.0, None, None),
                (10, 37.001, 10.0, None, None),
                (10 + gap, 37.002, 10.0, None, None),
            ]
        )
        stats = compute_track_stats(track)
        # Only the first interval (10s, moving) counts; the gapped interval is
        # excluded from both totals.
        assert stats.moving_seconds == pytest.approx(10.0)
        assert stats.stopped_seconds == pytest.approx(0.0)


class TestStopCounting:
    """stop_count: >=20s stretches, excluding ones touching the track's ends."""

    def test_mid_drive_stop_is_counted(self) -> None:
        track = _track(
            [
                (0, 37.0, 10.0, None, None),
                (10, 37.001, 10.0, None, None),
                (20, 37.002, 0.0, None, None),
                (40, 37.002, 0.0, None, None),
                (60, 37.002, 0.0, None, None),
                (70, 37.003, 10.0, None, None),
                (80, 37.004, 10.0, None, None),
            ]
        )
        stats = compute_track_stats(track)
        assert stats.stop_count == 1

    def test_stop_shorter_than_minimum_is_ignored(self) -> None:
        assert STOP_MIN_DURATION_SECONDS == 20.0
        track = _track(
            [
                (0, 37.0, 10.0, None, None),
                (10, 37.001, 10.0, None, None),
                (20, 37.002, 0.0, None, None),
                (25, 37.002, 10.0, None, None),
            ]
        )
        stats = compute_track_stats(track)
        assert stats.stop_count == 0

    def test_stop_at_start_and_end_are_excluded(self) -> None:
        track = _track(
            [
                (0, 37.0, 0.0, None, None),
                (10, 37.0001, 0.0, None, None),  # stopped stretch at the start
                (20, 37.001, 10.0, None, None),
                (30, 37.002, 10.0, None, None),
                (40, 37.003, 10.0, None, None),
                (60, 37.0031, 0.0, None, None),  # stopped stretch at the end
                (80, 37.0032, 0.0, None, None),
            ]
        )
        stats = compute_track_stats(track)
        assert stats.stop_count == 0

    def test_two_mid_drive_stops_both_counted(self) -> None:
        track = _track(
            [
                (0, 37.0, 10.0, None, None),
                (10, 37.001, 10.0, None, None),
                (20, 37.002, 0.0, None, None),
                (45, 37.002, 0.0, None, None),
                (55, 37.003, 10.0, None, None),
                (65, 37.004, 10.0, None, None),
                (75, 37.004, 0.0, None, None),
                (100, 37.004, 0.0, None, None),
                (110, 37.005, 10.0, None, None),
            ]
        )
        stats = compute_track_stats(track)
        assert stats.stop_count == 2


class TestClimbDescentHysteresis:
    """Altitude climb/descent with the 4 m hysteresis threshold."""

    def test_small_noise_on_flat_road_yields_zero(self) -> None:
        assert ALTITUDE_HYSTERESIS_M == 4.0
        noise = [0.0, 2.0, -2.0, 1.0, -1.0, 0.0, 2.0, -2.0]
        rows = [
            (i * 10, 37.0 + i * 0.0005, 10.0, 100.0 + noise[i % len(noise)], None)
            for i in range(24)
        ]
        track = _track(rows)
        stats = compute_track_stats(track)
        assert stats.climb_m == pytest.approx(0.0)
        assert stats.descent_m == pytest.approx(0.0)

    def test_hill_up_then_down_reports_matching_climb_and_descent(self) -> None:
        alts = list(range(0, 51, 2)) + list(range(48, -1, -2))
        rows = [
            (i * 5, 37.0 + i * 0.0005, 10.0, float(a), None) for i, a in enumerate(alts)
        ]
        track = _track(rows)
        stats = compute_track_stats(track)
        assert stats.climb_m == pytest.approx(50.0, abs=0.5)
        assert stats.descent_m == pytest.approx(50.0, abs=0.5)
        assert stats.min_alt_m == pytest.approx(0.0)
        assert stats.max_alt_m == pytest.approx(50.0)

    def test_small_reversal_within_threshold_does_not_confirm(self) -> None:
        # Climbs 10m, dips 3m (below the 4m threshold), climbs again: the dip
        # must not get committed as a separate descent leg.
        rows = [
            (0, 37.0, 10.0, 0.0, None),
            (10, 37.001, 10.0, 10.0, None),
            (20, 37.002, 10.0, 7.0, None),
            (30, 37.003, 10.0, 15.0, None),
        ]
        track = _track(rows)
        stats = compute_track_stats(track)
        assert stats.climb_m == pytest.approx(15.0)
        assert stats.descent_m == pytest.approx(0.0)


class TestRobustMaxSpeed:
    """max_speed_mps: 99th percentile, resistant to a single spike."""

    def test_one_spike_among_many_points_is_mostly_ignored(self) -> None:
        rows = [(i * 5, 37.0 + i * 0.0005, 10.0, None, None) for i in range(150)]
        track = _track(rows)
        # Overwrite the very last point's speed with a spike.
        track.points[-1].speed_mps = 100.0
        stats = compute_track_stats(track)
        assert stats.max_speed_mps is not None
        assert stats.max_speed_mps < 50.0  # nowhere near the 100 m/s spike

    def test_uniform_speed_reports_that_speed(self) -> None:
        rows = [(i * 5, 37.0 + i * 0.0005, 20.0, None, None) for i in range(10)]
        track = _track(rows)
        stats = compute_track_stats(track)
        assert stats.max_speed_mps == pytest.approx(20.0)


class TestHighwayShare:
    """pct_distance_over_70mph: distance-weighted share above the 70 mph threshold."""

    def test_half_the_legs_over_threshold_by_distance(self) -> None:
        assert HIGHWAY_SPEED_THRESHOLD_MPS == pytest.approx(31.29)
        highway_speed = 40.0
        local_speed = 10.0
        rows = [
            (0, 37.0, local_speed, None, None),
            (10, 37.001, local_speed, None, None),
            (20, 37.002, highway_speed, None, None),
            (30, 37.003, highway_speed, None, None),
            (40, 37.004, local_speed, None, None),
        ]
        track = _track(rows)
        stats = compute_track_stats(track)
        assert stats.pct_distance_over_70mph is not None
        assert 0.0 < stats.pct_distance_over_70mph < 100.0

    def test_never_above_threshold_is_zero(self) -> None:
        rows = [(i * 10, 37.0 + i * 0.0005, 10.0, None, None) for i in range(4)]
        track = _track(rows)
        stats = compute_track_stats(track)
        assert stats.pct_distance_over_70mph == pytest.approx(0.0)

    def test_always_above_threshold_is_hundred(self) -> None:
        rows = [(i * 10, 37.0 + i * 0.0005, 40.0, None, None) for i in range(4)]
        track = _track(rows)
        stats = compute_track_stats(track)
        assert stats.pct_distance_over_70mph == pytest.approx(100.0)
