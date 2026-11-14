"""Rivian (Unofficial)"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, timedelta
from functools import partial
import json
import logging
import os
from typing import Any, Final

from rivian import Rivian
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .analytics_db import DEFAULT_DB_RELATIVE_PATH, AnalyticsDatabase
from .config_flow import (
    CONF_ANALYTICS_RETENTION_DAYS,
    CONF_DRIVE_ANALYTICS,
    CONF_TRACK_FULL_DETAIL_DAYS,
    CONF_TRACK_RETENTION_DAYS,
    CONF_WEATHER_LOOKUPS,
    DEFAULT_ANALYTICS_RETENTION_DAYS,
    DEFAULT_DRIVE_ANALYTICS,
    DEFAULT_TRACK_FULL_DETAIL_DAYS,
    DEFAULT_TRACK_RETENTION_DAYS,
    DEFAULT_WEATHER_LOOKUPS,
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
    ISSUE_ANALYTICS_UNAVAILABLE,
    ISSUE_STATE_VALUES_CHANGED,
    ISSUE_URL,
    RIVIAN_ANALYTICS_UPDATED_EVENT,
    VERSION,
)
from .coordinator import UserCoordinator, VehicleCoordinator, WallboxCoordinator
from .drive_storage import DriveStore
from .drive_tracker import DriveEvent, DriveTracker
from .helpers import get_rivian_api_from_entry
from .history_backfill import async_backfill_from_recorder
from .statistics import async_rewrite_statistics
from .websocket_api import async_register_websocket_api

SERVICE_BACKFILL_DRIVE_HISTORY = "backfill_drive_history"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

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


def _drive_hour_start_ts(drive: Any) -> float | None:
    """Return the POSIX start of the hour a drive started in, or None."""
    start = dt_util.parse_datetime(drive.start_time or "")
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return start.replace(minute=0, second=0, microsecond=0).timestamp()


def _make_drive_complete_listener(
    hass: HomeAssistant, vin: str, store: DriveStore
) -> Any:
    """Build a DriveTracker listener that reacts to a finished drive.

    On ``DriveEvent.DRIVE_COMPLETE`` it fires the ``rivian_analytics_updated``
    bus event (so frontend subscribers refetch) and schedules a long-term
    statistics rewrite from the drive's hour as a background task, which
    re-sums every drive in that hour (an earlier drive in the same hour stays
    in the totals and the hour's means). The listener itself is
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
        start = _drive_hour_start_ts(last_drive) if last_drive is not None else None
        if start is not None:
            hass.async_create_task(
                async_rewrite_statistics(hass, store, start),
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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's services, once per Home Assistant instance."""
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_BACKFILL_DRIVE_HISTORY,
        partial(_async_handle_backfill, hass),
        schema=BACKFILL_SERVICE_SCHEMA,
    )
    return True


def _analytics_trackers(hass: HomeAssistant) -> dict[str, DriveTracker]:
    """Return the drive trackers of every loaded entry, keyed by VIN."""
    return {
        tracker.vin: tracker
        for entry_data in _iter_entry_datas(hass)
        for tracker in entry_data.get(ATTR_DRIVE_TRACKER, {}).values()
    }


async def _async_handle_backfill(hass: HomeAssistant, call: ServiceCall) -> None:
    """Rebuild drive history from the recorder for one configured vehicle or all."""
    vin = call.data.get("vin")
    days = call.data.get("days")
    dry_run = call.data.get("dry_run", True)
    tracks = call.data.get("tracks", True)

    trackers = _analytics_trackers(hass)
    if vin:
        if vin not in trackers:
            raise ServiceValidationError(
                f"No Rivian vehicle with drive analytics turned on has VIN {vin}"
            )
        targets = [trackers[vin]]
    else:
        targets = list(trackers.values())
        if not targets:
            raise ServiceValidationError(
                "Drive analytics isn't turned on for any Rivian vehicle"
            )

    for tracker in targets:
        _LOGGER.info(
            "Starting historical drive backfill for VIN %s (dry_run=%s)",
            tracker.vin,
            dry_run,
        )
        await async_backfill_from_recorder(
            hass=hass,
            vin=tracker.vin,
            days=days,
            dry_run=dry_run,
            store=tracker.store,
            tracks=tracks,
        )
        if not dry_run:
            hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": tracker.vin})
            await tracker.async_reload_history()


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

    drive_stores: dict[str, DriveStore] = {}
    drive_trackers: dict[str, DriveTracker] = {}
    issue_id = f"{ISSUE_ANALYTICS_UNAVAILABLE}_{entry.entry_id}"
    if entry.options.get(CONF_DRIVE_ANALYTICS, DEFAULT_DRIVE_ANALYTICS):
        try:
            drive_stores, drive_trackers = await _async_setup_analytics(
                hass, entry, vehicles, vehicle_coordinators
            )
        except Exception as err:
            _LOGGER.exception("Drive analytics could not start; continuing without it")
            async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=IssueSeverity.ERROR,
                translation_key=ISSUE_ANALYTICS_UNAVAILABLE,
                translation_placeholders={"error": str(err) or type(err).__name__},
            )
        else:
            async_delete_issue(hass, DOMAIN, issue_id)
    else:
        async_delete_issue(hass, DOMAIN, issue_id)

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

    if drive_trackers:
        async_register_websocket_api(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    if drive_stores:

        async def _run_retention_prune(now: Any = None) -> None:
            await _async_prune_analytics_retention(hass, entry, now)

        entry.async_on_unload(
            async_call_later(
                hass, RETENTION_PRUNE_INITIAL_DELAY_SECONDS, _run_retention_prune
            )
        )
        entry.async_on_unload(
            async_track_time_interval(
                hass, _run_retention_prune, RETENTION_PRUNE_INTERVAL
            )
        )

    return True


async def _async_setup_analytics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    vehicles: dict[str, dict[str, Any]],
    vehicle_coordinators: dict[str, VehicleCoordinator],
) -> tuple[dict[str, DriveStore], dict[str, DriveTracker]]:
    """Open the analytics database and start a drive tracker per vehicle.

    Raises on failure, after stopping any tracker it already started, so the
    caller can carry on without analytics.
    """
    analytics_db = await _async_get_or_create_analytics_db(hass)
    _async_check_analytics_db_issues(hass, analytics_db)
    # So removing the entry can delete its vehicles' data
    vins = sorted(
        str(info.get("vin", vehicle_id)) for vehicle_id, info in vehicles.items()
    )
    await hass.async_add_executor_job(
        analytics_db.set_meta, _entry_vins_meta_key(entry.entry_id), json.dumps(vins)
    )

    drive_stores: dict[str, DriveStore] = {}
    drive_trackers: dict[str, DriveTracker] = {}
    try:
        for vehicle_id, vehicle_info in vehicles.items():
            vin = str(vehicle_info.get("vin", vehicle_id))
            store = DriveStore(
                hass=hass,
                vin=vin,
                db=analytics_db,
                weather_lookups=entry.options.get(
                    CONF_WEATHER_LOOKUPS, DEFAULT_WEATHER_LOOKUPS
                ),
            )
            tracker = DriveTracker(
                hass=hass,
                entry=entry,
                coordinator=vehicle_coordinators[vehicle_id],
                vehicle_info=vehicle_info,
                store=store,
            )
            # Listen before setup: setup may finalize a drive recovered from a
            # mid-drive checkpoint, and that drive needs statistics too.
            entry.async_on_unload(
                tracker.async_add_listener(
                    _make_drive_complete_listener(hass, vin, store)
                )
            )
            await tracker.async_setup()
            drive_stores[vehicle_id] = store
            drive_trackers[vehicle_id] = tracker
    except Exception:
        for tracker in drive_trackers.values():
            await tracker.async_unload()
        raise
    return drive_stores, drive_trackers


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
        # shared, instance-wide state (the websocket API flag and the
        # AnalyticsDatabase connection itself).
        domain_data = hass.data.get(DOMAIN, {})
        db: AnalyticsDatabase | None = domain_data.pop(ATTR_ANALYTICS_DB, None)
        domain_data.pop(_ANALYTICS_DB_LOCK_KEY, None)
        domain_data.pop("_ws_api_registered", None)
        if db is not None:
            await hass.async_add_executor_job(db.close)
        if not domain_data:
            hass.data.pop(DOMAIN, None)

    return unload_ok


