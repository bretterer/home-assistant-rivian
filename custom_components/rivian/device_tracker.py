"""Rivian (Unofficial) Tracker"""
from __future__ import annotations

import logging

from homeassistant.components.device_tracker import SOURCE_TYPE_GPS
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_MODEL, ATTR_NAME
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
    CONF_VIN,
    DOMAIN,
    NAME,
)

from . import (
    get_device_identifier,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the Rivain binary_sensors by config_entry."""

    coordinator = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]

    entities = []
    value = "gnssLocation"
    entities.append(RivianDeviceEntity(coordinator, entry, value))

    async_add_entities(entities, True)


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
        self.entity_description = EntityDescription(name="Rivian", key=f"{DOMAIN}_telematics_gnss_position")
        self._vin = config_entry.data.get(CONF_VIN)

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{DOMAIN}_{self._attr_name}_{self._config_entry.entry_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        info = DeviceInfo(
            identifiers={get_device_identifier(self._config_entry)},
            manufacturer=NAME,
        )
        model_year = ""
        model_line = ""

        """Attempt to derive Rivian model information from VIN."""
        if self._vin[9] == "N":
            model_year = "2022"
        elif self._vin[9] == "P":
            model_year = "2023"

        if self._vin[0:5] == "7FCTG":
            model_line = "R1T"
        elif self._vin[0:5] == "7PDSG":
            model_line = "R1S"

        if model_year and model_line:
            info[ATTR_MODEL] = f"{model_year} Rivian {model_line}"
            info[ATTR_NAME] = f"Rivian {model_line}"
        else:
            info[ATTR_NAME] = "Rivian"

        return info

    @property
    def force_update(self):
        """Disable forced updated since we are polling via the coordinator updates."""
        return False

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        return self._tracker_data["latitude"]

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        return self._tracker_data["longitude"]

    @property
    def source_type(self):
        """Return the source type, eg gps or router, of the device."""
        return SOURCE_TYPE_GPS

    # @property
    # def location_accuracy(self) -> int:
    #     return self._tracker_data[6]

    @property
    def extra_state_attributes(self):
        """Return the state attributes of the device."""
        return {
            "last_update": self._tracker_data["timeStamp"],
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Respond to a DataUpdateCoordinator update."""
        self._tracker_data = self.coordinator.data[self._attribute]
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
