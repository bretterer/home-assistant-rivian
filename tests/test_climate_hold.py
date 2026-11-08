"""Tests for the climate hold switch and duration."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.rivian import number, switch

DURATION = next(d for d in number.NUMBERS if d.key == "climate_hold_duration")
SWITCH = next(d for d in switch.SWITCHES if d.key == "climate_hold")


def test_duration_in_minutes() -> None:
    """The vehicle's duration in seconds is shown in minutes, unset as unknown."""
    assert DURATION.value_lambda(28800) == 480
    assert DURATION.value_lambda(0) is None
    assert DURATION.supported_feature == SWITCH.supported_feature == "CLM_HOLD"


def test_switch_follows_status() -> None:
    """The switch is on only while a hold is running."""
    coor = MagicMock()
    for status, on in (("on", True), ("off", False), ("unavailable", False)):
        coor.get.side_effect = {"climateHoldStatus": status}.get
        assert SWITCH.is_on(coor) is on
