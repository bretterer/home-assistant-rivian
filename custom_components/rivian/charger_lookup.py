"""Identify a fast-charge station from OpenStreetMap, and normalize charger brands.

For a DC session with a location but no station details, ask the public
Overpass API for ``amenity=charging_station`` features within 150 m and read
the operator, brand, network, name and maximum output from their tags. Only
the location (rounded to ~100 m) is ever sent. Results, including "nothing
here", are cached per rounded location for 90 days in the same table as the
road-snapping cache, and every request goes through ``road_snap``'s
process-wide Overpass rate limiter (one request per 5 s).

``normalize_brand`` / ``brand_for`` are pure helpers shared with the Rivian
history import and the WebSocket payloads (brand key + display label).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from . import road_snap
from .drive_track import haversine_m

_LOGGER = logging.getLogger(__name__)

SEARCH_RADIUS_M: Final[int] = 150
# Rounding to 3 decimals is ~110 m: two sessions at the same site share a lookup.
LOCATION_DECIMALS: Final[int] = 3
CACHE_PREFIX: Final[str] = "chg:"

BRAND_HOME: Final[str] = "home"
BRAND_OTHER: Final[str] = "other"
BRAND_LABELS: Final[dict[str, str]] = {
    "tesla": "Tesla Supercharger",
    "rivian": "Rivian Adventure Network",
    "electrify_america": "Electrify America",
    "chargepoint": "ChargePoint",
    "evgo": "EVgo",
    "blink": "Blink",
    BRAND_OTHER: "Other",
    BRAND_HOME: "Home",
}

# (brand key, lowercase substrings that identify it); first match wins.
_BRAND_PATTERNS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("tesla", ("tesla", "supercharger")),
    ("rivian", ("rivian", "adventure network", "ran ")),
    (
        "electrify_america",
        ("electrify america", "electrify_america", "electrifyamerica"),
    ),
    ("chargepoint", ("chargepoint", "charge point")),
    ("evgo", ("evgo", "ev go")),
    ("blink", ("blink",)),
)
_KW_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(kw|mw|w)?", re.IGNORECASE
)


def normalize_brand(*texts: str | None) -> str:
    """Return the brand key (tesla, rivian, electrify_america, chargepoint,
    evgo, blink or other) for the first text that names one."""
    for text in texts:
        if not text:
            continue
        low = f"{str(text).lower()} "
        for key, needles in _BRAND_PATTERNS:
            if any(n in low for n in needles):
                return key
    return BRAND_OTHER


def brand_label(key: str) -> str:
    """Return the display label of a brand key."""
    return BRAND_LABELS.get(key, BRAND_LABELS[BRAND_OTHER])


def brand_for(
    vendor: str | None,
    network: str | None,
    station_name: str | None,
    is_home: bool | None = None,
) -> tuple[str, str]:
    """Return ``(brand key, display label)`` for a stored session's station fields.

    A home session is brand ``home``. Otherwise the first of network, vendor
    and station name that names a known brand wins; an unknown but named
    network or vendor keeps its own text as the label with key ``other``.
    """
    if is_home:
        return BRAND_HOME, BRAND_LABELS[BRAND_HOME]
    key = normalize_brand(network, vendor, station_name)
    if key != BRAND_OTHER:
        return key, BRAND_LABELS[key]
    return BRAND_OTHER, (network or vendor or BRAND_LABELS[BRAND_OTHER])


def parse_kw(value: Any) -> float | None:
    """Parse an OSM power value ("250 kW", "150000 W", "1.2 MW", "62.5") to kW.

    Several values ("250 kW;150 kW") give the largest.
    """
    if value is None:
        return None
    best: float | None = None
    for match in _KW_PATTERN.finditer(str(value)):
        number = float(match.group(1).replace(",", "."))
        unit = (match.group(2) or "kw").lower()
        kw = (
            number / 1000.0
            if unit == "w"
            else number * 1000.0
            if unit == "mw"
            else number
        )
        best = kw if best is None else max(best, kw)
    return best


def max_output_kw(tags: dict[str, Any]) -> float | None:
    """Return the highest per-socket output (kW) among a feature's tags."""
    values: list[float] = []
    for key, value in tags.items():
        if key.startswith("socket:") and key.endswith(":output"):
            kw = parse_kw(value)
            if kw:
                values.append(kw)
    for key in ("charging_station:output", "capacity:output"):
        kw = parse_kw(tags.get(key))
        if kw:
            values.append(kw)
    return max(values) if values else None


def station_version(
    brand: str, max_kw: float | None, text: str | None = None
) -> str | None:
    """Return the hardware generation: Tesla V2/V3/V4 (by tag text, else by max
    kW: up to 150 V2, up to 250 V3, above V4). Other networks have no
    published generation, so None (the brand chip already names them)."""
    if brand != "tesla":
        return None
    found = re.search(r"\bv\s?([234])\b", (text or "").lower())
    if found:
        return f"V{found.group(1)}"
    if max_kw is None:
        return None
    if max_kw <= 150:
        return "V2"
    if max_kw <= 250:
        return "V3"
    return "V4"


