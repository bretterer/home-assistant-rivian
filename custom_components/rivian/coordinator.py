"""Data update coordinator for the Rivian integration."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
import logging
import secrets
import time
from typing import Any, Generic, TypeVar

from aiohttp import ClientResponse
from rivian import Rivian, VehicleCommand
from rivian.exceptions import (
    RivianApiException,
    RivianApiRateLimitError,
    RivianExpiredTokenError,
    RivianUnauthenticated,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util

from .const import (
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    CHARGING_API_FIELDS,
    DEFAULT_CHARGING_SCHEDULE,
    DEFAULT_DEPARTURE_SCHEDULE,
    DEFAULT_PRECONDITION_LEAD_MINUTES,
    DEFAULT_PRECONDITION_TEMPERATURE,
    DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM,
    DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM,
    DOMAIN,
    INVALID_SENSOR_STATES,
    PRECONDITION_SCHEDULE_NAME,
    VEHICLE_STATE_API_FIELDS,
    WEEK_DAYS_ORDERED,
)
from .helpers import deep_merge, departure_schedule_to_input, redact

_LOGGER = logging.getLogger(__name__)
T = TypeVar("T", bound=dict[str, Any] | list[dict[str, Any]])

# Maximum time to wait for the first vehicle state to arrive after subscribing.
# The first `_process_new_data` callback has been observed ~27s after the
# subscription is established, so this needs meaningful headroom.
INITIAL_UPDATE_TIMEOUT = 60
CHARGING_SCHEDULE_COOL_OFF = 10
CHARGING_SCHEDULE_REFRESH_INTERVAL = 900
# How long to wait for the subscription to deliver the refreshed schedule list
# after a mutation before letting the next action proceed.
DEPARTURE_REFRESH_TIMEOUT = 10
# Grace period after a temporary precondition schedule's departure before it is
# deleted, so a following week's occurrence never fires.
PRECONDITION_CLEANUP_GRACE_SECONDS = 2 * 60
# A pending creation whose id never appears this long after its expiry is abandoned,
# so a create that silently produced no schedule does not linger forever.
PRECONDITION_PENDING_MAX_AGE_SECONDS = 24 * 60 * 60
PRECONDITION_STORE_VERSION = 1


class RivianDataUpdateCoordinator(DataUpdateCoordinator[T], ABC, Generic[T]):
    """Data update coordinator for the Rivian integration."""

    key: str
    _update_interval_seconds = 30
    _error_count = 0

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, client: Rivian
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=(
                timedelta(seconds=self._update_interval_seconds)
                if self._update_interval_seconds
                else None
            ),
            always_update=False,
        )
        self.api = client

    def _set_update_interval(self, seconds: float | None = None) -> None:
        """Set the update interval or calculate new one based on errors."""
        if not seconds:
            seconds = min(self._update_interval_seconds * 2**self._error_count, 900)
        if self._update_interval_seconds != seconds:
            refresh = self.update_interval and self._update_interval_seconds > seconds
            self.update_interval = timedelta(seconds=seconds)
            if refresh and self.data:
                task = self.async_request_refresh()
                self.config_entry.async_create_task(self.hass, task)
            else:
                self._schedule_refresh()
            _LOGGER.info("Polling set to %s seconds", seconds)

    async def _async_update_data(self) -> T:
        """Get the latest data from Rivian."""
        try:
            resp = await self._fetch_data()
            if resp.status == 200:
                data = await resp.json()
                _LOGGER.debug(
                    "[%s] %s",
                    self.__class__.__name__.replace("Coordinator", ""),
                    redact(data),
                )
                if self._error_count:
                    self._error_count = 0
                    self._set_update_interval()
                return data["data"][self.key]
            resp.raise_for_status()

        except RivianExpiredTokenError:
            _LOGGER.info("Rivian token expired, refreshing")
            await self.api.create_csrf_token()
            return await self._async_update_data()
        except RivianApiRateLimitError as err:
            _LOGGER.error("Rate limit being enforced: %s", err, exc_info=1)
            self._set_update_interval()
        except RivianUnauthenticated as err:
            await self.api.close()
            raise ConfigEntryAuthFailed from err
        except RivianApiException as ex:
            _LOGGER.error("Rivian api exception: %s", ex, exc_info=1)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error(
                "Unknown Exception while updating Rivian data: %s", ex, exc_info=1
            )

        self._error_count += 1
        if self.data:
            return self.data
        raise UpdateFailed("Error communicating with API")

    @abstractmethod
    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        raise NotImplementedError


class ChargingCoordinator(RivianDataUpdateCoordinator[dict[str, Any]]):
    """Charging data update coordinator for Rivian."""

    key = "getLiveSessionData"
    _unplugged_interval = 15 * 60  # 15 minutes
    _plugged_interval = 30  # 30 seconds
    _update_interval_seconds = _unplugged_interval  # 15 minutes

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: Rivian,
        vehicle_id: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass=hass, config_entry=config_entry, client=client)
        self.vehicle_id = vehicle_id

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_live_charging_session(
            vin=self.vehicle_id, properties=CHARGING_API_FIELDS
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Get the latest data from Rivian, gracefully handling deprecated endpoint failures."""
        try:
            return await super()._async_update_data()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Live charging session endpoint error: %s", err)
            return self.data or {}

    def adjust_update_interval(self, is_plugged_in: bool) -> None:
        """Adjust update interval based on plugged in status."""
        self._set_update_interval(
            self._plugged_interval if is_plugged_in else self._unplugged_interval
        )


