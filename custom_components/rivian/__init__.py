"""Rivian (Unofficial)"""
from __future__ import annotations

import logging
from typing import Any

from rivian import Rivian
from rivian.exceptions import RivianPhoneLimitReachedError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
import homeassistant.helpers.device_registry as dr

from .const import (
    ATTR_API,
    ATTR_CHARGING,
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    ATTR_WALLBOX,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    CONF_VEHICLE_CONTROL,
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
    Platform.COVER,
    Platform.DEVICE_TRACKER,
    Platform.IMAGE,
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
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

    client = Rivian(
        request_timeout=30,
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        user_session_token=entry.data.get(CONF_USER_SESSION_TOKEN),
    )
    try:
        await client.create_csrf_token()
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.error("Could not update Rivian Data: %s", err, exc_info=1)
        raise ConfigEntryNotReady("Error communicating with API") from err

    coordinator = UserCoordinator(hass=hass, client=client, include_phones=True)
    await coordinator.async_config_entry_first_refresh()
    vehicles = {
        vehicle["id"]: vehicle["vehicle"]
        | {
            "name": vehicle["name"],
            "public_key": vehicle.get("vas", {}).get("vehiclePublicKey"),
        }
        for vehicle in coordinator.data["vehicles"]
    }
    if entry.options.get(CONF_VEHICLE_CONTROL) and (
        enrolled := coordinator.get_enrolled_phone_data(entry.options.get("public_key"))
    ):
        for vehicle_id in vehicles:
            if vehicle_id in enrolled[1]:
                vehicles[vehicle_id]["phone_identity_id"] = enrolled[1][vehicle_id]

    vehicle_coordinators: dict[str, VehicleCoordinator] = {}
    charging_coordinators: dict[str, ChargingCoordinator] = {}
    for vin in vehicles:
        vehicle_id = vehicles[vin]["id"]
        coor = VehicleCoordinator(hass=hass, client=client, vehicle_id=vehicle_id)
        await coor.async_config_entry_first_refresh()
        vehicle_coordinators[vin] = coor

        await coor.charging_coordinator.async_config_entry_first_refresh()
        charging_coordinators[vin] = coor.charging_coordinator

    wallbox_coordinator = WallboxCoordinator(hass=hass, client=client)
    await wallbox_coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        ATTR_API: client,
        ATTR_VEHICLE: vehicles,
        ATTR_COORDINATOR: {
            ATTR_USER: coordinator,
            ATTR_VEHICLE: vehicle_coordinators,
            ATTR_CHARGING: charging_coordinators,
            ATTR_WALLBOX: wallbox_coordinator,
        },
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    api: Rivian = hass.data[DOMAIN][entry.entry_id][ATTR_API]
    await api.close()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    if public_key := entry.options.get("public_key"):
        client = Rivian(
            request_timeout=30,
            access_token=entry.data.get(CONF_ACCESS_TOKEN),
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
            user_session_token=entry.data.get(CONF_USER_SESSION_TOKEN),
        )
        coordinator = UserCoordinator(hass=hass, client=client, include_phones=True)
        await coordinator.async_config_entry_first_refresh()

        if enrolled_data := coordinator.get_enrolled_phone_data(public_key=public_key):
            for identity_id in enrolled_data[1].values():
                await client.disenroll_phone(identity_id=identity_id)
        await client.close()


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    if errors := await toggle_vehicle_control(hass, entry):
        options = entry.options.copy()
        options[CONF_VEHICLE_CONTROL] = [
            vehicle
            for vehicle in options.get(CONF_VEHICLE_CONTROL, [])
            if vehicle not in errors
        ]
        await hass.config_entries.async_update_entry(entry, options=options)
    await hass.config_entries.async_reload(entry.entry_id)


async def toggle_vehicle_control(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Toggle vehicle control."""
    errors = []

    device_registry = dr.async_get(hass)

    vehicle_control = entry.options.get(CONF_VEHICLE_CONTROL, [])
    public_key = entry.options.get("public_key")
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: Rivian = entry_data[ATTR_API]
    vehicles: dict[str, dict[str, Any]] = entry_data[ATTR_VEHICLE]
    user: UserCoordinator = entry_data[ATTR_COORDINATOR][ATTR_USER]
    user_id = user.data["id"]
    if enrolled_data := user.get_enrolled_phone_data(public_key=public_key):
        vehicle_identity = enrolled_data[1]
    else:
        vehicle_identity = {}

    if vehicle_control:
        control_vehicles = {
            identifier[1]: device_id
            for device_id in vehicle_control
            if (device := device_registry.async_get(device_id))
            for identifier in device.identifiers
            if identifier[0] == DOMAIN and identifier[1] in vehicles
        }
    else:
        control_vehicles = {}

    for vehicle_id, vehicle in vehicles.items():
        vehicle_name = vehicle["name"]

        # check if phone needs to be enrolled
        if vehicle_id in control_vehicles and vehicle_id not in vehicle_identity:
            success = False
            try:
                if not (
                    success := await api.enroll_phone(
                        user_id=user_id,
                        vehicle_id=vehicle["id"],
                        device_type="HA",
                        device_name=hass.config.location_name,
                        public_key=public_key,
                    )
                ):
                    _LOGGER.warning("Unable to enable control for %s", vehicle_name)
            except RivianPhoneLimitReachedError:
                _LOGGER.error(
                    "Unable to enable control for %s: phone limit reached", vehicle_name
                )
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error("Unable to enable control for %s: %s", vehicle_name, ex)
            if not success:
                errors.append(control_vehicles[vehicle_id])

        # check if phone needs to be disenrolled
        elif vehicle_id not in control_vehicles and vehicle_id in vehicle_identity:
            success = False
            try:
                identity_id = vehicle_identity[vehicle_id]
                if not (success := await api.disenroll_phone(identity_id=identity_id)):
                    _LOGGER.warning("Unable to disable control for %s", vehicle_name)
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error("Unable to disable control for %s: %s", vehicle_name, ex)
            # if not success:
            #     if device := device_registry.async_get_device({(DOMAIN, vehicle_id)}):
            #         errors.append(device.id)

    return errors
