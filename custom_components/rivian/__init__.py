"""Rivian (Unofficial)"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any, Final

from rivian import Rivian
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from .analytics_db import AnalyticsDatabase
from .config_flow import (
    CONF_ANALYTICS_RETENTION_DAYS,
    CONF_TRACK_FULL_DETAIL_DAYS,
    CONF_TRACK_RETENTION_DAYS,
    DEFAULT_ANALYTICS_RETENTION_DAYS,
    DEFAULT_TRACK_FULL_DETAIL_DAYS,
    DEFAULT_TRACK_RETENTION_DAYS,
)
from .const import (
    ATTR_ANALYTICS_DB,
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
    RIVIAN_ANALYTICS_UPDATED_EVENT,
    VERSION,
)
from .coordinator import UserCoordinator, VehicleCoordinator, WallboxCoordinator
from .drive_storage import DriveStore
from .drive_tracker import DriveEvent, DriveTracker
from .helpers import get_rivian_api_from_entry
from .history_backfill import async_backfill_from_recorder
from .statistics import async_update_statistics
from .websocket_api import async_register_websocket_api

SERVICE_BACKFILL_DRIVE_HISTORY = "backfill_drive_history"

# Special (non-config-entry) keys stored directly under hass.data[DOMAIN].
_ANALYTICS_DB_LOCK_KEY: Final = "_analytics_db_lock"
_SPECIAL_DOMAIN_DATA_KEYS: Final = frozenset(
    {
        ATTR_ANALYTICS_DB,
        _ANALYTICS_DB_LOCK_KEY,
        "_ws_api_registered",
    }
)

RETENTION_PRUNE_INITIAL_DELAY_SECONDS: Final = 30
RETENTION_PRUNE_INTERVAL: Final = timedelta(hours=24)
BACKFILL_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
        vol.Optional("days"): vol.Coerce(int),
        vol.Optional("dry_run", default=True): cv.boolean,
        vol.Optional("tracks", default=True): cv.boolean,
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


def _iter_entry_datas(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return the per-config-entry data dicts stored under hass.data[DOMAIN].

    hass.data[DOMAIN] also holds special, non-entry keys (the shared
    AnalyticsDatabase, its setup lock, and the websocket API registration
    flag) alongside the entry_id-keyed dicts, so callers must filter those
    out before assuming every value is a config entry's data dict.
    """
    return [
        value
        for key, value in hass.data.get(DOMAIN, {}).items()
        if key not in _SPECIAL_DOMAIN_DATA_KEYS and isinstance(value, dict)
    ]


async def _async_get_or_create_analytics_db(hass: HomeAssistant) -> AnalyticsDatabase:
    """Return the single shared AnalyticsDatabase for this HA instance.

    Created once per Home Assistant instance (not per config entry); a second
    vehicle/account config entry reuses the already-opened instance. Guarded
    by a lock so two config entries setting up concurrently can't both open
    the database.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    lock: asyncio.Lock = domain_data.setdefault(_ANALYTICS_DB_LOCK_KEY, asyncio.Lock())
    async with lock:
        db: AnalyticsDatabase | None = domain_data.get(ATTR_ANALYTICS_DB)
        if db is None:
            db = AnalyticsDatabase(hass)
            await hass.async_add_executor_job(db.setup)
            domain_data[ATTR_ANALYTICS_DB] = db
    return db


def _async_check_analytics_db_issues(
    hass: HomeAssistant, db: AnalyticsDatabase
) -> None:
    """Raise (or clear) repair issues reflecting the shared analytics DB's health."""
    try:
        if db.was_repaired:
            async_create_issue(
                hass,
                DOMAIN,
                "analytics_db_repaired",
                is_fixable=False,
                is_persistent=True,
                severity=IssueSeverity.WARNING,
                translation_key="analytics_db_repaired",
            )
        else:
            async_delete_issue(hass, DOMAIN, "analytics_db_repaired")

        if db.read_only:
            async_create_issue(
                hass,
                DOMAIN,
                "analytics_db_read_only",
                is_fixable=False,
                is_persistent=True,
                severity=IssueSeverity.WARNING,
                translation_key="analytics_db_read_only",
            )
        else:
            async_delete_issue(hass, DOMAIN, "analytics_db_read_only")
    except Exception as err:  # noqa: BLE001 - repair issues must never break setup
        _LOGGER.debug("Could not update analytics DB repair issues: %s", err)


def _make_drive_complete_listener(
    hass: HomeAssistant, vin: str, store: DriveStore
) -> Any:
    """Build a DriveTracker listener that reacts to a finished drive.

    On ``DriveEvent.DRIVE_COMPLETE`` it fires the ``rivian_analytics_updated``
    bus event (so frontend subscribers refetch) and schedules a
    long-term statistics update as a background task. The listener itself is
    a synchronous ``@callback`` -- it never awaits -- so it cannot block the
    DriveTracker's notification loop; the actual (async, executor-bound)
    statistics write happens later on the event loop via the scheduled task.
    """

    @callback
    def _on_drive_event(_drive_state: Any, event: DriveEvent) -> None:
        if event != DriveEvent.DRIVE_COMPLETE:
            return

        hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": vin})

        last_drive = store.last_drive
        if last_drive is not None:
            hass.async_create_task(
                async_update_statistics(hass, vin, [last_drive]),
                f"rivian_statistics_update_{vin}",
            )

    return _on_drive_event