def _entry_vins_meta_key(entry_id: str) -> str:
    """Return the meta key listing the VINs an entry recorded analytics for."""
    return f"entry_vins:{entry_id}"


def _delete_database_files(path: str) -> None:
    """Delete the analytics database and its WAL/SHM files. Executor-bound."""
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(f"{path}{suffix}")


async def _async_remove_analytics(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete a removed entry's analytics: the whole database with the last entry."""
    path = hass.config.path(DEFAULT_DB_RELATIVE_PATH)
    domain_data = hass.data.get(DOMAIN, {})
    others = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ]
    if not others:
        db: AnalyticsDatabase | None = domain_data.pop(ATTR_ANALYTICS_DB, None)
        if db is not None:
            await hass.async_add_executor_job(db.close)
        await hass.async_add_executor_job(_delete_database_files, path)
        return
    if not await hass.async_add_executor_job(os.path.exists, path):
        return
    db = domain_data.get(ATTR_ANALYTICS_DB)
    opened_here = db is None
    if db is None:
        db = AnalyticsDatabase(hass)
        await hass.async_add_executor_job(db.setup)
    try:
        key = _entry_vins_meta_key(entry.entry_id)
        raw = await hass.async_add_executor_job(db.get_meta, key)
        vins = set(json.loads(raw) if raw else [])
        # A vehicle another entry also records keeps its data
        for other in others:
            other_raw = await hass.async_add_executor_job(
                db.get_meta, _entry_vins_meta_key(other.entry_id)
            )
            vins -= set(json.loads(other_raw) if other_raw else [])
        for vin in sorted(vins):
            await hass.async_add_executor_job(db.delete_vin, vin)
        await hass.async_add_executor_job(db.delete_meta, key)
    finally:
        if opened_here:
            await hass.async_add_executor_job(db.close)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    try:
        await _async_remove_analytics(hass, entry)
    except Exception:
        _LOGGER.exception("Could not delete the removed entry's drive analytics")
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
