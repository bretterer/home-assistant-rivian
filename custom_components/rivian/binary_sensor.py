"""Rivian"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, BINARY_SENSORS, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianBinarySensorEntityDescription
from .entity import RivianVehicleEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities"""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, Any] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianBinarySensorEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        for model, descriptions in BINARY_SENSORS.items()
        if model in vehicle["model"]
        for description in descriptions
        if description.required_feature in (vehicle.get("supported_features") + [None])
    ]

    async_add_entities(entities)


class RivianBinarySensorEntity(RivianVehicleEntity, BinarySensorEntity):
    """Rivian Binary Sensor Entity."""

    entity_description: RivianBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: VehicleCoordinator,
        config_entry: ConfigEntry,
        description: RivianBinarySensorEntityDescription,
        vehicle: dict[str, Any],
    ) -> None:
        """Create a Rivian binary sensor."""
        super().__init__(coordinator, config_entry, description, vehicle)
        self._aggregate = isinstance(self.entity_description.field, set)

    @property
    def available(self) -> bool:
        """Return the availability of the entity."""
        if self._aggregate:
            return self._available and any(
                self._get_value(entity_key)
                for entity_key in self.entity_description.field
            )
        return super().available

    @property
    def is_on(self) -> bool:
        """Return true if sensor is on."""
        fields = self.entity_description.field
        if self._aggregate:
            return self.entity_description.on_value in (
                self._get_value(entity_key) for entity_key in fields
            )
        if (val := self._get_value(fields)) is not None:
            values = self.entity_description.on_value
            values = [values] if isinstance(values, str) else values
            result = (val.lower() if isinstance(val, str) else val) in values
            return result if not self.entity_description.negate else not result
        return STATE_UNAVAILABLE
