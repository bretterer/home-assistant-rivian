"""Tests for updating charging schedules."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rivian import coordinator
from custom_components.rivian.const import WEEK_DAYS_ORDERED
from homeassistant.exceptions import HomeAssistantError


def _response(payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(json=AsyncMock(return_value=payload))


def _vehicle_coordinator(
    schedules: list[dict[str, Any]],
    set_response: dict[str, Any] | None = None,
) -> coordinator.VehicleCoordinator:
    coor = object.__new__(coordinator.VehicleCoordinator)
    coor.hass = MagicMock()
    coor.hass.config.latitude = 37.8
    coor.hass.config.longitude = -122.4
    coor.vehicle_id = "vehicle-1"
    coor._charging_schedules = None
    coor._last_schedule_fetch = 0.0
    coor.async_update_listeners = MagicMock()
    coor.api = SimpleNamespace(
        get_charging_schedules=AsyncMock(
            return_value=_response(
                {"data": {"getVehicle": {"chargingSchedules": schedules}}}
            )
        ),
        set_charging_schedules=AsyncMock(
            return_value=_response(
                set_response or {"data": {"setChargingSchedules": {"success": True}}}
            )
        ),
    )
    return coor


async def test_update_charging_schedule_sends_only_updated_first_schedule() -> None:
    """When getVehicle returns extra inactive schedules with overlapping days, only send the updated first schedule."""
    daily_schedule = {
        "weekDays": list(WEEK_DAYS_ORDERED),
        "startTime": 780,
        "duration": 120,
        "location": {"latitude": 37.8, "longitude": -122.4},
        "amperage": 8,
        "enabled": True,
    }
    inactive_weekday = {
        "weekDays": list(WEEK_DAYS_ORDERED[:5]),
        "startTime": 0,
        "duration": 900,
        "location": {"latitude": 37.8, "longitude": -122.4},
        "amperage": 10,
        "enabled": False,
    }
    inactive_weekend = {
        "weekDays": list(WEEK_DAYS_ORDERED[5:]),
        "startTime": 0,
        "duration": 900,
        "location": {"latitude": 37.8, "longitude": -122.4},
        "amperage": 10,
        "enabled": False,
    }
    coor = _vehicle_coordinator([daily_schedule, inactive_weekday, inactive_weekend])

    await coor.update_charging_schedule_data({"amperage": 24})

    coor.api.set_charging_schedules.assert_called_once_with(
        "vehicle-1",
        [{**daily_schedule, "amperage": 24}],
    )
    assert coor.charging_schedule["amperage"] == 24
    coor.async_update_listeners.assert_called_once()


async def test_update_charging_schedule_raises_on_api_error() -> None:
    """An unsuccessful setChargingSchedules mutation raises HomeAssistantError."""
    coor = _vehicle_coordinator(
        [],
        set_response={
            "errors": [{"message": "Bad request error"}],
            "data": {"setChargingSchedules": None},
        },
    )

    with pytest.raises(HomeAssistantError):
        await coor.update_charging_schedule_data({"amperage": 16})
