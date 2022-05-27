"""Rivian (Unofficial)"""
import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from homeassistant.const import Platform

from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from rivian import Rivian

from .const import DOMAIN, VERSION, ISSUE_URL

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config_entry: ConfigEntry):
    """Disallow configuration via YAML."""
    _LOGGER.info(
        "Rivian Setup. Version %s. Please report issues at %s",
        VERSION,
        ISSUE_URL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Load the saved entries."""
    _LOGGER.info(
        "Rivian integration is starting under version %s. Please report issues at %s",
        VERSION,
        ISSUE_URL,
    )
    hass.data.setdefault(DOMAIN, {})
    # updated_config = config_entry.data.copy()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
