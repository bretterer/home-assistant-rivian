"""Tests for the R2's liftgate (rear) window entities."""

from __future__ import annotations

import pytest

from custom_components.rivian import const

R2_ONLY = {"window_rear_closed", "window_rear_calibrated"}


def keys_for(model: str) -> set[str]:
    """Entity keys created for a model, matched as setup does."""
    return {
        description.key
        for table in (const.SENSORS, const.BINARY_SENSORS)
        for models, descriptions in table.items()
        if any(m in model for m in models)
        for description in descriptions
    }


def test_r2_gets_the_liftgate_window() -> None:
    assert keys_for("R2") >= R2_ONLY


@pytest.mark.parametrize("model", ["R1T", "R1S"])
def test_r1_does_not(model: str) -> None:
    assert keys_for(model).isdisjoint(R2_ONLY)


def test_calibration_defaults_to_calibrated() -> None:
    """Like the other windows, assumed calibrated until reported otherwise."""
    assert const.DEFAULT_VEHICLE_STATE["windowRearCalibrated"] == "calibrated"
