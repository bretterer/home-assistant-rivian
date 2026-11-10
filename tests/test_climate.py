"""Tests for the cabin climate entity."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.rivian import climate


def entity(values: dict[str, object]) -> climate.RivianClimateEntity:
    """A climate entity reading the given vehicle values."""
    climate_entity = object.__new__(climate.RivianClimateEntity)
    climate_entity._get_value = MagicMock(side_effect=values.get)  # type: ignore[method-assign]
    return climate_entity


def test_native_temperatures() -> None:
    """Temperatures are the vehicle's, in °C."""
    cabin = entity(
        {"cabinClimateInteriorTemperature": 18.5, "hvacTargetTemperature": 21}
    )
    assert cabin.native_current_temperature == 18.5
    assert cabin.native_target_temperature == 21
    # The test harness's ClimateEntity stands in for Home Assistant before 2026.11
    assert (cabin.current_temperature, cabin.target_temperature) == (18.5, 21)


def test_lo_hi_presets_read_celsius() -> None:
    """LO and HI are the vehicle's 0 °C and 63.5 °C set points."""
    for target, preset in ((0, "LO"), (63.5, "HI"), (21, None)):
        cabin = entity({"defrostDefogStatus": "off", "hvacTargetTemperature": target})
        assert cabin.preset_mode == preset
