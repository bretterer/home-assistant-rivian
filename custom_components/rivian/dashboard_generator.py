"""Automated Turnkey Dashboard Generator for Rivian Trip Efficiency & Analytics."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import ATTR_VEHICLE, DASHBOARD_SCHEMA_VERSION, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DEFAULT_URL_PATH = "rivian-dashboard"
DEFAULT_TITLE = "Rivian"
DEFAULT_ICON = "mdi:car-electric"


def _live_storage_dashboard(hass: HomeAssistant, url_path: str) -> Any | None:
    """Return Lovelace's live storage-mode dashboard for ``url_path``, if registered.

    Once Lovelace has loaded a dashboard it serves the config from memory, so a
    direct write to the storage file stays invisible to browsers until a restart.
    Saving through this object, as the dashboard editor does, updates memory and
    file together and tells open browsers to reload.
    """
    dashboards = getattr(hass.data.get("lovelace"), "dashboards", None)
    if not isinstance(dashboards, dict):
        return None
    dashboard = dashboards.get(url_path)
    if getattr(dashboard, "mode", None) != "storage":
        return None
    return dashboard


def _notify_restart_needed(hass: HomeAssistant, title: str, url_path: str) -> None:
    """Tell the user a newly created dashboard needs one restart to appear."""
    message = (
        f"The **{title}** dashboard was created. Restart Home Assistant once to "
        f"add it to the sidebar (at `/{url_path}`). Regenerating it later applies "
        "without a restart."
    )
    _LOGGER.warning(
        "Dashboard '%s' (/%s) was created; restart Home Assistant to show it",
        title,
        url_path,
    )
    try:
        from homeassistant.components import persistent_notification

        persistent_notification.async_create(
            hass,
            message,
            title="Rivian dashboard created",
            notification_id=f"rivian_dashboard_{url_path}",
        )
    except Exception as err:  # noqa: BLE001 - the log line above still informs
        _LOGGER.debug("Could not create restart notification: %s", err)


async def _async_save_dashboard_config(
    hass: HomeAssistant, url_path: str, storage_key: str, config: dict[str, Any]
) -> None:
    """Save a dashboard's config so Lovelace serves it immediately."""
    if (dashboard := _live_storage_dashboard(hass, url_path)) is not None:
        await dashboard.async_save(config)
        return
    # Not registered with Lovelace yet (a brand-new dashboard): its file is read
    # when it's first loaded, after the restart that registers it.
    await Store(hass, 1, storage_key).async_save({"config": config})
    hass.bus.async_fire("lovelace_updated", {"url_path": url_path})


ENTITY_KEY_MAP: Final[dict[str, tuple[str, str]]] = {
    "soc": ("sensor", "battery_level"),
    "soc_limit": ("sensor", "battery_limit"),
    "range": ("sensor", "distance_to_empty"),
    "odometer": ("sensor", "vehicle_mileage"),
    "power_state": ("sensor", "power_state"),
    "gear": ("sensor", "gear_status"),
    "cabin_temperature": ("sensor", "cabin_temperature"),
    "location": ("device_tracker", "location"),
    "locked": ("binary_sensor", "locked_state"),
    "charging": ("binary_sensor", "charger_state"),
    "plugged_in": ("binary_sensor", "charger_status"),
    "charge_port": ("binary_sensor", "charge_port"),
    "charging_rate": ("sensor", "charging_rate"),
    "charging_speed": ("sensor", "charging_speed"),
    "charging_energy_delivered": ("sensor", "charging_energy_delivered"),
    "charging_time_remaining": ("sensor", "time_to_end_of_charge"),
    "charging_range_added": ("sensor", "charging_range_added"),
    "charging_cost": ("sensor", "charging_cost"),
    "software": ("update", "software_ota"),
    "drive_status": ("sensor", "drive_status"),
    "efficiency_30d": ("sensor", "efficiency_30d"),
    "efficiency_all_time": ("sensor", "efficiency_all_time"),
    "last_drive_efficiency": ("sensor", "last_drive_efficiency"),
    "image_light": ("image", "light-three-quarter"),
    "image_dark": ("image", "dark-three-quarter"),
    # The configurator render saved once per vehicle (see vehicle_picture.py).
    "picture": ("image", "picture"),
}

