"""Data update coordinator for the Rivian integration."""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from datetime import timedelta
import logging
from typing import Any, Generic, TypeVar

from aiohttp import ClientResponse
import async_timeout
from rivian import Rivian
from rivian.exceptions import RivianExpiredTokenError

from homeassistant.core import HomeAssistant, callback
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
    update_interval: int = 30

    def __init__(self, hass: HomeAssistant, client: Rivian) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.update_interval),
        )
        self.api = client

    async def _async_update_data(self) -> T:
        """Get the latest data from Rivian."""
        try:
            resp = await self._fetch_data()
            if resp.status == 200:
                data = await resp.json()
                _LOGGER.debug(data)
                return data["data"][self.key]
            resp.raise_for_status()

        except RivianExpiredTokenError:
            _LOGGER.info("Rivian token expired, refreshing")
            await self.api.create_csrf_token()
            return await self._async_update_data()
        except Exception as ex:
            _LOGGER.error(
                "Unknown Exception while updating Rivian data: %s", ex, exc_info=1
            )
            raise UpdateFailed("Error communicating with API") from ex

    @abstractmethod
    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        raise NotImplementedError


class ChargingCoordinator(RivianDataUpdateCoordinator[dict[str, Any]]):
    """Charging data update coordinator for Rivian."""

    key = "getLiveSessionData"

    def __init__(self, hass: HomeAssistant, client: Rivian, vin: str) -> None:
        """Initialize the coordinator."""
        super().__init__(hass=hass, client=client)
        self.vin = vin

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_live_charging_session(
            user_id=None, vin=self.vin, properties=CHARGING_API_FIELDS
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
    update_interval = 15 * 3600  # 15 minutes
    initial = asyncio.Event()

    def __init__(self, hass: HomeAssistant, client: Rivian, vin: str) -> None:
        """Initialize the coordinator."""
        super().__init__(hass=hass, client=client)
        self.vin = vin

    async def _async_update_data(self) -> dict[str, Any]:
        """Get the latest data from Rivian."""
        if not self.data or not self.last_update_success:
            await self.api.subscribe_for_vehicle_updates(
                vin=self.vin,
                properties=VEHICLE_STATE_API_FIELDS,
                callback=self._process_new_data,
            )

            try:
                async with async_timeout.timeout(1):
                    await self.initial.wait()
            except asyncio.TimeoutError:
                _LOGGER.warning("Didn't get subscription update quick enough")
            else:
                return self.data

        data = await super()._async_update_data()
        return self._build_vehicle_info_dict(data)

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_vehicle_state(
            vin=self.vin, properties=VEHICLE_STATE_API_FIELDS
        )

    @callback
    def _process_new_data(self, data: dict[str, Any]) -> None:
        """Process new data."""
        vehicle_info = self._build_vehicle_info_dict(data["payload"]["data"][self.key])
        self.async_set_updated_data(vehicle_info)
        self.initial.set()

    def _build_vehicle_info_dict(self, vijson: dict[str, Any]) -> dict[str, Any]:
        """Take the json output of vehicle_info and build a dictionary."""
        items = {
            k: v | ({"history": {v["value"]}} if "value" in v else {})
            for k, v in vijson.items()
            if v
        }

        _LOGGER.debug("VIN: %s, updated: %s", self.vin, items)

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


class WallboxCoordinator(RivianDataUpdateCoordinator[list[dict[str, Any]]]):
    """Wallbox data update coordinator for Rivian."""

    key = "getRegisteredWallboxes"

    async def _fetch_data(self) -> ClientResponse:
        """Fetch the data."""
        return await self.api.get_registered_wallboxes()
