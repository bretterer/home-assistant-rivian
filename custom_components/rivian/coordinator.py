"""Data update coordinator for the Rivian integration."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import logging
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    CHARGING_STATE_KEYS,
    DEFAULT_CHARGING_SCHEDULE,
    DOMAIN,
    INVALID_SENSOR_STATES,
    VEHICLE_STATE_API_FIELDS,
)
from .helpers import redact

_LOGGER = logging.getLogger(__name__)
T = TypeVar("T", bound=dict[str, Any] | list[dict[str, Any]])

# Maximum time to wait for the first vehicle state to arrive after subscribing.
# The first `_process_new_data` callback has been observed ~27s after the
# subscription is established, so this needs meaningful headroom.
INITIAL_UPDATE_TIMEOUT = 60
CHARGING_SCHEDULE_COOL_OFF = 10
CHARGING_SCHEDULE_REFRESH_INTERVAL = 900


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
    """Charging data update coordinator for Rivian.

    This coordinator receives live charging data from Parallax protobuf
    messages decoded by the VehicleCoordinator. It no longer polls the
    deprecated getLiveSessionData REST endpoint.
    """

    key = "getLiveSessionData"
    _unplugged_interval = 15 * 60  # 15 minutes
    _plugged_interval = 30  # 30 seconds
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
        self._is_charging: bool = False
        self._session_start_time: datetime | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Return current data without polling.

        Charging data is pushed via Parallax messages, so we don't need
        to poll the (now broken) getLiveSessionData endpoint. This method
        simply returns any data already received from Parallax, or an
        empty dict on first load.
        """
        return self.data or {}

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        raise NotImplementedError("Polling live session data no longer allowed")

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

        now = datetime.now(timezone.utc)
        new_data = dict(self.data or {})

        # If a verified startTime arrives from graph data that differs from existing,
        # it indicates a brand new charging session.
        if "startTime" in clean:
            old_start = new_data.get("startTime")
            if old_start and old_start != clean["startTime"]:
                # New session started - clear old session metrics
                new_data.clear()
            new_data["startTime"] = clean["startTime"]
        elif not new_data.get("startTime") and clean.get("power", 0) > 0:
            new_data["startTime"] = now.strftime("%Y-%m-%dT%H:%M:%S.%f%z")

        new_data.update(clean)

        self.async_set_updated_data(new_data)
        _LOGGER.debug("Charging data updated from Parallax: %s", clean)

    def adjust_update_interval(self, is_plugged_in: bool) -> None:
        """Adjust update interval based on plugged in status.

        With Parallax push, polling is disabled. This method is kept for
        backward compatibility with VehicleCoordinator's chargerStatus handler.
        """


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
        self._charging_schedule: dict[str, Any] | None = None
        self._last_schedule_fetch: float = 0.0

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

            # Subscribe to Parallax messages for live charging data
            self._unsub_parallax = await self.api.subscribe_for_parallax_messages(
                vehicle_id=self.vehicle_id,
                callback=self._process_parallax_data,
            )

            try:
                await asyncio.wait_for(self._initial.wait(), INITIAL_UPDATE_TIMEOUT)
            except asyncio.TimeoutError as err:
                raise UpdateFailed(
                    "Timed out waiting for initial vehicle data after "
                    f"{INITIAL_UPDATE_TIMEOUT}s"
                ) from err

        return self.data

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        raise NotImplementedError("Polling VehicleState no longer allowed")

    async def async_shutdown(self) -> None:
        await self._unsubscribe(True)
        return await super().async_shutdown()

    @callback
    def _process_parallax_data(self, data: dict[str, Any]) -> None:
        """Process incoming Parallax subscription messages."""
        if not (payload := data.get("payload")) or not (pdata := payload.get("data")):
            return
        px = pdata.get("parallaxMessages")
        if not px:
            return
        decoded = decode_parallax_message(**px)
        if not decoded:
            return

        clean = {k: v for k, v in decoded.items() if not k.startswith("_")}
        if not clean:
            return

        # Route charging fields to ChargingCoordinator
        if charging_keys := clean.keys() & CHARGING_STATE_KEYS:
            self.charging_coordinator.update_from_parallax(clean)

        # Route vehicle state fields to VehicleCoordinator
        # Note: timeToEndOfCharge is defined in VEHICLE_SENSORS, so it updates VehicleCoordinator too
        vehicle_keys = (clean.keys() - charging_keys) | (
            clean.keys() & {"timeToEndOfCharge"}
        )
        if vehicle_keys:
            vehicle_updates: dict[str, Any] = {}
            for k in vehicle_keys:
                if k == "gnssLocation":
                    vehicle_updates[k] = clean[k]
                elif k == "vehicleMileage":
                    # Parallax encodes odometer as integer km; GraphQL provides float meters.
                    # Both sources active causes oscillation that corrupts utility meters.
                    # Only accept Parallax value if >= stored (monotonic increase).
                    prev_val = (self.data or {}).get("vehicleMileage", {}).get("value", 0)
                    new_val = clean[k]
                    if isinstance(new_val, (int, float)) and new_val >= prev_val:
                        vehicle_updates[k] = {"value": new_val, "history": {new_val}}
                else:
                    vehicle_updates[k] = {"value": clean[k], "history": {clean[k]}}
            new_data = (self.data or {}) | vehicle_updates
            self.async_set_updated_data(new_data)
            _LOGGER.debug(
                "Vehicle state updated from Parallax (%s): %s", px.get("rvm"), clean
            )

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
            raw_status = str(charger_status.get("value", "")).lower()
            is_charging = (
                "charging" in raw_status
                and "not" not in raw_status
                and "disconnected" not in raw_status
            )
            self.charging_coordinator.adjust_update_interval(
                is_plugged_in=raw_status != "chrgr_sts_not_connected"
            )
            if not is_charging:
                # Reset instantaneous charging metrics when not actively charging
                items["timeToEndOfCharge"] = {"value": 0, "history": {0}}

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
        if unsub := self._unsub_parallax:
            await unsub()
            self._unsub_parallax = None
        if unsub := self._unsub_handler:
            await unsub()
            self._unsub_handler = None
            self._initial.clear()
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
            except asyncio.TimeoutError:
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
        self._last_updated = datetime.now(timezone.utc)
        return data


class WallboxCoordinator(RivianDataUpdateCoordinator[list[dict[str, Any]]]):
    """Wallbox data update coordinator for Rivian."""

    key = "getRegisteredWallboxes"

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_registered_wallboxes()
