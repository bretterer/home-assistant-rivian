"""Rivian (Unofficial)"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RivianDataUpdateCoordinator, RivianEntity
from .const import (
    ATTR_COORDINATOR,
    BINARY_SENSORS,
    CLOSURE_STATE_ENTITIES,
    CONF_VIN,
    DOMAIN,
    DOOR_STATE_ENTITIES,
    LOCK_STATE_ENTITIES,
)
from .data_classes import RivianBinarySensorEntity, RivianBinarySensorEntityDescription
from .helpers import get_model_and_year


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities"""
    coordinator = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]
    vin_model = get_model_and_year(entry.data[CONF_VIN])[0]

    entities = [
        RivianBinarySensor(
            coordinator=coordinator, config_entry=entry, sensor=sensor, prop_key=value
        )
        for model in (vin_model, vin_model[:2])
        for value, sensor in BINARY_SENSORS.get(model, {}).items()
    ]

    # custom aggregate entities
    entities.append(
        RivianAggregateBinarySensor(
            coordinator=coordinator,
            config_entry=entry,
            sensor=RivianBinarySensorEntity(
                entity_description=RivianBinarySensorEntityDescription(
                    name="Rivian Locked State",
                    key=f"{DOMAIN}_locked_state",
                    device_class=BinarySensorDeviceClass.LOCK,
                    on_value="unlocked",
                )
            ),
            prop_key_set=LOCK_STATE_ENTITIES,
        )
    )
    entities.append(
        RivianAggregateBinarySensor(
            coordinator=coordinator,
            config_entry=entry,
            sensor=RivianBinarySensorEntity(
                entity_description=RivianBinarySensorEntityDescription(
                    name="Rivian Door State",
                    key=f"{DOMAIN}_door_state",
                    device_class=BinarySensorDeviceClass.DOOR,
                    on_value="open",
                )
            ),
            prop_key_set=DOOR_STATE_ENTITIES,
        )
    )
    entities.append(
        RivianAggregateBinarySensor(
            coordinator=coordinator,
            config_entry=entry,
            sensor=RivianBinarySensorEntity(
                entity_description=RivianBinarySensorEntityDescription(
                    name="Rivian Closure State",
                    key=f"{DOMAIN}_closure_state",
                    device_class=BinarySensorDeviceClass.DOOR,
                    on_value="open",
                )
            ),
            prop_key_set=CLOSURE_STATE_ENTITIES,
        )
    )
    entities.append(
        RivianAggregateBinarySensor(
            coordinator=coordinator,
            config_entry=entry,
            sensor=RivianBinarySensorEntity(
                entity_description=RivianBinarySensorEntityDescription(
                    name="Rivian In Use State",
                    key=f"{DOMAIN}_use_state",
                    device_class=BinarySensorDeviceClass.MOVING,
                    on_value="go",
                )
            ),
            prop_key_set={"powerState"},
        )
    )

    async_add_entities(entities, True)


class RivianAggregateBinarySensor(RivianEntity, BinarySensorEntity):
    """Rivian Aggregate Binary Sensor Entity - OR the value of different entities and report as new entity"""

    def __init__(
        self,
        coordinator: RivianDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor: RivianBinarySensorEntity,
        prop_key_set: set(str),
    ) -> None:
        """Create a Rivian aggregate binary sensor."""
        super().__init__(coordinator, config_entry)
        self._sensor = sensor
        self.entity_description = sensor.entity_description
        self._name = self.entity_description.key
        self._prop_key_set = prop_key_set
        self.entity_id = f"binary_sensor.{self.entity_description.key}"
        self._vin = config_entry.data.get(CONF_VIN)

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{DOMAIN}_{self._name}_{self._config_entry.entry_id}"

    @property
    def is_on(self) -> bool:
        """Return true if sensor is on."""
        return self.entity_description.on_value in (
            self.coordinator.data.get(entity_key, {}).get("value")
            for entity_key in self._prop_key_set
        )


class RivianBinarySensor(RivianEntity, BinarySensorEntity):
    """Rivian Binary Sensor Entity."""

    def __init__(
        self,
        coordinator: RivianDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor: RivianBinarySensorEntity,
        prop_key: str,
    ) -> None:
        """Create a Rivian binary sensor."""
        super().__init__(coordinator, config_entry)
        self._sensor = sensor
        self.entity_description = sensor.entity_description
        self._name = self.entity_description.key
        self._prop_key = prop_key
        self.entity_id = f"binary_sensor.{self.entity_description.key}"
        self._vin = config_entry.data.get(CONF_VIN)

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{DOMAIN}_{self._name}_{self._config_entry.entry_id}"

    @property
    def is_on(self) -> bool:
        """Return true if sensor is on."""
        try:
            entity = self.coordinator.data[self._prop_key]
            if entity is None:
                return "Binary Sensor Unavailable"
            values = self.entity_description.on_value
            values = [values] if isinstance(values, str) else values
            result = entity["value"] in values
            return result if not self.entity_description.negate else not result
        except KeyError:
            return None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the state attributes of the device."""
        try:
            entity = self.coordinator.data[self._prop_key]
            if entity is None:
                return "Binary Sensor Unavailable"
            return {
                "value": entity["value"],
                "last_update": entity["timeStamp"],
                "history": str(entity["history"]),
            }
        except KeyError:
            return None