async def _async_prune_analytics_retention(
    hass: HomeAssistant, entry: ConfigEntry, _now: Any = None
) -> None:
    """Prune analytics history older than the configured retention windows.

    Drive/vampire-drain retention (``analytics_retention_days``) and GPS
    track retention/thinning (``track_retention_days``/
    ``track_full_detail_days``) are independent settings -- a retention of
    ``0`` days means "keep forever" for whichever one it applies to, so each
    prune step runs (or is skipped) on its own rather than one governing
    both. Runs on the event loop but every blocking step (the SQLite
    delete/thin and the cache rebuild) is delegated to the executor by
    ``DriveStore.async_prune``/``async_prune_tracks``.
    """
    retention_days = entry.options.get(
        CONF_ANALYTICS_RETENTION_DAYS, DEFAULT_ANALYTICS_RETENTION_DAYS
    )
    track_retention_days = entry.options.get(
        CONF_TRACK_RETENTION_DAYS, DEFAULT_TRACK_RETENTION_DAYS
    )
    track_full_detail_days = entry.options.get(
        CONF_TRACK_FULL_DETAIL_DAYS, DEFAULT_TRACK_FULL_DETAIL_DAYS
    )

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(entry_data, dict):
        return

    for store in entry_data.get(ATTR_DRIVE_STORE, {}).values():
        if retention_days:
            try:
                removed = await store.async_prune(retention_days)
                if removed:
                    _LOGGER.debug(
                        "Pruned %d analytics rows for VIN %s (retention=%d days)",
                        removed,
                        store.vin,
                        retention_days,
                    )
            except Exception as err:  # noqa: BLE001 - a prune failure must not crash HA
                _LOGGER.warning(
                    "Analytics retention prune failed for VIN %s: %s", store.vin, err
                )

        try:
            track_counts = await store.async_prune_tracks(
                track_retention_days, track_full_detail_days
            )
            if track_counts and (
                track_counts.get("deleted") or track_counts.get("thinned")
            ):
                _LOGGER.debug(
                    "Pruned/thinned GPS tracks for VIN %s: %s", store.vin, track_counts
                )
        except Exception as err:  # noqa: BLE001 - a prune failure must not crash HA
            _LOGGER.warning(
                "Track retention prune failed for VIN %s: %s", store.vin, err
            )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the saved entries."""
    _LOGGER.info(
        "Rivian integration is starting under version %s. Please report issues at %s",
        VERSION,
        ISSUE_URL,
    )

    hass.data.setdefault(DOMAIN, {})

    analytics_db = await _async_get_or_create_analytics_db(hass)
    _async_check_analytics_db_issues(hass, analytics_db)

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
        store = DriveStore(hass=hass, vin=vin, db=analytics_db)
        tracker = DriveTracker(
            hass=hass,
            entry=entry,
            coordinator=coor,
            vehicle_info=vehicle_info,
            store=store,
        )
        # Listen before setup: setup may finalize a drive recovered from a
        # mid-drive checkpoint, and that drive needs statistics too.
        entry.async_on_unload(
            tracker.async_add_listener(_make_drive_complete_listener(hass, vin, store))
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
        dry_run = call.data.get("dry_run", True)
        tracks = call.data.get("tracks", True)

        target_vins: list[str] = []
        if vin:
            target_vins.append(vin)
        else:
            for entry_data in _iter_entry_datas(hass):
                if ATTR_VEHICLE in entry_data:
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
            for entry_data in _iter_entry_datas(hass):
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
                store=matched_store,
                tracks=tracks,
            )

            if not dry_run and matched_store is not None:
                hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": target_vin})

            if not dry_run and matched_tracker is not None:
                await matched_tracker.store.async_load()
                matched_tracker._notify_listeners()

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL_DRIVE_HISTORY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_BACKFILL_DRIVE_HISTORY,
            async_handle_backfill,
            schema=BACKFILL_SERVICE_SCHEMA,
        )

    async_register_websocket_api(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    async def _run_retention_prune(now: Any = None) -> None:
        await _async_prune_analytics_retention(hass, entry, now)

    entry.async_on_unload(
        async_call_later(
            hass, RETENTION_PRUNE_INITIAL_DELAY_SECONDS, _run_retention_prune
        )
    )
    entry.async_on_unload(
        async_track_time_interval(hass, _run_retention_prune, RETENTION_PRUNE_INTERVAL)
    )

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

    if not _iter_entry_datas(hass):
        # No config entries left: this is the last one out, so tear down the
        # shared, instance-wide state (services, websocket API flag, and the
        # AnalyticsDatabase connection itself).
        if hass.services.has_service(DOMAIN, SERVICE_BACKFILL_DRIVE_HISTORY):
            hass.services.async_remove(DOMAIN, SERVICE_BACKFILL_DRIVE_HISTORY)

        domain_data = hass.data.get(DOMAIN, {})
        db: AnalyticsDatabase | None = domain_data.pop(ATTR_ANALYTICS_DB, None)
        domain_data.pop(_ANALYTICS_DB_LOCK_KEY, None)
        domain_data.pop("_ws_api_registered", None)
        if db is not None:
            await hass.async_add_executor_job(db.close)
        if not domain_data:
            hass.data.pop(DOMAIN, None)

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