# The subset of ENTITY_KEY_MAP surfaced to the Overview tab's vehicle card.
OVERVIEW_ENTITY_KEYS: Final[tuple[str, ...]] = (
    "soc",
    "soc_limit",
    "range",
    "odometer",
    "location",
    "locked",
    "charging",
    "plugged_in",
    "charging_rate",
    "power_state",
    "gear",
    "drive_status",
    "image_light",
    "image_dark",
    "picture",
)


async def _async_resolve_vehicle_entities(
    hass: HomeAssistant, vin: str
) -> dict[str, str]:
    """Resolve a vehicle's core/drive entity ids through the entity registry.

    Entity naming varies per install (users rename entities, HA slugifies
    differently across versions, etc.), so ids are never guessed: only a key
    whose (domain, DOMAIN, f"{vin}-{key}") unique_id is actually registered
    is returned. Non-string lookups (e.g. a MagicMock in tests) are treated
    as unresolved rather than surfaced as a broken entity id.
    """
    if not vin:
        return {}
    registry = er.async_get(hass)
    resolved: dict[str, str] = {}
    for name, (domain, key) in ENTITY_KEY_MAP.items():
        entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{vin}-{key}")
        if isinstance(entity_id, str):
            resolved[name] = entity_id
    return resolved


def _model_str(v_info: dict[str, Any]) -> str:
    """Build a "<year> <model>" label from a discovered vehicle info dict."""
    model = v_info.get("model")
    year = v_info.get("model_year") or v_info.get("modelYear")
    if model and year:
        return f"{year} {model}"
    return str(model) if model else ""


def _collect_vehicle_models(hass: HomeAssistant) -> dict[str, str]:
    """Map every discovered VIN to its "<year> <model>" label, if known."""
    models: dict[str, str] = {}
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(entry_data, dict) or ATTR_VEHICLE not in entry_data:
            continue
        for v_info in entry_data[ATTR_VEHICLE].values():
            vin = str(v_info.get("vin") or "")
            if vin:
                models[vin] = _model_str(v_info)
    return models


def _build_overview_view(
    vehicles_with_entry: list[tuple[str, str, str, str]],
    entities_by_vin: dict[str, dict[str, str]],
    vehicle_models: dict[str, str],
    url_path: str,
) -> dict[str, Any]:
    """Build the Overview tab: one `rivian-overview-card` listing every vehicle.

    No picker, no conditionals, regardless of vehicle count -- the card
    itself handles listing more than one vehicle. A vehicle without a
    resolved VIN is listed with its name only and an empty entities map.
    """
    vehicles_payload: list[dict[str, Any]] = []
    for name, _prefix, vin, _entry_id in vehicles_with_entry:
        entities = entities_by_vin.get(vin, {}) if vin else {}
        vehicles_payload.append(
            {
                "vin": vin,
                "name": name,
                "model": vehicle_models.get(vin, "") if vin else "",
                "entities": {
                    key: entities[key]
                    for key in OVERVIEW_ENTITY_KEYS
                    if key in entities
                },
            }
        )
    return {
        # Titled "Vehicles"; the path stays "overview" so bookmarks still work.
        "title": "Vehicles",
        "path": "overview",
        "icon": "mdi:car-multiple",
        "show_icon_and_title": True,
        # A panel view gives the card the full width, which the wide layout
        # (vehicles in one horizontally scrolling row) needs.
        "panel": True,
        "cards": [
            {
                "type": "custom:rivian-overview-card",
                "vehicles": vehicles_payload,
                "drives_path": f"/{url_path}/drives",
            }
        ],
    }


def _has_vin(vehicles: list[tuple[str, str, str]]) -> bool:
    return any(vin for (_name, _prefix, vin) in vehicles)


