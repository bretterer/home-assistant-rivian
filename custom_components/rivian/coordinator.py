"""Data update coordinator for the Rivian integration."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Awaitable, Callable, Coroutine
import contextlib
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
from rivian.parallax import decode_parallax_message

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
from homeassistant.helpers.storage import Store
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
    DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM,
    DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM,
    DOMAIN,
    INVALID_SENSOR_STATES,
    ISSUE_RATE_LIMITED,
    PARALLAX_IGNORED_RVMS,
    PARALLAX_NONE_CLEARS,
    PARALLAX_NONE_VALUES,
    PARALLAX_RVM_DEFAULTS,
    PARALLAX_UNDEFINED_IS_VALID,
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
# How soon cleanup runs again while a deletion has failed or a creation is still
# waiting for its schedule id to appear.
PRECONDITION_RETRY_SECONDS = 5 * 60
PRECONDITION_STORE_VERSION = 1
# Max age of a navigation location fix to use (trip_progress resends its last
# fix long after navigation ends)
NAVIGATION_FIX_MAX_AGE = timedelta(minutes=5)
# Fields set from the newest position fix (GNSS or navigation)
GNSS_FIX_FIELDS = ("gnssLocation", "gnssSpeed", "gnssBearing")
# Updates are stale after this long without either any message (the client
# resubscribes after a minute without one, and the subscriptions resend their
# snapshot, so a working connection never goes this long), or, while awake,
# newly reported data (the vehicle sends a location fix about once a minute;
# resent snapshots keep their original timestamps)
UPDATES_STALE_AFTER = timedelta(minutes=10)


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
        self._awake = asyncio.Event()
        # When any message last arrived, and the newest time the vehicle reported
        self._last_message: datetime | None = None
        self._last_report: datetime | None = None
        # When subscribing first failed for a rate limit, and how many times it
        # failed without a backoff, since it last worked
        self._rate_limited_since: datetime | None = None
        self._subscribe_failures = 0
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
        # cancels the timer for the next cleanup pass; independent of the refresh timer,
        # which every vehicle payload restarts and so cannot be relied on to fire
        self._precondition_timer: CALLBACK_TYPE | None = None
        # set on unload/reload (when hass.is_stopping is still False) so a cancelled
        # cleanup pass cannot re-arm its timer or start another pass afterwards
        self._shutting_down = False
        self.precondition_lead_minutes: int = DEFAULT_PRECONDITION_LEAD_MINUTES
        self.precondition_temperature: float = DEFAULT_PRECONDITION_TEMPERATURE

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
        finally:
            # whatever the outcome (success, error, timeout, cancellation) Rivian may
            # have applied the write, and a payload that arrived mid-request may predate
            # it, so mark the cache unconfirmed: the next read refreshes rather than
            # merging stale data. Only an authoritative payload (in
            # _process_departure_schedules) clears the flag.
            self._departure_stale = True
        if not ((data.get("data") or {}).get(key) or {}).get("success"):
            raise HomeAssistantError(f"Rivian rejected {key}: {redact(data)}")
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

    def has_paired_phone(self, identity_id: str | None) -> bool:
        """Return whether the enrolled phone has completed Bluetooth pairing.

        Enrollment alone (an identity id) cannot sign vehicle commands, so keyless
        controls stay available until pairing completes.
        """
        if not identity_id:
            return False
        device = self.drivers_coordinator.get_device_details(identity_id)
        return bool(device and device["isPaired"])

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
        # adopts the new id and arms the cleanup timer for its expiry, so the schedule is
        # deleted once it has departed and does not fire again next week; deleting does
        # not stop preconditioning that has already started
        await self._reconcile_and_cleanup()

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
        if self._shutting_down or self.hass.is_stopping:
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

        Safe to call repeatedly (from the cleanup timer, a refresh, or when a payload
        arrives): only schedules we own and whose grace period has passed are removed, a
        failed deletion stays tracked for the next attempt, and a user's same-named
        schedule is never touched. Every pass re-arms the cleanup timer.
        """
        async with self._precondition_cleanup_lock:
            try:
                await self._cleanup_precondition_schedules()
            finally:
                self._arm_precondition_timer()

    async def _cleanup_precondition_schedules(self) -> None:
        """Run one reconcile/cleanup pass; called under the cleanup lock."""
        await self._ensure_precondition_loaded()
        if self._departure_schedules is None:
            # no authoritative list yet; the retry timer or a later payload covers it
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
                # keep the id tracked so the next pass retries the deletion
                _LOGGER.debug("Could not delete precondition schedule: %s", err)
            else:
                self._precondition_ids.pop(schedule_id, None)
                changed = True
        if changed:
            await self._save_precondition_state()

    @callback
    def _arm_precondition_timer(self) -> None:
        """Schedule the next cleanup pass while temporary schedules need attention.

        Wakes at the earliest owned expiry, or retries soon if a deletion has failed or a
        creation has not yet been matched to its id. With nothing left to clean up, no
        timer is kept.
        """
        if self._precondition_timer:
            self._precondition_timer()
            self._precondition_timer = None
        if (
            self._shutting_down
            or self.hass.is_stopping
            or not (self._precondition_ids or self._precondition_pending)
        ):
            return
        now = time.time()
        waits = [
            expiry - now for expiry in self._precondition_ids.values() if expiry > now
        ]
        if self._precondition_pending or len(waits) < len(self._precondition_ids):
            # a creation is unmatched or an expired deletion failed: retry soon
            delay = min([PRECONDITION_RETRY_SECONDS, *waits])
        else:
            # every owned schedule is still waiting to expire; sleep until the first does
            delay = min(waits)
        self._precondition_timer = async_call_later(
            self.hass, max(delay, 1), self._precondition_timer_fired
        )

    @callback
    def _precondition_timer_fired(self, _now: datetime) -> None:
        """Run the cleanup pass the timer was armed for."""
        self._precondition_timer = None
        self._schedule_reconcile()

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
        current = self._current_data().get("gnssLocation") or {}
        if (
            (current_time := current.get("timeStamp"))
            and (new_time := location.get("timeStamp"))
            and new_time <= current_time
        ):
            return
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
        unsub_departure, self._unsub_departure_handler = (
            self._unsub_departure_handler,
            None,
        )
        if unsub_handler:
            self._initial.clear()
        if unsub_parallax:
            await unsub_parallax()
        if unsub_handler:
            await unsub_handler()
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
