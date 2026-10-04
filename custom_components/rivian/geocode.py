"""Reverse-geocode auto-detected places via the public OSM Nominatim API.

Mirrors ``road_snap.async_fetch_roads``'s HTTP pattern: Home Assistant's
shared aiohttp session, a clear User-Agent, a request timeout, and a
process-wide rate limit (Nominatim's usage policy asks for at most one
request per second). Only a place's coordinates ever leave Home Assistant,
and any network/parse failure returns None ("unknown, try again later"),
never raising -- callers retry on the next weekly pass rather than treating
it as a definitive "no name here".
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import VERSION

_LOGGER = logging.getLogger(__name__)

NOMINATIM_URL: Final[str] = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT: Final[str] = (
    f"home-assistant-rivian/{VERSION} "
    "(+https://github.com/bretterer/home-assistant-rivian)"
)
REQUEST_TIMEOUT_SECONDS: Final[float] = 20.0
# Nominatim's usage policy: at most one request per second, across the whole
# process (a module-level lock/timestamp, not per-hass or per-VIN).
MIN_REQUEST_INTERVAL_SECONDS: Final[float] = 1.0
ZOOM: Final[int] = 18

# Point-of-interest address fields Nominatim may return, checked in order.
_POI_ADDRESS_KEYS: Final[tuple[str, ...]] = (
    "shop",
    "amenity",
    "tourism",
    "office",
    "leisure",
    "building",
)


def _name_from_address(
    address: dict[str, Any], display_name: str | None, poi_name: str | None = None
) -> str | None:
    """Build a place name from a Nominatim address block.

    Preference order: POI name (the result's own ``name``, else a shop/
    amenity/... address field) plus the road, else house number plus road,
    else the road alone, else the first comma-separated part of
    ``display_name``.
    """
    poi = poi_name or next(
        (address[key] for key in _POI_ADDRESS_KEYS if address.get(key)), None
    )
    road = address.get("road")
    house_number = address.get("house_number")

    if poi and road:
        return f"{poi}, {road}"
    if house_number and road:
        return f"{house_number} {road}"
    if road:
        return road
    if display_name:
        first = display_name.split(",", 1)[0].strip()
        return first or None
    return None


# Module-level rate limit state: a single-element list so it can be mutated
# from inside the lock without a `global` statement (mirrors road_snap.py).
_rate_lock: Final[asyncio.Lock] = asyncio.Lock()
_last_request_ts: Final[list[float]] = [0.0]


async def async_reverse(hass: Any, lat: float, lon: float) -> str | None:
    """Reverse-geocode one point via Nominatim; return a name, or None on failure.

    None also means "no usable name in the response" (e.g. the middle of a
    field with no address at all) -- callers treat it the same as a network
    failure: retry later, don't give up permanently.
    """
    session = async_get_clientsession(hass)
    params = {
        "format": "jsonv2",
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "zoom": str(ZOOM),
        "addressdetails": "1",
    }
    async with _rate_lock:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_ts[0])
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_ts[0] = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with session.get(
                NOMINATIM_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    _LOGGER.debug(
                        "Nominatim returned status %s for %.6f,%.6f",
                        response.status,
                        lat,
                        lon,
                    )
                    return None
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.debug("Nominatim request failed for %.6f,%.6f: %s", lat, lon, err)
            return None

    if not isinstance(data, dict):
        return None
    try:
        address = data.get("address") or {}
        return _name_from_address(
            address, data.get("display_name"), data.get("name") or None
        )
    except (TypeError, AttributeError) as err:
        _LOGGER.debug("Nominatim response could not be parsed: %s", err)
        return None
