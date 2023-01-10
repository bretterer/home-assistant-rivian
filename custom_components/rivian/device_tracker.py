"""Rivian (Unofficial) Tracker"""
from __future__ import annotations

import logging

from homeassistant.components.device_tracker import SOURCE_TYPE_GPS
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityDescription

from .const import (
    ATTR_COORDINATOR,
    DOMAIN,
    NAME,
)

from . import (
    get_device_identifier,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up the Rivain binary_sensors by config_entry."""

    # coordinator = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]

    # entities = []
    # value = "gnssLocation"
    # entities.append(RivianDeviceEntity(coordinator, entry, value))

    # async_add_entities(entities, True)


class RivianDeviceEntity(CoordinatorEntity, TrackerEntity):
    """A class representing a Rivian device."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry: ConfigEntry,
        attribute,
    ):
        """"""
        super().__init__(coordinator)
        self._attribute = attribute
        self._tracker_data = coordinator.data[self._attribute]
        self._config_entry = config_entry
        self.entity_id = f"device_tracker.{DOMAIN}_telematics_gnss_position"
        self._attr_name = "Rivian Location"
        self.entity_description = EntityDescription(
            name="Rivian", key=f"{DOMAIN}_telematics_gnss_position"
        )

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{DOMAIN}_{self._attr_name}_{self._config_entry.entry_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_device_identifier(self._config_entry)},
            "name": NAME,
            "manufacturer": NAME,
        }

    @property
    def force_update(self):
        """Disable forced updated since we are polling via the coordinator updates."""
        return False

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        return self._tracker_data[1]["lat"]

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        return self._tracker_data[1]["lon"]

    @property
    def source_type(self):
        """Return the source type, eg gps or router, of the device."""
        return SOURCE_TYPE_GPS

    # @property
    # def location_accuracy(self) -> int:
    #     return self._tracker_data[6]

    # @property
    # def extra_state_attributes(self):
    #     """Return the state attributes of the device."""
    #     return {
    #         "trackr_id": self.unique_id,
    #         "altitude": self._tracker_data[3],
    #         "heading": self._tracker_data[4],
    #         "speed": self._tracker_data[5],
    #         "last_update": self._tracker_data[0],
    #     }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Respond to a DataUpdateCoordinator update."""
        self._tracker_data = self.coordinator.data[self._attribute]
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