def _build_panel_view(
    vehicles: list[tuple[str, str, str]],
    *,
    title: str,
    path: str,
    icon: str,
    card_type: str,
) -> dict[str, Any]:
    """Build a panel-mode tab holding ONE card for every vehicle.

    The card follows the shared vehicle selection (the vehicle bar is drawn
    inside the card's header); its optional ``vins`` config would pin a fixed
    set instead. Without any resolved VIN there is nothing to show.
    """
    return {
        "title": title,
        "path": path,
        "icon": icon,
        # Tabs show their icon AND name (HA otherwise shows only the icon).
        "show_icon_and_title": True,
        "panel": True,
        "cards": [{"type": card_type}] if _has_vin(vehicles) else [],
    }


def _build_drives_view(vehicles: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Build the panel-mode Drives tab: one drive-explorer card."""
    return _build_panel_view(
        vehicles,
        title="Drives",
        path="drives",
        icon="mdi:map-marker-path",
        card_type="custom:rivian-drive-explorer-card",
    )


def _build_places_view(vehicles: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Build the panel-mode Places tab: one places card."""
    return _build_panel_view(
        vehicles,
        title="Destinations",
        path="places",
        icon="mdi:map-marker-star",
        card_type="custom:rivian-places-card",
    )


def _build_routes_view(vehicles: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Build the panel-mode Routes tab: one favorite-drives card."""
    return _build_panel_view(
        vehicles,
        title="Fav Routes",
        path="routes",
        icon="mdi:routes",
        card_type="custom:rivian-routes-card",
    )


async def async_discover_vehicle_prefixes(
    hass: HomeAssistant,
) -> list[tuple[str, str, str, str]]:
    """Discover configured Rivian vehicle names, entity ID prefixes, VINs, and entry ids.

    The fourth tuple element is the owning config entry's `entry_id`; it's
    empty when discovered via the entity-registry fallback below, which has
    no entry context.
    """
    results: list[tuple[str, str, str, str]] = []
    entity_registry = er.async_get(hass)

    # Check hass.data[DOMAIN] entries
    for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
        if isinstance(entry_data, dict) and ATTR_VEHICLE in entry_data:
            for v_info in entry_data[ATTR_VEHICLE].values():
                name = str(v_info.get("name") or v_info.get("model") or "Rivian")
                vin = str(v_info.get("vin") or "")

                # Exact match through the registry first: with several
                # vehicles, the name/"rivian" substring heuristics below can
                # map one vehicle onto another's entities.
                registered = (
                    entity_registry.async_get_entity_id(
                        "sensor", DOMAIN, f"{vin}-last_drive_efficiency"
                    )
                    if vin
                    else None
                )
                if isinstance(registered, str) and registered.endswith(
                    "_last_drive_efficiency"
                ):
                    prefix = registered[: -len("last_drive_efficiency")]
                    results.append((name, prefix, vin, str(entry_id)))
                    continue

                # Look up matching entity in hass.states
                for entity_id in hass.states.async_entity_ids("sensor"):
                    if entity_id.endswith("_last_drive_efficiency") and (
                        (vin and vin.lower() in entity_id.lower())
                        or (
                            name and name.lower().replace(" ", "_") in entity_id.lower()
                        )
                        or "rivian" in entity_id.lower()
                    ):
                        prefix = entity_id[: -len("last_drive_efficiency")]
                        results.append((name, prefix, vin, str(entry_id)))
                        break

    if not results:
        # Fallback: scan all sensor entities for *_last_drive_efficiency and
        # recover the VIN from the entity registry's unique_id (f"{vin}-{key}").
        for entity_id in hass.states.async_entity_ids("sensor"):
            if entity_id.endswith("_last_drive_efficiency"):
                prefix = entity_id[: -len("last_drive_efficiency")]
                v_name = prefix.replace("sensor.", "").replace("_", " ").title().strip()
                vin = ""
                entry = entity_registry.async_get(entity_id)
                if entry and entry.unique_id.endswith("-last_drive_efficiency"):
                    vin = entry.unique_id[: -len("-last_drive_efficiency")]
                results.append((v_name, prefix, vin, ""))

    return results


async def async_create_efficiency_dashboard(
    hass: HomeAssistant,
    title: str = DEFAULT_TITLE,
    icon: str = DEFAULT_ICON,
    url_path: str = DEFAULT_URL_PATH,
) -> bool:
    """Create or update the turnkey, tabbed Rivian dashboard in Home Assistant.

    Views/tabs are generated in order: Overview, Drives (panel), Places
    (panel) and Routes (panel). The Overview tab is a
    single `rivian-overview-card` listing every vehicle (see
    `_build_overview_view`). Drives, Places and Routes each hold ONE card
    that follows the shared vehicle selection. There is no picker or
    conditional card anywhere.
    """
    dashboard_id = url_path.replace("-", "_")
    vehicles_with_entry = await async_discover_vehicle_prefixes(hass)

    if not vehicles_with_entry:
        _LOGGER.warning(
            "No Rivian vehicle efficiency entities found to generate dashboard"
        )
        vehicles_with_entry = [("Rivian", "sensor.rivian_", "", "")]

    for vehicle_name, _prefix, vin, _entry_id in vehicles_with_entry:
        if not vin:
            _LOGGER.warning(
                "No VIN resolved for %s; analytics charts and the drive "
                "explorer will be omitted for it. Re-run this service once "
                "the vehicle's entities are fully registered",
                vehicle_name,
            )

    vehicles = [
        (name, prefix, vin) for (name, prefix, vin, _eid) in vehicles_with_entry
    ]

    entities_by_vin: dict[str, dict[str, str]] = {}
    for _name, _prefix, vin in vehicles:
        if vin:
            entities_by_vin[vin] = await _async_resolve_vehicle_entities(hass, vin)
    vehicle_models = _collect_vehicle_models(hass)

    overview_view = _build_overview_view(
        vehicles_with_entry, entities_by_vin, vehicle_models, url_path
    )
    drives_view = _build_drives_view(vehicles)
    places_view = _build_places_view(vehicles)
    routes_view = _build_routes_view(vehicles)

    views: list[dict[str, Any]] = [
        overview_view,
        drives_view,
        routes_view,
        places_view,
        # No vehicle status/controls tab for now; a redesigned one is planned.
    ]

    dashboard_config = {
        "title": title,
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "views": views,
    }

    # 1. Persist the dashboard configuration through Lovelace
    await _async_save_dashboard_config(
        hass, url_path, f"lovelace.{dashboard_id}", dashboard_config
    )
    _LOGGER.info("Saved dashboard configuration to .storage/lovelace.%s", dashboard_id)

    # 2. Register dashboard in .storage/lovelace_dashboards
    dashboards_store = Store(hass, 1, "lovelace_dashboards")
    dashboards_data = await dashboards_store.async_load() or {"items": []}
    items = dashboards_data.get("items", [])

    existing = next((item for item in items if item.get("url_path") == url_path), None)
    if existing:
        existing["title"] = title
        existing["icon"] = icon
        existing["show_in_sidebar"] = True
    else:
        items.append(
            {
                "id": dashboard_id,
                "title": title,
                "icon": icon,
                "url_path": url_path,
                "mode": "storage",
                "require_admin": False,
                "show_in_sidebar": True,
            }
        )

    dashboards_data["items"] = items
    await dashboards_store.async_save(dashboards_data)
    _LOGGER.info(
        "Registered '%s' in lovelace_dashboards (url_path: %s)", title, url_path
    )
    if _live_storage_dashboard(hass, url_path) is None:
        # Lovelace's dashboards collection is private to the lovelace
        # integration, so a brand-new dashboard only appears after a restart.
        # Regenerating an existing one updates live (see step 1).
        _notify_restart_needed(hass, title, url_path)

    return True
