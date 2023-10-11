"""Data update coordinator for the Rivian integration."""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Coroutine
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Generic, TypeVar

from aiohttp import ClientResponse
from rivian import Rivian, VehicleCommand
from rivian.exceptions import (
    RivianApiException,
    RivianApiRateLimitError,
    RivianExpiredTokenError,
    RivianUnauthenticated,
)

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    CHARGING_API_FIELDS,
    DOMAIN,
    INVALID_SENSOR_STATES,
    VEHICLE_STATE_API_FIELDS,
)
from .helpers import redact

_LOGGER = logging.getLogger(__name__)
T = TypeVar("T", bound=dict[str, Any] | list[dict[str, Any]])


class RivianDataUpdateCoordinator(DataUpdateCoordinator[T], Generic[T], ABC):
    """Data update coordinator for the Rivian integration."""

    key: str
    _update_interval: int = 30
    _error_count = 0

    def __init__(self, hass: HomeAssistant, client: Rivian) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=self._update_interval)
            if self._update_interval
            else None,
        )
        self.api = client

    def _set_update_interval(self, seconds: int | None = None) -> None:
        """Set the update interval or calculate new one based on errors."""
        if seconds:
            self._update_interval = seconds
        else:
            seconds = min(self._update_interval * 2**self._error_count, 900)
        if self.update_interval != (update_interval := timedelta(seconds=seconds)):
            refresh = self.update_interval and self.update_interval.seconds > seconds
            self.update_interval = update_interval
            if refresh and self.data:
                self.hass.async_add_job(self.async_request_refresh)
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
    _unplugged_interval = 15 * 60 # 15 minutes
    _plugged_interval = 30 # 30 seconds
    _update_interval = _unplugged_interval  # 15 minutes

    def __init__(self, hass: HomeAssistant, client: Rivian, vehicle_id: str) -> None:
        """Initialize the coordinator."""
        super().__init__(hass=hass, client=client)
        self.vehicle_id = vehicle_id

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_live_charging_session(
            vin=self.vehicle_id, properties=CHARGING_API_FIELDS
        )

    def adjust_update_interval(self, is_plugged_in: bool) -> None:
        """Adjust update interval based on plugged in status."""
        self._set_update_interval(self._plugged_interval if is_plugged_in else self._unplugged_interval)


class DriverKeyCoordinator(RivianDataUpdateCoordinator[dict[str, Any]]):
    """Drivers/keys data update coordinator for Rivian."""

    key = "getVehicle"
    _update_interval = 15 * 60  # 15 minutes

    def __init__(self, hass: HomeAssistant, client: Rivian, vehicle_id: str) -> None:
        """Initialize the coordinator."""
        super().__init__(hass=hass, client=client)
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
        self, hass: HomeAssistant, client: Rivian, include_phones: bool = False
    ) -> None:
        super().__init__(hass, client)
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
    _update_interval = 15 * 60  # 15 minutes

    def __init__(self, hass: HomeAssistant, client: Rivian, vehicle_id: str) -> None:
        """Initialize the coordinator."""
        super().__init__(hass=hass, client=client)
        self.vehicle_id = vehicle_id
        self.charging_coordinator = ChargingCoordinator(hass, client, vehicle_id)
        self.drivers_coordinator = DriverKeyCoordinator(hass, client, vehicle_id)
        self._initial = asyncio.Event()
        self._unsub_handler: Coroutine[None, None, None] | None = None
        self._awake = asyncio.Event()

    async def _async_update_data(self) -> dict[str, Any]:
        """Get the latest data from Rivian."""
        if not self.data or not self.last_update_success:
            await self._unsubscribe()
            self._unsub_handler = await self.api.subscribe_for_vehicle_updates(
                vehicle_id=self.vehicle_id,
                properties=VEHICLE_STATE_API_FIELDS,
                callback=self._process_new_data,
            )

            try:
                await asyncio.wait_for(self._initial.wait(), 1)
            except asyncio.TimeoutError:
                pass  # we'll fetch it from the API
            else:
                return self.data

        data = await super()._async_update_data()
        return self._build_vehicle_info_dict(data)

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_vehicle_state(
            vin=self.vehicle_id, properties=VEHICLE_STATE_API_FIELDS
        )

    async def async_shutdown(self) -> None:
        await self._unsubscribe(True)
        return await super().async_shutdown()

    @callback
    def _process_new_data(self, data: dict[str, Any]) -> None:
        """Process new data."""
        if not (payload := data.get("payload")) or not (pdata := payload.get("data")):
            _LOGGER.error("Received an unknown subscription update: %s", data)
            self._error_count += 1
            if not self._initial.is_set() or self._error_count > 5:
                self.hass.async_add_job(self._unsubscribe, True)
                self._set_update_interval(20)
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
            if str(value).lower() in INVALID_SENSOR_STATES:
                new_data[key] = prev_items[key]
            new_data[key]["history"] |= prev_items.get(key, {}).get("history", set())

        return new_data

    async def _unsubscribe(self, close_monitor: bool = False):
        """Unsubscribe."""
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
    _update_interval = 0  # disabled
    _last_updated: datetime | None = None

    def __init__(self, hass: HomeAssistant, client: Rivian, version: str) -> None:
        """Initialize the coordinator."""
        super().__init__(hass=hass, client=client)
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
