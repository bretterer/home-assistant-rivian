"""Rivian (Unofficial)"""
from __future__ import annotations

import logging
from typing import Any
import random

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import slugify
from homeassistant.const import LENGTH_MILES
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback


from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_COORDINATOR,
    DOMAIN,
    NAME,
)

from . import (
    get_entity_unique_id,
    get_device_identifier,
    RivianEntity,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up the sensor entities"""
    coordinator = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]

    _LOGGER.info("==== coordinator data ====")
    coordData = await coordinator.data.json()
    _LOGGER.info(coordData["data"]["dynamics/odometer/value"])
    entities = []
    for key, value in enumerate(coordData["data"]):

        entities.append(
            RivianSensor(coordinator, entry, value, coordData["data"][value])
        )

    async_add_entities(entities)


class RivianSensor(RivianEntity, CoordinatorEntity, SensorEntity):
    """Representation of a Sensor."""

    # _attr_name = "Odometer (Miles)"
    # _attr_native_unit_of_measurement = LENGTH_MILES
    # _attr_state_class = SensorStateClass.MEASUREMENT
    # _attr_unique_id = "rivian:odometer"

    def __init__(self, coordinator, config_entry, property_id, data):
        """"""
        RivianEntity.__init__(self, config_entry)
        CoordinatorEntity.__init__(self, coordinator)
        self._coordinator = coordinator
        self._data = data
        self._name = slugify(property_id)
        self._property_id = property_id
        self._config_entry = config_entry

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_entity_unique_id(self._config_entry.entry_id, self._name)

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_device_identifier(self._config_entry)},
            "name": NAME,
            "model": self._get_model(),
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return slugify(self._name)

    @property
    def native_value(self) -> str:
        return self._get_native_value()

    @property
    def unit_of_measurement(self) -> str:
        """Return the unit of measurement of the sensor."""
        return LENGTH_MILES

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return "mdi:speedometer"

    def _get_native_value(self) -> str:
        if self._property_id == "dynamics/odometer/value":
            return round(self._data[1] / 1609.344, 2)
        elif self._property_id == "body/closures/door_FL_locked_state":
            return self._data[1]
            # return random.randrange(1, 100000)
        else:
            return self._data[1]
