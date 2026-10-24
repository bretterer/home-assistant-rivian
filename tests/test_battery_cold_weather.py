"""Tests for the battery and cold weather sensors."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import binary_sensor, const, coordinator

DESCRIPTIONS = {d.key: d for ds in const.SENSORS.values() for d in ds}
BINARY_DESCRIPTIONS = {d.key: d for ds in const.BINARY_SENSORS.values() for d in ds}


@pytest.fixture
def coor() -> coordinator.VehicleCoordinator:
    """A vehicle coordinator whose data updates are kept."""
    coor = coordinator.VehicleCoordinator(
        hass=MagicMock(), config_entry=MagicMock(), client=MagicMock(), vehicle_id="V"
    )
    coor.data = {}

    def set_data(data: dict[str, Any]) -> None:
        coor.data = data

    coor.async_set_updated_data = set_data
    coor.charging_coordinator = MagicMock()
    return coor


def parallax(
    coor: coordinator.VehicleCoordinator, rvm: str, decoded: dict[str, Any]
) -> None:
    """Deliver a Parallax message that decodes to `decoded`."""
    message = {
        "payload": {
            "data": {"parallaxMessages": {"rvm": rvm, "payload": "", "timestamp": 0}}
        }
    }
    decoded = {"timestamp": datetime(2026, 10, 12, tzinfo=UTC)} | decoded
    with patch.object(coordinator, "decode_parallax_message", return_value=decoded):
        coor._process_parallax_data(message)


def test_battery_cell_type(coor: coordinator.VehicleCoordinator) -> None:
    """The cell type is one of the sensor's options."""
    parallax(
        coor,
        "energy.high_voltage.battery_characteristics",
        {"batteryCellType": "lg_4695"},
    )

    assert coor.get("batteryCellType") == "lg_4695"
    assert "lg_4695" in DESCRIPTIONS["battery_cell_type"].options


@pytest.mark.parametrize(("value", "is_on"), [("true", True), ("false", False)])
def test_battery_needs_lfp_calibration(
    coor: coordinator.VehicleCoordinator, value: str, is_on: bool
) -> None:
    """The binary sensor is on when the battery needs calibrating."""
    parallax(
        coor,
        "energy.high_voltage.battery_state",
        {"batteryNeedsLfpCalibration": value},
    )

    entity = binary_sensor.RivianBinarySensorEntity(
        coor,
        MagicMock(),
        BINARY_DESCRIPTIONS["battery_needs_lfp_calibration"],
        {"id": "V", "vin": "VIN", "name": "R1T", "model": "R1T"},
    )
    assert entity.is_on is is_on


def test_cold_weather_soc(coor: coordinator.VehicleCoordinator) -> None:
    """The cold weather graph's values reach the sensors' fields."""
    parallax(
        coor,
        "energy_edge_compute.graphs.cold_weather_soc",
        {"coldWeatherSoc": 46, "coldWeatherSocBlue": 0, "coldRangeImpact": 30},
    )

    assert coor.get(DESCRIPTIONS["cold_weather_soc_green"].field) == 46
    assert coor.get(DESCRIPTIONS["cold_weather_soc_blue"].field) == 0
    assert coor.get(DESCRIPTIONS["cold_range_impact"].field) == 30
