"""Rivian (Unofficial) Tracker"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RivianDataUpdateCoordinator, RivianEntity
from .const import ATTR_COORDINATOR, CONF_VIN, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Rivain binary_sensors by config_entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]
    async_add_entities([RivianDeviceEntity(coordinator, entry, "gnssLocation")], True)


class RivianDeviceEntity(RivianEntity, TrackerEntity):
    """A class representing a Rivian device."""

    _attr_has_entity_name = True
    _attr_name = "Location"

    def __init__(
        self,
        coordinator: RivianDataUpdateCoordinator,
        config_entry: ConfigEntry,
        attribute: str,
    ) -> None:
        """Create a Rivian device tracker entity."""
        super().__init__(coordinator, config_entry)
        self._attribute = attribute
        self._tracker_data = coordinator.data[self._attribute]
        self.entity_id = f"device_tracker.{DOMAIN}_telematics_gnss_position"
        self.entity_description = EntityDescription(
            name="Rivian", key=f"{DOMAIN}_telematics_gnss_position"
        )
        self._vin = config_entry.data.get(CONF_VIN)

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{DOMAIN}_Rivian {self._attr_name}_{self._config_entry.entry_id}"

    @property
    def force_update(self) -> bool:
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
    def source_type(self) -> SourceType:
        """Return the source type, eg gps or router, of the device."""
        return SourceType.GPS

    # @property
    # def location_accuracy(self) -> int:
    #     return self._tracker_data[6]

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return the state attributes of the device."""
        return {
            "last_update": self._tracker_data["timeStamp"],
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Respond to a DataUpdateCoordinator update."""
        try:
            if (
                self.coordinator.data[self._attribute]["timeStamp"]
                != self._tracker_data["timeStamp"]
            ):
                self._tracker_data = self.coordinator.data[self._attribute]
                self.async_write_ha_state()
        except:
            self._tracker_data = self.coordinator.data[self._attribute]
