"""Data update coordinator for the Rivian integration."""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Coroutine
from datetime import timedelta
import logging
from typing import Any, Generic, TypeVar

from aiohttp import ClientResponse
import async_timeout
from rivian import Rivian
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
    CHARGING_API_FIELDS,
    DOMAIN,
    INVALID_SENSOR_STATES,
    VEHICLE_STATE_API_FIELDS,
)

_LOGGER = logging.getLogger(__name__)
T = TypeVar("T", bound=dict[str, Any] | list[dict[str, Any]])


class RivianDataUpdateCoordinator(DataUpdateCoordinator[T], Generic[T], ABC):
    """Data update coordinator for the Rivian integration."""

    key: str
    _update_interval = 30
    _error_count = 0

    def __init__(self, hass: HomeAssistant, client: Rivian) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=self._update_interval),
        )
        self.api = client

    def _set_update_interval(self, seconds: int | None = None) -> None:
        """Set the update interval or calculate new one based on errors."""
        if seconds:
            self._update_interval = seconds
        else:
            seconds = min(self._update_interval * 2**self._error_count, 900)
        self.update_interval = timedelta(seconds=seconds)
        _LOGGER.info("Polling set to %s seconds", seconds)

    async def _async_update_data(self) -> T:
        """Get the latest data from Rivian."""
        try:
            resp = await self._fetch_data()
            if resp.status == 200:
                data = await resp.json()
                _LOGGER.debug(data)
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
        self._set_update_interval(
            self._plugged_interval if is_plugged_in else self._unplugged_interval
        )


class UserCoordinator(RivianDataUpdateCoordinator[dict[str, Any]]):
    """User data update coordinator for Rivian."""

    key = "currentUser"

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_user_information()


class VehicleCoordinator(RivianDataUpdateCoordinator[dict[str, Any]]):
    """Vehicle data update coordinator for Rivian."""

    key = "vehicleState"
    _update_interval = 15 * 60  # 15 minutes
    _initial = asyncio.Event()
    _unsub_handler: Coroutine[None, None, None] | None = None

    def __init__(self, hass: HomeAssistant, client: Rivian, vehicle_id: str) -> None:
        """Initialize the coordinator."""
        super().__init__(hass=hass, client=client)
        self.vehicle_id = vehicle_id
        self.charging_coordinator = ChargingCoordinator(hass, client, vehicle_id)

    async def _async_update_data(self) -> dict[str, Any]:
        """Get the latest data from Rivian."""
        if not self.data or not self.last_update_success:
            self._unsubscribe()
            self._unsub_handler = await self.api.subscribe_for_vehicle_updates(
                vehicle_id=self.vehicle_id,
                properties=VEHICLE_STATE_API_FIELDS,
                callback=self._process_new_data,
            )

            try:
                async with async_timeout.timeout(1):
                    await self._initial.wait()
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

    @callback
    def _process_new_data(self, data: dict[str, Any]) -> None:
        """Process new data."""
        if not (payload := data.get("payload")) or not (pdata := payload.get("data")):
            _LOGGER.error("Received an unknown subscription update: %s", data)
            self._error_count += 1
            if not self._initial.is_set() or self._error_count > 5:
                self._unsubscribe(True)
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
            value = items[key]["value"]
            if str(value).lower() in INVALID_SENSOR_STATES:
                new_data[key] = prev_items[key]
            new_data[key]["history"] |= prev_items[key]["history"]

        return new_data

    def _unsubscribe(self, close_monitor: bool = False):
        """Unsubscribe."""
        if unsub := self._unsub_handler:
            self.hass.async_add_job(unsub)
            self._unsub_handler = None
        if close_monitor and (monitor := self.api._ws_monitor):
            self.hass.async_add_job(monitor.close)


class WallboxCoordinator(RivianDataUpdateCoordinator[list[dict[str, Any]]]):
    """Wallbox data update coordinator for Rivian."""

    key = "getRegisteredWallboxes"

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_registered_wallboxes()
