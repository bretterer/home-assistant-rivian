"""Each vehicle's configurator picture: looked up once, then kept in the analytics DB.

Rivian's legacy image API (``getVehicleMobileImages``) returns nothing for some
vehicles (seen for a 2025 R1S at every image version). The Rivian app shows
configurator renders instead, served publicly from ``rivian.com/compimg`` and
addressed by a full set of option codes (paint, build, package, wheels): the
configurator serves only pre-rendered combinations, so a partial set is refused.

Lookups, in order, the first time a vehicle is seen:

1. **Order:** the vehicle's order lists every option code, so the render is
   exact. Accounts that didn't place the order (e.g. a used car) have none.
2. **Guess:** the account's vehicle info has the paint and trim codes; the
   build and wheel codes seen in working renders are tried with them, once.

The result (or a failed attempt) is saved per VIN by the caller. The user can
also set a picture from any image URL (``rivian.set_vehicle_picture``).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Final

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .analytics_db import VehiclePicture

_LOGGER = logging.getLogger(__name__)

GATEWAY_URL: Final = "https://rivian.com/api/gql/gateway/graphql"
ORDERS_URL: Final = "https://rivian.com/api/gql/orders/graphql"
COMPIMG_BASE: Final = "https://rivian.com/compimg"
PICTURE_SIZE: Final = "3072x2688"
PICTURE_VIEW: Final = "front"  # a front three-quarter view
RETRY_AFTER_SECONDS: Final = 7 * 86400
DOWNLOAD_TIMEOUT: Final = aiohttp.ClientTimeout(total=30)
MAX_PICTURE_BYTES: Final = 10 * 1024 * 1024

# Only the fields needed: a full order also carries addresses and payments.
ORDERS_QUERY: Final = (
    "query vehicleOrders { orders(input: {orderTypes: [PRE_ORDER, VEHICLE], "
    "pageInfo: {from: 0, size: 100}}) { data { id } } }"
)
ORDER_QUERY: Final = (
    "query order($id: String!) { order(id: $id) { vin items { configuration { "
    "ruleset { meta { vehicle version country } } "
    "options { optionId optionDetails { visualExterior } } } } } }"
)
VEHICLE_CONFIG_QUERY: Final = (
    "query vehicleConfiguration { currentUser { vehicles { vin vehicle { model "
    "mobileConfiguration { trimOption { optionId } exteriorColorOption { optionId } } "
    "} } } }"
)

# The account API has paint and trim but not the build or wheel codes a render
# also needs; these are the ones seen in working configurator renders.
GUESS_RULESET: Final = {"version": "2.2", "country": "us"}
GUESS_BUILDS: Final[tuple[str, ...]] = ("ORD-AD3",)
GUESS_WHEELS: Final[tuple[str, ...]] = ("WHL-1RD", "WHL-2SD", "WHL-2SS", "WHL-0A1")


def order_vehicle_config(
    order_json: Any,
) -> tuple[str, dict[str, Any], list[str]] | None:
    """Return (vin, ruleset meta, exterior option ids) from an ``order`` response."""
    order = ((order_json or {}).get("data") or {}).get("order") or {}
    vin = order.get("vin")
    if not vin:
        return None
    for item in order.get("items") or []:
        configuration = (item or {}).get("configuration") or {}
        options = configuration.get("options") or []
        if not options:
            continue
        meta = (configuration.get("ruleset") or {}).get("meta") or {}
        exterior = [
            option["optionId"]
            for option in options
            if option.get("optionId")
            and (option.get("optionDetails") or {}).get("visualExterior")
        ]
        return str(vin), meta, exterior
    return None


def user_vehicle_configs(user_json: Any) -> dict[str, tuple[str, str, str]]:
    """Return {vin: (model, paint code, trim code)} from the account's vehicles."""
    user = ((user_json or {}).get("data") or {}).get("currentUser") or {}
    configs: dict[str, tuple[str, str, str]] = {}
    for entry in user.get("vehicles") or []:
        vehicle = (entry or {}).get("vehicle") or {}
        mobile = vehicle.get("mobileConfiguration") or {}
        paint = (mobile.get("exteriorColorOption") or {}).get("optionId")
        trim = (mobile.get("trimOption") or {}).get("optionId")
        model = vehicle.get("model")
        if entry.get("vin") and model and paint and trim:
            configs[str(entry["vin"])] = (str(model), str(paint), str(trim))
    return configs


