"""Tests for the cabin climate entity."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.rivian import climate


def entity(values: dict[str, object]) -> climate.RivianClimateEntity:
    """A climate entity reading the given vehicle values."""
    climate_entity = object.__new__(climate.RivianClimateEntity)
    climate_entity._get_value = MagicMock(side_effect=values.get)  # type: ignore[method-assign]
    return climate_entity


def test_native_unit_is_celsius() -> None:
    """The native unit is °C; Home Assistant converts it for display."""
    assert (
        climate.RivianClimateEntity._attr_native_temperature_unit
        == climate.UnitOfTemperature.CELSIUS
    )


def test_lo_hi_presets_read_celsius() -> None:
    """LO and HI are the vehicle's 0 °C and 63.5 °C set points."""
    for target, preset in ((0, "LO"), (63.5, "HI"), (21, None)):
        cabin = entity({"defrostDefogStatus": "off", "hvacTargetTemperature": target})
        assert cabin.preset_mode == preset
