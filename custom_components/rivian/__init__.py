"""Rivian (Unofficial)"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
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
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
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
from .const import (
    ATTR_ANALYTICS_DB,
    CONF_ANALYTICS_RETENTION_DAYS,
    CONF_DRIVE_ANALYTICS,
    CONF_TRACK_FULL_DETAIL_DAYS,
    CONF_TRACK_RETENTION_DAYS,
    CONF_VEHICLE_CONTROL,
    CONF_WEATHER_LOOKUPS,
    DEFAULT_ANALYTICS_RETENTION_DAYS,
    DEFAULT_DRIVE_ANALYTICS,
    DEFAULT_TRACK_FULL_DETAIL_DAYS,
    DEFAULT_TRACK_RETENTION_DAYS,
    DEFAULT_WEATHER_LOOKUPS,
    DOMAIN,
    ISSUE_ANALYTICS_UNAVAILABLE,
    ISSUE_STATE_VALUES_CHANGED,
    ISSUE_URL,
    PRECONDITION_SCHEDULE_NAME,
    RIVIAN_ANALYTICS_UPDATED_EVENT,
    VERSION,
)
from .coordinator import (
    DEPARTURE_EMPTY_LIST_HOLD,
    RivianConfigEntry,
    RivianData,
    UserCoordinator,
    VehicleCoordinator,
    WallboxCoordinator,
    is_precondition_schedule,
)
from .drive_storage import (
    ANALYTICS_BACKGROUND_TASKS_KEY,
    ANALYTICS_TASKS_KEY,
    DriveStore,
    async_cancel_background_analytics_tasks,
    async_track_analytics_task,
    async_wait_for_analytics_tasks,
)
from .drive_tracker import DriveEvent, DriveTracker
from .frontend import async_register_cards
from .helpers import (
    departure_mutation_succeeded,
    departure_schedules_from_message,
    get_rivian_api_from_entry,
    redact_vin,
)
from .history_backfill import async_backfill_from_recorder
from .services import async_setup_services
from .statistics import STATISTICS_REWRITE_LOCKS_KEY, async_rewrite_statistics
from .weather import OpenMeteoWeatherClient
from .websocket_api import async_register_websocket_api

SERVICE_BACKFILL_DRIVE_HISTORY = "backfill_drive_history"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Instance-wide state stored directly under hass.data[DOMAIN]; each config
# entry's own data lives in entry.runtime_data.
_ANALYTICS_DB_LOCK_KEY: Final = "_analytics_db_lock"
_FINAL_WRITE_UNSUB_KEY: Final = "_analytics_final_write_unsub"
# How long closing the database (last unload, removal, final write) waits for
# the must-finish analytics tasks; background ones are cancelled first
CLOSE_TASK_TIMEOUT_SECONDS: Final = 30
# When the analytics database wouldn't open (locked, disk full, I/O error),
# opening it is retried after these delays, the last one repeating; once it
# opens, the entry reloads to start analytics. The attempt count outlives that
# reload (keyed by entry id), so a lock that keeps coming back can't reload the
# entry every minute.
ANALYTICS_RETRY_DELAYS_S: Final = (60, 120, 300, 600)
_ANALYTICS_RETRY_ATTEMPTS_KEY: Final = "_analytics_retry_attempts"

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
# How long to wait for a vehicle's departure schedules when removing the entry
DEPARTURE_SCHEDULES_WAIT = 10


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
            # Config entries aren't unloaded on shutdown, so close the
            # database (leaving the clean-shutdown mark) once the trackers
            # have flushed their checkpoints on the stop event.
            domain_data[_FINAL_WRITE_UNSUB_KEY] = hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_FINAL_WRITE,
                partial(_async_close_analytics_on_final_write, hass),
            )
    return db


async def _async_close_analytics_on_final_write(
    hass: HomeAssistant, _event: Any = None
) -> None:
    """Close the shared database when Home Assistant shuts down."""
    domain_data = hass.data.get(DOMAIN, {})
    # A listen-once listener is already gone once it fired; drop our handle so
    # nothing tries to remove it again
    domain_data.pop(_FINAL_WRITE_UNSUB_KEY, None)
    db: AnalyticsDatabase | None = domain_data.pop(ATTR_ANALYTICS_DB, None)
    if db is None:
        return
    await _async_close_analytics_db(hass, db)


async def _async_close_analytics_db(hass: HomeAssistant, db: AnalyticsDatabase) -> None:
    """Stop the in-flight analytics tasks, then close the database.

    The caller has already removed ``db`` from ``hass.data``. Background tasks
    (backfills, recomputes: safe to redo, and the ones Home Assistant cancels
    when it stops) are cancelled, so a reload never waits minutes for one.
    Must-finish tasks (the statistics rewrite) get up to
    ``CLOSE_TASK_TIMEOUT_SECONDS``; after that the database closes anyway and
    the stragglers fail against it.
    """
    if cancelled := await async_cancel_background_analytics_tasks(hass):
        _LOGGER.debug("Cancelled %s background analytics task(s)", cancelled)
    timeout = CLOSE_TASK_TIMEOUT_SECONDS
    if not await async_wait_for_analytics_tasks(hass, timeout):
        _LOGGER.warning(
            "Analytics tasks still running after %s s; closing the database anyway",
            timeout,
        )
    await hass.async_add_executor_job(db.close)


def _remove_final_write_listener(hass: HomeAssistant) -> None:
    """Remove the final-write listener if it hasn't fired."""
    if unsub := hass.data.get(DOMAIN, {}).pop(_FINAL_WRITE_UNSUB_KEY, None):
        unsub()


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
    statistics write happens later on the event loop via the scheduled task,
    which unload and shutdown wait for (``async_track_analytics_task``) rather than
    cancel.
    """

    @callback
    def _on_drive_event(_drive_state: Any, event: DriveEvent) -> None:
        if event != DriveEvent.DRIVE_COMPLETE:
            return

        hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": vin})

        last_drive = store.last_drive
        start = _drive_hour_start_ts(last_drive) if last_drive is not None else None
        if start is not None:
            async_track_analytics_task(
                hass,
                async_rewrite_statistics(hass, store, start),
                f"rivian_statistics_update_{vin}",
                background=False,
            )

    return _on_drive_event


async def _async_prune_analytics_retention(
    hass: HomeAssistant, entry: RivianConfigEntry, _now: Any = None
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

    for store in entry.runtime_data.drive_stores.values():
        if retention_days:
            try:
                removed = await store.async_prune(retention_days)
                if removed:
                    _LOGGER.debug(
                        "Pruned %d analytics rows for VIN %s (retention=%d days)",
                        removed,
                        redact_vin(store.vin),
                        retention_days,
                    )
            except Exception as err:  # noqa: BLE001 - a prune failure must not crash HA
                _LOGGER.warning(
                    "Analytics retention prune failed for VIN %s: %s",
                    redact_vin(store.vin),
                    err,
                )

        try:
            track_counts = await store.async_prune_tracks(
                track_retention_days, track_full_detail_days
            )
            if track_counts and (
                track_counts.get("deleted") or track_counts.get("thinned")
            ):
                _LOGGER.debug(
                    "Pruned/thinned GPS tracks for VIN %s: %s",
                    redact_vin(store.vin),
                    track_counts,
                )
        except Exception as err:  # noqa: BLE001 - a prune failure must not crash HA
            _LOGGER.warning(
                "Track retention prune failed for VIN %s: %s",
                redact_vin(store.vin),
                err,
            )


def _tracked_service(
    hass: HomeAssistant,
    handler: Callable[[HomeAssistant, ServiceCall], Coroutine[Any, Any, None]],
) -> Callable[[ServiceCall], Coroutine[Any, Any, None]]:
    """Run an analytics service handler as a tracked task.

    Unloading the last entry then stops it (it's a background task, safe to
    run again) before closing the database, rather than closing the database
    under it. Shielded, so cancelling the service call itself doesn't stop the
    work half-way.
    """

    async def _async_run(call: ServiceCall) -> None:
        task = async_track_analytics_task(
            hass, handler(hass, call), f"rivian service {call.service}"
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and (current is None or not current.cancelling()):
                # The unload stopped the work, not a cancel of this call
                raise HomeAssistantError(
                    f"rivian.{call.service} stopped because the Rivian "
                    "integration was unloaded; run it again once it's loaded"
                ) from None
            raise

    return _async_run


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Rivian integration."""
    async_setup_services(hass)
    # The analytics services, once per Home Assistant instance
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_BACKFILL_DRIVE_HISTORY,
        _tracked_service(hass, _async_handle_backfill),
        schema=BACKFILL_SERVICE_SCHEMA,
    )
    try:
        await async_register_cards(hass)
    except Exception:  # the vehicles still set up without the dashboard cards
        _LOGGER.exception("Couldn't load the Rivian dashboard cards")
    return True


