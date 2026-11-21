"""Unit tests for `custom_components.rivian.charging` SoC-derived power curve estimation."""

from __future__ import annotations

import pytest

from custom_components.rivian.charging import (
    dedupe_soc_points,
    estimate_charge_curve,
    should_append_soc_point,
)
from custom_components.rivian.drive_models import DCFC_MIN_POWER_KW


def _linear_points(
    start_soc: float, end_soc: float, duration_s: float, step_s: float
) -> list[tuple[float, float]]:
    """Build (ts, soc) points rising linearly from start_soc to end_soc."""
    n_steps = int(duration_s // step_s)
    return [
        (
            i * step_s,
            start_soc + (end_soc - start_soc) * (i / n_steps),
        )
        for i in range(n_steps + 1)
    ]


class TestDedupeSocPoints:
    """Tests for `dedupe_soc_points`."""

    def test_keeps_first_point(self) -> None:
        assert dedupe_soc_points([(0.0, 50.0)]) == [(0.0, 50.0)]

    def test_drops_point_too_close_in_time_and_value(self) -> None:
        points = [(0.0, 50.0), (5.0, 50.02), (10.0, 50.03)]
        assert dedupe_soc_points(points) == [(0.0, 50.0)]

    def test_keeps_point_after_enough_time_even_if_soc_unchanged(self) -> None:
        points = [(0.0, 50.0), (25.0, 50.0)]
        assert dedupe_soc_points(points) == [(0.0, 50.0), (25.0, 50.0)]

    def test_keeps_point_with_big_soc_jump_even_if_soon(self) -> None:
        points = [(0.0, 50.0), (2.0, 51.0)]
        assert dedupe_soc_points(points) == [(0.0, 50.0), (2.0, 51.0)]

    def test_empty_input(self) -> None:
        assert dedupe_soc_points([]) == []


class TestShouldAppendSocPoint:
    """Tests for `should_append_soc_point`."""

    def test_no_last_point_always_appends(self) -> None:
        assert should_append_soc_point(None, (0.0, 50.0)) is True

    def test_matches_dedupe_soc_points_incrementally(self) -> None:
        raw = [(0.0, 50.0), (5.0, 50.02), (25.0, 50.03), (26.0, 60.0)]
        incremental: list[tuple[float, float]] = []
        for point in raw:
            if should_append_soc_point(incremental[-1] if incremental else None, point):
                incremental.append(point)
        assert incremental == dedupe_soc_points(raw)


class TestEstimateChargeCurve:
    """Tests for `estimate_charge_curve`."""

    def test_empty_or_single_point_yields_nothing(self) -> None:
        assert estimate_charge_curve([], 135.0) == ([], 0.0)
        assert estimate_charge_curve([(0.0, 50.0)], 135.0) == ([], 0.0)

    def test_real_dcfc_rise_estimates_sane_power(self) -> None:
        """45.0% -> 80.9% over 33 minutes should read as a real DCFC-range curve."""
        points = _linear_points(45.0, 80.9, 33 * 60, 30.0)
        samples, max_power = estimate_charge_curve(points, capacity_kwh=135.0)
        assert samples
        assert 60.0 < max_power < 225.0
        assert max_power > DCFC_MIN_POWER_KW
        # Samples are thinned, not one per input point.
        assert len(samples) < len(points)
        assert samples[0].soc <= samples[-1].soc

    def test_l2_like_rise_stays_below_dcfc_floor(self) -> None:
        """A ~7 kW-equivalent rise over an hour should estimate well under the DCFC floor."""
        end_soc = 40.0 + (7.0 / 135.0) * 100.0
        points = _linear_points(40.0, end_soc, 3600.0, 30.0)
        _samples, max_power = estimate_charge_curve(points, capacity_kwh=135.0)
        assert max_power < DCFC_MIN_POWER_KW

    def test_power_capped_at_225kw(self) -> None:
        """An implausibly fast SoC rise is capped, not left to blow past the sanity ceiling."""
        points = [(0.0, 10.0), (30.0, 90.0), (60.0, 95.0)]
        _samples, max_power = estimate_charge_curve(points, capacity_kwh=135.0)
        assert max_power == pytest.approx(225.0)

    def test_flat_soc_yields_no_samples(self) -> None:
        points = [(0.0, 50.0), (30.0, 50.0), (60.0, 50.0), (90.0, 50.0)]
        samples, max_power = estimate_charge_curve(points, capacity_kwh=135.0)
        assert samples == []
        assert max_power == 0.0
