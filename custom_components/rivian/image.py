"""Rivian vehicle image entity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    CONF_VEHICLE_IMAGE_STYLE,
    DOMAIN,
    IMAGE_STYLE_CEL,
    IMAGE_STYLE_NONE,
)
from .coordinator import VehicleImageCoordinator
from .entity import RivianEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Rivian vehicle images using config entry."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, Any] = data[ATTR_VEHICLE]
    client = data[ATTR_COORDINATOR][ATTR_USER].api
    vehicle_image_style = entry.options.get(CONF_VEHICLE_IMAGE_STYLE, IMAGE_STYLE_CEL)

    if vehicle_image_style == IMAGE_STYLE_NONE:
        return

    version = "3" if vehicle_image_style == IMAGE_STYLE_CEL else "2"
    coordinator = VehicleImageCoordinator(
        hass=hass, config_entry=entry, client=client, version=version
    )
    await coordinator.async_config_entry_first_refresh()

    entities = [
        RivianVehicleImageEntity(
            coordinator=coordinator, vin=vehicles[image["vehicleId"]]["vin"], data=image
        )
        for image in coordinator.data
        if image["size"] == "large"
    ]
    async_add_entities(entities)


class RivianVehicleImageEntity(RivianEntity, ImageEntity):
    """Rivian vehicle image entity."""

    _attr_content_type = "image/png"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: VehicleImageCoordinator,
        vin: str,
        data: dict[str, str],
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        ImageEntity.__init__(self, coordinator.hass)

        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, vin)})
        self._attr_image_url = data["url"]
        self._attr_name = f"{data['placement'].capitalize()} {data['design']}"
        self._attr_unique_id = f"{vin}-{data['design']}-{data['placement']}"

    @property
    def image_last_updated(self) -> datetime | None:
        """The time when the image was last updated."""
        return self.coordinator._last_updated  # pylint: disable=protected-access