class DriverKeyCoordinator(RivianDataUpdateCoordinator[dict[str, Any]]):
    """Drivers/keys data update coordinator for Rivian."""

    key = "getVehicle"
    _update_interval_seconds = 15 * 60  # 15 minutes

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: Rivian,
        vehicle_id: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass=hass, config_entry=config_entry, client=client)
        self.vehicle_id = vehicle_id

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_drivers_and_keys(vehicle_id=self.vehicle_id)

    def get_device_details(self, identity_id: str) -> dict[str, Any] | None:
        """Get the details of a device."""
        if not self.data:
            return None
        return next(
            (
                device
                for user in self.data.get("invitedUsers")
                if user["__typename"] == "ProvisionedUser"
                for device in user["devices"]
                if device["mappedIdentityId"] == identity_id
            ),
            None,
        )


class UserCoordinator(RivianDataUpdateCoordinator[dict[str, Any]]):
    """User data update coordinator for Rivian."""

    key = "currentUser"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: Rivian,
        include_phones: bool = False,
    ) -> None:
        super().__init__(hass=hass, config_entry=config_entry, client=client)
        self.include_phones = include_phones

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_user_information(self.include_phones)

    def get_enrolled_phone_data(
        self, public_key: str
    ) -> tuple[str, dict[str, str]] | None:
        """Get enrolled phone data."""
        phones = self.data.get("enrolledPhones", [])
        if phone := next(
            (phone for phone in phones if phone["vas"]["publicKey"] == public_key), None
        ):
            phone_id = phone["vas"]["vasPhoneId"]
            vehicle_entry = {
                entry["vehicleId"]: entry["identityId"] for entry in phone["enrolled"]
            }
            return (phone_id, vehicle_entry)
        return None

    def get_vehicles(self) -> dict[str, dict[str, Any]]:
        """Get the user's vehicles."""
        return {
            vehicle["id"]: vehicle["vehicle"]
            | {
                "name": vehicle["name"],
                "supported_features": [
                    supported_feature.get("name")
                    for supported_feature in vehicle.get("vehicle", {})
                    .get("vehicleState", {})
                    .get("supportedFeatures", [])
                    if supported_feature.get("status") == "AVAILABLE"
                ],
                "vas_id": (vas := vehicle.get("vas", {})).get("vasVehicleId"),
                "public_key": vas.get("vehiclePublicKey"),
            }
            for vehicle in self.data["vehicles"]
        }


