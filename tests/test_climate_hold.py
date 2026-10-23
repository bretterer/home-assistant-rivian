"""Tests for the climate hold duration number."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import coordinator, number
from homeassistant.exceptions import HomeAssistantError

RVM = "comfort.cabin.climate_hold_setting"
VEHICLE = {"id": "vehicle-1", "vin": "VIN1", "name": "Truck", "model": "R1T"}


@pytest.fixture
def coor() -> coordinator.VehicleCoordinator:
    """A vehicle coordinator whose data updates are kept."""
    coor = coordinator.VehicleCoordinator(
        hass=MagicMock(),
        config_entry=MagicMock(),
        client=MagicMock(),
        vehicle_id="vehicle-1",
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


def make_number(
    coor: coordinator.VehicleCoordinator,
) -> number.RivianClimateHoldDurationEntity:
    return number.RivianClimateHoldDurationEntity(
        coor, MagicMock(), number.CLIMATE_HOLD_DURATION_NUMBER, VEHICLE
    )


def test_duration_in_minutes(coor: coordinator.VehicleCoordinator) -> None:
    """The vehicle reports seconds; the number shows minutes."""
    parallax(coor, RVM, {"climateHoldDuration": 7200})

    entity = make_number(coor)
    assert entity.native_value == 120
    assert entity.available


def test_no_duration(coor: coordinator.VehicleCoordinator) -> None:
    """No duration set, or none reported yet, has no value."""
    entity = make_number(coor)
    assert entity.native_value is None
    assert entity.available

    parallax(coor, RVM, {"climateHoldDuration": 0})
    assert coor.get("climateHoldDuration") == 0
    assert entity.native_value is None


async def test_set_duration(coor: coordinator.VehicleCoordinator) -> None:
    """Setting the number sends the duration in minutes."""
    coor.api.set_climate_hold_duration = AsyncMock(return_value=True)

    await make_number(coor).async_set_native_value(195.0)

    coor.api.set_climate_hold_duration.assert_awaited_once_with("vehicle-1", 195)


async def test_set_duration_fails(coor: coordinator.VehicleCoordinator) -> None:
    """A request that isn't accepted raises."""
    coor.api.set_climate_hold_duration = AsyncMock(return_value=False)

    with pytest.raises(HomeAssistantError):
        await make_number(coor).async_set_native_value(240.0)