def picture_url(meta: dict[str, Any], option_ids: list[str]) -> str | None:
    """Build the configurator render URL, or None when parts are missing.

    The path is ``<model>/<ruleset version>/<country>/<codes>`` with the codes
    lowercased, sorted and joined by ``_`` (e.g. ``exp-lgr_ord-ad3_pkg-lch_whl-1rd``).
    """
    vehicle = str(meta.get("vehicle") or "").lower()
    version = str(meta.get("version") or "")
    country = str(meta.get("country") or "us").lower()
    if not vehicle or not version or not option_ids:
        return None
    codes = "_".join(sorted(code.lower() for code in option_ids))
    return f"{COMPIMG_BASE}/{vehicle}/{version}/{country}/{codes}@{PICTURE_SIZE}.{PICTURE_VIEW}.webp"


def guessed_picture_candidates(
    model: str, paint: str, trim: str
) -> list[tuple[str, list[str]]]:
    """Return (url, codes) to try for a vehicle known only by model, paint and trim."""
    candidates: list[tuple[str, list[str]]] = []
    for build in GUESS_BUILDS:
        for wheel in GUESS_WHEELS:
            codes = [paint, build, trim, wheel]
            url = picture_url({"vehicle": model, **GUESS_RULESET}, codes)
            if url:
                candidates.append((url, codes))
    return candidates


def _headers(client: Any) -> dict[str, str]:
    """Headers for Rivian's GraphQL APIs, mirroring rivian-python-client's."""
    try:
        from rivian.rivian import BASE_HEADERS
    except ImportError:  # pragma: no cover - the client is a hard requirement
        BASE_HEADERS = {}
    return dict(BASE_HEADERS) | {
        "A-Sess": getattr(client, "_app_session_token", "") or "",
        "U-Sess": getattr(client, "_user_session_token", "") or "",
        "Csrf-Token": getattr(client, "_csrf_token", "") or "",
    }


async def _graphql(client: Any, url: str, body: dict[str, Any]) -> Any:
    """Run a GraphQL query through the client's own transport and error handling.

    rivian-python-client has no public method for arbitrary queries; its
    private one is pinned by the integration's exact client requirement.
    """
    response = await client._Rivian__graphql_query(_headers(client), url, body)
    return await response.json()


