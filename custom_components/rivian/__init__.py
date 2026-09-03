"""Rivian (Unofficial)"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rivian import Rivian
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from .const import (
    ATTR_API,
    ATTR_COORDINATOR,
    ATTR_DRIVE_STORE,
    ATTR_DRIVE_TRACKER,
    ATTR_USER,
    ATTR_VEHICLE,
    ATTR_WALLBOX,
    CONF_VEHICLE_CONTROL,
    DOMAIN,
    ISSUE_URL,
    VERSION,
)

try:
    from homeassistant.components.frontend import add_extra_js_url
except ImportError:
    def add_extra_js_url(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass

try:
    from homeassistant.components.http import StaticPathConfig
except ImportError:
    class StaticPathConfig:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

from .coordinator import UserCoordinator, VehicleCoordinator, WallboxCoordinator
from .dashboard_generator import (
    DEFAULT_ICON,
    DEFAULT_TITLE,
    DEFAULT_URL_PATH,
    async_create_efficiency_dashboard,
)
from .drive_storage import DriveStore
from .drive_tracker import DriveTracker
from .helpers import get_rivian_api_from_entry
from .history_backfill import async_backfill_from_recorder

SERVICE_BACKFILL_DRIVE_HISTORY = "backfill_drive_history"
SERVICE_CREATE_EFFICIENCY_DASHBOARD = "create_efficiency_dashboard"

BACKFILL_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
        vol.Optional("days"): vol.Coerce(int),
        vol.Optional("dry_run", default=False): cv.boolean,
        vol.Optional("db_path"): cv.string,
    }
)

CREATE_DASHBOARD_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("title", default=DEFAULT_TITLE): cv.string,
        vol.Optional("icon", default=DEFAULT_ICON): cv.string,
        vol.Optional("url_path", default=DEFAULT_URL_PATH): cv.string,
    }
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
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


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register bundled frontend cards so they load automatically without HACS."""
    frontend_dir = Path(__file__).parent / "frontend"
    if not frontend_dir.is_dir():
        return

    static_url = f"/{DOMAIN}_static"
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(static_url, str(frontend_dir), cache_headers=True)]
        )
    except (RuntimeError, ValueError, AttributeError):
        pass

    plotly_js = frontend_dir / "plotly-graph-card.js"
    if plotly_js.is_file():
        try:
            add_extra_js_url(hass, f"{static_url}/plotly-graph-card.js")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not add plotly-graph-card.js extra URL: %s", err)

    mushroom_js = frontend_dir / "mushroom.js"
    if mushroom_js.is_file():
        try:
            add_extra_js_url(hass, f"{static_url}/mushroom.js")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not add mushroom.js extra URL: %s", err)

    # Also register in Lovelace resources so Lovelace dashboard loads them
    try:
        import uuid

        from homeassistant.helpers.storage import Store

        store = Store(hass, 1, "lovelace_resources")
        data = await store.async_load() or {"items": []}
        items = data.get("items", [])
        urls = [x.get("url", "") for x in items]
        changed = False

        if plotly_js.is_file() and not any(f"{static_url}/plotly-graph-card.js" in u for u in urls):
            items.append({
                "id": uuid.uuid4().hex,
                "url": f"{static_url}/plotly-graph-card.js",
                "type": "module",
            })
            changed = True

        if mushroom_js.is_file() and not any(f"{static_url}/mushroom.js" in u for u in urls):
            items.append({
                "id": uuid.uuid4().hex,
                "url": f"{static_url}/mushroom.js",
                "type": "module",
            })
            changed = True

        if changed:
            data["items"] = items
            await store.async_save(data)
            _LOGGER.debug("Registered bundled cards in lovelace_resources")
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not register cards in lovelace_resources: %s", err)


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
    if vehicle_control and not coordinator.data.get("registrationChannels"):
        vehicle_control = []
        async_create_issue(
            hass,
            DOMAIN,
            entry.entry_id,
            is_fixable=False,
            is_persistent=False,
            severity=IssueSeverity.WARNING,
            translation_key="2fa_missing",
        )
    else:
        async_delete_issue(hass, DOMAIN, entry.entry_id)

    vehicles = coordinator.get_vehicles()
    if vehicle_control and (
        enrolled := coordinator.get_enrolled_phone_data(entry.options.get("public_key"))
    ):
        for vehicle_id in vehicles:
            if vehicle_id in enrolled[1]:
                vehicles[vehicle_id]["phone_identity_id"] = enrolled[1][vehicle_id]

    vehicle_coordinators: dict[str, VehicleCoordinator] = {}
    drive_stores: dict[str, DriveStore] = {}
    drive_trackers: dict[str, DriveTracker] = {}
    for vehicle_id in vehicles:
        coor = VehicleCoordinator(
            hass=hass, config_entry=entry, client=client, vehicle_id=vehicle_id
        )
        await coor.async_config_entry_first_refresh()
        if not coor.data:
            raise ConfigEntryNotReady("Issue loading vehicle data")
        await coor.charging_coordinator.async_config_entry_first_refresh()
        await coor.drivers_coordinator.async_config_entry_first_refresh()
        vehicle_coordinators[vehicle_id] = coor

        vehicle_info = vehicles[vehicle_id]
        vin = str(vehicle_info.get("vin", vehicle_id))
        store = DriveStore(hass=hass, vin=vin)
        tracker = DriveTracker(
            hass=hass,
            entry=entry,
            coordinator=coor,
            vehicle_info=vehicle_info,
            store=store,
        )
        await tracker.async_setup()
        drive_stores[vehicle_id] = store
        drive_trackers[vehicle_id] = tracker

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
        ATTR_DRIVE_TRACKER: drive_trackers,
        ATTR_DRIVE_STORE: drive_stores,
    }

    async def async_handle_backfill(call: ServiceCall) -> None:
        """Handle backfill historical drives service call."""
        vin = call.data.get("vin")
        days = call.data.get("days")
        dry_run = call.data.get("dry_run", False)
        db_path = call.data.get("db_path")

        target_vins: list[str] = []
        if vin:
            target_vins.append(vin)
        else:
            for entry_data in hass.data.get(DOMAIN, {}).values():
                if isinstance(entry_data, dict) and ATTR_VEHICLE in entry_data:
                    for v_info in entry_data[ATTR_VEHICLE].values():
                        v_vin = str(v_info.get("vin", ""))
                        if v_vin and v_vin not in target_vins:
                            target_vins.append(v_vin)

        if not target_vins:
            _LOGGER.warning("No Rivian vehicles configured to backfill")
            return

        for target_vin in target_vins:
            _LOGGER.info(
                "Starting historical drive backfill for VIN %s (dry_run=%s)",
                target_vin,
                dry_run,
            )
            matched_store = None
            matched_tracker = None
            for entry_data in hass.data.get(DOMAIN, {}).values():
                if isinstance(entry_data, dict):
                    trackers = entry_data.get(ATTR_DRIVE_TRACKER, {})
                    stores = entry_data.get(ATTR_DRIVE_STORE, {})
                    for v_id, trk in trackers.items():
                        if trk.vin == target_vin:
                            matched_tracker = trk
                            matched_store = stores.get(v_id) or trk.store
                            break

            await async_backfill_from_recorder(
                hass=hass,
                vin=target_vin,
                days=days,
                dry_run=dry_run,
                db_path=db_path,
                store=matched_store,
            )

            if not dry_run and matched_tracker is not None:
                await matched_tracker.store.async_load()
                matched_tracker._notify_listeners()

    async def async_handle_create_dashboard(call: ServiceCall) -> None:
        """Handle the service call to create or update the turnkey efficiency dashboard."""
        title = call.data.get("title", DEFAULT_TITLE)
        icon = call.data.get("icon", DEFAULT_ICON)
        url_path = call.data.get("url_path", DEFAULT_URL_PATH)
        await async_create_efficiency_dashboard(
            hass=hass,
            title=title,
            icon=icon,
            url_path=url_path,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL_DRIVE_HISTORY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_BACKFILL_DRIVE_HISTORY,
            async_handle_backfill,
            schema=BACKFILL_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_CREATE_EFFICIENCY_DASHBOARD):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CREATE_EFFICIENCY_DASHBOARD,
            async_handle_create_dashboard,
            schema=CREATE_DASHBOARD_SERVICE_SCHEMA,
        )

    await _async_register_frontend(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    if drive_trackers := entry_data.get(ATTR_DRIVE_TRACKER):
        for tracker in drive_trackers.values():
            await tracker.async_unload()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    api: Rivian | None = entry_data.get(ATTR_API)
    if api:
        await api.close()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    if not hass.data.get(DOMAIN) and hass.services.has_service(
        DOMAIN, SERVICE_BACKFILL_DRIVE_HISTORY
    ):
        hass.services.async_remove(DOMAIN, SERVICE_BACKFILL_DRIVE_HISTORY)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    if public_key := entry.options.get("public_key"):
        client = get_rivian_api_from_entry(hass, entry)
        coordinator = UserCoordinator(
            hass=hass, config_entry=entry, client=client, include_phones=True
        )
        await coordinator.async_config_entry_first_refresh()

        if enrolled_data := coordinator.get_enrolled_phone_data(public_key=public_key):
            for identity_id in enrolled_data[1].values():
                await client.disenroll_phone(identity_id=identity_id)
        await client.close()


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
