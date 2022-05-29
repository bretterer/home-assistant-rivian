"""Rivian (Unofficial)"""
from __future__ import annotations
import asyncio
import logging
from typing import Any

from rivian.exceptions import RivianExpiredTokenError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.helpers.entity import Entity

from homeassistant.const import Platform
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    ATTR_MODEL,
)

from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
)

from rivian import Rivian

from .const import (
    DOMAIN,
    VERSION,
    ISSUE_URL,
    UPDATE_INTERVAL,
    CONF_VIN,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    ATTR_COORDINATOR,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config_entry: ConfigEntry):
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

    model = f"{(await async_get_integration(hass, DOMAIN)).version}"

    hass.data[DOMAIN][config_entry.entry_id] = {
        ATTR_COORDINATOR: coordinator,
        ATTR_MODEL: model,
    }

    hass.config_entries.async_setup_platforms(config_entry, PLATFORMS)

    return True


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
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via library."""
        try:
            data = await self._api.get_vehicle_info(
                vin=self._vin, access_token=self._access_token, properties=[]
            )
            return data
        except RivianExpiredTokenError as ex:
            _LOGGER.info(ex)
            _LOGGER.warning("Rivian token expired, refreshing")
            token = await self._api.refresh_access_token(
                self._refresh_token, self._client_id, self._client_secret
            )
            _LOGGER.info("=== Rivian Refresh Token ===")
            _LOGGER.info(token)
            new_tokens = await token.json()
            data = {**self._entry.data}
            data[CONF_ACCESS_TOKEN] = new_tokens[CONF_ACCESS_TOKEN]
            self._hass.config_entries.async_update_entry(
                self._entry, data=data, title="Rivian (Unofficial)"
            )

            return await self._async_update_data()
        except Exception as ex:
            _LOGGER.warning("Unknown Exception", ex)


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