def sniff_image_type(data: bytes) -> str | None:
    """The raster image type ``data`` really is (by its magic bytes), or None.

    Only JPEG, PNG, GIF and WebP are accepted. The stored picture is served
    from Home Assistant's own origin by the image entity, so a document type
    that can carry script (SVG, HTML) must never be stored, whatever the
    server's Content-Type said.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def download_image(hass: HomeAssistant, url: str) -> tuple[str, bytes] | None:
    """Fetch an image; return (content type, bytes), or None if it isn't one.

    The returned type comes from the bytes (``sniff_image_type``), not the
    response header, so only real JPEG/PNG/GIF/WebP data is ever kept.
    """
    session = async_get_clientsession(hass)
    async with session.get(url, timeout=DOWNLOAD_TIMEOUT) as response:
        if response.status != 200 or not response.content_type.startswith("image/"):
            _LOGGER.debug(
                "Picture request returned HTTP %s (%s): %s",
                response.status,
                response.content_type,
                url,
            )
            return None
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > MAX_PICTURE_BYTES:
                _LOGGER.warning("Picture at %s is larger than 10 MB; not saved", url)
                return None
            chunks.append(chunk)
        data = b"".join(chunks)
        kind = sniff_image_type(data)
        if kind is None:
            _LOGGER.warning(
                "Picture at %s is not a JPEG, PNG, GIF or WebP image; not saved", url
            )
            return None
        return kind, data


async def async_fetch_vehicle_pictures(
    hass: HomeAssistant, client: Any, vins: set[str]
) -> dict[str, VehiclePicture]:
    """Look up and download a picture for each VIN (order first, then a guess).

    Every requested VIN gets a record: status "ok" with the image, or "failed"
    so the caller can wait before retrying. Never raises.
    """
    now = time.time()
    results: dict[str, VehiclePicture] = {}
    notes: list[str] = []  # why lookups failed; masked VINs and codes only

    def saved(
        content_type: str, data: bytes, url: str, codes: list[str]
    ) -> VehiclePicture:
        return VehiclePicture("ok", content_type, data, url, codes, now)

    try:
        orders = await _graphql(
            client,
            GATEWAY_URL,
            {"operationName": "vehicleOrders", "query": ORDERS_QUERY, "variables": {}},
        )
        order_ids = [
            order["id"]
            for order in (((orders or {}).get("data") or {}).get("orders") or {}).get(
                "data"
            )
            or []
            if order.get("id")
        ]
        notes.append(f"{len(order_ids)} order(s)")
        for order_id in order_ids:
            if vins <= results.keys():
                break
            order = await _graphql(
                client,
                ORDERS_URL,
                {
                    "operationName": "order",
                    "query": ORDER_QUERY,
                    "variables": {"id": order_id},
                },
            )
            config = order_vehicle_config(order)
            if config is None or config[0] not in vins or config[0] in results:
                continue
            vin, meta, exterior = config
            url = picture_url(meta, exterior)
            downloaded = await download_image(hass, url) if url else None
            if downloaded is None:
                notes.append(f"order render for {vin[-6:]} unavailable ({url})")
                continue
            results[vin] = saved(*downloaded, url, exterior)
    except Exception as err:  # noqa: BLE001 - a picture is never worth failing setup
        notes.append(f"order lookup failed: {err}")

    missing = vins - results.keys()
    if missing:
        try:
            user = await _graphql(
                client,
                GATEWAY_URL,
                {
                    "operationName": "vehicleConfiguration",
                    "query": VEHICLE_CONFIG_QUERY,
                    "variables": {},
                },
            )
            configs = user_vehicle_configs(user)
            for vin in missing:
                if vin not in configs:
                    notes.append(f"no paint/trim for {vin[-6:]}")
                    continue
                model, paint, trim = configs[vin]
                for url, codes in guessed_picture_candidates(model, paint, trim):
                    downloaded = await download_image(hass, url)
                    if downloaded is not None:
                        results[vin] = saved(*downloaded, url, codes)
                        break
                else:
                    notes.append(
                        f"{vin[-6:]} is {model} {paint} {trim}; no render matched"
                    )
        except Exception as err:  # noqa: BLE001
            notes.append(f"vehicle configuration lookup failed: {err}")

    for vin in results:
        _LOGGER.info("Saved configurator picture for vehicle ending %s", vin[-6:])
    if missing := vins - results.keys():
        _LOGGER.warning(
            "No configurator picture for vehicle(s) ending %s (%s). Set one with the "
            "rivian.set_vehicle_picture service",
            ", ".join(sorted(v[-6:] for v in missing)),
            "; ".join(notes),
        )
    for vin in missing:
        results[vin] = VehiclePicture("failed", None, None, None, [], now)
    return results


async def async_picture_from_url(hass: HomeAssistant, url: str) -> VehiclePicture:
    """Download a user-chosen picture; raise ValueError if it isn't a usable image."""
    downloaded = await download_image(hass, url)
    if downloaded is None:
        raise ValueError(f"{url} did not return an image (or it is over 10 MB)")
    content_type, data = downloaded
    return VehiclePicture("ok", content_type, data, url, ["manual"], time.time())