def _analytics_trackers(hass: HomeAssistant) -> dict[str, DriveTracker]:
    """Return the drive trackers of every loaded entry, keyed by VIN."""
    return {
        tracker.vin: tracker
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        for tracker in entry.runtime_data.drive_trackers.values()
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
            redact_vin(tracker.vin),
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


async def async_setup_entry(hass: HomeAssistant, entry: RivianConfigEntry) -> bool:
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

    # One Open-Meteo client per entry, shared by the Outside temperature
    # sensor and drive analytics so a spot is looked up once for both
    weather_client = OpenMeteoWeatherClient(
        hass=hass,
        enabled=entry.options.get(CONF_WEATHER_LOOKUPS, DEFAULT_WEATHER_LOOKUPS),
    )
    drive_stores: dict[str, DriveStore] = {}
    drive_trackers: dict[str, DriveTracker] = {}
    issue_id = f"{ISSUE_ANALYTICS_UNAVAILABLE}_{entry.entry_id}"
    if entry.options.get(CONF_DRIVE_ANALYTICS, DEFAULT_DRIVE_ANALYTICS):
        try:
            drive_stores, drive_trackers = await _async_setup_analytics(
                hass, entry, vehicles, vehicle_coordinators, weather_client
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
            if ATTR_ANALYTICS_DB not in hass.data[DOMAIN]:
                # The database didn't open, which often clears on its own (a
                # lock someone else held, a disk that filled up for a moment)
                _async_schedule_analytics_retry(hass, entry)
        else:
            async_delete_issue(hass, DOMAIN, issue_id)
            _analytics_retry_attempts(hass).pop(entry.entry_id, None)
    else:
        async_delete_issue(hass, DOMAIN, issue_id)
        _analytics_retry_attempts(hass).pop(entry.entry_id, None)

    entry.runtime_data = RivianData(
        api=client,
        vehicles=vehicles,
        user_coordinator=coordinator,
        vehicle_coordinators=vehicle_coordinators,
        wallbox_coordinator=wallbox_coordinator,
        weather_client=weather_client,
        drive_trackers=drive_trackers,
        drive_stores=drive_stores,
    )

    # The wallbox refresh above was the last step that can raise
    # ConfigEntryNotReady, so a retry never leaves a started tracker behind
    # (its stop listener would outlive the failed setup).
    try:
        if drive_trackers:
            async_register_websocket_api(hass)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # Trackers aren't unloaded by HA on a failed setup. The shared database
        # stays open for the retry, which reuses it (it closes at final write).
        for tracker in drive_trackers.values():
            try:
                await tracker.async_unload()
            except Exception:
                _LOGGER.exception(
                    "Error stopping drive tracker %s", redact_vin(tracker.vin)
                )
        raise

    entry.async_on_unload(entry.add_update_listener(update_listener))

    if drive_stores:

        @callback
        def _run_retention_prune(now: Any = None) -> None:
            # Tracked, so unloading the last entry waits for a running prune
            async_track_analytics_task(
                hass,
                _async_prune_analytics_retention(hass, entry, now),
                f"rivian retention prune {entry.entry_id}",
            )

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


def _analytics_retry_attempts(hass: HomeAssistant) -> dict[str, int]:
    """Return the per-entry count of failed attempts to open the database."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        _ANALYTICS_RETRY_ATTEMPTS_KEY, {}
    )


@callback
def _async_schedule_analytics_retry(
    hass: HomeAssistant, entry: RivianConfigEntry
) -> None:
    """Keep trying to open the analytics database, then reload the entry.

    The rest of the integration keeps running meanwhile. Opening the database is
    all a retry does; once it opens, reloading the entry starts analytics
    through the usual setup (and creates the drive sensors and picture). The
    timer stops when the entry unloads.
    """
    attempts = _analytics_retry_attempts(hass)
    cancel: Callable[[], None] | None = None
    stopped = False

    @callback
    def _arm() -> None:
        nonlocal cancel
        attempt = attempts.get(entry.entry_id, 0)
        delay = ANALYTICS_RETRY_DELAYS_S[
            min(attempt, len(ANALYTICS_RETRY_DELAYS_S) - 1)
        ]
        cancel = async_call_later(hass, delay, _async_retry)

    async def _async_retry(_now: Any = None) -> None:
        nonlocal cancel
        cancel = None
        attempts[entry.entry_id] = attempts.get(entry.entry_id, 0) + 1
        try:
            await _async_get_or_create_analytics_db(hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Analytics database still unavailable: %s", err)
            if not stopped:
                _arm()
            return
        if stopped:  # the entry unloaded meanwhile
            return
        _LOGGER.info(
            "The analytics database opened; reloading to start drive analytics"
        )
        hass.config_entries.async_schedule_reload(entry.entry_id)

    @callback
    def _stop() -> None:
        nonlocal cancel, stopped
        stopped = True
        if cancel is not None:
            cancel()
            cancel = None

    entry.async_on_unload(_stop)
    _arm()


async def _async_setup_analytics(
    hass: HomeAssistant,
    entry: RivianConfigEntry,
    vehicles: dict[str, dict[str, Any]],
    vehicle_coordinators: dict[str, VehicleCoordinator],
    weather_client: OpenMeteoWeatherClient,
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
                weather_client=weather_client,
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


async def async_unload_entry(hass: HomeAssistant, entry: RivianConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    data = entry.runtime_data
    # The drive trackers read the vehicle coordinators, so they stop first
    for tracker in data.drive_trackers.values():
        try:
            await tracker.async_unload()
        except Exception:
            # the others still stop, and the client still closes
            _LOGGER.exception(
                "Error stopping drive tracker %s", redact_vin(tracker.vin)
            )

    # Stop the vehicles' background work, such as a precondition cleanup, before
    # closing the client it uses. Home Assistant shuts them down again after this
    # returns, which does nothing more
    for coordinator in data.vehicle_coordinators.values():
        try:
            await coordinator.async_shutdown()
        except Exception:
            # the others still stop, and the client still closes
            _LOGGER.exception("Error stopping vehicle %s", coordinator.vehicle_id)
    await data.api.close()

    # Home Assistant doesn't count the unloading entry as loaded, but leave it
    # out explicitly
    if not any(
        other.entry_id != entry.entry_id
        for other in hass.config_entries.async_loaded_entries(DOMAIN)
    ):
        # No other loaded entry: this is the last one out, so tear down the
        # shared, instance-wide state (the websocket API flag and the
        # AnalyticsDatabase connection itself).
        domain_data = hass.data.get(DOMAIN, {})
        db: AnalyticsDatabase | None = domain_data.pop(ATTR_ANALYTICS_DB, None)
        domain_data.pop(_ANALYTICS_DB_LOCK_KEY, None)
        domain_data.pop("_ws_api_registered", None)
        _remove_final_write_listener(hass)
        if db is not None:
            await _async_close_analytics_db(hass, db)
        domain_data.pop(ANALYTICS_TASKS_KEY, None)
        domain_data.pop(ANALYTICS_BACKGROUND_TASKS_KEY, None)
        domain_data.pop(STATISTICS_REWRITE_LOCKS_KEY, None)
        # Kept while a retry is pending, so the reload it causes keeps counting
        if not domain_data.get(_ANALYTICS_RETRY_ATTEMPTS_KEY):
            domain_data.pop(_ANALYTICS_RETRY_ATTEMPTS_KEY, None)
        if not domain_data:
            hass.data.pop(DOMAIN, None)

    return True


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
    domain_data.get(_ANALYTICS_RETRY_ATTEMPTS_KEY, {}).pop(entry.entry_id, None)
    others = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ]
    if not others:
        db: AnalyticsDatabase | None = domain_data.pop(ATTR_ANALYTICS_DB, None)
        _remove_final_write_listener(hass)
        if db is not None:
            await _async_close_analytics_db(hass, db)
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
    """Handle removal of an entry.

    Deletes HA's temporary precondition schedules, which nothing would delete once
    the integration is gone, so they'd precondition again every week, then
    disenrolls the phone. Also deletes the entry's drive analytics.
    """
    try:
        await _async_remove_analytics(hass, entry)
    except Exception:
        _LOGGER.exception("Could not delete the removed entry's drive analytics")

    client = get_rivian_api_from_entry(hass, entry)
    coordinator = UserCoordinator(
        hass=hass, config_entry=entry, client=client, include_phones=True
    )
    try:
        # The entry is no longer in setup, so first_refresh isn't allowed here
        await coordinator.async_refresh()
        if not coordinator.last_update_success:
            _LOGGER.warning(
                "Unable to fetch user data to clean up; delete any departure "
                "schedules named %s in the Rivian app",
                PRECONDITION_SCHEDULE_NAME,
            )
            return

        async def remove_preconditions(vehicle_id: str) -> None:
            try:
                await _async_remove_preconditions(client, vehicle_id)
            except Exception:
                _LOGGER.exception(
                    "Unable to delete vehicle %s's precondition schedules", vehicle_id
                )

        try:
            # schedule changes need it; disenrolling the phone doesn't
            await client.create_csrf_token()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Unable to delete precondition schedules; delete any named %s in the "
                "Rivian app: %s",
                PRECONDITION_SCHEDULE_NAME,
                type(err).__name__,
            )
        else:
            # at once, as each waits for its vehicle's schedule list
            await asyncio.gather(
                *(
                    remove_preconditions(vehicle_id)
                    for vehicle_id in coordinator.get_vehicles()
                )
            )
        if (public_key := entry.options.get("public_key")) and (
            enrolled_data := coordinator.get_enrolled_phone_data(public_key)
        ):
            for identity_id in enrolled_data[1].values():
                await client.disenroll_phone(identity_id=identity_id)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Unable to clean up after removal: %s", err)
    finally:
        await client.close()


async def _async_remove_preconditions(client: Rivian, vehicle_id: str) -> None:
    """Delete a vehicle's temporary precondition schedules."""
    if (schedules := await _async_departure_schedules(client, vehicle_id)) is None:
        _LOGGER.warning(
            "Unable to get vehicle %s's departure schedules; delete any named %s "
            "in the Rivian app",
            vehicle_id,
            PRECONDITION_SCHEDULE_NAME,
        )
        return
    for schedule in schedules:
        if not is_precondition_schedule(schedule):
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
            _LOGGER.warning(
                "Unable to delete vehicle %s's precondition schedule; delete it in "
                "the Rivian app: %s",
                vehicle_id,
                type(err).__name__,
            )


async def _async_departure_schedules(
    client: Rivian, vehicle_id: str
) -> list[dict[str, Any]] | None:
    """Return a vehicle's departure schedules, which are only sent by subscription.

    Rivian can send an empty list just before the full one, so like the
    coordinator, an empty list only counts if no other list follows within
    DEPARTURE_EMPTY_LIST_HOLD. Returns None if no list arrived within
    DEPARTURE_SCHEDULES_WAIT, or subscribing failed.
    """
    received: asyncio.Future[list[dict[str, Any]]] = (
        asyncio.get_running_loop().create_future()
    )
    empty = asyncio.Event()

    def on_schedules(data: dict[str, Any]) -> None:
        if (schedules := departure_schedules_from_message(data)) is None:
            return
        if schedules and not received.done():
            received.set_result(schedules)
        elif not schedules:
            empty.set()

    unsubscribe = await client.subscribe_for_departure_schedules(
        vehicle_id, on_schedules
    )
    if not unsubscribe:
        return None
    try:
        async with asyncio.timeout(DEPARTURE_SCHEDULES_WAIT):
            # a full list, or an empty one that nothing follows within the hold
            empty_wait = asyncio.ensure_future(empty.wait())
            try:
                await asyncio.wait(
                    (received, empty_wait), return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                empty_wait.cancel()
        if received.done():
            return received.result()
        try:
            return await asyncio.wait_for(received, DEPARTURE_EMPTY_LIST_HOLD)
        except TimeoutError:
            return []
    except TimeoutError:
        return None
    finally:
        await unsubscribe()


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: RivianConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    data = config_entry.runtime_data
    vehicles = data.user_coordinator.get_vehicles().keys()
    wallboxes = {x["wallboxId"] for x in data.wallbox_coordinator.data}

    return not any(
        identifier
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN and identifier[1] in vehicles | wallboxes
    )
