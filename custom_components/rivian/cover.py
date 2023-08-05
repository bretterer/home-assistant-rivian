"""Support for Rivian cover entities."""
from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianCoverEntityDescription
from .entity import RivianVehicleControlEntity

_LOGGER = logging.getLogger(__name__)


COVERS: Final[tuple[RivianCoverEntityDescription, ...]] = (
    RivianCoverEntityDescription(
        key="windows",
        device_class=CoverDeviceClass.WINDOW,
        name="Windows",
        is_closed=lambda coordinator: not any(
            coordinator.get(key) == "open"
            for key in (
                "windowFrontLeftClosed",
                "windowFrontRightClosed",
                "windowRearLeftClosed",
                "windowRearRightClosed",
            )
        ),
        close_cover=lambda coordinator: coordinator.send_vehicle_command(
            command=VehicleCommand.CLOSE_ALL_WINDOWS
        ),
        open_cover=lambda coordinator: coordinator.send_vehicle_command(
            command=VehicleCommand.OPEN_ALL_WINDOWS
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities"""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianCoverEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for description in COVERS
    ]
    async_add_entities(entities)


class RivianCoverEntity(RivianVehicleControlEntity, CoverEntity):
    """Representation of a Rivian sensor entity."""

    entity_description: RivianCoverEntityDescription
    _attr_supported_features = CoverEntityFeature.CLOSE | CoverEntityFeature.OPEN

    @property
    def is_closed(self) -> bool:
        """Return if the cover is closed or not."""
        return self.entity_description.is_closed(self.coordinator)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        return await self.entity_description.close_cover(self.coordinator)

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self.entity_description.open_cover(self.coordinator)