def location_key(lat: float, lon: float) -> str:
    """Cache key: the location rounded to ~110 m."""
    return (
        f"{CACHE_PREFIX}{round(lat, LOCATION_DECIMALS)},{round(lon, LOCATION_DECIMALS)}"
    )


def build_query(lat: float, lon: float) -> str:
    """Overpass QL for charging stations within ``SEARCH_RADIUS_M`` of a point."""
    around = f"(around:{SEARCH_RADIUS_M},{lat:.5f},{lon:.5f})"
    return (
        "[out:json][timeout:20];("
        f'node["amenity"="charging_station"]{around};'
        f'way["amenity"="charging_station"]{around};'
        ");out center tags;"
    )


def _center(element: dict[str, Any]) -> tuple[float, float] | None:
    if "lat" in element and "lon" in element:
        return float(element["lat"]), float(element["lon"])
    center = element.get("center")
    if isinstance(center, dict) and "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])
    return None


def parse_stations(data: Any, lat: float, lon: float) -> dict[str, Any] | None:
    """Pick the nearest station in an Overpass response and normalize it.

    Returns ``{"brand", "brand_label", "operator", "network", "name",
    "max_kw", "version"}`` (None values where unknown), or None when the
    response holds no charging station.
    """
    elements = data.get("elements") if isinstance(data, dict) else None
    best: tuple[float, dict[str, Any]] | None = None
    for element in elements or []:
        if not isinstance(element, dict) or not isinstance(element.get("tags"), dict):
            continue
        center = _center(element)
        dist = haversine_m(lat, lon, *center) if center is not None else 1e12
        if best is None or dist < best[0]:
            best = (dist, element)
    if best is None:
        return None
    tags: dict[str, Any] = best[1]["tags"]
    operator = tags.get("operator") or None
    network = tags.get("network") or None
    name = tags.get("name") or None
    osm_brand = tags.get("brand") or None
    key = normalize_brand(network, operator, osm_brand, name)
    max_kw = max_output_kw(tags)
    return {
        "brand": key,
        "brand_label": brand_label(key),
        "operator": operator,
        "network": network or osm_brand or operator,
        "name": name,
        "max_kw": max_kw,
        "version": station_version(
            key, max_kw, " ".join(str(v) for v in (name, tags.get("description")) if v)
        ),
    }


async def async_lookup_station(
    hass: Any, db: Any, lat: float, lon: float
) -> tuple[bool, dict[str, Any] | None]:
    """Look up the station at a location, via the cache then Overpass.

    Returns ``(resolved, station)``. ``resolved`` is False only when the
    network or parse failed (try again later); ``station`` is None when
    OpenStreetMap has no charging station there (that outcome is cached).
    """
    key = location_key(lat, lon)
    cached = await hass.async_add_executor_job(db.get_cached_osm, key)
    if isinstance(cached, dict) and "station" in cached:
        return True, cached["station"]
    data = await road_snap.async_overpass_json(hass, build_query(lat, lon))
    if data is None:
        return False, None
    try:
        station = parse_stations(data, lat, lon)
    except (TypeError, ValueError, KeyError) as err:
        _LOGGER.debug("Overpass charger response could not be parsed: %s", err)
        return False, None
    await hass.async_add_executor_job(db.save_cached_osm, key, {"station": station})
    return True, station


async def async_enrich_sessions(hass: Any, db: Any, vin: str, limit: int = 20) -> int:
    """Fill station details of a VIN's located DC sessions that have none.

    Returns how many sessions were updated. Never raises.
    """
    updated = 0
    try:
        await hass.async_add_executor_job(db.fill_session_locations_from_drives, vin)
        rows = await hass.async_add_executor_job(
            db.dc_sessions_needing_station, vin, limit
        )
        for row in rows:
            resolved, station = await async_lookup_station(
                hass, db, row["lat"], row["lon"]
            )
            if not resolved:
                continue
            if station is None:
                # Nothing mapped here: remember it on the session so it isn't
                # asked again on every pass (the cache would answer anyway,
                # but the query for pending rows stays small).
                await hass.async_add_executor_job(
                    db.update_session_fields,
                    vin,
                    row["session_id"],
                    {"vendor": ""},
                )
                continue
            await hass.async_add_executor_job(
                db.update_session_fields,
                vin,
                row["session_id"],
                {
                    "vendor": station["operator"] or station["network"] or "",
                    "network": station["network"],
                    "station_name": station["name"],
                    "station_version": station["version"],
                    "charger_max_kw": station["max_kw"],
                },
            )
            updated += 1
    except Exception as err:  # noqa: BLE001 - a lookup must never break a caller
        _LOGGER.debug("Charger lookup failed for a vehicle: %s", err)
    return updated
