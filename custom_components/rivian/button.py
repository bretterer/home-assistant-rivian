"""Support for Rivian button entities."""
from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianButtonEntityDescription
from .entity import RivianVehicleControlEntity

_LOGGER = logging.getLogger(__name__)


BUTTONS: Final[dict[str | None, tuple[RivianButtonEntityDescription, ...]]] = {
    None: (
        RivianButtonEntityDescription(
            key="wake",
            icon="mdi:weather-night",
            name="Wake",
            available=lambda coordinator: coordinator.get("powerState") == "sleep",
            press_fn=lambda coordinator: coordinator.send_vehicle_command(
                command=VehicleCommand.WAKE_VEHICLE
            ),
        ),
    ),
    "SIDE_BIN_NXT_ACT": (
        RivianButtonEntityDescription(
            key="open_gear_tunnel_left",
            name="Open Gear Tunnel Left",
            press_fn=lambda coordinator: coordinator.send_vehicle_command(
                command=VehicleCommand.RELEASE_LEFT_SIDE_BIN
            ),
        ),
        RivianButtonEntityDescription(
            key="open_gear_tunnel_right",
            name="Open Gear Tunnel Right",
            press_fn=lambda coordinator: coordinator.send_vehicle_command(
                command=VehicleCommand.RELEASE_RIGHT_SIDE_BIN
            ),
        ),
    ),
    "TAILGATE_CMD": (
        RivianButtonEntityDescription(
            key="drop_tailgate",
            name="Drop Tailgate",
            available=lambda coordinator: coordinator.get("closureTailgateClosed")
            != "open",
            press_fn=lambda coordinator: coordinator.send_vehicle_command(
                command=VehicleCommand.OPEN_LIFTGATE_UNLATCH_TAILGATE
            ),
        ),
    ),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the button entities"""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianButtonEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for feature, descriptions in BUTTONS.items()
        if feature is None or feature in (vehicle.get("supported_features", []))
        for description in descriptions
    ]
    async_add_entities(entities)


class RivianButtonEntity(RivianVehicleControlEntity, ButtonEntity):
    """Representation of a Rivian button entity."""

    entity_description: RivianButtonEntityDescription

    async def async_press(self) -> None:
        """Press the button."""
        await self.entity_description.press_fn(self.coordinator)
