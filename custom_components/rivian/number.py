"""Support for Rivian number entities."""

from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianNumberEntityDescription
from .entity import RivianVehicleControlEntity, RivianChargingScheduleEntity

_LOGGER = logging.getLogger(__name__)


NUMBERS: Final[tuple[RivianNumberEntityDescription, ...]] = (
    RivianNumberEntityDescription(
        key="charge_limit",
        device_class=NumberDeviceClass.BATTERY,
        icon="mdi:battery-charging-70",
        name="Charge Limit",
        native_min_value=50,
        native_unit_of_measurement=PERCENTAGE,
        field="batteryLimit",
        set_fn=lambda coordinator, value: coordinator.send_vehicle_command(
            command=VehicleCommand.CHARGING_LIMITS, params={"SOC_limit": int(value)}
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the number entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianNumberEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for description in NUMBERS
    ]
    
    for vehicle_id, vehicle in vehicles.items():
        coor = coordinators[vehicle_id]
        if hasattr(coor, "charging_schedule_coordinator"):
            entities.append(RivianChargingScheduleAmperage(coor.charging_schedule_coordinator, vehicle))

    async_add_entities(entities)


class RivianNumberEntity(RivianVehicleControlEntity, NumberEntity):
    """Representation of a Rivian number entity."""

    entity_description: RivianNumberEntityDescription

    @property
    def native_value(self) -> float | None:
        """Return the value reported by the number."""
        return self._get_value(self.entity_description.field)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        await self.entity_description.set_fn(self.coordinator, value)

class RivianChargingScheduleAmperage(RivianChargingScheduleEntity, NumberEntity):
    """Representation of a Rivian charging schedule amperage."""

    def __init__(self, coordinator, vehicle):
        super().__init__(coordinator, vehicle)
        self._attr_name = "Charging Schedule Amperage"
        self._attr_unique_id = f"{self._vin}-charging_schedule_amperage"
        self._attr_icon = "mdi:current-ac"
        self._attr_native_min_value = 8
        self._attr_native_max_value = 48
        self._attr_native_step = 2
        self._attr_native_unit_of_measurement = "A"

    @property
    def native_value(self) -> float | None:
        """Return the value of the entity."""
        schedule = self._get_schedule()
        return schedule.get("amperage", 48)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        schedule = self._get_schedule()
        schedule["amperage"] = int(value)
        await self.coordinator.set_charging_schedule([schedule])
