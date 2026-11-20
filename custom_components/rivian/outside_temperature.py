"""Outside temperature sensor, from Open-Meteo at the vehicle's location."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import math
from typing import Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .coordinator import RivianConfigEntry, VehicleCoordinator
from .entity import RivianVehicleEntity
from .weather import OpenMeteoWeatherClient

_LOGGER = logging.getLogger(__name__)


OUTSIDE_TEMPERATURE_SENSOR = SensorEntityDescription(
    key="outside_temperature",
    translation_key="outside_temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=SensorStateClass.MEASUREMENT,
    # The weather client asks Open-Meteo for Fahrenheit
    native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
)
# Open-Meteo's current conditions change every 15 minutes
OUTSIDE_TEMPERATURE_REFRESH = timedelta(minutes=15)
# A failed lookup is tried again after these, then every OUTSIDE_TEMPERATURE_REFRESH,
# so a lookup that fails at startup doesn't leave the sensor empty for 15 minutes
OUTSIDE_TEMPERATURE_RETRY: Final = (
    timedelta(minutes=1),
    timedelta(minutes=2),
    timedelta(minutes=5),
)
# Moving further than this from the last lookup looks the temperature up again
OUTSIDE_TEMPERATURE_MOVE_M: Final = 1000.0
# ...but never more often than this, so a drive sends a few requests at most
OUTSIDE_TEMPERATURE_MIN_GAP = timedelta(minutes=5)
# A value older than this (the lookups keep failing) is shown as unavailable
OUTSIDE_TEMPERATURE_MAX_AGE = timedelta(hours=1)
_EARTH_RADIUS_M: Final = 6_371_000.0


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Return the great-circle distance between two (lat, lon) points in meters."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


class RivianOutsideTemperatureEntity(RivianVehicleEntity, SensorEntity):
    """Open-Meteo's current temperature at the vehicle's location.

    The vehicle doesn't report an outside temperature. It's looked up when
    the location is first known, 15 minutes after each lookup, and when the
    vehicle has moved over a kilometer from the last lookup, at most once per
    5 minutes. The weather client rounds the location to about 1 km.
    """

    def __init__(
        self,
        coordinator: VehicleCoordinator,
        config_entry: RivianConfigEntry,
        description: SensorEntityDescription,
        vehicle: dict[str, Any],
        client: OpenMeteoWeatherClient,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, config_entry, description, vehicle)
        self._client = client
        self._value: float | None = None
        self._value_time: datetime | None = None
        self._lookup_time: datetime | None = None
        self._lookup_location: tuple[float, float] | None = None
        self._looking_up = False
        self._failed_lookups = 0
        self._unsub_refresh: CALLBACK_TYPE | None = None
        self._lookup_task: asyncio.Task[None] | None = None

    async def async_added_to_hass(self) -> None:
        """Look the temperature up once the vehicle's location is known."""
        await super().async_added_to_hass()
        self.async_on_remove(self._async_stop)
        self._async_maybe_look_up(refresh=False)

    @property
    def available(self) -> bool:
        """Return True while the looked-up value is recent enough."""
        return (
            self._value is not None
            and self._value_time is not None
            and dt_util.utcnow() - self._value_time <= OUTSIDE_TEMPERATURE_MAX_AGE
        )

    @property
    def native_value(self) -> float | None:
        """Return the last looked-up temperature."""
        return self._value

    def _location(self) -> tuple[float, float] | None:
        """Return the vehicle's (lat, lon), or None when it isn't known."""
        # The fix itself, as the device tracker reads it: it has no "value"
        # key, so _get_value can't return it
        data = self.coordinator.data
        location = data.get("gnssLocation") if data else None
        if not isinstance(location, dict):
            return None
        try:
            latitude = float(location["latitude"])
            longitude = float(location["longitude"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(latitude) and math.isfinite(longitude)):
            return None
        return latitude, longitude

    @callback
    def _cancel_refresh(self) -> None:
        """Cancel the scheduled refresh."""
        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

    @callback
    def _async_stop(self) -> None:
        """Stop the refresh timer and any lookup still running."""
        self._cancel_refresh()
        if self._lookup_task is not None:
            self._lookup_task.cancel()
            self._lookup_task = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Look the temperature up again once the vehicle has moved far enough."""
        self._async_maybe_look_up(refresh=False)
        self.async_write_ha_state()

    @callback
    def _async_refresh(self, _now: datetime) -> None:
        """Look the temperature up again where the vehicle is now."""
        self._unsub_refresh = None
        self._async_maybe_look_up(refresh=True)
        if not self._looking_up:
            # No location yet: check again later
            self._unsub_refresh = async_call_later(
                self.hass, OUTSIDE_TEMPERATURE_REFRESH, self._async_refresh
            )
        # Turns the value unavailable once lookups have failed for an hour
        self.async_write_ha_state()

    @callback
    def _async_maybe_look_up(self, *, refresh: bool) -> None:
        """Start a lookup if one is due."""
        if self._looking_up or (location := self._location()) is None:
            return
        now = dt_util.utcnow()
        # The timer spaces its own lookups; these limits are for moving
        if (
            not refresh
            and self._lookup_time is not None
            and self._lookup_location is not None
        ):
            if now - self._lookup_time < OUTSIDE_TEMPERATURE_MIN_GAP:
                return
            if (
                _distance_m(self._lookup_location, location)
                <= OUTSIDE_TEMPERATURE_MOVE_M
            ):
                return
        self._looking_up = True
        self._lookup_time = now
        self._lookup_location = location
        self._cancel_refresh()
        self._lookup_task = self.hass.async_create_background_task(
            self._async_look_up(location), f"{self.entity_id} outside temperature"
        )

    async def _async_look_up(self, location: tuple[float, float]) -> None:
        """Look the temperature up, then schedule the next refresh."""
        try:
            value = await self._client.async_get_current_temperature(*location)
        except Exception:
            _LOGGER.debug("Outside temperature lookup failed", exc_info=True)
            value = None
        finally:
            # Also on removal, which cancels the lookup and ends here
            self._looking_up = False
            self._lookup_task = None
        if value is None:
            # A failed lookup isn't cached, so a retry asks Open-Meteo again
            delay = (
                OUTSIDE_TEMPERATURE_RETRY[self._failed_lookups]
                if self._failed_lookups < len(OUTSIDE_TEMPERATURE_RETRY)
                else OUTSIDE_TEMPERATURE_REFRESH
            )
            self._failed_lookups += 1
        else:
            # Counted from the lookup's end, so the client's 15-minute cache
            # of this spot has expired by then
            delay = OUTSIDE_TEMPERATURE_REFRESH
            self._failed_lookups = 0
            self._value = value
            self._value_time = dt_util.utcnow()
        self._unsub_refresh = async_call_later(self.hass, delay, self._async_refresh)
        self.async_write_ha_state()
