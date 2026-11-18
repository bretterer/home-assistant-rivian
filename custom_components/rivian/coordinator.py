"""Data update coordinator for the Rivian integration."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Awaitable, Callable, Coroutine
import contextlib
from datetime import UTC, datetime, time as dt_time, timedelta
from functools import partial
import logging
import time
from typing import Any, Generic, TypeVar
import uuid

from aiohttp import ClientError, ClientResponse
from rivian import Rivian, VehicleCommand
from rivian.exceptions import (
    RivianApiException,
    RivianApiRateLimitError,
    RivianExpiredTokenError,
    RivianUnauthenticated,
)
from rivian.parallax import (
    SET_CLIMATE_HOLD_RVM,
    decode_command_state_message,
    decode_parallax_message,
    encode_climate_hold,
    encode_vehicle_command,
    vehicle_command_feature,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    CHARGING_STATE_KEYS,
    DEFAULT_CHARGING_SCHEDULE,
    DEFAULT_DEPARTURE_SCHEDULE,
    DEFAULT_PRECONDITION_LEAD_MINUTES,
    DEFAULT_PRECONDITION_TEMPERATURE,
    DEFAULT_VEHICLE_STATE,
    DOMAIN,
    INVALID_SENSOR_STATES,
    ISSUE_RATE_LIMITED,
    MINUTES_PER_HOUR,
    PARALLAX_IGNORED_RVMS,
    PARALLAX_NONE_CLEARS,
    PARALLAX_NONE_VALUES,
    PARALLAX_RVM_DEFAULTS,
    PARALLAX_UNDEFINED_IS_VALID,
    PRECONDITION_LEAD_MINUTES_MAXIMUM,
    PRECONDITION_LEAD_MINUTES_MINIMUM,
    PRECONDITION_SCHEDULE_NAME,
    VEHICLE_STATE_API_FIELDS,
    WEEK_DAYS_ORDERED,
)
from .helpers import (
    deep_merge,
    departure_mutation_succeeded,
    departure_schedule_to_input,
    departure_schedules_from_message,
    redact,
    schedule_cabin_temperature,
)

_LOGGER = logging.getLogger(__name__)
T = TypeVar("T", bound=dict[str, Any] | list[dict[str, Any]])

# Maximum time to wait for the first vehicle state to arrive after subscribing.
# The first `_process_new_data` callback has been observed ~27s after the
# subscription is established, so this needs meaningful headroom.
INITIAL_UPDATE_TIMEOUT = 60
# How long to wait before subscribing again when subscribing fails without the
# web socket backing off, doubling for each such failure in a row up to the max
SUBSCRIBE_RETRY_DELAY = 60
SUBSCRIBE_RETRY_MAX_DELAY = 900
# How long the web socket can stay rate limited before offering to log in again,
# which starts a new session without the rate limit, and then before requiring it
RATE_LIMIT_ISSUE_AFTER = timedelta(minutes=30)
RATE_LIMIT_REAUTH_AFTER = timedelta(hours=6)
RATE_LIMIT_ISSUE_PLACEHOLDERS = {
    "issue_after": f"{RATE_LIMIT_ISSUE_AFTER / timedelta(minutes=1):g}",
    "reauth_after": f"{RATE_LIMIT_REAUTH_AFTER / timedelta(hours=1):g}",
}
# How long to wait for the vehicle's result for a Parallax command, which can
# include waking it up
PARALLAX_COMMAND_TIMEOUT = 90
# How long to wait for a sleeping vehicle to wake after a climate hold, then at
# least for its climate hold to start up (1-5.6 s seen after waking) before
# sending it again, and then for a hold still starting up
CLIMATE_HOLD_WAKE_WAIT = 30
CLIMATE_HOLD_START_WAIT = 8
CLIMATE_HOLD_START_UP_WAIT = 10
SIDE_BIN_COMMANDS = {
    VehicleCommand.RELEASE_LEFT_SIDE_BIN,
    VehicleCommand.RELEASE_RIGHT_SIDE_BIN,
}
CHARGING_SCHEDULE_COOL_OFF = 10
CHARGING_SCHEDULE_REFRESH_INTERVAL = 900
# How long to wait for the subscription to deliver the refreshed schedule list
# after a mutation before letting the next action proceed.
DEPARTURE_REFRESH_TIMEOUT = 10
# How long an empty schedule list after schedules is held back, as Rivian can send
# one just before the full list; it's used if no other list arrives by then
DEPARTURE_EMPTY_LIST_HOLD = 5
# How long after its departure a temporary precondition schedule counts as current,
# before it's deleted so its next weekly occurrence never fires
PRECONDITION_CLEANUP_GRACE_SECONDS = 2 * 60
# How soon cleanup runs again after a deletion failed
PRECONDITION_RETRY_SECONDS = 5 * 60
# Max age of a navigation location fix to use (trip_progress resends its last
# fix long after navigation ends)
NAVIGATION_FIX_MAX_AGE = timedelta(minutes=5)
# Fields set from the newest position fix (GNSS or navigation)
GNSS_FIX_FIELDS = ("gnssLocation", "gnssSpeed", "gnssBearing")
# GraphQL sends a position fix every few seconds while driving, where Parallax's
# dynamics.vehicle.gnss sends one about once a minute. It's a subscription of its
# own: if Rivian stops accepting the field, only that subscription fails (with an
# error message, and no data) and everything else carries on
GRAPHQL_LOCATION_FIELDS = {"gnssLocation"}
# Updates are stale after this long without either any message (the client
# resubscribes after a minute without one, and the subscriptions resend their
# snapshot, so a working connection never goes this long), or, while awake,
# newly reported data (the vehicle sends a location fix about once a minute;
# resent snapshots keep their original timestamps)
UPDATES_STALE_AFTER = timedelta(minutes=10)

# Seconds to wait for video monitoring to turn on before a live stream
GEAR_GUARD_VIDEO_WAIT = 30
# Live stream starts to try; each waits up to 40 s for the vehicle
GEAR_GUARD_LIVE_ATTEMPTS = 3


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
                # The client returns, rather than raises, errors that come with
                # partial data, which can leave this key null
                if (result := (data.get("data") or {}).get(self.key)) is None:
                    raise RivianApiException(
                        f"No {self.key} in response: {data.get('errors')}"
                    )
                if self._error_count:
                    self._error_count = 0
                    self._set_update_interval()
                return result
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
    """Charging data update coordinator for Rivian.

    This coordinator receives live charging data from Parallax protobuf
    messages decoded by the VehicleCoordinator; it never polls.
    """

    _update_interval_seconds = 0  # disabled - data is pushed via Parallax

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
        # Whether startTime was estimated from power, before the graph sent one
        self._estimated_start = False

    async def _async_update_data(self) -> dict[str, Any]:
        """Return the data received from Parallax, or an empty dict before any."""
        return self.data or {}

    async def _fetch_data(self) -> ClientResponse:
        """Charging data is pushed via Parallax, never fetched."""
        raise NotImplementedError

    @callback
    def update_from_parallax(self, decoded: dict[str, Any]) -> None:
        """Update charging data from decoded Parallax protobuf fields.

        Merges new fields into existing data and notifies listeners.
        Internal/private fields (prefixed with '_') are excluded.
        """
        # Filter out internal decoder fields
        clean = {k: v for k, v in decoded.items() if not k.startswith("_")}
        if not clean:
            return

        new_data = dict(self.data or {})

        # If a verified startTime arrives from graph data that differs from existing,
        # it indicates a brand new charging session. An estimated startTime is
        # replaced without clearing, as it's the same session.
        if "startTime" in clean:
            old_start = new_data.get("startTime")
            if (
                old_start
                and old_start != clean["startTime"]
                and not self._estimated_start
            ):
                # New session started - clear old session metrics
                new_data.clear()
            self._estimated_start = False
        elif not new_data.get("startTime") and clean.get("power", 0) > 0:
            new_data["startTime"] = datetime.now(UTC)
            self._estimated_start = True

        new_data.update(clean)

        self.async_set_updated_data(new_data)
        _LOGGER.debug("Charging data updated from Parallax: %s", redact(clean))


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


def precondition_departure(now: datetime, lead_minutes: int) -> datetime:
    """Return the departure for a temporary precondition schedule.

    Schedules depart on a whole minute. Rounding up keeps the departure at least
    ``lead_minutes`` away: rounding down would shorten the lead by up to a minute,
    and the vehicle may not precondition for a departure about 15 minutes away.
    """
    # Add in UTC, so a DST change in between doesn't shorten or stretch the lead
    # or land on a local time that doesn't exist
    zone = now.tzinfo
    depart = (now.astimezone(UTC) if zone else now) + timedelta(minutes=lead_minutes)
    if depart.second or depart.microsecond:
        depart = depart.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return depart.astimezone(zone) if zone else depart


def is_precondition_schedule(schedule: dict[str, Any]) -> bool:
    """Return whether a schedule is one of HA's temporary precondition schedules.

    Identified by the reserved name, which the actions don't let users take.
    """
    name = schedule.get("name")
    return (
        isinstance(name, str)
        and name.strip().casefold() == PRECONDITION_SCHEDULE_NAME.casefold()
    )


def precondition_current_departure(
    schedule: dict[str, Any], now: float
) -> datetime | None:
    """Return a temporary precondition schedule's departure, if it's current.

    Current means departing within the longest lead time, or having departed less
    than PRECONDITION_CLEANUP_GRACE_SECONDS ago. A temporary schedule is created
    to depart within the longest lead, and repeats weekly like any schedule, so one
    that isn't current has departed, or was moved further out in the Rivian app,
    and is deleted. In Home Assistant's timezone, like ``precondition_departure``.
    """
    occurrence = schedule.get("occurrence") or {}
    days = occurrence.get("days") or []
    minutes = occurrence.get("startsAtMin")
    if not days or minutes is None:
        return None
    earliest = now - PRECONDITION_CLEANUP_GRACE_SECONDS
    # the departure minute is rounded up, so allow a minute more than the lead
    latest = now + (PRECONDITION_LEAD_MINUTES_MAXIMUM + 1) * 60
    today = dt_util.as_local(dt_util.utc_from_timestamp(now))
    for offset in (-1, 0, 1):
        day = today.date() + timedelta(days=offset)
        if WEEK_DAYS_ORDERED[day.weekday()] not in days:
            continue
        wall = datetime.combine(
            day,
            dt_time(minutes // MINUTES_PER_HOUR, minutes % MINUTES_PER_HOUR),
            today.tzinfo,
        )
        # A wall time repeated as the clocks go back is two instants
        for departure in (wall, wall.replace(fold=1)):
            if earliest <= departure.timestamp() <= latest:
                return departure
    return None


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
        self._unsub_handler: Callable[[], Awaitable[None]] | None = None
        self._unsub_parallax: Callable[[], Awaitable[None]] | None = None
        self._unsub_location: Callable[[], Awaitable[None]] | None = None
        # Whether GraphQL's position fix subscription failed, e.g. the field
        # failing validation; it isn't subscribed again until the entry reloads
        self._location_failed = False
        # Time of the fix gnssSpeed and gnssBearing last came from, which can be
        # older than gnssLocation's when GraphQL's fix is the newer position
        self._motion_fix_time: datetime | None = None
        self._awake = asyncio.Event()
        # When any message last arrived, and the newest time the vehicle reported
        self._last_message: datetime | None = None
        self._last_report: datetime | None = None
        # When subscribing first failed for a rate limit, and how many times it
        # failed without a backoff, since it last worked
        self._rate_limited_since: datetime | None = None
        self._subscribe_failures = 0
        # Bumped by each climate hold request, so an older one isn't sent again
        self._climate_hold_request = 0
        # All of the vehicle's charging schedules (entities manage the first);
        # None until fetched successfully
        self._charging_schedules: list[dict[str, Any]] | None = None
        self._last_schedule_fetch: float = 0.0
        self._departure_schedules: list[dict[str, Any]] | None = None
        self._unsub_departure_handler: Callable[[], Awaitable[None]] | None = None
        # serialize schedule mutations and wait for the refreshed list between them;
        # _departure_stale marks the cache unconfirmed after a write whose refresh we
        # never saw, so the next read forces a fresh list before merging
        self._departure_lock = asyncio.Lock()
        self._departure_refreshed = asyncio.Event()
        self._departure_stale = False
        self._precondition_cleanup_lock = asyncio.Lock()
        # set when a deletion failed, so cleanup retries soon
        self._precondition_cleanup_failed = False
        # Serializes precondition_now, so two calls can't each create a schedule.
        # Taken before _departure_lock, as is the cleanup lock
        self._precondition_lock = asyncio.Lock()
        # Serializes (re)subscribing to the departure schedules, so overlapping
        # calls don't each open a subscription and lose one's handle
        self._departure_subscribe_lock = asyncio.Lock()
        # Set while deleting the last schedule, so the empty list that follows
        # confirms the cache rather than being taken for a brief one
        self._expect_empty_schedules = False
        self._empty_list_hold: CALLBACK_TYPE | None = None
        self._departure_subscription = 0
        self._departure_failure_logged = False
        # the in-flight reconcile task, tracked so overlapping schedules collapse into one
        # and a pending pass can be cancelled cleanly on shutdown
        self._reconcile_task: asyncio.Task[None] | None = None
        # cancels the timer for the next cleanup pass; independent of the refresh timer,
        # which every vehicle payload restarts and so cannot be relied on to fire
        self._precondition_timer: CALLBACK_TYPE | None = None
        # set on unload/reload (when hass.is_stopping is still False) so a cancelled
        # cleanup pass cannot re-arm its timer or start another pass afterwards
        self._shutting_down = False

    @property
    def charging_schedule(self) -> dict[str, Any]:
        """Return the first charging schedule, or the default if there's none."""
        if self._charging_schedules:
            return self._charging_schedules[0]
        return DEFAULT_CHARGING_SCHEDULE

    async def _fetch_charging_schedules(self) -> list[dict[str, Any]]:
        """Fetch all of the vehicle's charging schedules."""
        response = await self.api.get_charging_schedules(self.vehicle_id)
        res_json = await response.json()
        if not (vehicle := (res_json.get("data") or {}).get("getVehicle")):
            raise RivianApiException(
                f"No charging schedules in response: {res_json.get('errors')}"
            )
        return vehicle.get("chargingSchedules") or []

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
        if self._charging_schedules is None or (
            now - self._last_schedule_fetch > cooldown
        ):
            self._last_schedule_fetch = now
            try:
                schedules = await self._fetch_charging_schedules()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error fetching charging schedule: %s", err)
            else:
                if schedules != self._charging_schedules:
                    self._charging_schedules = schedules
                    self.async_update_listeners()
        return self.charging_schedule

    async def update_charging_schedule_data(self, schedule: dict[str, Any]) -> None:
        """Update the first charging schedule via Rivian API mutation.

        Without any existing schedule, a default schedule is created.
        """
        try:
            schedules = await self._fetch_charging_schedules()
        except Exception as err:
            raise HomeAssistantError(
                f"Unable to fetch the current charging schedules: {err}"
            ) from err
        if not schedules:
            # A schedule applies at a location; use Home Assistant's home
            schedules = [
                DEFAULT_CHARGING_SCHEDULE
                | {
                    "location": {
                        "latitude": self.hass.config.latitude,
                        "longitude": self.hass.config.longitude,
                    }
                }
            ]
        updated_schedule = schedules[0] | schedule
        try:
            response = await self.api.set_charging_schedules(
                self.vehicle_id, [updated_schedule]
            )
            res_json = await response.json()
        except Exception as err:
            raise HomeAssistantError(
                f"Unable to update the charging schedule: {err}"
            ) from err
        if not ((res_json.get("data") or {}).get("setChargingSchedules") or {}).get(
            "success"
        ):
            raise HomeAssistantError(
                f"Unable to update the charging schedule: {res_json.get('errors')}"
            )
        self._charging_schedules = [updated_schedule, *schedules[1:]]
        self._last_schedule_fetch = time.time()
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
            self._require_departure_schedules()
            if not (current := self.get_departure_schedule(schedule_id)):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="schedule_not_found",
                    translation_placeholders={"schedule_id": schedule_id},
                )
            schedule = deep_merge(departure_schedule_to_input(current), changes)
            await self._departure_schedule_mutation(
                "updateDepartureSchedule",
                self.api.update_departure_schedule(
                    self.vehicle_id, schedule_id, schedule
                ),
            )

    async def delete_departure_schedule(
        self,
        schedule_id: str,
        keep: Callable[[dict[str, Any]], bool] | None = None,
    ) -> bool:
        """Delete a departure schedule via Rivian API mutation.

        ``keep`` is checked against the schedule as it stands just before, under the
        lock; if it returns True, the schedule isn't deleted. Returns whether it was.
        """
        async with self._departure_lock:
            await self._ensure_fresh_departure_schedules()
            self._require_departure_schedules()
            if not (schedule := self.get_departure_schedule(schedule_id)):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="schedule_not_found",
                    translation_placeholders={"schedule_id": schedule_id},
                )
            if keep and keep(schedule):
                return False
            # deleting the last schedule leaves an empty list, which is then real
            self._expect_empty_schedules = self._departure_schedules == [schedule]
            try:
                await self._departure_schedule_mutation(
                    "deleteDepartureSchedule",
                    self.api.delete_departure_schedule(self.vehicle_id, schedule_id),
                )
            finally:
                self._expect_empty_schedules = False
            return True

    async def _departure_schedule_mutation(
        self, key: str, request: Coroutine[Any, Any, ClientResponse]
    ) -> None:
        """Run a departure schedule mutation and refresh the schedules."""
        try:
            response = await request
            data = await response.json()
        except (ClientError, TimeoutError, ValueError) as err:
            # Such as an error page instead of JSON, after which Rivian may still
            # have applied it
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="schedule_unconfirmed",
                translation_placeholders={"action": key, "reason": type(err).__name__},
            ) from None
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
            if not errors:
                # Such as a timeout, after which Rivian may still have applied it
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="schedule_unconfirmed",
                    translation_placeholders={
                        "action": key,
                        "reason": type(err).__name__,
                    },
                ) from None
            reason = ", ".join(str(error.get("message")) for error in errors)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="schedule_rejected",
                translation_placeholders={"action": key, "reason": reason},
            ) from None
        finally:
            # whatever the outcome (success, error, timeout, cancellation) Rivian may
            # have applied the write, and a payload that arrived mid-request may predate
            # it, so mark the cache unconfirmed: the next read refreshes rather than
            # merging stale data. Only an authoritative payload (in
            # _process_departure_schedules) clears the flag.
            self._departure_stale = True
        if (succeeded := departure_mutation_succeeded(data, key)) is not True:
            # Only an explicit failure means nothing was written; no result, such
            # as partial data with an error, may follow a write that took effect
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=(
                    "schedule_rejected"
                    if succeeded is False
                    else "schedule_unconfirmed"
                ),
                translation_placeholders={"action": key, "reason": str(redact(data))},
            )
        # clear before resubscribing: the refreshed list often arrives during the
        # resubscribe itself, and clearing afterwards would discard that signal
        self._departure_refreshed.clear()
        # Rivian accepted the write, so a failed refresh isn't reported as it
        # failing, which a retry would then repeat. The cache stays unconfirmed, so
        # the next read refreshes it
        try:
            await self._subscribe_departure_schedules(force=True)
            await asyncio.wait_for(
                self._departure_refreshed.wait(), DEPARTURE_REFRESH_TIMEOUT
            )
        except TimeoutError:
            _LOGGER.debug("Timed out waiting for refreshed schedules after %s", key)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Unable to refresh schedules after %s: %s", key, err)

    def _require_departure_schedules(self) -> None:
        """Raise if the schedules haven't arrived, as none can be found then."""
        if self._departure_schedules is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="schedules_out_of_sync"
            )

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
                translation_domain=DOMAIN, translation_key="schedules_out_of_sync"
            ) from err

    async def _subscribe_departure_schedules(self, force: bool = False) -> None:
        """Subscribe to departure schedules, retrying if not currently subscribed.

        ``force`` tears down and re-establishes an existing subscription to pull a
        fresh list (used after a mutation); otherwise an active subscription is left
        in place and only a missing one is (re)established.
        """
        async with self._departure_subscribe_lock:
            if self._unsub_departure_handler and not force:
                return
            if unsub := self._unsub_departure_handler:
                self._unsub_departure_handler = None
                await unsub()
            # an empty list held from the previous subscription isn't this one's
            self._cancel_empty_list_hold()
            if self._shutting_down:
                return
            # Numbered, so an error from an earlier subscription can't end this one
            self._departure_subscription += 1
            self._unsub_departure_handler = (
                await self.api.subscribe_for_departure_schedules(
                    self.vehicle_id,
                    self._process_departure_schedules,
                    on_error=partial(
                        self._departure_subscription_failed,
                        self._departure_subscription,
                    ),
                )
            )
            if not self._unsub_departure_handler:
                _LOGGER.warning(
                    "Unable to subscribe to departure schedules for %s",
                    self.vehicle_id,
                )

    @callback
    def _departure_subscription_failed(
        self, subscription: int, message: dict[str, Any]
    ) -> None:
        """End a departure schedule subscription that failed, to retry it later.

        Such as Rivian rejecting the query. The subscription has ended without
        data but its handle remains, which would keep the next refresh from
        subscribing again, so it's ended here and retried on the next refresh.
        Warns once until schedules arrive again, rather than on every retry.
        """
        log = _LOGGER.debug if self._departure_failure_logged else _LOGGER.warning
        self._departure_failure_logged = True
        log(
            "Rivian ended vehicle %s departure schedule updates (%s); retrying later",
            self.vehicle_id,
            redact(message.get("payload") or message),
        )
        self.config_entry.async_create_task(
            self.hass, self._end_departure_subscription(subscription), eager_start=True
        )

    async def _end_departure_subscription(self, subscription: int) -> None:
        """Unsubscribe from a failed subscription, so a refresh subscribes again."""
        async with self._departure_subscribe_lock:
            if subscription != self._departure_subscription:
                return  # a newer subscription has replaced it
            unsub, self._unsub_departure_handler = self._unsub_departure_handler, None
            self._cancel_empty_list_hold()
            # no more updates, so the next read-modify-write gets a fresh list
            self._departure_stale = True
            if unsub:
                await unsub()

    @callback
    def _process_departure_schedules(self, data: dict[str, Any]) -> None:
        """Process departure schedules.

        Rivian can send an empty list just before the full one, such as after
        subscribing or creating the first schedule. So an empty list is held back
        for DEPARTURE_EMPTY_LIST_HOLD, unless it's expected after deleting the
        last schedule, and only used if no other list arrives in the meantime.
        Until then the cache isn't confirmed. This is the one place the quirk is
        handled; everything else sees only confirmed lists.
        """
        if (schedules := departure_schedules_from_message(data)) is None:
            _LOGGER.debug("Received an unknown departure schedule update: %s", data)
            return
        _LOGGER.debug(
            "Vehicle %s departure schedules: %s", self.vehicle_id, redact(schedules)
        )
        self._departure_failure_logged = False
        if not schedules and not self._expect_empty_schedules:
            self._departure_stale = True
            self._departure_refreshed.clear()
            if self._empty_list_hold is None:
                self._empty_list_hold = async_call_later(
                    self.hass,
                    DEPARTURE_EMPTY_LIST_HOLD,
                    partial(self._accept_empty_list, self._departure_subscription),
                )
            return
        self._cancel_empty_list_hold()
        self._apply_departure_schedules(schedules)

    @callback
    def _accept_empty_list(self, subscription: int, _now: datetime) -> None:
        """Use an empty list no other list followed: they're really all gone.

        Only from the subscription that's still current.
        """
        self._empty_list_hold = None
        if subscription == self._departure_subscription:
            self._apply_departure_schedules([])

    @callback
    def _cancel_empty_list_hold(self) -> None:
        if self._empty_list_hold:
            self._empty_list_hold()
            self._empty_list_hold = None

    @callback
    def _apply_departure_schedules(self, schedules: list[dict[str, Any]]) -> None:
        """Use a schedule list as the current one."""
        if schedules != self._departure_schedules:
            self._departure_schedules = schedules
            self.async_update_listeners()
        # a delivered list is authoritative: let a waiting mutation proceed and clear
        # the stale marker so reads no longer need to force a refresh
        self._departure_refreshed.set()
        self._departure_stale = False
        # delete HA's departed temporary schedules, or wait for the current one
        if self._precondition_schedules():
            self._schedule_reconcile()

    def has_paired_phone(self, identity_id: str | None) -> bool:
        """Return whether the enrolled phone has completed Bluetooth pairing.

        Enrollment alone (an identity id) cannot sign vehicle commands, so keyless
        controls stay available until pairing completes.
        """
        if not identity_id:
            return False
        device = self.drivers_coordinator.get_device_details(identity_id)
        # A disabled key can't sign commands either
        return bool(
            device and device["isPaired"] and device.get("isEnabled") is not False
        )

    async def precondition_now(
        self, lead_minutes: int | None = None, temperature: float | None = None
    ) -> None:
        """Start cabin preconditioning without a paired phone.

        Creates a temporary departure schedule named PRECONDITION_SCHEDULE_NAME,
        departing ``lead_minutes`` from now (20 by default), which the vehicle
        preconditions for right away, at ``temperature`` in °C (by default the
        driver's set temperature). The vehicle may not precondition for a departure
        about 15 minutes or less away, so the lead is at least
        ``PRECONDITION_LEAD_MINUTES_MINIMUM`` and the departure minute is rounded
        up, never down. If HA's temporary schedule is still current, it's moved
        instead of adding another. Turning it off or deleting it stops
        preconditioning early. It's deleted once it has departed, so it doesn't
        repeat weekly.

        The departure minute and day are computed in Home Assistant's timezone.
        Rivian interprets them in the vehicle's local timezone, which the API does
        not expose here, so this assumes the two match. They usually do (the
        vehicle is near home); if they differ, the schedule fires at the wrong
        wall-clock time and may not precondition.
        """
        async with self._precondition_lock:
            # without the list, HA's current schedule can't be found to move, so
            # another would be created
            self._require_departure_schedules()
            if self._departure_stale:
                # one predating HA's own change could also create a second
                async with self._departure_lock:
                    await self._ensure_fresh_departure_schedules()
            repeats_weekly, comfort = self._precondition_request(
                lead_minutes, temperature
            )
            changes = {
                "isEnabled": True,
                "repeatsWeekly": repeats_weekly,
                "departureSettings": {"comfortSettings": comfort},
            }
            now = time.time()
            if current := next(
                (
                    schedule
                    for schedule in self._precondition_schedules()
                    if precondition_current_departure(schedule, now)
                ),
                None,
            ):
                await self.update_departure_schedule(current["id"], changes)
            else:
                await self.create_departure_schedule(
                    deep_merge(
                        DEFAULT_DEPARTURE_SCHEDULE,
                        {"name": PRECONDITION_SCHEDULE_NAME, **changes},
                    )
                )
        # the refreshed list arms the cleanup for its departure

    def _precondition_request(
        self, lead_minutes: int | None, temperature: float | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return a precondition schedule's occurrence and comfort settings.

        Departing ``lead_minutes`` from now, at ``temperature`` in °C, each clamped
        to what a schedule allows. Without a temperature, the driver's set
        temperature is used, like the Rivian app.
        """
        lead = max(
            PRECONDITION_LEAD_MINUTES_MINIMUM,
            min(
                PRECONDITION_LEAD_MINUTES_MAXIMUM,
                int(lead_minutes or DEFAULT_PRECONDITION_LEAD_MINUTES),
            ),
        )
        if temperature is None:
            target = self.get("hvacTargetTemperature")
            temperature = (
                target
                if isinstance(target, (int, float))
                else DEFAULT_PRECONDITION_TEMPERATURE
            )
        depart = precondition_departure(dt_util.now(), lead)
        repeats_weekly = {
            "days": [WEEK_DAYS_ORDERED[depart.weekday()]],
            "startsAtMin": depart.hour * MINUTES_PER_HOUR + depart.minute,
        }
        return repeats_weekly, {
            "cabinTempCelsius": schedule_cabin_temperature(temperature)
        }

    def _precondition_schedules(self) -> list[dict[str, Any]]:
        """Return HA's temporary precondition schedules."""
        return [
            schedule
            for schedule in self._departure_schedules or []
            if is_precondition_schedule(schedule)
        ]

    @callback
    def _schedule_reconcile(self) -> None:
        """Queue a background cleanup pass.

        Skipped while Home Assistant is stopping, and collapsed into one already
        in flight (each pass reads the current list, so dropping a redundant one
        is safe).
        """
        if self._shutting_down or self.hass.is_stopping:
            return
        if self._reconcile_task and not self._reconcile_task.done():
            return
        self._reconcile_task = self.config_entry.async_create_task(
            self.hass,
            self._reconcile_and_cleanup(),
            name=f"rivian precondition cleanup {self.vehicle_id}",
            eager_start=False,
        )

    async def _reconcile_and_cleanup(self) -> None:
        """Delete HA's departed temporary schedules, then arm the next pass."""
        async with self._precondition_cleanup_lock:
            try:
                await self._cleanup_precondition_schedules()
            finally:
                self._arm_precondition_timer()

    async def _cleanup_precondition_schedules(self) -> None:
        """Run one cleanup pass; called under the cleanup lock."""
        if self._departure_schedules is None:
            return  # a later list or the retry timer covers it
        self._precondition_cleanup_failed = False

        def still_current(schedule: dict[str, Any]) -> bool:
            # rechecked as it stands just before deleting, such as moved in the app
            return precondition_current_departure(schedule, time.time()) is not None

        for schedule in self._precondition_schedules():
            if still_current(schedule):
                continue
            try:
                await self.delete_departure_schedule(schedule["id"], keep=still_current)
            except Exception as err:  # noqa: BLE001
                # Retried soon; if it's gone, the next list won't have it
                self._precondition_cleanup_failed = True
                _LOGGER.debug("Could not delete precondition schedule: %s", err)

    @callback
    def _arm_precondition_timer(self) -> None:
        """Schedule the next cleanup pass, if HA's temporary schedules need one.

        At the current one's departure plus the grace period, or soon if a
        deletion failed. Independent of the refresh timer, which every vehicle
        payload restarts.
        """
        if self._precondition_timer:
            self._precondition_timer()
            self._precondition_timer = None
        if self._shutting_down or self.hass.is_stopping:
            return
        now = time.time()
        if self._precondition_cleanup_failed:
            delay = PRECONDITION_RETRY_SECONDS
        elif departures := [
            departure.timestamp()
            for schedule in self._precondition_schedules()
            if (departure := precondition_current_departure(schedule, now))
        ]:
            delay = min(departures) + PRECONDITION_CLEANUP_GRACE_SECONDS - now
        else:
            return
        self._precondition_timer = async_call_later(
            self.hass, max(delay, 1), self._precondition_timer_fired
        )

    @callback
    def _precondition_timer_fired(self, _now: datetime) -> None:
        """Run the cleanup pass the timer was armed for."""
        self._precondition_timer = None
        self._schedule_reconcile()

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

            # Subscribe to Parallax messages for live vehicle and charging data,
            # unless the web socket already failed
            if self._unsub_handler:
                self._unsub_parallax = await self.api.subscribe_for_parallax_messages(
                    vehicle_id=self.vehicle_id,
                    callback=self._process_parallax_data,
                )

            # The client returns None, rather than raising, when subscribing fails
            if not self._unsub_handler or not self._unsub_parallax:
                await self._unsubscribe()
                message = f"Unable to subscribe to vehicle {self.vehicle_id} updates"
                delay = self._subscribe_retry_delay(message)
                if self.data is None:
                    # Finish setting up without data and subscribe again later, so
                    # it doesn't wait on Home Assistant's slower setup retries once
                    # a rate limit has passed. The client has already logged why
                    _LOGGER.debug("%s, retrying in %.0f seconds", message, delay)
                    self.config_entry.async_on_unload(
                        async_call_later(self.hass, delay, self._retry_subscribe)
                    )
                    return {}
                raise UpdateFailed(message, retry_after=delay)

            self._subscribe_failures = 0
            if self._rate_limited_since:
                self._rate_limited_since = None
                async_delete_issue(self.hass, DOMAIN, self._rate_limited_issue_id)
            try:
                await asyncio.wait_for(self._initial.wait(), INITIAL_UPDATE_TIMEOUT)
            except TimeoutError as err:
                raise UpdateFailed(
                    "Timed out waiting for initial vehicle data after "
                    f"{INITIAL_UPDATE_TIMEOUT}s"
                ) from err
            await self._subscribe_location()

            # the websocket was just (re)established; refresh the departure feed too
            await self._subscribe_departure_schedules(force=True)
        else:
            if self._unsub_handler and not self._unsub_location:
                # Subscribing to the position fixes can fail on its own, e.g. if the
                # web socket dropped in between, and the client only resubscribes
                # what it has, so try again on the regular refresh
                await self._subscribe_location()
            # retry a previously failed departure subscription on this refresh without
            # disturbing a healthy one (the vehicle-state subscription may be fine)
            await self._subscribe_departure_schedules()

        # run on every cycle so a precondition schedule whose one-shot timer was lost to
        # a restart is still cleaned up after it expires, and failed deletions are retried
        if self._precondition_schedules():
            self._schedule_reconcile()

        return self.data

    async def _subscribe_location(self) -> None:
        """Subscribe to GraphQL's position fixes, which are optional.

        Not subscribing (the client has already logged why) or the field failing
        GraphQL validation leaves the position to Parallax, as before. After a
        subscription error it isn't subscribed again until the entry reloads.
        """
        if self._location_failed:
            return
        try:
            self._unsub_location = await self.api.subscribe_for_vehicle_updates(
                vehicle_id=self.vehicle_id,
                properties=GRAPHQL_LOCATION_FIELDS,
                callback=self._process_graphql_location,
                on_error=self._location_subscription_failed,
            )
        except Exception:  # optional, so it never fails the update
            _LOGGER.debug(
                "Couldn't subscribe to vehicle %s GraphQL position fixes",
                self.vehicle_id,
                exc_info=True,
            )
        if not self._unsub_location:
            _LOGGER.debug(
                "Not subscribed to vehicle %s GraphQL position fixes; using Parallax's",
                self.vehicle_id,
            )
        elif self._location_failed:
            # The error arrived before the unsubscribe handle did
            await self._end_location_subscription()

    @callback
    def _location_subscription_failed(self, message: dict[str, Any]) -> None:
        """Fall back to Parallax's position fixes once GraphQL's subscription fails.

        Such as Rivian no longer accepting the field. The subscription has ended,
        and the client would subscribe it again after every reconnect, so it's
        ended here and not subscribed again until the entry reloads.
        """
        if self._location_failed:
            return
        self._location_failed = True
        errors = message.get("payload")
        reason = (
            "; ".join(
                str(error.get("message") or error)
                for error in errors
                if isinstance(error, dict)
            )
            if isinstance(errors, list)
            else None
        ) or redact(message)
        _LOGGER.warning(
            "Rivian stopped sending vehicle %s position fixes over GraphQL (%s). "
            "Using Parallax's instead, which update about once a minute",
            self.vehicle_id,
            reason,
        )
        self.config_entry.async_create_task(
            self.hass, self._end_location_subscription(), eager_start=True
        )

    async def _end_location_subscription(self) -> None:
        """Unsubscribe from GraphQL's position fixes."""
        unsub_location, self._unsub_location = self._unsub_location, None
        if unsub_location:
            await unsub_location()

    def _subscribe_retry_delay(self, message: str) -> float:
        """Return how long to wait before subscribing again after it failed.

        Raises ConfigEntryAuthFailed when only a new login can help: the session
        was rejected, or has been rate limited for RATE_LIMIT_REAUTH_AFTER. Before
        that, a rate limit lasting RATE_LIMIT_ISSUE_AFTER raises a repair issue
        that offers to log in again.
        """
        # The web socket doesn't reconnect once the session is rejected
        if self.api.ws_unauthenticated:
            raise ConfigEntryAuthFailed(f"{message}: session unauthenticated")
        if self.api.ws_rate_limited:
            now = dt_util.utcnow()
            self._rate_limited_since = since = self._rate_limited_since or now
            if now - since >= RATE_LIMIT_REAUTH_AFTER:
                raise ConfigEntryAuthFailed(
                    f"{message}: rate limited since {since.isoformat()}"
                )
            if now - since >= RATE_LIMIT_ISSUE_AFTER:
                async_create_issue(
                    self.hass,
                    DOMAIN,
                    self._rate_limited_issue_id,
                    data={"entry_id": self.config_entry.entry_id},
                    is_fixable=True,
                    is_persistent=False,
                    severity=IssueSeverity.WARNING,
                    translation_key=ISSUE_RATE_LIMITED,
                    translation_placeholders=RATE_LIMIT_ISSUE_PLACEHOLDERS,
                )
        # Subscribing fails without connecting while the web socket is backing off,
        # so wait that out
        if backoff := self.api.ws_backoff_remaining:
            return backoff + 1
        # Otherwise back off here, as each retry connects
        delay = min(
            SUBSCRIBE_RETRY_DELAY * 2**self._subscribe_failures,
            SUBSCRIBE_RETRY_MAX_DELAY,
        )
        self._subscribe_failures += 1
        return delay

    async def _retry_subscribe(self, _: datetime) -> None:
        """Subscribe again after subscribing failed during setup."""
        await self.async_request_refresh()

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        raise NotImplementedError("Polling VehicleState no longer allowed")

    @property
    def _rate_limited_issue_id(self) -> str:
        """Return the ID of the repair issue for a lasting rate limit."""
        return f"{ISSUE_RATE_LIMITED}_{self.config_entry.entry_id}"

    async def async_shutdown(self) -> None:
        # Raised again if the rate limit outlasts a reload
        async_delete_issue(self.hass, DOMAIN, self._rate_limited_issue_id)
        # mark first: a cancelled cleanup pass unwinds through code that would
        # otherwise re-arm its timer or schedule another pass
        self._shutting_down = True
        if (task := self._reconcile_task) and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._precondition_timer:
            self._precondition_timer()
            self._precondition_timer = None
        self._cancel_empty_list_hold()
        await self._unsubscribe(True)
        return await super().async_shutdown()

    @callback
    def _process_parallax_data(self, data: dict[str, Any]) -> None:
        """Process incoming Parallax subscription messages."""
        if not (payload := data.get("payload")) or not (pdata := payload.get("data")):
            self._handle_subscription_error("Parallax", data)
            return
        self._error_count = 0
        self._last_message = dt_util.utcnow()
        px = pdata.get("parallaxMessages")
        if not px or px.get("rvm") in PARALLAX_IGNORED_RVMS:
            return
        decoded = decode_parallax_message(**px)
        if not decoded:
            return
        self._note_report(decoded.get("timestamp"))

        clean = {k: v for k, v in decoded.items() if not k.startswith("_")}
        if not clean:
            return
        clean = PARALLAX_RVM_DEFAULTS.get(px.get("rvm"), {}) | clean
        self._use_newest_location(clean)

        # Route charging fields to ChargingCoordinator
        if charging_keys := clean.keys() & CHARGING_STATE_KEYS:
            self.charging_coordinator.update_from_parallax(
                {k: clean[k] for k in charging_keys}
            )

        # Route vehicle state fields to VehicleCoordinator, skipping fields that
        # GraphQL owns and unsent (None) or invalid values
        # Note: timeToEndOfCharge is defined in VEHICLE_SENSORS, so it updates VehicleCoordinator too
        vehicle_keys = (
            ((clean.keys() - charging_keys) | (clean.keys() & {"timeToEndOfCharge"}))
            - VEHICLE_STATE_API_FIELDS
            - {"timestamp"}
        )
        timestamp = clean.get("timestamp")
        current = self._current_data()
        vehicle_updates: dict[str, Any] = {}
        cleared = {
            k for k in vehicle_keys & PARALLAX_NONE_CLEARS if clean[k] is None
        } & current.keys()
        for k in vehicle_keys:
            if (value := clean[k]) is None:
                value = PARALLAX_NONE_VALUES.get(k)
            if value is None or (
                str(value).lower() in INVALID_SENSOR_STATES
                and not (value == "undefined" and k in PARALLAX_UNDEFINED_IS_VALID)
            ):
                continue
            if k == "gnssLocation":
                if current.get(k) != value:
                    vehicle_updates[k] = value
                continue
            # Skip unchanged values, e.g. the snapshot resent on every reconnect
            if k in current and current[k].get("value") == value:
                continue
            vehicle_updates[k] = {"value": value}
            if timestamp:
                vehicle_updates[k]["timeStamp"] = timestamp.isoformat()
        if vehicle_updates or cleared:
            self._process_state_changes(vehicle_updates)
            kept = {k: v for k, v in current.items() if k not in cleared}
            self.async_set_updated_data(kept | vehicle_updates)
            _LOGGER.debug(
                "Vehicle state updated from Parallax (%s): %s",
                px.get("rvm"),
                redact(clean),
            )

    @callback
    def _process_new_data(self, data: dict[str, Any]) -> None:
        """Process new data."""
        if not (payload := data.get("payload")) or not (pdata := payload.get("data")):
            self._handle_subscription_error("vehicle state", data)
            return
        vijson = pdata.get(self.key) or {}
        # Before notifying listeners, which include the freshness entities
        self._last_message = dt_util.utcnow()
        for item in vijson.values():
            if isinstance(item, dict) and isinstance(ts := item.get("timeStamp"), str):
                self._note_report(dt_util.parse_datetime(ts))
        vehicle_info = self._build_vehicle_info_dict(vijson)
        self.async_set_updated_data(vehicle_info)
        self._error_count = 0
        self._initial.set()

    @callback
    def _process_graphql_location(self, data: dict[str, Any]) -> None:
        """Use a GraphQL position fix if it's newer than the current one.

        Anything unexpected is ignored rather than counted as a subscription
        error: the position also comes from Parallax, so this feed is optional.
        """
        payload = data.get("payload")
        pdata = payload.get("data") if isinstance(payload, dict) else None
        state = pdata.get(self.key) if isinstance(pdata, dict) else None
        location = state.get("gnssLocation") if isinstance(state, dict) else None
        if not isinstance(location, dict):
            _LOGGER.debug("Ignoring a GraphQL position update: %s", redact(data))
            return
        self._last_message = dt_util.utcnow()
        lat, lon = location.get("latitude"), location.get("longitude")
        fix_time = (
            dt_util.parse_datetime(ts)
            if isinstance(ts := location.get("timeStamp"), str)
            else None
        )
        if (
            not isinstance(lat, int | float)
            or not isinstance(lon, int | float)
            or fix_time is None
            or fix_time.tzinfo is None
            or location.get("isAuthorized") is False
        ):
            return
        self._note_report(fix_time)
        current = self._current_data()
        current_time = (current.get("gnssLocation") or {}).get("timeStamp")
        if (
            isinstance(current_time, datetime)
            and current_time.tzinfo
            and fix_time <= current_time
        ):
            return
        self.async_set_updated_data(
            current
            | {
                "gnssLocation": {
                    "latitude": round(lat, 6),
                    "longitude": round(lon, 6),
                    "timeStamp": fix_time,
                }
            }
        )

    @callback
    def _handle_subscription_error(self, source: str, data: dict[str, Any]) -> None:
        """Count a subscription error, unsubscribing both if they persist."""
        _LOGGER.error("Received an unknown %s subscription update: %s", source, data)
        self._error_count += 1
        if not self._initial.is_set() or self._error_count > 5:
            task = self._unsubscribe()
            self.config_entry.async_create_task(self.hass, task, eager_start=True)
            # Lets the next refresh resubscribe; it checks last_update_success
            self.async_set_update_error(
                UpdateFailed(
                    f"Vehicle {self.vehicle_id} subscription stopped after errors"
                )
            )

    def _use_newest_location(self, clean: dict[str, Any]) -> None:
        """Set gnssLocation, gnssSpeed and gnssBearing from the newest fix.

        dynamics.vehicle.gnss sends a fix about once a minute; while navigating,
        trip_progress's location fix comes every ~5 seconds. trip_progress keeps
        resending its last fix after navigation ends, so it's only used while
        recent, and only a newer fix wins.

        GraphQL's position fixes (_process_graphql_location) are often newer,
        but carry no speed or bearing, so those are compared with the fix they
        last came from rather than with the position's.
        """
        fix = {k: clean.pop(k) for k in GNSS_FIX_FIELDS if k in clean}
        lat, lon = clean.get("latitude"), clean.get("longitude")
        if (
            (fix_time := clean.get("locationTime"))
            and dt_util.utcnow() - fix_time <= NAVIGATION_FIX_MAX_AGE
            and lat is not None
            and lon is not None
        ):
            fix = {
                "gnssLocation": {
                    "latitude": round(lat, 6),
                    "longitude": round(lon, 6),
                    "timeStamp": fix_time,
                },
                "gnssSpeed": round(clean.get("speed") or 0, 2),
                "gnssBearing": round(clean.get("bearing") or 0, 1),
            }
        if not (location := fix.get("gnssLocation")):
            return
        new_time = location.get("timeStamp")
        if new_time and self._motion_fix_time and new_time <= self._motion_fix_time:
            fix.pop("gnssSpeed", None)
            fix.pop("gnssBearing", None)
        elif new_time:
            self._motion_fix_time = new_time
        current = self._current_data().get("gnssLocation") or {}
        if (
            (current_time := current.get("timeStamp"))
            and new_time
            and (new_time <= current_time)
        ):
            del fix["gnssLocation"]
        clean.update(fix)

    def _current_data(self) -> dict[str, Any]:
        """Return the current data, or the default vehicle state before any."""
        if self.data:
            return self.data
        return {k: {"value": v} for k, v in DEFAULT_VEHICLE_STATE.items()}

    def _process_state_changes(self, items: dict[str, Any]) -> None:
        """Act on power and charger state changes in a batch of new items."""
        if power_state := items.get("powerState"):
            if power_state.get("value") == "sleep":
                self._awake.clear()
            else:
                self._awake.set()
        if (charger_state := items.get("chargerState")) and charger_state.get(
            "value"
        ) != "charging_active":
            # Reset instantaneous charging metrics when not actively charging
            items["timeToEndOfCharge"] = {"value": 0}

    def _build_vehicle_info_dict(self, vijson: dict[str, Any]) -> dict[str, Any]:
        """Take the json output of vehicle_info and build a dictionary."""
        items = {k: v for k, v in vijson.items() if v}

        if items:
            _LOGGER.debug("Vehicle %s updated: %s", self.vehicle_id, redact(items))

        self._process_state_changes(items)

        prev_items = self._current_data()
        if not items or prev_items == items:
            return prev_items

        new_data = prev_items | items
        for key in filter(lambda i: i != "gnssLocation", items):
            value = items[key].get("value")
            if str(value).lower() in INVALID_SENSOR_STATES and key in prev_items:
                new_data[key] = prev_items[key]

        return new_data

    async def _unsubscribe(self, close_monitor: bool = False):
        """Unsubscribe."""
        # Take the handles before awaiting, so overlapping calls unsubscribe once
        # and don't clear handles set by a newer subscription
        unsub_parallax, self._unsub_parallax = self._unsub_parallax, None
        unsub_handler, self._unsub_handler = self._unsub_handler, None
        unsub_location, self._unsub_location = self._unsub_location, None
        if unsub_handler:
            self._initial.clear()
        if unsub_location:
            await unsub_location()
        if unsub_parallax:
            await unsub_parallax()
        if unsub_handler:
            await unsub_handler()
        # Under the lock, so a mutation's resubscribe in progress finishes first and
        # its new subscription is the one that's ended
        async with self._departure_subscribe_lock:
            unsub_departure, self._unsub_departure_handler = (
                self._unsub_departure_handler,
                None,
            )
            # The feed has ended: a held empty list from it no longer counts, and
            # the next read-modify-write gets a fresh list
            self._departure_subscription += 1
            self._cancel_empty_list_hold()
            self._departure_stale = True
            if unsub_departure:
                await unsub_departure()
        if close_monitor and (monitor := self.api._ws_monitor):
            await monitor.close()

    def _note_report(self, reported: datetime | None) -> None:
        """Keep the newest time the vehicle reported data."""
        # Skip times without a timezone, which can't be compared with the others
        if (
            reported
            and reported.tzinfo
            and (self._last_report is None or reported > self._last_report)
        ):
            self._last_report = reported

    @property
    def stale_since(self) -> datetime | None:
        """Return when the vehicle last reported data, once that's a while ago.

        None while updates are recent. Unlike updates_stale, this applies while
        asleep too, so a long sleep shows how long it's been.
        """
        if self._last_report and (
            dt_util.utcnow() - self._last_report > UPDATES_STALE_AFTER
        ):
            return self._last_report
        return None

    @property
    def updates_stale(self) -> bool:
        """Return True if updates have stopped arriving."""
        now = dt_util.utcnow()
        if self._last_message is None or now - self._last_message > UPDATES_STALE_AFTER:
            return True
        # Asleep, the vehicle reports nothing new, so only the connection counts
        if not self.data or self.get("powerState") in (None, "sleep"):
            return False
        return (
            self._last_report is None or now - self._last_report > UPDATES_STALE_AFTER
        )

    def get(self, key: str) -> Any | None:
        """Get a data value by key."""
        if entity := self.data.get(key, {}):
            return entity.get("value")
        return None

    def _phone_key(self) -> dict[str, str]:
        """Return the paired phone's key, as `send_parallax_command` takes it."""
        vehicle = self.hass.data[DOMAIN][self.config_entry.entry_id][ATTR_VEHICLE][
            self.vehicle_id
        ]
        return {
            "identity_id": vehicle["phone_identity_id"],
            "vehicle_key": vehicle["public_key"],
            "private_key": self.config_entry.options.get("private_key"),
        }

    async def send_parallax_command(
        self,
        rvm: str,
        payload: bytes = b"",
        *,
        command: VehicleCommand | None = None,
    ) -> None:
        """Send a signed Parallax command and wait for the vehicle's result.

        A released side bin that doesn't clear its latch reports a timeout though
        it opened, so for those `command`s that's logged instead of raised.
        """
        vehicle = self.hass.data[DOMAIN][self.config_entry.entry_id][ATTR_VEHICLE][
            self.vehicle_id
        ]
        command_id = str(uuid.uuid4())
        result: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )

        def on_state(message: dict[str, Any]) -> None:
            state = decode_command_state_message(message)
            done = state and (state["error"] or state["progress"] == "complete")
            if done and not result.done():
                result.set_result(state)

        # Subscribe first: states from before the subscription aren't replayed
        unsubscribe = await self.api.subscribe_for_parallax_command_state(
            self.vehicle_id, command_id, on_state
        )
        _LOGGER.debug("Sending %s over Parallax, command %s", rvm, command_id)
        try:
            if not await self.api.send_parallax_command(
                self.vehicle_id,
                rvm,
                payload,
                command_id=command_id,
                **self._phone_key(),
            ):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_rejected",
                    translation_placeholders={"rvm": rvm},
                )
            if not unsubscribe:
                # Couldn't follow it (already logged): sent, but unconfirmed
                return
            try:
                state = await asyncio.wait_for(result, PARALLAX_COMMAND_TIMEOUT)
            except TimeoutError as err:
                # Not retried: the vehicle may still carry it out
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_unconfirmed",
                    translation_placeholders={"rvm": rvm},
                ) from err
            _LOGGER.debug("%s command %s finished: %s", rvm, command_id, state)
            if command in SIDE_BIN_COMMANDS and state["status"] == "timeout":
                _LOGGER.warning(
                    "%s sent to %s, but the side bin may not have fully opened",
                    command,
                    vehicle["name"],
                )
                return
            if state["error"] or state["status"] != "success":
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_failed",
                    translation_placeholders={
                        "rvm": rvm,
                        "reason": (state["error"] or {}).get("message")
                        or state["status"],
                    },
                )
        finally:
            if unsubscribe:
                await unsubscribe()

    async def start_climate_hold(self) -> None:
        """Start a climate hold, sending it again if a waking vehicle lost it.

        A sleeping vehicle seems to get the command while its climate hold is
        still starting up, and loses it: the command fails, or succeeds and the
        hold then restarts (reads unavailable, for no given reason). Sending it
        again once the vehicle is awake and the hold has started up starts it.
        Without a known power state, the vehicle is taken as awake. It isn't
        sent again once the hold is turned off or started again.
        """
        self._climate_hold_request += 1
        request = self._climate_hold_request
        if self.get("powerState") != "sleep":
            await self.send_parallax_command(
                SET_CLIMATE_HOLD_RVM, encode_climate_hold(True)
            )
            return
        awake = asyncio.Event()
        held = asyncio.Event()
        restarted = asyncio.Event()
        started_up = asyncio.Event()
        # A restart before the result is the command's own, which may still work
        result_in = False
        seen_restart = False

        def restarting() -> bool:
            return self.get("climateHoldStatus") == "unavailable" and self.get(
                "climateHoldUnavailabilityReason"
            ) in (None, "unknown")

        @callback
        def watch() -> None:
            nonlocal seen_restart
            if self.get("powerState") not in (None, "sleep"):
                awake.set()
            if self.get("climateHoldStatus") == "on":
                held.set()
            if restarting():
                seen_restart = True
                # A restart starts with the whole hold unavailable; one already
                # past that began before the result
                if result_in and self.get("climateHoldAvailability") == "unavailable":
                    restarted.set()
                started_up.clear()
            elif seen_restart and self.get("climateHoldStatus") == "off":
                # Not e.g. the stale "unspecified" the vehicle replays
                started_up.set()

        unsubscribe = self.async_add_listener(watch)
        try:
            error: HomeAssistantError | None = None
            try:
                await self.send_parallax_command(
                    SET_CLIMATE_HOLD_RVM, encode_climate_hold(True)
                )
            except HomeAssistantError as err:
                # Only a sent command reaches, and wakes, the vehicle
                if err.translation_key not in ("command_failed", "command_unconfirmed"):
                    raise
                error = err
            result_in = True
            watch()
            try:
                await asyncio.wait_for(awake.wait(), CLIMATE_HOLD_WAKE_WAIT)
                # A hold that comes on needs no more waiting
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(held.wait(), CLIMATE_HOLD_START_WAIT)
                if restarted.is_set() and restarting():
                    await asyncio.wait_for(
                        started_up.wait(), CLIMATE_HOLD_START_UP_WAIT
                    )
            except TimeoutError:
                if error:
                    raise error from None
                return
        finally:
            unsubscribe()
        if self.get("climateHoldStatus") == "on":
            return
        if not (error or restarted.is_set()) or self.get(
            "climateHoldUnavailabilityReason"
        ) not in (None, "unknown"):
            # Not lost (it may still be starting), or unavailable for a reason
            # like low_soc that won't clear
            if error:
                raise error
            return
        if request != self._climate_hold_request:
            # Turned off or started again meanwhile
            return
        _LOGGER.debug("Sending the climate hold again, now that the vehicle is awake")
        await self.send_parallax_command(
            SET_CLIMATE_HOLD_RVM, encode_climate_hold(True)
        )

    def cancel_climate_hold_resend(self) -> None:
        """Keep a climate hold waiting to be sent again from being sent."""
        self._climate_hold_request += 1

    async def stop_climate_hold(self) -> None:
        """Stop a climate hold, and any still waiting to be sent again."""
        self.cancel_climate_hold_resend()
        await self.send_parallax_command(
            SET_CLIMATE_HOLD_RVM, encode_climate_hold(False)
        )

    async def set_climate_hold_duration(self, minutes: int) -> None:
        """Set how long a climate hold lasts."""
        try:
            accepted = await self.api.set_climate_hold_duration(
                self.vehicle_id, minutes
            )
        except RivianApiException as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="climate_hold_duration_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if not accepted:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="climate_hold_duration_rejected",
            )

    async def start_gear_guard_live_stream(self, camera: str) -> dict[str, Any] | None:
        """Start a Gear Guard live stream, turning video monitoring on first."""
        if self.get("gearGuardVideoStatus") == "disabled":
            await self.send_vehicle_command(VehicleCommand.ENABLE_GEAR_GUARD_VIDEO)
            for _ in range(GEAR_GUARD_VIDEO_WAIT):
                await asyncio.sleep(1)
                if self.get("gearGuardVideoStatus") != "disabled":
                    break
        # A sleeping vehicle can wake too late for the first start, and a start
        # can't be reused, so retry with a fresh one
        for _ in range(GEAR_GUARD_LIVE_ATTEMPTS):
            if config := await self.api.start_gear_guard_live_stream(
                self.vehicle_id,
                camera=camera,
                device_id="home-assistant",
                **self._phone_key(),
            ):
                return config
        return None

    async def send_vehicle_command(
        self, command: VehicleCommand, params: dict[str, Any] | None = None
    ) -> None:
        """Send a command to the vehicle, over Parallax if it takes it that way."""
        entry_data = self.hass.data[DOMAIN][self.config_entry.entry_id]
        vehicle = entry_data[ATTR_VEHICLE][self.vehicle_id]
        if (feature := vehicle_command_feature(command)) and feature in vehicle[
            "supported_features"
        ]:
            # The model picks an R1T's tailgate or another model's liftgate
            rvm, payload = encode_vehicle_command(
                command, params, model=vehicle["model"]
            )
            # The cloud wakes the vehicle for Parallax commands
            await self.send_parallax_command(rvm, payload, command=command)
            return

        if self.get("powerState") == "sleep" and command != VehicleCommand.WAKE_VEHICLE:
            await self.send_vehicle_command(VehicleCommand.WAKE_VEHICLE)
            try:
                await asyncio.wait_for(self._awake.wait(), 30)
            except TimeoutError:
                pass  # didn't wake-up in time, but we'll try command anyway

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
