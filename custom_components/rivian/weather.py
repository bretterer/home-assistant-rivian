"""Open-Meteo weather client."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Final

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import VERSION

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

OPEN_METEO_FORECAST_URL: Final[str] = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0
USER_AGENT: Final[str] = (
    f"home-assistant-rivian/{VERSION} "
    "(+https://github.com/bretterer/home-assistant-rivian)"
)
_HEADERS: Final[dict[str, str]] = {"User-Agent": USER_AGENT}
LIVE_CACHE_TTL_SECONDS: Final[float] = 900.0  # 15 minutes
# Coordinates sent to Open-Meteo are rounded to 2 decimals (about 1 km),
# plenty for weather and the same grid the caches use.
REQUEST_COORDINATE_DECIMALS: Final[int] = 2


class OpenMeteoWeatherClient:
    """Async Open-Meteo REST API client with caching, grid rounding, and error resilience."""

    def __init__(
        self,
        hass: HomeAssistant | None = None,
        session: aiohttp.ClientSession | None = None,
        enabled: bool = True,
    ) -> None:
        """Initialize Open-Meteo weather client.

        ``enabled`` is the ``weather_lookups`` option: when off, every lookup
        returns no data without contacting Open-Meteo.
        """
        self.hass = hass
        self.enabled = enabled
        self._custom_session = session
        self._live_cache: dict[tuple[float, float], tuple[float, float]] = {}

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the given session, else Home Assistant's shared one."""
        if self._custom_session is not None:
            return self._custom_session
        if self.hass is None:
            raise RuntimeError("OpenMeteoWeatherClient needs hass or a session")
        return async_get_clientsession(self.hass)

    async def async_get_current_temperature(
        self, latitude: float, longitude: float
    ) -> float | None:
        """Fetch live ambient temperature from Open-Meteo Forecast API with caching."""
        if not self.enabled:
            return None
        grid_lat = round(latitude, 2)
        grid_lon = round(longitude, 2)
        cache_key = (grid_lat, grid_lon)
        now_mono = time.monotonic()

        if cache_key in self._live_cache:
            cache_time, cached_temp = self._live_cache[cache_key]
            if now_mono - cache_time < LIVE_CACHE_TTL_SECONDS:
                _LOGGER.debug(
                    "Returning cached live temperature %.1f°F for grid (%s, %s)",
                    cached_temp,
                    grid_lat,
                    grid_lon,
                )
                return cached_temp

        params = {
            "latitude": round(latitude, REQUEST_COORDINATE_DECIMALS),
            "longitude": round(longitude, REQUEST_COORDINATE_DECIMALS),
            "current": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "timeformat": "iso8601",
        }

        session = self._get_session()
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with session.get(
                OPEN_METEO_FORECAST_URL,
                params=params,
                timeout=timeout,
                headers=_HEADERS,
            ) as response:
                if response.status != 200:
                    _LOGGER.warning(
                        "Open-Meteo forecast API returned status %s for (%s, %s)",
                        response.status,
                        grid_lat,
                        grid_lon,
                    )
                    return None
                data = await response.json()
                current = data.get("current", {})
                temp = current.get("temperature_2m")
                if temp is not None:
                    temp_f = round(float(temp), 1)
                    self._live_cache[cache_key] = (now_mono, temp_f)
                    return temp_f
                return None
        except (
            TimeoutError,
            aiohttp.ClientError,
            KeyError,
            TypeError,
            ValueError,
        ) as err:
            _LOGGER.debug(
                "Failed to fetch live temperature for (%s, %s): %s",
                grid_lat,
                grid_lon,
                err,
            )
            return None
