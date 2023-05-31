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
    ATTR_USER,
    ATTR_VEHICLE,
    ATTR_WALLBOX,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    DOMAIN,
    ISSUE_URL,
    VERSION,
)
from .coordinator import (
    ChargingCoordinator,
    UserCoordinator,
    VehicleCoordinator,
    WallboxCoordinator,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
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

    coordinator = UserCoordinator(hass=hass, client=client)
    await coordinator.async_config_entry_first_refresh()
    vehicles = {
        vehicle["vin"]: vehicle["vehicle"] | {"name": vehicle["name"]}
        for vehicle in coordinator.data["vehicles"]
    }

    vehicle_coordinators: dict[str, VehicleCoordinator] = {}
    charging_coordinators: dict[str, ChargingCoordinator] = {}
    for vin in vehicles:
        coor = VehicleCoordinator(hass=hass, client=client, vin=vin)
        await coor.async_config_entry_first_refresh()
        vehicle_coordinators[vin] = coor
        coor = ChargingCoordinator(hass=hass, client=client, vin=vin)
        await coor.async_config_entry_first_refresh()
        charging_coordinators[vin] = coor

    wallbox_coordinator = WallboxCoordinator(hass=hass, client=client)
    await wallbox_coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        ATTR_VEHICLE: vehicles,
        ATTR_COORDINATOR: {
            ATTR_USER: coordinator,
            ATTR_VEHICLE: vehicle_coordinators,
            ATTR_CHARGING: charging_coordinators,
            ATTR_WALLBOX: wallbox_coordinator,
        },
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    api: Rivian = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR][ATTR_USER].api
    await api.close()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
