"""Rivian (Unofficial)"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import (
    DOMAIN as PLATFORM,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, BINARY_SENSORS, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianBinarySensorEntityDescription
from .entity import RivianVehicleEntity, async_update_unique_id


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities"""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, Any] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianBinarySensorEntity(coordinators[vin], entry, description, vehicle)
        for vin, vehicle in vehicles.items()
        for model, descriptions in BINARY_SENSORS.items()
        if model in vehicle["model"]
        for description in descriptions
    ]

    # Migrate unique ids to support multiple VIN
    async_update_unique_id(hass, PLATFORM, entities)

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
            result = val in values
            return result if not self.entity_description.negate else not result
        return STATE_UNAVAILABLE

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the state attributes of the device."""
        if self._aggregate:
            return None
        try:
            entity = self.coordinator.data[self.entity_description.field]
            if entity is None:
                return "Binary Sensor Unavailable"
            return {
                "value": entity["value"],
                "last_update": entity["timeStamp"],
                "history": str(entity["history"]),
            }
        except KeyError:
            return None
