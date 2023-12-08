"""Diagnostics support for Rivian."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import ATTR_COORDINATOR, ATTR_USER, ATTR_VEHICLE, ATTR_WALLBOX, DOMAIN
from .coordinator import UserCoordinator, VehicleCoordinator, WallboxCoordinator
from .helpers import redact


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinators = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]
    user_coordinator: UserCoordinator = coordinators[ATTR_USER]
    vehicle_coordinators: dict[str, VehicleCoordinator] = coordinators[ATTR_VEHICLE]
    wallbox_coordinator: WallboxCoordinator = coordinators[ATTR_WALLBOX]
    data = {
        "user": user_coordinator.data,
        "vehicle": [coor.data for coor in vehicle_coordinators.values()],
        "charging": [
            coor.charging_coordinator.data for coor in vehicle_coordinators.values()
        ],
        "drivers": [
            coor.drivers_coordinator.data for coor in vehicle_coordinators.values()
        ],
        "wallbox": wallbox_coordinator.data,
    }
    return redact(data)
