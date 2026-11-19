"""Diagnostics support for Rivian."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import RivianConfigEntry
from .helpers import redact


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RivianConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    vehicle_coordinators = entry.runtime_data.vehicle_coordinators
    data = {
        "user": entry.runtime_data.user_coordinator.data,
        "vehicle": [coor.data for coor in vehicle_coordinators.values()],
        "charging": [
            coor.charging_coordinator.data for coor in vehicle_coordinators.values()
        ],
        "drivers": [
            coor.drivers_coordinator.data for coor in vehicle_coordinators.values()
        ],
        "wallbox": entry.runtime_data.wallbox_coordinator.data,
    }
    return redact(data)