class VehicleCoordinator(RivianDataUpdateCoordinator[dict[str, Any]]):
    """Vehicle data update coordinator for Rivian."""

    key = "vehicleState"
    _update_interval_seconds = 15 * 60  # 15 minutes

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: Rivian,
        vehicle_id: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass=hass, config_entry=config_entry, client=client)
        self.vehicle_id = vehicle_id
        self.charging_coordinator = ChargingCoordinator(
            hass=hass, config_entry=config_entry, client=client, vehicle_id=vehicle_id
        )
        self.drivers_coordinator = DriverKeyCoordinator(
            hass=hass, config_entry=config_entry, client=client, vehicle_id=vehicle_id
        )
        self._initial = asyncio.Event()
        self._unsub_handler: Coroutine[None, None, None] | None = None
        self._awake = asyncio.Event()
        self._charging_schedule: dict[str, Any] | None = None
        self._last_schedule_fetch: float = 0.0
        self._departure_schedules: list[dict[str, Any]] | None = None
        self._unsub_departure_handler: Coroutine[None, None, None] | None = None
        # serialize schedule mutations and wait for the refreshed list between them;
        # _departure_stale marks the cache unconfirmed after a write whose refresh we
        # never saw, so the next read forces a fresh list before merging
        self._departure_lock = asyncio.Lock()
        self._departure_refreshed = asyncio.Event()
        self._departure_stale = False
        # temporary schedules this integration's precondition button created: confirmed
        # ids mapped to the epoch after which they may be deleted, plus pending creations
        # whose id has not been seen yet (each keeps the pre-existing same-named ids so a
        # user's/app's schedule is never adopted). Persisted so cleanup survives a
        # restart and only ever removes our own schedules.
        self._precondition_store: Store[dict[str, Any]] = Store(
            hass, PRECONDITION_STORE_VERSION, f"{DOMAIN}.{vehicle_id}.precondition"
        )
        self._precondition_ids: dict[str, float] = {}
        self._precondition_pending: list[dict[str, Any]] = []
        self._precondition_loaded = False
        self._precondition_cleanup_lock = asyncio.Lock()
        # the in-flight reconcile task, tracked so overlapping schedules collapse into one
        # and a pending pass can be cancelled cleanly on shutdown
        self._reconcile_task: asyncio.Task[None] | None = None
        self.precondition_lead_minutes: int = DEFAULT_PRECONDITION_LEAD_MINUTES
        self.precondition_temperature: float = DEFAULT_PRECONDITION_TEMPERATURE

    @property
    def charging_schedule(self) -> dict[str, Any]:
        """Return the charging schedule or empty dict."""
        return self._charging_schedule or {}

    async def get_charging_schedule_data(
        self, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Fetch charging schedule via Rivian API."""
        now = time.time()
        cooldown = (
            CHARGING_SCHEDULE_COOL_OFF
            if force_refresh
            else CHARGING_SCHEDULE_REFRESH_INTERVAL
        )
        if self._charging_schedule is None or (
            now - self._last_schedule_fetch > cooldown
        ):
            self._last_schedule_fetch = now
            try:
                response = await self.api.get_charging_schedules(self.vehicle_id)
                res_json = await response.json()
                if (
                    res_json
                    and "data" in res_json
                    and res_json["data"].get("getVehicle")
                ):
                    schedules = res_json["data"]["getVehicle"].get(
                        "chargingSchedules", []
                    )
                    if schedules:
                        old_schedule = self._charging_schedule
                        self._charging_schedule = schedules[0]
                        if old_schedule != self._charging_schedule:
                            self.async_update_listeners()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error fetching charging schedule: %s", err)

            if self._charging_schedule is None:
                self._charging_schedule = dict(DEFAULT_CHARGING_SCHEDULE)
        return self._charging_schedule

    async def update_charging_schedule_data(self, schedule: dict[str, Any]) -> None:
        """Update charging schedule via Rivian API mutation."""
        current = dict(await self.get_charging_schedule_data(force_refresh=True))
        current.update(schedule)
        try:
            await self.api.set_charging_schedules(self.vehicle_id, [current])
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error setting charging schedule: %s", err)
        self._charging_schedule = current
        self.async_update_listeners()

    @property
    def departure_schedules(self) -> list[dict[str, Any]] | None:
        """Return the departure schedules, or None if not yet received."""
        return self._departure_schedules

    def get_departure_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        """Get a departure schedule by id."""
        return next(
            (
                schedule
                for schedule in self._departure_schedules or []
                if schedule.get("id") == schedule_id
            ),
            None,
        )

    async def create_departure_schedule(self, schedule: dict[str, Any]) -> None:
        """Create a departure schedule via Rivian API mutation."""
        async with self._departure_lock:
            await self._departure_schedule_mutation(
                "createDepartureSchedule",
                self.api.create_departure_schedule(self.vehicle_id, schedule),
            )

    async def update_departure_schedule(
        self, schedule_id: str, changes: dict[str, Any]
    ) -> None:
        """Update a departure schedule via Rivian API mutation."""
        # hold the lock across read, merge and send so a concurrent mutation cannot
        # merge its own changes against a now-stale cached schedule
        async with self._departure_lock:
            await self._ensure_fresh_departure_schedules()
            if not (current := self.get_departure_schedule(schedule_id)):
                raise ServiceValidationError(
                    f"Departure schedule {schedule_id} not found"
                )
            schedule = deep_merge(departure_schedule_to_input(current), changes)
            await self._departure_schedule_mutation(
                "updateDepartureSchedule",
                self.api.update_departure_schedule(
                    self.vehicle_id, schedule_id, schedule
                ),
            )

    async def delete_departure_schedule(self, schedule_id: str) -> None:
        """Delete a departure schedule via Rivian API mutation."""
        async with self._departure_lock:
            await self._ensure_fresh_departure_schedules()
            if not self.get_departure_schedule(schedule_id):
                raise ServiceValidationError(
                    f"Departure schedule {schedule_id} not found"
                )
            await self._departure_schedule_mutation(
                "deleteDepartureSchedule",
                self.api.delete_departure_schedule(self.vehicle_id, schedule_id),
            )

    async def _departure_schedule_mutation(
        self, key: str, request: Coroutine[Any, Any, ClientResponse]
    ) -> None:
        """Run a departure schedule mutation and refresh the schedules."""
        try:
            response = await request
            data = await response.json()
        except RivianApiException as err:
            # the exception holds the request with its session tokens, keep those out
            errors = next(
                (
                    arg["errors"]
                    for arg in err.args
                    if isinstance(arg, dict) and arg.get("errors")
                ),
                [],
            )
            reason = (
                ", ".join(str(error.get("message")) for error in errors)
                or type(err).__name__
            )
            raise HomeAssistantError(f"Rivian rejected {key}: {reason}") from None
        if not ((data.get("data") or {}).get(key) or {}).get("success"):
            raise HomeAssistantError(f"Rivian rejected {key}: {redact(data)}")
        # the write landed but the cache no longer reflects it; mark it unconfirmed
        # *before* the interruptible refresh so a timeout or cancellation still forces
        # the next read to refresh rather than merge stale data. Only an authoritative
        # payload (in _process_departure_schedules) clears the flag.
        self._departure_stale = True
        await self._subscribe_departure_schedules(force=True)
        self._departure_refreshed.clear()
        try:
            await asyncio.wait_for(
                self._departure_refreshed.wait(), DEPARTURE_REFRESH_TIMEOUT
            )
        except TimeoutError:
            _LOGGER.debug("Timed out waiting for refreshed schedules after %s", key)

    async def _ensure_fresh_departure_schedules(self) -> None:
        """Refresh the cached schedules before a read if a prior write went unconfirmed.

        Called while holding ``_departure_lock``. Raises if a coherent list cannot be
        obtained, so a mutation never merges its changes onto a schedule that may be
        out of date with Rivian. ``_departure_stale`` is cleared only by an authoritative
        payload, so a cancellation here leaves the cache marked unconfirmed.
        """
        if not self._departure_stale:
            return
        await self._subscribe_departure_schedules(force=True)
        if not self._departure_stale:  # a payload already refreshed during subscribe
            return
        self._departure_refreshed.clear()
        try:
            await asyncio.wait_for(
                self._departure_refreshed.wait(), DEPARTURE_REFRESH_TIMEOUT
            )
        except TimeoutError as err:
            raise HomeAssistantError(
                "Departure schedules are out of sync with Rivian; please try again"
            ) from err

    async def _subscribe_departure_schedules(self, force: bool = False) -> None:
        """Subscribe to departure schedules, retrying if not currently subscribed.

        ``force`` tears down and re-establishes an existing subscription to pull a
        fresh list (used after a mutation); otherwise an active subscription is left
        in place and only a missing one is (re)established.
        """
        if self._unsub_departure_handler and not force:
            return
        if unsub := self._unsub_departure_handler:
            self._unsub_departure_handler = None
            await unsub()
        self._unsub_departure_handler = (
            await self.api.subscribe_for_departure_schedules(
                self.vehicle_id, self._process_departure_schedules
            )
        )
        if not self._unsub_departure_handler:
            _LOGGER.warning(
                "Unable to subscribe to departure schedules for %s", self.vehicle_id
            )

    @callback
    def _process_departure_schedules(self, data: dict[str, Any]) -> None:
        """Process departure schedules."""
        schedules = ((data.get("payload") or {}).get("data") or {}).get(
            "vehicleDepartureSchedules"
        )
        if not isinstance(schedules, list):
            _LOGGER.debug("Received an unknown departure schedule update: %s", data)
            return
        _LOGGER.debug(
            "Vehicle %s departure schedules: %s", self.vehicle_id, redact(schedules)
        )
        if schedules != self._departure_schedules:
            self._departure_schedules = schedules
            self.async_update_listeners()
        # a delivered list is authoritative: let a waiting mutation proceed and clear
        # the stale marker so reads no longer need to force a refresh
        self._departure_refreshed.set()
        self._departure_stale = False
        # adopt any late-arriving creation and remove our own expired schedules; only
        # schedule the async work when there is something for it to do
        if self._precondition_pending or self._precondition_ids:
            self._schedule_reconcile()

    async def precondition_now(self) -> None:
        """Start cabin preconditioning without a paired phone.

        Creates a temporary departure schedule a configurable number of minutes out,
        which the vehicle preconditions for right away. There is no keyless way to stop
        it early, so it runs until the departure time. The schedule is deleted shortly
        after so it does not repeat weekly.

        The departure minute and day are computed in Home Assistant's timezone. Rivian
        interprets them in the vehicle's local timezone, which the API does not expose
        here, so this assumes the two match. They usually do (the vehicle is near home);
        if they differ, the schedule fires at the wrong wall-clock time and may not
        precondition. Setting a longer lead time or using a full departure schedule
        avoids the mismatch.
        """
        await self._ensure_precondition_loaded()
        lead = int(self.precondition_lead_minutes)
        temperature = max(
            DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM,
            min(DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM, self.precondition_temperature),
        )
        depart = dt_util.now() + timedelta(minutes=lead)
        # give the schedule a unique name so its id can be identified unambiguously and
        # no unrelated schedule that merely shares the base name is ever claimed as ours
        name = f"{PRECONDITION_SCHEDULE_NAME} {secrets.token_hex(3)}"
        expiry = time.time() + lead * 60 + PRECONDITION_CLEANUP_GRACE_SECONDS
        schedule = deep_merge(
            DEFAULT_DEPARTURE_SCHEDULE,
            {
                "name": name,
                "isEnabled": True,
                "repeatsWeekly": {
                    "days": [WEEK_DAYS_ORDERED[depart.weekday()]],
                    "startsAtMin": depart.hour * 60 + depart.minute,
                },
                "departureSettings": {
                    "comfortSettings": {"cabinTempCelsius": temperature}
                },
            },
        )
        # persist the intent *before* the remote create so an interruption after Rivian
        # accepts it but before we record the id can still reconcile the schedule by its
        # unique name; reconciliation runs below (fast path) and on later payloads
        self._precondition_pending.append({"name": name, "expiry": expiry})
        await self._save_precondition_state()
        await self.create_departure_schedule(schedule)
        await self._reconcile_and_cleanup()
        # run cleanup once the schedule has departed so it does not fire again next week;
        # deleting does not stop preconditioning that has already started. Register the
        # timer's cancel so an unload/shutdown drops it instead of leaking a coroutine.
        cancel_timer = async_call_later(
            self.hass,
            lead * 60 + PRECONDITION_CLEANUP_GRACE_SECONDS,
            lambda _now: self._schedule_reconcile(),
        )
        self.config_entry.async_on_unload(cancel_timer)

    def _reconcile_pending(self) -> bool:
        """Adopt the id of each pending creation by its unique name; drop stale records.

        Matching is by exact name, which carries a random per-creation marker, so only
        the schedule this integration created is ever claimed — never an unrelated app
        schedule that shares the base name. A record whose name never appears is
        abandoned once well past its expiry. Returns True if state changed.
        """
        if not self._precondition_pending:
            return False
        ids_by_name: dict[str, list[str]] = {}
        for schedule in self._departure_schedules or []:
            if schedule.get("id"):
                ids_by_name.setdefault(schedule.get("name"), []).append(schedule["id"])
        now = time.time()
        changed = False
        remaining: list[dict[str, Any]] = []
        for record in self._precondition_pending:
            new_ids = [
                sid
                for sid in ids_by_name.get(record["name"], [])
                if sid not in self._precondition_ids
            ]
            if new_ids:
                for schedule_id in new_ids:
                    self._precondition_ids[schedule_id] = record["expiry"]
                changed = True
            elif now > record["expiry"] + PRECONDITION_PENDING_MAX_AGE_SECONDS:
                changed = True  # the creation never produced a schedule; give up
            else:
                remaining.append(record)
        self._precondition_pending = remaining
        return changed

    @callback
    def _schedule_reconcile(self) -> None:
        """Queue a background reconcile/cleanup pass.

        Skips scheduling while Home Assistant is stopping so the shutting-down event loop
        never tears down a pending task, and collapses overlapping requests into the
        single in-flight task (each pass re-reads current state under the cleanup lock, so
        dropping a redundant one is safe — the next payload or refresh cycle covers it).
        """
        if self.hass.is_stopping:
            return
        if self._reconcile_task and not self._reconcile_task.done():
            return
        self._reconcile_task = self.config_entry.async_create_task(
            self.hass,
            self._reconcile_and_cleanup(),
            name=f"rivian reconcile {self.vehicle_id}",
            eager_start=False,
        )

    async def _reconcile_and_cleanup(self) -> None:
        """Adopt late creations and delete our own expired schedules.

        Safe to call repeatedly (periodically, from the per-run timer, or when a payload
        arrives): only schedules we own and whose grace period has passed are removed, a
        failed deletion stays tracked for the next attempt, and a user's same-named
        schedule is never touched.
        """
        async with self._precondition_cleanup_lock:
            await self._ensure_precondition_loaded()
            if self._departure_schedules is None:
                # no authoritative list yet; a later payload reschedules this safely
                return
            changed = self._reconcile_pending()
            now = time.time()
            present_ids = {
                schedule.get("id") for schedule in self._departure_schedules or []
            }
            # forget ids that no longer exist (deleted in the app or already removed)
            for schedule_id in [
                sid for sid in self._precondition_ids if sid not in present_ids
            ]:
                del self._precondition_ids[schedule_id]
                changed = True
            expired = [
                sid
                for sid, expiry in self._precondition_ids.items()
                if expiry <= now and sid in present_ids
            ]
            for schedule_id in expired:
                try:
                    await self.delete_departure_schedule(schedule_id)
                except Exception as err:  # noqa: BLE001
                    # keep the id tracked so the next run retries the deletion
                    _LOGGER.debug("Could not delete precondition schedule: %s", err)
                else:
                    self._precondition_ids.pop(schedule_id, None)
                    changed = True
            if changed:
                await self._save_precondition_state()

    async def _ensure_precondition_loaded(self) -> None:
        """Load persisted precondition ownership state once per run."""
        if self._precondition_loaded:
            return
        stored = await self._precondition_store.async_load() or {}
        # "owned" is the current schema; "schedules" was the earlier one
        owned = stored.get("owned") or stored.get("schedules") or {}
        if isinstance(owned, dict):
            self._precondition_ids = {
                str(sid): float(expiry) for sid, expiry in owned.items()
            }
        pending = stored.get("pending") or []
        if isinstance(pending, list):
            self._precondition_pending = [
                {"name": str(record["name"]), "expiry": float(record["expiry"])}
                for record in pending
                if isinstance(record, dict) and "name" in record and "expiry" in record
            ]
        self._precondition_loaded = True

    async def _save_precondition_state(self) -> None:
        """Persist owned precondition ids and pending creations."""
        await self._precondition_store.async_save(
            {"owned": self._precondition_ids, "pending": self._precondition_pending}
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Get the latest data from Rivian."""
        await self.get_charging_schedule_data()
        if not self.data or not self.last_update_success:
            await self._unsubscribe()
            self._unsub_handler = await self.api.subscribe_for_vehicle_updates(
                vehicle_id=self.vehicle_id,
                properties=VEHICLE_STATE_API_FIELDS,
                callback=self._process_new_data,
            )

            try:
                await asyncio.wait_for(self._initial.wait(), INITIAL_UPDATE_TIMEOUT)
            except TimeoutError as err:
                raise UpdateFailed(
                    "Timed out waiting for initial vehicle data after "
                    f"{INITIAL_UPDATE_TIMEOUT}s"
                ) from err

            await self._ensure_precondition_loaded()
            # the websocket was just (re)established; refresh the departure feed too
            await self._subscribe_departure_schedules(force=True)
        else:
            # retry a previously failed departure subscription on this refresh without
            # disturbing a healthy one (the vehicle-state subscription may be fine)
            await self._subscribe_departure_schedules()

        # run on every cycle so a precondition schedule whose one-shot timer was lost to
        # a restart is still cleaned up after it expires, and failed deletions are retried
        if self._precondition_pending or self._precondition_ids:
            self._schedule_reconcile()

        return self.data

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        raise NotImplementedError("Polling VehicleState no longer allowed")

    async def async_shutdown(self) -> None:
        if self._reconcile_task and not self._reconcile_task.done():
            self._reconcile_task.cancel()
        await self._unsubscribe(True)
        return await super().async_shutdown()

    @callback
    def _process_new_data(self, data: dict[str, Any]) -> None:
        """Process new data."""
        if not (payload := data.get("payload")) or not (pdata := payload.get("data")):
            _LOGGER.error("Received an unknown subscription update: %s", data)
            self._error_count += 1
            if not self._initial.is_set() or self._error_count > 5:
                task = self._unsubscribe()
                self.config_entry.async_create_task(self.hass, task, eager_start=True)
            return
        vehicle_info = self._build_vehicle_info_dict(pdata.get(self.key, {}))
        self.async_set_updated_data(vehicle_info)
        self._error_count = 0
        self._initial.set()

    def _build_vehicle_info_dict(self, vijson: dict[str, Any]) -> dict[str, Any]:
        """Take the json output of vehicle_info and build a dictionary."""
        items = {
            k: v | ({"history": {v["value"]}} if "value" in v else {})
            for k, v in vijson.items()
            if v
        }

        if items:
            _LOGGER.debug("Vehicle %s updated: %s", self.vehicle_id, redact(items))

        if power_state := items.get("powerState"):
            if power_state.get("value") == "sleep":
                self._awake.clear()
            else:
                self._awake.set()
        if charger_status := items.get("chargerStatus"):
            self.charging_coordinator.adjust_update_interval(
                is_plugged_in=charger_status.get("value") != "chrgr_sts_not_connected"
            )

        if not (prev_items := (self.data or {})):
            return items
        if not items or prev_items == items:
            return prev_items

        new_data = prev_items | items
        for key in filter(lambda i: i != "gnssLocation", items):
            value = items[key].get("value")
            if str(value).lower() in INVALID_SENSOR_STATES and key in prev_items:
                new_data[key] = prev_items[key]
            new_data[key]["history"] |= prev_items.get(key, {}).get("history", set())

        return new_data

    async def _unsubscribe(self, close_monitor: bool = False):
        """Unsubscribe."""
        if unsub := self._unsub_handler:
            await unsub()
            self._unsub_handler = None
            self._initial.clear()
        if unsub := self._unsub_departure_handler:
            await unsub()
            self._unsub_departure_handler = None
        if close_monitor and (monitor := self.api._ws_monitor):
            await monitor.close()

    def get(self, key: str) -> Any | None:
        """Get a data value by key."""
        if entity := self.data.get(key, {}):
            return entity.get("value")
        return None

    async def send_vehicle_command(
        self, command: VehicleCommand, params: dict[str, Any] | None = None
    ) -> None:
        """Send a command to the vehicle."""
        if self.get("powerState") == "sleep" and command != VehicleCommand.WAKE_VEHICLE:
            await self.send_vehicle_command(VehicleCommand.WAKE_VEHICLE)
            try:
                await asyncio.wait_for(self._awake.wait(), 30)
            except TimeoutError:
                pass  # didn't wake-up in time, but we'll try command anyway

        entry_data = self.hass.data[DOMAIN][self.config_entry.entry_id]
        vehicle = entry_data[ATTR_VEHICLE][self.vehicle_id]
        user: UserCoordinator = entry_data[ATTR_COORDINATOR][ATTR_USER]
        phone_info = user.get_enrolled_phone_data(
            self.config_entry.options.get("public_key")
        )

        if response := await self.api.send_vehicle_command(
            command=command,
            vehicle_id=self.vehicle_id,
            phone_id=phone_info[0],
            identity_id=vehicle["phone_identity_id"],
            vehicle_key=vehicle["public_key"],
            private_key=self.config_entry.options.get("private_key"),
            params=params,
        ):
            _LOGGER.debug("%s response was: %s", command, response)


class VehicleImageCoordinator(RivianDataUpdateCoordinator[dict[str, Any]]):
    """Vehicle image data update coordinator for Rivian."""

    key = "getVehicleMobileImages"
    _update_interval_seconds = 0  # disabled
    _last_updated: datetime | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: Rivian,
        version: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass=hass, config_entry=config_entry, client=client)
        self.version = version

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        data = await self.api.get_vehicle_images(
            resolution="@3x", vehicle_version=self.version
        )
        self._last_updated = datetime.now(UTC)
        return data


class WallboxCoordinator(RivianDataUpdateCoordinator[list[dict[str, Any]]]):
    """Wallbox data update coordinator for Rivian."""

    key = "getRegisteredWallboxes"

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_registered_wallboxes()
