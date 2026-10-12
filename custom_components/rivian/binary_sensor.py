"""Rivian (Unofficial)"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, BINARY_SENSORS, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianBinarySensorEntityDescription
from .entity import RivianVehicleEntity, RivianVehicleFreshnessEntity
from .helpers import async_remove_stale_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the binary sensor entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, Any] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianBinarySensorEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        for models, descriptions in BINARY_SENSORS.items()
        if any(model in vehicle["model"] for model in models)
        for description in descriptions
        if description.supported_feature is None
        or description.supported_feature in vehicle.get("supported_features", [])
    ]

    async_remove_stale_entities(
        hass,
        BINARY_SENSOR_DOMAIN,
        (
            f"{vehicle['vin']}-{description.key}"
            for vehicle in vehicles.values()
            for descriptions in BINARY_SENSORS.values()
            for description in descriptions
            # Keep feature-gated entities if the features didn't load
            if description.supported_feature is None
            or vehicle.get("supported_features")
        ),
        (entity.unique_id for entity in entities),
    )
    entities.extend(
        RivianVehicleUpdatesEntity(
            coordinators[vehicle_id], entry, VEHICLE_UPDATES_DESCRIPTION, vehicle
        )
        for vehicle_id, vehicle in vehicles.items()
    )
    async_add_entities(entities)


VEHICLE_UPDATES_DESCRIPTION = BinarySensorEntityDescription(
    key="vehicle_updates",
    translation_key="vehicle_updates",
    device_class=BinarySensorDeviceClass.PROBLEM,
    entity_category=EntityCategory.DIAGNOSTIC,
)


class RivianVehicleUpdatesEntity(RivianVehicleFreshnessEntity, BinarySensorEntity):
    """Problem sensor for when updates from Rivian have stopped arriving."""

    @property
    def is_on(self) -> bool:
        """Return true if updates have stopped arriving."""
        return self.coordinator.updates_stale


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
    def is_on(self) -> bool | None:
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
        return None
