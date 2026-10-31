"""Rivian vehicle image entity."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
import time
from typing import Any, Final

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .analytics_db import VehiclePicture
from .const import (
    ATTR_COORDINATOR,
    ATTR_DRIVE_STORE,
    ATTR_USER,
    ATTR_VEHICLE,
    CONF_VEHICLE_IMAGE_STYLE,
    DOMAIN,
    IMAGE_STYLE_CEL,
    IMAGE_STYLE_NONE,
)
from .coordinator import VehicleImageCoordinator
from .entity import RivianEntity
from .vehicle_picture import RETRY_AFTER_SECONDS, async_fetch_vehicle_pictures

_LOGGER = logging.getLogger(__name__)


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

    pictures = await _async_picture_entities(
        hass, client, vehicles, data.get(ATTR_DRIVE_STORE) or {}
    )
    # Kept so rivian.set_vehicle_picture can update or add a picture live.
    data[PICTURE_PLATFORM] = {
        "add": async_add_entities,
        "entities": {entity.vin: entity for entity in pictures},
    }
    entities: list[ImageEntity] = list(pictures)
    entities.extend(
        await _async_legacy_image_entities(
            hass, entry, client, vehicles, vehicle_image_style
        )
    )
    async_add_entities(entities)


PICTURE_PLATFORM: Final = "picture_platform"


@callback
def async_apply_vehicle_picture(
    hass: HomeAssistant, entry_id: str, vin: str, picture: VehiclePicture
) -> bool:
    """Show a newly saved picture: update the vehicle's entity, or add one.

    Returns False when the image platform isn't set up for that entry (e.g.
    vehicle images are turned off); the picture is still saved and shows
    after the next start.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    platform = entry_data.get(PICTURE_PLATFORM)
    if not platform:
        return False
    entity = platform["entities"].get(vin)
    if entity is not None:
        entity.async_set_picture(picture)
    else:
        entity = RivianVehiclePictureEntity(hass, vin, picture)
        platform["entities"][vin] = entity
        platform["add"]([entity])
    return True


async def _async_picture_entities(
    hass: HomeAssistant,
    client: Any,
    vehicles: dict[str, Any],
    stores: dict[str, Any],
) -> list[RivianVehiclePictureEntity]:
    """Serve each vehicle's saved configurator picture, fetching it only once.

    A vehicle with no saved picture is looked up now and the result saved; a
    failed lookup is retried at most every RETRY_AFTER_SECONDS.
    """
    now = time.time()
    store_by_vin: dict[str, Any] = {}
    pictures: dict[str, VehiclePicture] = {}
    to_fetch: set[str] = set()
    for vehicle_id, info in vehicles.items():
        vin = str(info.get("vin") or "")
        store = stores.get(vehicle_id)
        if not vin or store is None:
            continue
        store_by_vin[vin] = store
        saved = await store.async_get_vehicle_picture()
        if saved is not None and saved.status == "ok" and saved.image:
            pictures[vin] = saved
        elif saved is None or now - saved.fetched_ts >= RETRY_AFTER_SECONDS:
            to_fetch.add(vin)

    if to_fetch:
        fetched = await async_fetch_vehicle_pictures(hass, client, to_fetch)
        for vin, picture in fetched.items():
            await store_by_vin[vin].async_save_vehicle_picture(picture)
            if picture.status == "ok" and picture.image:
                pictures[vin] = picture

    return [
        RivianVehiclePictureEntity(hass, vin, picture)
        for vin, picture in pictures.items()
    ]


async def _async_legacy_image_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: Any,
    vehicles: dict[str, Any],
    vehicle_image_style: str,
) -> list[ImageEntity]:
    """Images from Rivian's legacy image API, trying each image version."""
    preferred = "3" if vehicle_image_style == IMAGE_STYLE_CEL else "2"
    coordinator: VehicleImageCoordinator | None = None
    images: list[dict[str, Any]] = []
    tried: list[str] = []
    for version in _version_candidates(preferred):
        candidate = VehicleImageCoordinator(
            hass=hass, config_entry=entry, client=client, version=version
        )
        # Only the configured version may fail setup; fallbacks are best effort.
        try:
            await candidate.async_config_entry_first_refresh()
        except Exception as err:
            if not tried:
                raise
            _LOGGER.debug("Vehicle image version %s failed: %s", version, err)
            tried.append(version)
            continue
        tried.append(version)
        coordinator = candidate
        images = _vehicle_images(candidate.data, vehicles)
        if images:
            break

    if not images or coordinator is None:
        _LOGGER.debug(
            "Rivian's image API returned no vehicle images (versions tried: %s)",
            ", ".join(tried),
        )
        return []
    if tried[-1] != preferred:
        _LOGGER.info(
            "Rivian returned no images for image version %s; using version %s",
            preferred,
            tried[-1],
        )
    return [
        RivianVehicleImageEntity(
            coordinator=coordinator, vin=vehicles[image["vehicleId"]]["vin"], data=image
        )
        for image in images
    ]


# Rivian's image API knows versions "1" and "2"; any other value returns v1
# images. A version can come back empty for a vehicle (seen for "2" on a 2025
# R1S), so the configured version is tried first, then the others.
_IMAGE_VERSIONS: Final[tuple[str, ...]] = ("2", "1")


def _version_candidates(preferred: str) -> list[str]:
    """Return image versions to try, the configured one first."""
    return [preferred, *(v for v in _IMAGE_VERSIONS if v != preferred)]


def _vehicle_images(data: Any, vehicles: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the large images that belong to one of this entry's vehicles."""
    if not isinstance(data, list):
        return []
    return [
        image
        for image in data
        if isinstance(image, dict)
        and image.get("size") == "large"
        and image.get("vehicleId") in vehicles
    ]


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


class RivianVehiclePictureEntity(ImageEntity):
    """The vehicle's configurator picture, served from the analytics DB copy."""

    _attr_has_entity_name = True
    _attr_name = "Picture"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, vin: str, picture: VehiclePicture) -> None:
        """Initialize the entity from a saved picture."""
        super().__init__(hass)
        self.vin = vin
        self._attr_unique_id = f"{vin}-picture"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, vin)})
        self._set_picture(picture)

    def _set_picture(self, picture: VehiclePicture) -> None:
        self._picture = picture
        # Only raster types are ever served from HA's origin (an SVG could
        # carry script); anything else falls back to WebP, the render type.
        self._attr_content_type = (
            picture.content_type
            if picture.content_type
            in ("image/jpeg", "image/png", "image/gif", "image/webp")
            else "image/webp"
        )
        self._attr_image_last_updated = datetime.fromtimestamp(picture.fetched_ts, UTC)

    @callback
    def async_set_picture(self, picture: VehiclePicture) -> None:
        """Replace the picture (e.g. set by the user) and refresh the frontend."""
        self._set_picture(picture)
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """Return the saved image bytes; no network access."""
        return self._picture.image
