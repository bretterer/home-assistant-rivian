"""Rivian (Unofficial)"""
from __future__ import annotations

import logging

from rivian import Rivian

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import ATTR_COORDINATOR, DOMAIN, ISSUE_URL, VERSION
from .entity import RivianDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.UPDATE,
]


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Load the saved entries."""
    _LOGGER.info(
        "Rivian integration is starting under version %s. Please report issues at %s",
        VERSION,
        ISSUE_URL,
    )

    hass.data.setdefault(DOMAIN, {})
    updated_config = config_entry.data.copy()

    try:
        client = Rivian("", "")
        await client.create_csrf_token()
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.error("Could not update Rivian Data: %s", err, exc_info=1)
        raise ConfigEntryNotReady("Error communicating with API") from err

    coordinator = RivianDataUpdateCoordinator(hass, client=client, entry=config_entry)
    await coordinator.async_config_entry_first_refresh()

    if updated_config != config_entry.data:
        hass.config_entries.async_update_entry(config_entry, data=updated_config)

    config_entry.add_update_listener(update_listener)

    hass.data[DOMAIN][config_entry.entry_id] = {ATTR_COORDINATOR: coordinator}

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener."""
    _LOGGER.debug("Attempting to reload sensors from the %s integration", DOMAIN)
    if entry.data == entry.options:
        _LOGGER.debug("No changes detected not reloading sensors.")
        return

    new_data = entry.options.copy()

    hass.config_entries.async_update_entry(
        entry=entry,
        data=new_data,
    )

    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Rivian async_unload_entry")
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
