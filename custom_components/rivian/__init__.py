"""Rivian (Unofficial)"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rivian import Rivian

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_API,
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    ATTR_WALLBOX,
    CONF_VEHICLE_CONTROL,
    DOMAIN,
    ISSUE_STATE_VALUES_CHANGED,
    ISSUE_URL,
    PRECONDITION_SCHEDULE_NAME,
    VERSION,
)
from .coordinator import (
    UserCoordinator,
    VehicleCoordinator,
    WallboxCoordinator,
    precondition_store,
)
from .frontend import async_register_cards
from .helpers import (
    departure_mutation_succeeded,
    departure_schedules_from_message,
    get_rivian_api_from_entry,
)
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.DEVICE_TRACKER,
    Platform.IMAGE,
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
    Platform.UPDATE,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
# How long to wait for a vehicle's departure schedules when removing the entry
DEPARTURE_SCHEDULES_WAIT = 10


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Rivian integration."""
    async_setup_services(hass)
    try:
        await async_register_cards(hass)
    except Exception:  # the vehicles still set up without the dashboard cards
        _LOGGER.exception("Couldn't load the Rivian dashboard cards")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the saved entries."""
    _LOGGER.info(
        "Rivian integration is starting under version %s. Please report issues at %s",
        VERSION,
        ISSUE_URL,
    )

    hass.data.setdefault(DOMAIN, {})

    client = get_rivian_api_from_entry(hass, entry)
    try:
        await client.create_csrf_token()
    except Exception as err:
        _LOGGER.error("Could not update Rivian Data: %s", err, exc_info=1)
        await client.close()
        raise ConfigEntryNotReady("Error communicating with API") from err

    coordinator = UserCoordinator(
        hass=hass, config_entry=entry, client=client, include_phones=True
    )
    await coordinator.async_config_entry_first_refresh()

    vehicle_control = entry.options.get(CONF_VEHICLE_CONTROL)

    vehicles = coordinator.get_vehicles()
    if vehicle_control and (
        enrolled := coordinator.get_enrolled_phone_data(entry.options.get("public_key"))
    ):
        for vehicle_id in vehicles:
            if vehicle_id in enrolled[1]:
                vehicles[vehicle_id]["phone_identity_id"] = enrolled[1][vehicle_id]

    vehicle_coordinators: dict[str, VehicleCoordinator] = {}
    for vehicle_id in vehicles:
        coor = VehicleCoordinator(
            hass=hass, config_entry=entry, client=client, vehicle_id=vehicle_id
        )
        # Without data when subscribing failed, until a retry succeeds
        await coor.async_config_entry_first_refresh()
        await coor.charging_coordinator.async_config_entry_first_refresh()
        await coor.drivers_coordinator.async_config_entry_first_refresh()
        vehicle_coordinators[vehicle_id] = coor

    wallbox_coordinator = WallboxCoordinator(
        hass=hass, config_entry=entry, client=client
    )
    await wallbox_coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        ATTR_API: client,
        ATTR_VEHICLE: vehicles,
        ATTR_COORDINATOR: {
            ATTR_USER: coordinator,
            ATTR_VEHICLE: vehicle_coordinators,
            ATTR_WALLBOX: wallbox_coordinator,
        },
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry."""
    if entry.version > 1:
        # Downgraded from a future version
        return False

    if entry.minor_version < 2:
        # Sensor states changed from formatted text to lowercase API values,
        # which automations and templates comparing them need updating for
        async_create_issue(
            hass,
            DOMAIN,
            ISSUE_STATE_VALUES_CHANGED,
            is_fixable=True,
            is_persistent=True,
            severity=IssueSeverity.WARNING,
            translation_key=ISSUE_STATE_VALUES_CHANGED,
        )
        hass.config_entries.async_update_entry(entry, minor_version=2)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    # Stop the vehicles' background work, such as a precondition cleanup, before
    # closing the client it uses. Home Assistant shuts them down again after this
    # returns, which does nothing more
    data = hass.data[DOMAIN].pop(entry.entry_id)
    for coordinator in data[ATTR_COORDINATOR][ATTR_VEHICLE].values():
        try:
            await coordinator.async_shutdown()
        except Exception:
            # the others still stop, and the client still closes
            _LOGGER.exception("Error stopping vehicle %s", coordinator.vehicle_id)
    api: Rivian = data[ATTR_API]
    await api.close()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    try:
        await _async_remove_preconditions(hass, entry)
    except Exception:
        # the phone is still disenrolled
        _LOGGER.exception("Error deleting precondition schedules")
    if public_key := entry.options.get("public_key"):
        client = get_rivian_api_from_entry(hass, entry)
        coordinator = UserCoordinator(
            hass=hass, config_entry=entry, client=client, include_phones=True
        )
        try:
            # The entry is no longer in setup, so first_refresh isn't allowed here
            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                _LOGGER.warning("Unable to disenroll phone: could not fetch user data")
                return
            if enrolled_data := coordinator.get_enrolled_phone_data(public_key):
                for identity_id in enrolled_data[1].values():
                    await client.disenroll_phone(identity_id=identity_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Unable to disenroll phone: %s", err)
        finally:
            await client.close()


async def _async_remove_preconditions(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the temporary precondition schedules Home Assistant still owns.

    Nothing would delete them once the integration is gone, so each would
    precondition again every week. A vehicle's store is removed once its schedules
    are; if one can't be deleted, the store is kept, so setting the integration up
    again picks the cleanup back up.
    """
    # The entry's devices are still registered here; their identifiers include
    # the vehicle ids, and only vehicles have a precondition store
    stores = {}
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain != DOMAIN:
                continue
            store = precondition_store(hass, entry.entry_id, identifier)
            try:
                data = await store.async_load()
            except Exception:
                _LOGGER.exception(
                    "Unable to read vehicle %s's precondition store", identifier
                )
                continue
            if isinstance(data, dict):
                stores[identifier] = (store, data)
    if not stores:
        return

    client = get_rivian_api_from_entry(hass, entry)
    try:
        await client.create_csrf_token()
        for vehicle_id, (store, data) in stores.items():
            await _async_remove_vehicle_preconditions(client, vehicle_id, store, data)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Unable to delete precondition schedules; delete any named %s in the "
            "Rivian app: %s",
            PRECONDITION_SCHEDULE_NAME,
            type(err).__name__,
        )
    finally:
        await client.close()


async def _async_remove_vehicle_preconditions(
    client: Rivian, vehicle_id: str, store: Store[dict[str, Any]], data: dict[str, Any]
) -> None:
    """Delete a vehicle's temporary precondition schedules, then its store."""
    if data.get("owned") or data.get("pending"):
        schedules = await _async_departure_schedules(client, vehicle_id)
    else:
        schedules = []  # nothing to delete
    if schedules is None:
        # can't tell which still exist, so keep the store
        _LOGGER.warning(
            "Unable to confirm vehicle %s's departure schedules; if any named %s "
            "remain, delete them in the Rivian app",
            vehicle_id,
            PRECONDITION_SCHEDULE_NAME,
        )
        return
    owned = set(data.get("owned") or {})
    # created without its id being recorded; found by their unique name
    pending = {
        name
        for record in data.get("pending") or []
        if isinstance(record, dict) and isinstance(name := record.get("name"), str)
    }
    deleted_all = True
    # ones no longer in the list were already deleted, such as in the app
    for schedule in schedules:
        name = schedule.get("name")
        if schedule.get("id") not in owned and not (
            isinstance(name, str) and name in pending
        ):
            continue
        try:
            response = await client.delete_departure_schedule(
                vehicle_id, schedule["id"]
            )
            if not departure_mutation_succeeded(
                await response.json(), "deleteDepartureSchedule"
            ):
                raise ValueError("not deleted")
        except Exception as err:  # noqa: BLE001
            deleted_all = False
            _LOGGER.warning(
                "Unable to delete precondition schedule %s from vehicle %s; "
                "delete it in the Rivian app: %s",
                name,
                vehicle_id,
                type(err).__name__,
            )
    if deleted_all:
        try:
            await store.async_remove()
        except Exception:
            _LOGGER.exception(
                "Unable to remove vehicle %s's precondition store", vehicle_id
            )


async def _async_departure_schedules(
    client: Rivian, vehicle_id: str
) -> list[dict[str, Any]] | None:
    """Return a vehicle's departure schedules, which are only sent by subscription.

    Rivian can send an empty list just before the full one, and a timeout or
    disconnect can follow that empty list too, so an empty list never counts:
    removing the integration only removes a store once the list is confirmed.
    Returns None if no schedules arrived, or subscribing failed.
    """
    received: asyncio.Future[list[dict[str, Any]]] = (
        asyncio.get_running_loop().create_future()
    )

    def on_schedules(data: dict[str, Any]) -> None:
        if (
            schedules := departure_schedules_from_message(data)
        ) and not received.done():
            received.set_result(schedules)

    unsubscribe = await client.subscribe_for_departure_schedules(
        vehicle_id, on_schedules
    )
    if not unsubscribe:
        return None
    try:
        return await asyncio.wait_for(received, DEPARTURE_SCHEDULES_WAIT)
    except TimeoutError:
        return None
    finally:
        await unsubscribe()


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    coordinators = hass.data[DOMAIN][config_entry.entry_id][ATTR_COORDINATOR]
    user_coordinator: UserCoordinator = coordinators[ATTR_USER]
    wallbox_coordinator: WallboxCoordinator = coordinators[ATTR_WALLBOX]

    vehicles = user_coordinator.get_vehicles().keys()
    wallboxes = {x["wallboxId"] for x in wallbox_coordinator.data}

    return not any(
        identifier
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN and identifier[1] in vehicles | wallboxes
    )
