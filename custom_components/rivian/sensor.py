"""Rivian (Unofficial)"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import LENGTH_MILES
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)

from .const import (
    ATTR_COORDINATOR,
    DOMAIN,
)

from . import RivianEntity, RivianDataUpdateCoordinator


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
    # for key, value in coordinator.data.items():
    entities.append(
        RivianSensor(coordinator, coordData["data"]["dynamics/odometer/value"])
    )

    async_add_entities(entities)


class RivianSensor(SensorEntity):
    """Representation of a Sensor."""

    _attr_name = "Odometer (Miles)"
    _attr_native_unit_of_measurement = LENGTH_MILES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_unique_id = "rivian:odometer"

    def __init__(self, coordinator, data):
        """"""
        self._coordinator = coordinator
        self._data = data

    @property
    def native_value(self):
        return round(self._data[1] / 1609.344, 2)
