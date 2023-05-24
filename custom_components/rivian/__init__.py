"""Rivian (Unofficial)"""
from __future__ import annotations

import logging

from rivian import Rivian
from rivian.exceptions import RivianUnauthenticated

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import (
    ATTR_CHARGING,
    ATTR_COORDINATOR,
    ATTR_VEHICLE,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    DOMAIN,
    ISSUE_URL,
    VERSION,
)
from .coordinator import ChargingDataUpdateCoordinator, RivianDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.UPDATE,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the saved entries."""
    _LOGGER.info(
        "Rivian integration is starting under version %s. Please report issues at %s",
        VERSION,
        ISSUE_URL,
    )

    hass.data.setdefault(DOMAIN, {})

    client = Rivian("", "")
    # pylint: disable=protected-access
    client._access_token = entry.data.get(CONF_ACCESS_TOKEN)
    client._refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
    client._user_session_token = entry.data.get(CONF_USER_SESSION_TOKEN)
    try:
        await client.create_csrf_token()
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.error("Could not update Rivian Data: %s", err, exc_info=1)
        raise ConfigEntryNotReady("Error communicating with API") from err

    coordinator = RivianDataUpdateCoordinator(hass, client=client, entry=entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except RivianUnauthenticated as err:
        raise ConfigEntryAuthFailed from err

    charging_coordinators: dict[str, ChargingDataUpdateCoordinator] = {}
    for vin in coordinator.vehicles:
        coor = ChargingDataUpdateCoordinator(hass=hass, client=client, vin=vin)
        await coor.async_config_entry_first_refresh()
        charging_coordinators[vin] = coor

    hass.data[DOMAIN][entry.entry_id] = {
        ATTR_COORDINATOR: {
            ATTR_VEHICLE: coordinator,
            ATTR_CHARGING: charging_coordinators,
        }
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Rivian async_unload_entry")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # pylint: disable=protected-access
    api: Rivian = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]._api
    await api.close()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
