"""Support for Rivian select entities."""
from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianSelectEntityDescription
from .entity import RivianVehicleControlEntity

_LOGGER = logging.getLogger(__name__)

LEVEL_MAP = {"Off": "0", "On": "1", "Level_1": "2", "Level_2": "3", "Level_3": "4"}
OFF_ON = ["Off", "On"]
LEVELS = ["Off", "Level_1", "Level_2", "Level_3"]


SELECTS: Final[tuple[RivianSelectEntityDescription, ...]] = (
    RivianSelectEntityDescription(
        key="seat_front_left_heat",
        icon="mdi:car-seat-heater",
        name="Seat Front Left Heat",
        options=LEVELS,
        field="seatFrontLeftHeat",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_LEFT_SEAT_HEAT,
            params={"level": int(option)},
        ),
    ),
    RivianSelectEntityDescription(
        key="seat_front_left_vent",
        icon="mdi:car-seat-cooler",
        name="Seat Front Left Vent",
        options=LEVELS,
        field="seatFrontLeftVent",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_LEFT_SEAT_VENT,
            params={"level": int(option)},
        ),
    ),
    RivianSelectEntityDescription(
        key="seat_front_right_heat",
        icon="mdi:car-seat-heater",
        name="Seat Front Right Heat",
        options=LEVELS,
        field="seatFrontRightHeat",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_RIGHT_SEAT_HEAT,
            params={"level": int(option)},
        ),
    ),
    RivianSelectEntityDescription(
        key="seat_front_right_vent",
        icon="mdi:car-seat-cooler",
        name="Seat Front Right Vent",
        options=LEVELS,
        field="seatFrontRightVent",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_RIGHT_SEAT_VENT,
            params={"level": int(option)},
        ),
    ),
    RivianSelectEntityDescription(
        key="seat_rear_left_heat",
        icon="mdi:car-seat-heater",
        name="Seat Rear Left Heat",
        options=LEVELS,
        field="seatRearLeftHeat",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_REAR_LEFT_SEAT_HEAT,
            params={"level": int(option)},
        ),
    ),
    RivianSelectEntityDescription(
        key="seat_rear_right_heat",
        icon="mdi:car-seat-heater",
        name="Seat Rear Right Heat",
        options=LEVELS,
        field="seatRearRightHeat",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_REAR_RIGHT_SEAT_HEAT,
            params={"level": int(option)},
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the select entities"""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianSelectEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for description in SELECTS
    ]
    async_add_entities(entities)


class RivianSelectEntity(RivianVehicleControlEntity, SelectEntity):
    """Representation of a Rivian select entity."""

    entity_description: RivianSelectEntityDescription

    @property
    def current_option(self) -> str | None:
        """Return the selected entity option to represent the entity state."""
        return self._get_value(self.entity_description.field)

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        await self.entity_description.select(self.coordinator, LEVEL_MAP[option])
