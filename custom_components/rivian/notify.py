"""Support for Rivian notify entities."""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components.notify import NotifyEntity, NotifyEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .entity import RivianVehicleEntity

SEND_DESTINATION: Final = NotifyEntityDescription(
    key="navigation", translation_key="navigation"
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the notify entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    async_add_entities(
        RivianSendDestinationEntity(
            coordinators[vehicle_id], entry, SEND_DESTINATION, vehicle
        )
        for vehicle_id, vehicle in vehicles.items()
    )


class RivianSendDestinationEntity(RivianVehicleEntity, NotifyEntity):
    """Send a destination to a vehicle's navigation.

    The message is the destination: an address, a place, or "lat,lng", which
    Rivian parses. Unlike vehicle controls, this needs no paired phone key.
    """

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Send the message to the vehicle as a destination."""
        try:
            response = await self.coordinator.api.send_location_to_vehicle(
                message, self.coordinator.vehicle_id
            )
            res_json = await response.json()
        except Exception as err:
            raise HomeAssistantError(f"Unable to send the destination: {err}") from err
        data = (res_json.get("data") or {}).get("parseAndShareLocationToVehicle") or {}
        result = (data.get("publishResponse") or {}).get("result")
        if result != 0:
            raise HomeAssistantError(
                f"Unable to send the destination: {res_json.get('errors') or result}"
            )
