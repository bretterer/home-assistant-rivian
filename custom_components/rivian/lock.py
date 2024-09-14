"""Support for Rivian lock entities."""

from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN, LOCK_STATE_ENTITIES
from .coordinator import VehicleCoordinator
from .data_classes import RivianLockEntityDescription
from .entity import RivianVehicleControlEntity

_LOGGER = logging.getLogger(__name__)


LOCKS: Final[tuple[RivianLockEntityDescription, ...]] = (
    RivianLockEntityDescription(
        key="closures",
        name="Closures",
        is_locked=lambda coordinator: not any(
            coordinator.get(key) == "unlocked" for key in LOCK_STATE_ENTITIES
        ),
        lock=lambda coordinator: coordinator.send_vehicle_command(
            command=VehicleCommand.LOCK_ALL_CLOSURES_FEEDBACK
        ),
        unlock=lambda coordinator: coordinator.send_vehicle_command(
            command=VehicleCommand.UNLOCK_ALL_CLOSURES
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
        RivianLockEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for description in LOCKS
    ]
    async_add_entities(entities)


class RivianLockEntity(RivianVehicleControlEntity, LockEntity):
    """Representation of a Rivian sensor entity."""

    entity_description: RivianLockEntityDescription

    @property
    def is_locked(self) -> bool:
        """Return true if the lock is locked."""
        return self.entity_description.is_locked(self.coordinator)

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock."""
        return await self.entity_description.lock(self.coordinator)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        await self.entity_description.unlock(self.coordinator)
