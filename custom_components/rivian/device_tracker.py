"""Rivian (Unofficial) Tracker"""
from __future__ import annotations

import logging

from homeassistant.components.device_tracker import SOURCE_TYPE_GPS
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityDescription

from .const import (
    ATTR_COORDINATOR,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up the Rivain binary_sensors by config_entry."""

    coordinator = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]

    coord_data = coordinator.data

    entities = []
    for _, value in enumerate(coord_data):
        if value == "$gnss":
            entities.append(RivianDeviceEntity(coordinator, entry, value))

    async_add_entities(entities)


class RivianDeviceEntity(TrackerEntity):
    """A class representing a Rivian device."""

    def __init__(
        self,
        coordinator,
        config_entry,
        attribute,
    ):
        """"""
        self._attribute = attribute
        self._tracker_data = coordinator.data[self._attribute]
        self._config_entry = config_entry
        self.entity_id = f"device_tracker.{DOMAIN}_gnss"
        self._attr_name = "Rivian Location"
        self.entity_description = EntityDescription(name="Rivian", key=f"{DOMAIN}_gnss")

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{DOMAIN}_{self._attr_name}_{self._config_entry.entry_id}"

    @property
    def force_update(self):
        """Disable forced updated since we are polling via the coordinator updates."""
        return False

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        return self._tracker_data[1]

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        return self._tracker_data[2]

    @property
    def source_type(self):
        """Return the source type, eg gps or router, of the device."""
        return SOURCE_TYPE_GPS

    @property
    def location_accuracy(self) -> int:
        return self._tracker_data[6]

    @property
    def extra_state_attributes(self):
        """Return the state attributes of the device."""
        return {
            "trackr_id": self.unique_id,
            "heading": self._tracker_data[4],
            "speed": self._tracker_data[5],
        }


class RivianDevice(CoordinatorEntity):
    """Representation of a Device"""

    def __init__(self, gnss, coordinator):
        """Initialize the Rivian device."""
        super().__init__(coordinator)
        self.tracker = gnss
        self._name: str = "Tracker"
        self._unique_id: str = "rivian_gnss"

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._unique_id

    @property
    def icon(self):
        """Return the icon of the sensor."""
        return "mdi:crosshairs-gps"
