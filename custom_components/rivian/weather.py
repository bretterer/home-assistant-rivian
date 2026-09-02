"""Open-Meteo REST and Historical Archive Weather Client."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import TYPE_CHECKING, Any, Final

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

OPEN_METEO_FORECAST_URL: Final[str] = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL: Final[str] = "https://archive-api.open-meteo.com/v1/archive"
REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0
LIVE_CACHE_TTL_SECONDS: Final[float] = 900.0  # 15 minutes


def _parse_iso_datetime(dt_val: str | datetime) -> datetime | None:
    """Parse string or datetime to timezone-aware UTC datetime."""
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            return dt_val.replace(tzinfo=timezone.utc)
        return dt_val.astimezone(timezone.utc)
    if not isinstance(dt_val, str) or not dt_val:
        return None
    try:
        normalized = dt_val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def calculate_distance_weighted_temperature(
    weather_samples: list[dict[str, Any]],
    total_distance_miles: float = 0.0,
) -> float | None:
    """Calculate distance-weighted integrated temperature across route weather waypoints."""
    if not weather_samples:
        return None

    valid_samples: list[dict[str, Any]] = []
    for s in weather_samples:
        if isinstance(s, dict) and s.get("temp_f") is not None:
            try:
                temp = float(s["temp_f"])
                dist = float(s.get("distance_at_sample", 0.0))
                valid_samples.append({"temp_f": temp, "distance_at_sample": dist})
            except (ValueError, TypeError):
                continue

    if not valid_samples:
        return None

    if len(valid_samples) == 1:
        return round(valid_samples[0]["temp_f"], 1)

    # Sort samples by distance at sample
    valid_samples.sort(key=lambda s: s["distance_at_sample"])

    weighted_sum = 0.0
    total_weight = 0.0

    # Account for head segment (from start 0.0 to first sample)
    first_dist = valid_samples[0]["distance_at_sample"]
    if first_dist > 0.0:
        weighted_sum += valid_samples[0]["temp_f"] * first_dist
        total_weight += first_dist

    # Intermediate segments between waypoints
    for i in range(1, len(valid_samples)):
        d_prev = valid_samples[i - 1]["distance_at_sample"]
        d_curr = valid_samples[i]["distance_at_sample"]
        delta_d = max(0.0, d_curr - d_prev)
        avg_temp = (valid_samples[i - 1]["temp_f"] + valid_samples[i]["temp_f"]) / 2.0
        weighted_sum += avg_temp * delta_d
        total_weight += delta_d

    # Account for tail segment (from last sample to total drive distance)
    last_dist = valid_samples[-1]["distance_at_sample"]
    if total_distance_miles > last_dist:
        tail_delta = total_distance_miles - last_dist
        weighted_sum += valid_samples[-1]["temp_f"] * tail_delta
        total_weight += tail_delta

    if total_weight > 0.0:
        return round(weighted_sum / total_weight, 1)

    # Fallback to simple average if total distance delta is zero
    simple_avg = sum(s["temp_f"] for s in valid_samples) / len(valid_samples)
    return round(simple_avg, 1)


def get_interpolated_temperature(
    hourly_temps: dict[str, float],
    target_time: datetime | str,
) -> float | None:
    """Interpolate temperature from hourly temperature mapping for a specific timestamp."""
    if not hourly_temps:
        return None

    target_dt = _parse_iso_datetime(target_time)
    if target_dt is None:
        return None

    parsed_entries: list[tuple[datetime, float]] = []
    for ts_str, temp in hourly_temps.items():
        dt = _parse_iso_datetime(ts_str)
        if dt is not None and isinstance(temp, (int, float)):
            parsed_entries.append((dt, float(temp)))

    if not parsed_entries:
        return None

    parsed_entries.sort(key=lambda item: item[0])

    # Check boundaries
    if target_dt <= parsed_entries[0][0]:
        return round(parsed_entries[0][1], 1)
    if target_dt >= parsed_entries[-1][0]:
        return round(parsed_entries[-1][1], 1)

    # Find bounding bracket
    for i in range(len(parsed_entries) - 1):
        t0, temp0 = parsed_entries[i]
        t1, temp1 = parsed_entries[i + 1]
        if t0 <= target_dt <= t1:
            total_sec = (t1 - t0).total_seconds()
            if total_sec <= 0:
                return round(temp0, 1)
            alpha = (target_dt - t0).total_seconds() / total_sec
            interpolated = temp0 + alpha * (temp1 - temp0)
            return round(interpolated, 2)

    return round(parsed_entries[-1][1], 2)


class OpenMeteoWeatherClient:
    """Async Open-Meteo REST API client with caching, grid rounding, and error resilience."""

    def __init__(
        self,
        hass: HomeAssistant | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize Open-Meteo weather client."""
        self.hass = hass
        self._custom_session = session
        self._live_cache: dict[tuple[float, float], tuple[float, float]] = {}
        self._archive_cache: dict[tuple[float, float, str, str], dict[str, float]] = {}

    def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp ClientSession."""
        if self._custom_session is not None:
            return self._custom_session
        if self.hass is not None:
            try:
                session = async_get_clientsession(self.hass)
                if session is not None:
                    return session
            except (ImportError, AttributeError):
                pass
        return aiohttp.ClientSession()

    def clear_cache(self) -> None:
        """Clear live and historical weather caches."""
        self._live_cache.clear()
        self._archive_cache.clear()

    async def async_get_current_temperature(
        self, latitude: float, longitude: float
    ) -> float | None:
        """Fetch live ambient temperature from Open-Meteo Forecast API with caching."""
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
            "latitude": round(latitude, 4),
            "longitude": round(longitude, 4),
            "current": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "timeformat": "iso8601",
        }

        session = self._get_session()
        created_session = session is not self._custom_session and self.hass is None

        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with session.get(
                OPEN_METEO_FORECAST_URL, params=params, timeout=timeout
            ) as response:
                if response.status != 200:
                    _LOGGER.warning(
                        "Open-Meteo forecast API returned status %s for (%s, %s)",
                        response.status,
                        latitude,
                        longitude,
                    )
                    return None
                data = await response.json()
                temp = data.get("current", {}).get("temperature_2m")
                if temp is not None:
                    temp_f = round(float(temp), 1)
                    self._live_cache[cache_key] = (now_mono, temp_f)
                    return temp_f
                return None
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            KeyError,
            TypeError,
            ValueError,
        ) as err:
            _LOGGER.debug(
                "Failed to fetch live temperature for (%s, %s): %s",
                latitude,
                longitude,
                err,
            )
            return None
        finally:
            if created_session:
                await session.close()

    async def async_get_historical_temperatures(
        self,
        latitude: float,
        longitude: float,
        start_date: str,
        end_date: str,
    ) -> dict[str, float] | None:
        """Fetch historical hourly temperatures from Open-Meteo Archive API with caching."""
        grid_lat = round(latitude, 2)
        grid_lon = round(longitude, 2)
        cache_key = (grid_lat, grid_lon, start_date, end_date)

        if cache_key in self._archive_cache:
            _LOGGER.debug(
                "Returning cached archive weather for grid (%s, %s, %s, %s)",
                grid_lat,
                grid_lon,
                start_date,
                end_date,
            )
            return dict(self._archive_cache[cache_key])

        params = {
            "latitude": round(latitude, 4),
            "longitude": round(longitude, 4),
            "start_date": start_date,
            "end_date": end_date,
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "timezone": "UTC",
        }

        session = self._get_session()
        created_session = session is not self._custom_session and self.hass is None

        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with session.get(
                OPEN_METEO_ARCHIVE_URL, params=params, timeout=timeout
            ) as response:
                if response.status != 200:
                    _LOGGER.warning(
                        "Open-Meteo archive API returned status %s for (%s, %s)",
                        response.status,
                        latitude,
                        longitude,
                    )
                    return None
                data = await response.json()
                hourly_data = data.get("hourly", {})
                times = hourly_data.get("time", [])
                temps = hourly_data.get("temperature_2m", [])

                if not times or not temps or len(times) != len(temps):
                    return None

                result: dict[str, float] = {}
                for t, val in zip(times, temps, strict=False):
                    if val is not None:
                        result[str(t)] = round(float(val), 1)

                self._archive_cache[cache_key] = result
                return dict(result)
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            KeyError,
            TypeError,
            ValueError,
        ) as err:
            _LOGGER.debug(
                "Failed to fetch historical temperatures for (%s, %s): %s",
                latitude,
                longitude,
                err,
            )
            return None
        finally:
            if created_session:
                await session.close()

    async def async_get_historical_temperature_for_timestamp(
        self,
        latitude: float,
        longitude: float,
        target_time: datetime | str,
    ) -> float | None:
        """Fetch and interpolate historical temperature for a specific timestamp."""
        target_dt = _parse_iso_datetime(target_time)
        if target_dt is None:
            return None

        date_str = target_dt.strftime("%Y-%m-%d")
        hourly = await self.async_get_historical_temperatures(
            latitude=latitude,
            longitude=longitude,
            start_date=date_str,
            end_date=date_str,
        )
        if not hourly:
            return None

        return get_interpolated_temperature(hourly, target_dt)


async def async_get_current_temperature(
    hass: HomeAssistant,
    latitude: float,
    longitude: float,
    session: aiohttp.ClientSession | None = None,
) -> float | None:
    """Helper to fetch current live ambient temperature from Open-Meteo."""
    client = OpenMeteoWeatherClient(hass=hass, session=session)
    return await client.async_get_current_temperature(latitude, longitude)


async def async_get_historical_temperatures(
    hass: HomeAssistant,
    latitude: float,
    longitude: float,
    start_date: str,
    end_date: str,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, float] | None:
    """Helper to fetch historical hourly temperatures from Open-Meteo Archive API."""
    client = OpenMeteoWeatherClient(hass=hass, session=session)
    return await client.async_get_historical_temperatures(
        latitude, longitude, start_date, end_date
    )


async def async_get_historical_temperature_for_timestamp(
    hass: HomeAssistant,
    latitude: float,
    longitude: float,
    target_time: datetime | str,
    session: aiohttp.ClientSession | None = None,
) -> float | None:
    """Helper to fetch and interpolate historical temperature for a specific timestamp."""
    client = OpenMeteoWeatherClient(hass=hass, session=session)
    return await client.async_get_historical_temperature_for_timestamp(
        latitude, longitude, target_time
    )
