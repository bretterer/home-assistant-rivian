"""Rivian (Unofficial)"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_COORDINATOR,
    CLOSURE_STATE_ENTITIES,
    DOMAIN,
    DOOR_STATE_ENTITIES,
    LOCK_STATE_ENTITIES,
    NAME,
    BINARY_SENSORS,
)

from .data_classes import RivianBinarySensorEntity, RivianBinarySensorEntityDescription

from . import (
    get_device_identifier,
    RivianEntity,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up the sensor entities"""
    coordinator = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]

    entities = []
    for _, value in enumerate(BINARY_SENSORS):
        entities.append(
            RivianBinarySensor(
                coordinator=coordinator,
                config_entry=entry,
                sensor=BINARY_SENSORS[value],
                prop_key=value,
            )
        )

    # custom aggregiate entities
    entities.append(
        RivianAggregiateBinarySensor(
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
        RivianAggregiateBinarySensor(
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
        RivianAggregiateBinarySensor(
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
        RivianAggregiateBinarySensor(
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


class RivianAggregiateBinarySensor(RivianEntity, CoordinatorEntity, BinarySensorEntity):
    """Rivian Aggregiate Binary Sensor Entity - OR the value of different entities and report as new entity"""

    def __init__(
        self,
        coordinator,
        config_entry,
        sensor: RivianBinarySensorEntity,
        prop_key_set: set(str),
    ):
        """"""
        CoordinatorEntity.__init__(self, coordinator)
        BinarySensorEntity.__init__(self)
        super().__init__(coordinator)
        self._sensor = sensor
        self.entity_description = sensor.entity_description
        self.coordinator = coordinator
        self._config_entry = config_entry
        self._name = self.entity_description.key
        self._prop_key_set = prop_key_set
        self.entity_id = f"binary_sensor.{self.entity_description.key}"

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{DOMAIN}_{self._name}_{self._config_entry.entry_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_device_identifier(self._config_entry)},
            "name": NAME,
            "manufacturer": NAME,
        }

    @property
    def is_on(self) -> bool:
        """Return true if sensor is on."""
        for entity_key in self._prop_key_set:
            entity = self.coordinator.data[entity_key]
            if entity is not None:
                values = self.entity_description.on_value
                values = [values] if isinstance(values, str) else values
                result = entity["value"] in values
                return result if not self.entity_description.negate else not result


class RivianBinarySensor(RivianEntity, CoordinatorEntity, BinarySensorEntity):
    """Rivian Binary Sensor Entity."""

    def __init__(
        self,
        coordinator,
        config_entry,
        sensor: RivianBinarySensorEntity,
        prop_key: str,
    ):
        """"""
        CoordinatorEntity.__init__(self, coordinator)
        BinarySensorEntity.__init__(self)
        super().__init__(coordinator)
        self._sensor = sensor
        self.entity_description = sensor.entity_description
        self.coordinator = coordinator
        self._config_entry = config_entry
        self._name = self.entity_description.key
        self._prop_key = prop_key
        self.entity_id = f"binary_sensor.{self.entity_description.key}"

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{DOMAIN}_{self._name}_{self._config_entry.entry_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_device_identifier(self._config_entry)},
            "name": NAME,
            "manufacturer": NAME,
        }

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
    def extra_state_attributes(self):
        """Return the state attributes of the device."""
        try:
            entity = self.coordinator.data[self._prop_key]
            if entity is None:
                return "Binary Sensor Unavailable"
            return {
                "value": entity["value"],
                "last_update": entity["timeStamp"],
            }
        except KeyError:
            return None
