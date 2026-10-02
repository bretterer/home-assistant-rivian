"""Rivian (Unofficial) Tracker"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianTrackerEntityDescription
from .entity import RivianVehicleEntity

LOCATION_DESCRIPTION = RivianTrackerEntityDescription(key="location", name="Location")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the device tracker entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, Any] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianDeviceEntity(
            coordinators[vehicle_id], entry, LOCATION_DESCRIPTION, vehicle
        )
        for vehicle_id, vehicle in vehicles.items()
    ]

    async_add_entities(entities)


class RivianDeviceEntity(RivianVehicleEntity, TrackerEntity):
    """A class representing a Rivian device."""

    entity_description: RivianTrackerEntityDescription

    def __init__(
        self,
        coordinator: VehicleCoordinator,
        config_entry: ConfigEntry,
        description: RivianTrackerEntityDescription,
        vehicle: dict[str, Any],
    ) -> None:
        """Create a Rivian device tracker entity."""
        super().__init__(coordinator, config_entry, description, vehicle)
        self._attribute = "gnssLocation"
        self._tracker_data = (
            coordinator.data.get(self._attribute) if coordinator.data else None
        )

    @property
    def force_update(self) -> bool:
        """Disable forced updated since we are polling via the coordinator updates."""
        return False

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        if not self._tracker_data:
            return None
        return self._tracker_data.get("latitude")

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        if not self._tracker_data:
            return None
        return self._tracker_data.get("longitude")

    @property
    def source_type(self) -> SourceType:
        """Return the source type, eg gps or router, of the device."""
        return SourceType.GPS

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the state attributes of the device."""
        if not self._tracker_data:
            return None
        return {
            "last_update": self._tracker_data.get("timeStamp"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Respond to a DataUpdateCoordinator update."""
        if not (data := self.coordinator.data):
            return
        if not (entity := data.get(self._attribute)):
            return
        if not self._tracker_data or entity != self._tracker_data:
            self._tracker_data = entity
            self.async_write_ha_state()
