"""Support for Rivian number entities."""
from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianNumberEntityDescription
from .entity import RivianVehicleControlEntity

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
    RivianNumberEntityDescription(
        key="cabin_preconditioning_temperature",
        device_class=NumberDeviceClass.TEMPERATURE,
        mode=NumberMode.BOX,
        name="Cabin Preconditioning Temperature",
        native_max_value=29,
        native_min_value=16,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        field="cabinClimateDriverTemperature",
        set_fn=lambda coordinator, value: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_PRECONDITIONING_SET_TEMP,
            params={"HVAC_set_temp": value},
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities"""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianNumberEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for description in NUMBERS
    ]
    async_add_entities(entities)


class RivianNumberEntity(RivianVehicleControlEntity, NumberEntity):
    """Representation of a Rivian sensor entity."""

    entity_description: RivianNumberEntityDescription

    @property
    def native_value(self) -> str | None:
        """Return the value reported by the number."""
        return self._get_value(self.entity_description.field)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        await self.entity_description.set_fn(self.coordinator, value)
