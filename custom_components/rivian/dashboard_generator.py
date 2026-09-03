"""Automated Turnkey Dashboard Generator for Rivian Trip Efficiency & Analytics."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .const import ATTR_VEHICLE, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DEFAULT_DASHBOARD_ID = "rivian_efficiency"
DEFAULT_URL_PATH = "rivian-efficiency"
DEFAULT_TITLE = "Rivian Efficiency"
DEFAULT_ICON = "mdi:gauge"


def _build_vehicle_analytics_view(
    vehicle_name: str,
    entity_prefix: str,
) -> dict[str, Any]:
    """Build Advanced Analytics view (Plotly & Mushroom) for a specific vehicle."""
    eff_30d_entity = f"{entity_prefix}efficiency_30_days"
    last_eff_entity = f"{entity_prefix}last_drive_efficiency"
    last_dist_entity = f"{entity_prefix}last_drive_distance"
    last_mpge_entity = f"{entity_prefix}last_drive_mpge"
    mpge_30d_entity = f"{entity_prefix}mpge_30_days"
    mpge_all_entity = f"{entity_prefix}mpge_all_time"
    eff_all_entity = f"{entity_prefix}efficiency_all_time"
    status_entity = f"{entity_prefix}drive_status"

    return {
        "title": f"{vehicle_name} Efficiency",
        "path": vehicle_name.lower().replace(" ", "-"),
        "icon": "mdi:gauge",
        "cards": [
            # Section 1: Hero & Status Overview
            {
                "type": "vertical-stack",
                "title": f"{vehicle_name} Trip Efficiency Overview",
                "cards": [
                    {
                        "type": "custom:mushroom-template-card",
                        "primary": f"{{{{ states('{last_eff_entity}') }}}} mi/kWh",
                        "secondary": (
                            f"Last Drive: {{{{ states('{last_dist_entity}') }}}} mi "
                            f"({{{{ states('{last_mpge_entity}') }}}} MPGe) | "
                            f"Status: {{{{ states('{status_entity}') }}}}"
                        ),
                        "icon": "mdi:leaf",
                        "icon_color": (
                            f"{{% set eff = states('{last_eff_entity}') | float(0) %}}"
                            "{{% if eff >= 2.8 %}}green{{% elif eff >= 2.2 %}}amber{{% else %}}red{{% endif %}}"
                        ),
                        "badge_icon": (
                            f"{{% if is_state('{status_entity}', 'Driving') %}}mdi:car-electric"
                            "{{% else %}}mdi:car-parking-lights{{% endif %}}"
                        ),
                        "badge_color": (
                            f"{{% if is_state('{status_entity}', 'Driving') %}}green{{% else %}}blue{{% endif %}}"
                        ),
                        "tap_action": {"action": "more-info", "entity": last_eff_entity},
                    },
                    {
                        "type": "custom:mushroom-chips-card",
                        "alignment": "center",
                        "chips": [
                            {
                                "type": "template",
                                "icon": "mdi:calendar-month",
                                "icon_color": "green",
                                "content": (
                                    f"30-Day: {{{{ states('{eff_30d_entity}') }}}} mi/kWh "
                                    f"({{{{ states('{mpge_30d_entity}') }}}} MPGe)"
                                ),
                                "entity": eff_30d_entity,
                                "tap_action": {"action": "more-info", "entity": eff_30d_entity},
                            },
                            {
                                "type": "template",
                                "icon": "mdi:all-inclusive",
                                "icon_color": "purple",
                                "content": (
                                    f"All-Time: {{{{ states('{eff_all_entity}') }}}} mi/kWh "
                                    f"({{{{ states('{mpge_all_entity}') }}}} MPGe)"
                                ),
                                "entity": eff_all_entity,
                                "tap_action": {"action": "more-info", "entity": eff_all_entity},
                            },
                            {
                                "type": "template",
                                "icon": "mdi:gas-station-off",
                                "icon_color": "teal",
                                "content": f"Last MPGe: {{{{ states('{last_mpge_entity}') }}}}",
                                "entity": last_mpge_entity,
                                "tap_action": {"action": "more-info", "entity": last_mpge_entity},
                            },
                            {
                                "type": "entity",
                                "entity": status_entity,
                                "icon": "mdi:car",
                                "icon_color": "blue",
                                "tap_action": {"action": "more-info", "entity": status_entity},
                            },
                        ],
                    },
                ],
            },
            # Section 2: Temperature vs. Efficiency Scatterplot (Elevation Color-Coded)
            {
                "type": "custom:plotly-graph",
                "title": "Temperature vs. Efficiency (Elevation Color-Coded)",
                "layout": {
                    "xaxis": {
                        "title": "Ambient Route Temperature (°F)",
                        "gridcolor": "#444444",
                        "zeroline": False,
                    },
                    "yaxis": {
                        "title": "Efficiency (mi/kWh)",
                        "gridcolor": "#444444",
                        "zeroline": False,
                    },
                    "legend": {"orientation": "h", "y": -0.25, "x": 0.05},
                    "margin": {"l": 50, "r": 20, "t": 40, "b": 60},
                },
                "config": {"displayModeBar": False},
                "entities": [
                    {
                        "entity": eff_30d_entity,
                        "name": "Downhill (Δh < -100 ft)",
                        "mode": "markers",
                        "marker": {
                            "color": "#1E88E5",
                            "symbol": "circle",
                            "opacity": 0.85,
                            "line": {"width": 1, "color": "#ffffff"},
                            "size": (
                                f"$ex (function() {{ "
                                f"const drives = hass.states['{eff_30d_entity}']?.attributes?.recent_drives || []; "
                                "return drives.filter(d => d.elevation_change_ft < -100).map(d => Math.max(8, Math.min(22, Math.round(d.distance * 1.8)))); "
                                "})()"
                            ),
                        },
                        "hovertemplate": "<b>Downhill Drive</b><br>Temperature: %{x}°F<br>Efficiency: %{y:.2f} mi/kWh<br>Elevation: &lt; -100 ft<extra></extra>",
                        "x": (
                            f"$ex (function() {{ "
                            f"const drives = hass.states['{eff_30d_entity}']?.attributes?.recent_drives || []; "
                            "return drives.filter(d => d.elevation_change_ft < -100).map(d => d.temp_f ?? 70); "
                            "})()"
                        ),
                        "y": (
                            f"$ex (function() {{ "
                            f"const drives = hass.states['{eff_30d_entity}']?.attributes?.recent_drives || []; "
                            "return drives.filter(d => d.elevation_change_ft < -100).map(d => d.efficiency); "
                            "})()"
                        ),
                    },
                    {
                        "entity": eff_30d_entity,
                        "name": "Flat (-100 to +100 ft)",
                        "mode": "markers",
                        "marker": {
                            "color": "#43A047",
                            "symbol": "circle",
                            "opacity": 0.85,
                            "line": {"width": 1, "color": "#ffffff"},
                            "size": (
                                f"$ex (function() {{ "
                                f"const drives = hass.states['{eff_30d_entity}']?.attributes?.recent_drives || []; "
                                "return drives.filter(d => d.elevation_change_ft >= -100 && d.elevation_change_ft <= 100).map(d => Math.max(8, Math.min(22, Math.round(d.distance * 1.8)))); "
                                "})()"
                            ),
                        },
                        "hovertemplate": "<b>Flat Drive</b><br>Temperature: %{x}°F<br>Efficiency: %{y:.2f} mi/kWh<br>Elevation: ±100 ft<extra></extra>",
                        "x": (
                            f"$ex (function() {{ "
                            f"const drives = hass.states['{eff_30d_entity}']?.attributes?.recent_drives || []; "
                            "return drives.filter(d => d.elevation_change_ft >= -100 && d.elevation_change_ft <= 100).map(d => d.temp_f ?? 70); "
                            "})()"
                        ),
                        "y": (
                            f"$ex (function() {{ "
                            f"const drives = hass.states['{eff_30d_entity}']?.attributes?.recent_drives || []; "
                            "return drives.filter(d => d.elevation_change_ft >= -100 && d.elevation_change_ft <= 100).map(d => d.efficiency); "
                            "})()"
                        ),
                    },
                    {
                        "entity": eff_30d_entity,
                        "name": "Uphill (Δh > +100 ft)",
                        "mode": "markers",
                        "marker": {
                            "color": "#FB8C00",
                            "symbol": "circle",
                            "opacity": 0.85,
                            "line": {"width": 1, "color": "#ffffff"},
                            "size": (
                                f"$ex (function() {{ "
                                f"const drives = hass.states['{eff_30d_entity}']?.attributes?.recent_drives || []; "
                                "return drives.filter(d => d.elevation_change_ft > 100).map(d => Math.max(8, Math.min(22, Math.round(d.distance * 1.8)))); "
                                "})()"
                            ),
                        },
                        "hovertemplate": "<b>Uphill Drive</b><br>Temperature: %{x}°F<br>Efficiency: %{y:.2f} mi/kWh<br>Elevation: &gt; +100 ft<extra></extra>",
                        "x": (
                            f"$ex (function() {{ "
                            f"const drives = hass.states['{eff_30d_entity}']?.attributes?.recent_drives || []; "
                            "return drives.filter(d => d.elevation_change_ft > 100).map(d => d.temp_f ?? 70); "
                            "})()"
                        ),
                        "y": (
                            f"$ex (function() {{ "
                            f"const drives = hass.states['{eff_30d_entity}']?.attributes?.recent_drives || []; "
                            "return drives.filter(d => d.elevation_change_ft > 100).map(d => d.efficiency); "
                            "})()"
                        ),
                    },
                ],
            },
            # Section 3: Speed Bin Distribution Bar Chart
            {
                "type": "custom:plotly-graph",
                "title": "Speed Bin Distribution (Miles per 10 mph Bin)",
                "layout": {
                    "xaxis": {
                        "title": "Speed Range (mph)",
                        "type": "category",
                        "tickmode": "array",
                        "tickvals": ["0-9", "10-19", "20-29", "30-39", "40-49", "50-59", "60-69", "70-79", "80+"],
                    },
                    "yaxis": {"title": "Total Miles", "gridcolor": "#444444", "zeroline": False},
                    "margin": {"l": 50, "r": 20, "t": 40, "b": 50},
                },
                "config": {"displayModeBar": False},
                "entities": [
                    {
                        "entity": last_eff_entity,
                        "name": "Miles in Speed Bin",
                        "type": "bar",
                        "marker": {"color": "#26A69A", "line": {"width": 1, "color": "#ffffff"}},
                        "hovertemplate": "Speed Bin: %{x} mph<br>Distance: %{y:.1f} miles<extra></extra>",
                        "x": ["0-9", "10-19", "20-29", "30-39", "40-49", "50-59", "60-69", "70-79", "80+"],
                        "y": (
                            f"$ex (function() {{ "
                            f"const bins = hass.states['{last_eff_entity}']?.attributes?.speed_bins || {{}}; "
                            "const keys = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                            "return keys.map(function(k) { "
                            "const b = bins[k]; "
                            "if (typeof b === 'number') return b; "
                            "if (b && typeof b.miles === 'number') return b.miles; "
                            "return 0.0; "
                            "}); "
                            "})()"
                        ),
                    }
                ],
            },
            # Section 4: Detailed Statistics Grid
            {
                "type": "grid",
                "title": "Drive Telemetry",
                "columns": 3,
                "square": False,
                "cards": [
                    {"type": "custom:mushroom-entity-card", "entity": last_dist_entity, "name": "Last Distance", "icon": "mdi:map-marker-distance", "icon_color": "blue"},
                    {"type": "custom:mushroom-entity-card", "entity": last_mpge_entity, "name": "Last MPGe", "icon": "mdi:gas-station-off", "icon_color": "teal"},
                    {"type": "custom:mushroom-entity-card", "entity": status_entity, "name": "Drive Status", "icon": "mdi:car-electric", "icon_color": "green"},
                    {"type": "custom:mushroom-entity-card", "entity": eff_30d_entity, "name": "30-Day Efficiency", "icon": "mdi:calendar-month", "icon_color": "green"},
                    {"type": "custom:mushroom-entity-card", "entity": mpge_30d_entity, "name": "30-Day MPGe", "icon": "mdi:gauge", "icon_color": "teal"},
                    {"type": "custom:mushroom-entity-card", "entity": eff_all_entity, "name": "All-Time Efficiency", "icon": "mdi:all-inclusive", "icon_color": "purple"},
                ],
            },
        ],
    }


def _build_core_fallback_view(
    vehicle_name: str,
    entity_prefix: str,
) -> dict[str, Any]:
    """Build Native Core HA Fallback view (zero third-party cards)."""
    eff_30d_entity = f"{entity_prefix}efficiency_30_days"
    last_eff_entity = f"{entity_prefix}last_drive_efficiency"
    last_dist_entity = f"{entity_prefix}last_drive_distance"
    last_mpge_entity = f"{entity_prefix}last_drive_mpge"
    mpge_30d_entity = f"{entity_prefix}mpge_30_days"
    mpge_all_entity = f"{entity_prefix}mpge_all_time"
    eff_all_entity = f"{entity_prefix}efficiency_all_time"
    status_entity = f"{entity_prefix}drive_status"

    return {
        "title": f"{vehicle_name} (Native Core)",
        "path": f"{vehicle_name.lower().replace(' ', '-')}-core",
        "icon": "mdi:view-dashboard-outline",
        "cards": [
            {
                "type": "grid",
                "title": f"{vehicle_name} Drive Efficiency (Core Cards)",
                "columns": 2,
                "square": False,
                "cards": [
                    {"type": "tile", "entity": last_eff_entity, "name": "Last Drive Efficiency", "icon": "mdi:leaf", "color": "green"},
                    {"type": "tile", "entity": last_dist_entity, "name": "Last Drive Distance", "icon": "mdi:map-marker-distance", "color": "blue"},
                    {"type": "tile", "entity": last_mpge_entity, "name": "Last Drive MPGe", "icon": "mdi:gas-station-off", "color": "teal"},
                    {"type": "tile", "entity": status_entity, "name": "Drive Status", "icon": "mdi:car-electric", "color": "amber"},
                    {"type": "tile", "entity": eff_30d_entity, "name": "30-Day Efficiency", "icon": "mdi:calendar-month", "color": "green"},
                    {"type": "tile", "entity": mpge_30d_entity, "name": "30-Day MPGe", "icon": "mdi:gauge", "color": "teal"},
                    {"type": "tile", "entity": eff_all_entity, "name": "All-Time Efficiency", "icon": "mdi:all-inclusive", "color": "purple"},
                    {"type": "tile", "entity": mpge_all_entity, "name": "All-Time MPGe", "icon": "mdi:gas-station-off", "color": "indigo"},
                ],
            },
            {
                "type": "history-graph",
                "title": "Drive History (72 Hours)",
                "hours_to_show": 72,
                "entities": [
                    {"entity": last_eff_entity, "name": "Efficiency (mi/kWh)"},
                    {"entity": last_dist_entity, "name": "Distance (mi)"},
                    {"entity": last_mpge_entity, "name": "MPGe"},
                ],
            },
            {
                "type": "statistics-graph",
                "title": "30-Day vs All-Time Efficiency Trend",
                "entities": [eff_30d_entity, eff_all_entity],
                "chart_type": "line",
                "period": "day",
                "stat_types": ["mean"],
                "days_to_show": 30,
            },
        ],
    }


async def async_discover_vehicle_prefixes(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Discover configured Rivian vehicle names and entity ID prefixes."""
    results: list[tuple[str, str]] = []

    # Check hass.data[DOMAIN] entries
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and ATTR_VEHICLE in entry_data:
            for v_info in entry_data[ATTR_VEHICLE].values():
                name = str(v_info.get("name") or v_info.get("model") or "Rivian")
                vin = str(v_info.get("vin") or "")

                # Look up matching entity in hass.states
                for entity_id in hass.states.async_entity_ids("sensor"):
                    if entity_id.endswith("_last_drive_efficiency") and (
                        (vin and vin.lower() in entity_id.lower())
                        or (name and name.lower().replace(" ", "_") in entity_id.lower())
                        or "rivian" in entity_id.lower()
                    ):
                        prefix = entity_id[: -len("last_drive_efficiency")]
                        results.append((name, prefix))
                        break

    if not results:
        # Fallback: scan all sensor entities for *_last_drive_efficiency
        for entity_id in hass.states.async_entity_ids("sensor"):
            if entity_id.endswith("_last_drive_efficiency"):
                prefix = entity_id[: -len("last_drive_efficiency")]
                v_name = prefix.replace("sensor.", "").replace("_", " ").title().strip()
                results.append((v_name, prefix))

    return results


