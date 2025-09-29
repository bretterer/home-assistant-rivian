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

WINDOWS: Final[tuple[str, ...]] = (
    "windowFrontLeftClosed",
    "windowFrontRightClosed",
    "windowRearLeftClosed",
    "windowRearRightClosed",
)

COVERS: Final[dict[str | None, tuple[RivianCoverEntityDescription, ...]]] = {
    None: (
        RivianCoverEntityDescription(
            key="windows",
            translation_key="windows",
            device_class=CoverDeviceClass.WINDOW,
            is_closed=lambda coor: not any(coor.get(key) == "open" for key in WINDOWS),
            close_cover=lambda coor: coor.send_vehicle_command(
                command=VehicleCommand.CLOSE_ALL_WINDOWS
            ),
            open_cover=lambda coor: coor.send_vehicle_command(
                command=VehicleCommand.OPEN_ALL_WINDOWS
            ),
        ),
    ),
    "CHARG_PORT_DOOR_COMMAND": (
        RivianCoverEntityDescription(
            key="charge_port",
            translation_key="charge_port",
            device_class=CoverDeviceClass.DOOR,
            can_close=lambda coor: (
                coor.get("closureChargePortDoorNextAction") != "close_not_available"
            ),
            is_closed=lambda coor: coor.get("chargePortState") != "open",
            is_closing=lambda coor: (
                coor.get("chargePortState") == "closing"
                or coor.get("closureChargePortDoorNextAction") == "closing"
            ),
            is_opening=lambda coor: (
                coor.get("chargePortState") == "opening"
                or coor.get("closureChargePortDoorNextAction") == "opening"
            ),
            close_cover=lambda coor: coor.send_vehicle_command(
                command=VehicleCommand.CLOSE_CHARGE_PORT_DOOR
            ),
            open_cover=lambda coor: coor.send_vehicle_command(
                command=VehicleCommand.OPEN_CHARGE_PORT_DOOR
            ),
        ),
    ),
    "LIFTGATE_CMD": (
        RivianCoverEntityDescription(
            key="liftgate",
            translation_key="liftgate",
            device_class=CoverDeviceClass.DOOR,
            is_closed=lambda coor: coor.get("closureLiftgateClosed") != "open",
            close_cover=lambda coor: coor.send_vehicle_command(
                command=VehicleCommand.CLOSE_LIFTGATE
            ),
            open_cover=lambda coor: coor.send_vehicle_command(
                command=VehicleCommand.OPEN_LIFTGATE_UNLATCH_TAILGATE
            ),
        ),
    ),
    "FRUNK_NXT_ACT": (
        RivianCoverEntityDescription(
            key="frunk",
            translation_key="frunk",
            device_class=CoverDeviceClass.DOOR,
            is_closed=lambda coor: coor.get("closureFrunkClosed") != "open",
            close_cover=lambda coor: coor.send_vehicle_command(
                command=VehicleCommand.CLOSE_FRUNK
            ),
            open_cover=lambda coor: coor.send_vehicle_command(
                command=VehicleCommand.OPEN_FRUNK
            ),
        ),
    ),
    "TONNEAU_CMD": (
        RivianCoverEntityDescription(
            key="tonneau",
            translation_key="tonneau",
            device_class=CoverDeviceClass.DOOR,
            is_closed=lambda coor: coor.get("closureTonneauClosed") != "open",
            close_cover=lambda coor: coor.send_vehicle_command(
                command=VehicleCommand.CLOSE_TONNEAU_COVER
            ),
            open_cover=lambda coor: coor.send_vehicle_command(
                command=VehicleCommand.OPEN_TONNEAU_COVER
            ),
        ),
    ),
}


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
        for feature, descriptions in COVERS.items()
        if feature is None or feature in (vehicle.get("supported_features", []))
        for description in descriptions
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

    @property
    def is_closing(self) -> bool | None:
        """Return if the cover is closing or not."""
        if is_closing := self.entity_description.is_closing:
            return is_closing(self.coordinator)
        return super().is_closing

    @property
    def is_opening(self) -> bool | None:
        """Return if the cover is opening or not."""
        if is_opening := self.entity_description.is_opening:
            return is_opening(self.coordinator)
        return super().is_opening

    @property
    def supported_features(self) -> CoverEntityFeature:
        """Flag supported features."""
        if can_close := self.entity_description.can_close:
            if not can_close(self.coordinator):
                return CoverEntityFeature.OPEN
        return self._attr_supported_features

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        return await self.entity_description.close_cover(self.coordinator)

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self.entity_description.open_cover(self.coordinator)
