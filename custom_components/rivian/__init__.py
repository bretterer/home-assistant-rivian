"""Rivian (Unofficial)"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import async_timeout
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_MODEL, CONF_CLIENT_ID, CONF_CLIENT_SECRET, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.loader import async_get_integration
from homeassistant.util import slugify

from rivian import Rivian
from rivian.exceptions import RivianExpiredTokenError

from .const import (
    ATTR_COORDINATOR,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ISSUE_URL,
    SENSORS,
    UPDATE_INTERVAL,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.DEVICE_TRACKER]


async def async_setup(
    hass: HomeAssistant, config_entry: ConfigEntry
):  # pylint: disable=unused-argument
    """Disallow configuration via YAML."""
    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Load the saved entries."""
    _LOGGER.info(
        "Rivian integration is starting under version %s. Please report issues at %s",
        VERSION,
        ISSUE_URL,
    )
    hass.data.setdefault(DOMAIN, {})

    client = Rivian(
        config_entry.data.get(CONF_CLIENT_ID), config_entry.data.get(CONF_CLIENT_SECRET)
    )

    coordinator = RivianDataUpdateCoordinator(hass, client=client, entry=config_entry)
    await coordinator.async_config_entry_first_refresh()

    config_entry.async_on_unload(config_entry.add_update_listener(update_listener))

    model = f"{(await async_get_integration(hass, DOMAIN)).version}"

    hass.data[DOMAIN][config_entry.entry_id] = {
        ATTR_COORDINATOR: coordinator,
        ATTR_MODEL: model,
    }

    hass.config_entries.async_setup_platforms(config_entry, PLATFORMS)

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener."""
    await hass.config_entries.async_reload(entry.entry_id)


def get_entity_unique_id(config_entry_id: str, name: str) -> str:
    """Get the unique_id for a Rivian entity."""
    return f"{config_entry_id}:{DOMAIN}_{name}"


def get_device_identifier(entry: ConfigEntry, name: str | None = None) -> tuple[str, str]:
    """Get a device identifier."""
    if name:
        return (DOMAIN, f"{entry.entry_id}:{DOMAIN}:{slugify(name)}")
    else:
        return (DOMAIN, f"{DOMAIN}:{entry.entry_id}")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Rivian async_unload_entry")
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


class RivianDataUpdateCoordinator(DataUpdateCoordinator):  # type: ignore[misc]
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, client: Rivian, entry: ConfigEntry):
        """Initialize."""
        self._hass = hass
        self._api = client
        self._entry = entry
        self._vin = entry.data.get("vin")
        self._access_token = entry.data.get(CONF_ACCESS_TOKEN)
        self._refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
        self._client_id = entry.data.get(CONF_CLIENT_ID)
        self._client_secret = entry.data.get(CONF_CLIENT_SECRET)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_update_data(self):
        """Update data via library."""
        for i in range(3):
            try:
                sensors = []
                for _, val in enumerate(SENSORS):
                    sensors.append(val)

                sensors.append("$gnss")
                vehicle_info = await self._api.get_vehicle_info(
                    vin=self._vin,
                    access_token=self._access_token,
                    properties=sensors,
                )
                vijson = await vehicle_info.json()

                vehicle_info_items = self.build_vehicle_info_dict(vijson)
                return vehicle_info_items

            except RivianExpiredTokenError:
                _LOGGER.info("Rivian token expired, refreshing")
                token = await self._api.refresh_access_token(
                    self._refresh_token, self._client_id, self._client_secret
                )
                new_tokens = await token.json()
                self._access_token = new_tokens[CONF_ACCESS_TOKEN]
                continue
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.error("Unknown Exception while updating Rivian data", exc_info=1)
                raise Exception(f"Error communicating with API: {err}")
            else:
                break

    def build_vehicle_info_dict(self, vijson) -> dict[str, dict[str, Any]]:
        """take the json output of vehicle_info and build a dictionary"""
        return vijson["data"]


class RivianEntity(Entity):
    """Base class for Rivian entities."""

    def __init__(self, config_entry: ConfigEntry):
        """Construct a RivianEntity."""
        Entity.__init__(self)

        self._config_entry = config_entry
        self._available = True

    @property
    def available(self) -> bool:
        """Return the availability of the entity."""
        return self._available

    def _get_model(self) -> str:
        """Get the Rivian model string."""
        return str(self.hass.data[DOMAIN][self._config_entry.entry_id][ATTR_MODEL])