async def async_create_efficiency_dashboard(
    hass: HomeAssistant,
    title: str = DEFAULT_TITLE,
    icon: str = DEFAULT_ICON,
    url_path: str = DEFAULT_URL_PATH,
) -> bool:
    """Create or update a turnkey Rivian Efficiency dashboard in Home Assistant."""
    dashboard_id = url_path.replace("-", "_")
    vehicles = await async_discover_vehicle_prefixes(hass)

    if not vehicles:
        _LOGGER.warning("No Rivian vehicle efficiency entities found to generate dashboard")
        vehicles = [("Rivian", "sensor.rivian_")]

    views: list[dict[str, Any]] = []
    for vehicle_name, prefix in vehicles:
        views.append(_build_vehicle_analytics_view(vehicle_name, prefix))
        views.append(_build_core_fallback_view(vehicle_name, prefix))

    dashboard_config = {
        "title": title,
        "views": views,
    }

    # 1. Persist dashboard configuration into .storage/lovelace.{dashboard_id}
    store = Store(hass, 1, f"lovelace.{dashboard_id}")
    await store.async_save({"config": dashboard_config})
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
        items.append({
            "id": dashboard_id,
            "title": title,
            "icon": icon,
            "url_path": url_path,
            "mode": "storage",
            "require_admin": False,
            "show_in_sidebar": True,
        })

    dashboards_data["items"] = items
    await dashboards_store.async_save(dashboards_data)
    _LOGGER.info(
        "Registered '%s' in lovelace_dashboards (url_path: %s). Refresh browser to view!",
        title,
        url_path,
    )
    return True
